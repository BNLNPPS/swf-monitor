#!/usr/bin/env python3
"""stash-drain.py — move stashed outputs to JLab and register them there.

The registrar of the failover stash (swf-epicprod
docs/RUCIO_FAILOVER_STASH.md). A job whose output JLab would not take
writes it to a BNL dCache space and records what it stashed; this brings
those files home when JLab is reachable again.

One pass:

1. Read what the jobs stashed, from the payload reports — the PanDA
   metatable for a finished job, the swept copy for a failed one — and
   from the BNL catalog's own listing of stash entries, which is the
   authority when a report is lost.
2. Register the stash replica in the BNL catalog if it is not already
   there, so the stash is catalogued rather than a pile of files: the
   deterministic PFN, the size and checksum the storage reports, and the
   destination and event count in the metadata. The name is flat,
   because the BNL instance refuses a path-like DID.
3. Probe JLab. If it does not answer, the pass ends without touching
   anything: a stash that cannot be drained is a backlog, which is the
   point of having one.
4. Move each file to the destination RSE at the path the catalog's
   deterministic algorithm gives its logical name, through the RSE's
   write door; verify size and checksum there; register it in the JLab
   catalog by logical name with its event count; verify the replica
   reads AVAILABLE.
5. Delete the stash replica and its catalog entry only after that
   verification.

The copy is streamed through this host: third-party copy is not
supported at the BNL-XRD write door (measured 2026-09-07), and the
production account's credential is accepted by the stash door and the
destination doors alike, so one credential carries the whole move.

Retries live here and nowhere else. A file that will not move keeps its
stash entry and is tried again on later passes, once an hour and a
bounded number of times, with its reason kept in the drain's state file;
nothing is deleted that has not been verified at JLab first.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/stash-drain.py \
        [--hours 168] [--limit N] [--dry-run]

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 5 no usable credential · 6 the BNL catalog is unreachable.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import connections  # noqa: E402

from monitor_app.models import EpicProdJob  # noqa: E402
from monitor_app.panda.constants import PANDA_SCHEMA  # noqa: E402

BNL_RUCIO_URL = os.environ.get('RUCIO_BNL_URL', 'https://nprucio01.sdcc.bnl.gov:443')
BNL_ACCOUNT = os.environ.get('RUCIO_BNL_ACCOUNT', 'panda')
BNL_VO = os.environ.get('RUCIO_BNL_VO', 'eic')
BNL_SCOPE = 'group.EIC'
STASH_RSE = os.environ.get('STASH_RSE', 'BNL_PROD_DISK_1')
STASH_DOOR = os.environ.get('STASH_DOOR', 'root://dcintdoor.sdcc.bnl.gov:1094')
# The proxy the BNL catalog and door accept. A private copy at mode 0600,
# because xrootd refuses a credential with wider rights than that.
BNL_PROXY = os.environ.get('BNL_X509_PROXY', os.path.expanduser('~/.bnl-rucio-proxy'))
BNL_PROXY_SOURCE = os.environ.get('BNL_X509_PROXY_SOURCE',
                                  '/etc/swf-monitor/longproxy-for-rucio')
# Where the payload writes a stash file (run.sh STASH_BASE), for an entry
# known only from the catalog.
STASH_BASE = os.environ.get('STASH_BASE',
                            '/pnfs/sdcc.bnl.gov/eic/epic/disk/group/EIC/stash')
DEFAULT_HOURS = 168

# The move home. The destination is the RSE the payload uploads to, the
# registrar's default; the copy is streamed through this host with the
# production account's credential (third-party copy is not supported at the
# BNL-XRD write door, measured 2026-09-07: "tpc not supported (destination)").
# Bandwidth-bound and fine for a backlog of days; a week of production would
# want the doors to support third-party copy.
DEST_RSE = os.environ.get('REGISTRAR_RSE', 'BNL-XRD')
STATE_PATH = os.environ.get('STASH_DRAIN_STATE',
                            '/data/wenauseic/swf-delivery/stash-drain-state.json')
COPY_MIN_TIMEOUT_S = 600
COPY_BYTES_PER_S = 20 * 1024 * 1024
RETRY_INTERVAL_S = int(os.environ.get('STASH_RETRY_INTERVAL_S', 3600))
MAX_ATTEMPTS = int(os.environ.get('STASH_MAX_ATTEMPTS', 8))
# A test output (under /TEST/) brought home removes itself, as the payload
# canaries' do.
TEST_LIFETIME_S = 7 * 86400

# The JLab catalog's registrar: its identity, client, deterministic path and
# one registration by logical name. Imported rather than copied, so the two
# writers into the catalog of record cannot drift.
_spec = importlib.util.spec_from_file_location(
    'registrar', os.path.join(THIS_DIR, 'registrar.py'))
_reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_reg)
JLAB_SCOPE = _reg.RUCIO_SCOPE


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def resolve_proxy():
    """The private-mode copy of the BNL proxy, refreshed from its source."""
    try:
        source = open(BNL_PROXY_SOURCE, 'rb').read()
    except OSError as e:
        _log(f'ERROR: the BNL proxy source is unreadable: {e}')
        return ''
    try:
        fd = os.open(BNL_PROXY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, source)
        finally:
            os.close(fd)
        os.chmod(BNL_PROXY, 0o600)
    except OSError as e:
        _log(f'ERROR: the private proxy copy could not be written: {e}')
        return ''
    return BNL_PROXY


def bnl_client(proxy):
    """The BNL catalog as the production account."""
    from rucio.client import Client
    return Client(rucio_host=BNL_RUCIO_URL, auth_host=BNL_RUCIO_URL,
                  account=BNL_ACCOUNT, vo=BNL_VO, auth_type='x509_proxy',
                  creds={'client_proxy': proxy}, ca_cert=False)


def stashed_entries(since, limit=None):
    """What jobs said they stashed: [(pandaid, entry, report)] from the
    reports."""
    found, seen = [], set()
    sql = f"""
        SELECT m."pandaid", m."metadata"
        FROM "{PANDA_SCHEMA}"."metatable" m
        JOIN "{PANDA_SCHEMA}"."jobsarchived4" j ON j."pandaid" = m."pandaid"
        WHERE j."modificationtime" >= %s AND j."processingtype" = 'epicproduction'
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [since])
            for pandaid, raw in cursor.fetchall():
                try:
                    metadata = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, TypeError):
                    continue
                report = (metadata or {}).get('payload') or {}
                for entry in report.get('stash') or []:
                    seen.add(int(pandaid))
                    found.append((int(pandaid), entry, report))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: metatable read failed: {e}')

    try:
        for job in (EpicProdJob.objects.filter(updated_at__gte=since)
                    .only('pandaid', 'data').iterator()):
            if int(job.pandaid) in seen:
                continue
            report = ((job.data or {}).get('payload_report') or {}).get('report') or {}
            for entry in report.get('stash') or []:
                found.append((int(job.pandaid), entry, report))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: filed reports unreadable: {e}')
    return found[:limit] if limit else found


def catalogued_entries(client, known):
    """Stash entries the BNL catalog holds that no report named: the
    authority when a report is lost. [(pandaid, entry, None)]; the job is
    read from the flat name, swf.stash.<pandaid>.<file>, 0 when it carries
    none."""
    found = []
    try:
        names = list(client.list_dids(BNL_SCOPE, filters=[{'name': 'swf.stash.*'}],
                                      did_type='file'))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: the BNL catalog listing of the stash failed: {e}')
        return found
    for name in names:
        if name in known:
            continue
        meta = stash_metadata(client, name)
        owes = str(meta.get('owes') or '')
        if not owes:
            _log(f'{name}: catalogued with no destination; left for a person')
            continue
        parts = name.split('.')
        pandaid = int(parts[2]) if len(parts) > 3 and parts[2].isdigit() else 0
        events = meta.get('events')
        found.append((pandaid, {
            'stashed_as': name,
            'path': f'{STASH_BASE}/{name}',
            'owes': owes,
            'reason': str(meta.get('stash_reason') or ''),
            'events': int(events) if isinstance(events, (int, str)) and str(events).isdigit() else None,
        }, None))
    return found


def stored_at(door, path, proxy):
    """(bytes, adler32) of a file at a door, or None when it is not there."""
    env = dict(os.environ, X509_USER_PROXY=proxy)
    try:
        stat = subprocess.run(['xrdfs', door, 'stat', path],
                              capture_output=True, text=True, timeout=120, env=env)
        if stat.returncode != 0:
            return None
        size = None
        for line in (stat.stdout or '').splitlines():
            if line.strip().startswith('Size:'):
                size = int(line.split(':', 1)[1].strip())
        check = subprocess.run(['xrdfs', door, 'query', 'checksum', path],
                               capture_output=True, text=True, timeout=300, env=env)
        adler = ''
        if check.returncode == 0:
            parts = (check.stdout or '').split()
            if len(parts) >= 2 and parts[0].startswith('adler32'):
                adler = parts[1]
        return (size, adler) if size is not None and adler else None
    except Exception as e:                                    # noqa: BLE001
        _log(f'WARNING: storage probe failed for {path}: {e}')
        return None


def catalogue_stash(client, entry, size, adler, events=None):
    """Register the stash replica in the BNL catalog, with what it owes.

    A stash nobody catalogued is a pile of files: the catalog is what lets
    a later pass, or a person, find what is owed when a report is lost.
    The event count rides along so the registration at JLab can be made
    from the catalog entry alone (RUCIO_REGISTRATION_CONTRACT.md).
    """
    name = entry['stashed_as']
    try:
        client.add_replicas(rse=STASH_RSE, files=[{
            'scope': BNL_SCOPE, 'name': name, 'bytes': size, 'adler32': adler,
        }], ignore_availability=True)
    except Exception as e:                                    # noqa: BLE001
        if 'Data identifier already added' not in str(e):
            return f'add_replicas: {e}'
    # Custom keys live under the JSON plugin on this instance, and are read
    # back with plugin='JSON': the default view returns none of them, which
    # reads as metadata that did not stick.
    for key, value in (('staging', 'true'), ('owes', entry.get('owes', '')),
                       ('stash_reason', entry.get('reason', '')[:200]),
                       ('events', str(events) if events is not None else '')):
        if not value:
            continue
        try:
            client.set_metadata(BNL_SCOPE, name, key, value)
        except Exception as e:                                # noqa: BLE001
            _log(f'WARNING: {key} not set on the stash entry {name}: {e}')
    return ''


def stash_metadata(client, name):
    """What a stash entry says it owes. Custom keys are JSON-plugin keys."""
    try:
        return client.get_metadata(BNL_SCOPE, name, plugin='JSON') or {}
    except Exception as e:                                    # noqa: BLE001
        _log(f'WARNING: stash metadata unreadable for {name}: {e}')
        return {}


def load_state():
    """The drain's own record of every entry it has tried to bring home:
    attempts, last attempt, outcome, reason. A file beside the storage
    store, no table."""
    try:
        with open(STATE_PATH) as handle:
            return json.load(handle) or {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        _log(f'WARNING: the drain state at {STATE_PATH} is unreadable: {e}')
        return {}


def save_state(state):
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w') as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        _log(f'ERROR: the drain state was not written to {STATE_PATH}: {e}')


def due(entry_state):
    """Whether this entry is due an attempt now: never after it is home,
    once an hour otherwise, and a bounded number of times."""
    if entry_state.get('outcome') == 'home':
        return False
    if int(entry_state.get('attempts') or 0) >= MAX_ATTEMPTS:
        return False
    last = entry_state.get('last_attempt')
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(dt_timezone.utc) - when).total_seconds() >= RETRY_INTERVAL_S


def write_prefix(client, rse):
    """The RSE's write door as a PFN prefix: the xrootd protocol with the
    best wan write priority. The read door can be another port with no
    write at all (BNL-XRD reads on 1095 and writes on 1094), so the write
    protocol is chosen by its own domain, never inferred from the read one."""
    best = None
    for protocol in client.get_protocols(rse):
        write = ((protocol.get('domains') or {}).get('wan') or {}).get('write')
        if not write or protocol.get('scheme') != 'root':
            continue
        if best is None or write < best[0]:
            best = (write, protocol)
    if best is None:
        raise RuntimeError(f'no xrootd write protocol on RSE {rse}')
    protocol = best[1]
    port = protocol.get('port')
    netloc = f"{protocol['hostname']}:{port}" if port else protocol['hostname']
    return f"{protocol['scheme']}://{netloc}//{(protocol.get('prefix') or '/').strip('/')}"


def _door_and_path(prefix, did):
    """('root://host:port', '/abs/path') for a logical name under a prefix."""
    scheme, _, rest = prefix.partition('://')
    host, _, base = rest.partition('/')
    path = '/' + '/'.join(p for p in (base.strip('/') + '/' + did.lstrip('/')).split('/') if p)
    return f'{scheme}://{host}', path


def move_home(entry, size, adler, events, jlab, jlab_proxy, write_base,
              read_base, rse, bnl, bnl_proxy):
    """One stashed file to its destination RSE, registered by logical name
    in the catalog of record, verified, then removed from the stash.
    Returns (outcome, reason): 'home', or 'registered' when the file is
    home but the stash entry could not be removed (retried, nothing
    re-copied), or 'failed' with the reason."""
    owes = entry['owes']
    src = f"{STASH_DOOR}/{entry['path']}"
    write_door, write_path = _door_and_path(write_base, owes)
    read_door, read_path = _door_and_path(read_base, owes)

    # 1. The copy, unless the destination already holds these bytes.
    there = stored_at(read_door, read_path, jlab_proxy)
    if there != (size, adler):
        env = dict(os.environ, X509_USER_PROXY=jlab_proxy)
        timeout = COPY_MIN_TIMEOUT_S + size // COPY_BYTES_PER_S
        try:
            p = subprocess.run(['xrdcp', '-f', '--path', src, f'{write_door}/{write_path}'],
                               capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return 'failed', f'copy timed out after {timeout}s'
        if p.returncode != 0:
            return 'failed', f'copy failed: {(p.stderr or p.stdout).strip()[-300:]}'
        there = stored_at(read_door, read_path, jlab_proxy)
        if there != (size, adler):
            return 'failed', (f'the copy at {rse} does not match the stash: '
                              f'{there} against ({size}, {adler})')

    # 2. Registered by logical name, with its event count, the registrar's way.
    outcome, reason = _reg.complete(jlab, rse, owes, events, jlab_proxy)
    if outcome != 'registered':
        return 'failed', f'registration {outcome}: {reason}'

    # 3. The replica reads AVAILABLE before anything is removed.
    try:
        replicas = list(jlab.list_replicas([{'scope': JLAB_SCOPE, 'name': owes}],
                                           all_states=True))
    except Exception as e:                                    # noqa: BLE001
        return 'failed', f'the registration could not be read back: {e}'
    if not any((r.get('states') or {}).get(rse) == 'AVAILABLE' for r in replicas):
        return 'failed', f'the replica at {rse} does not read AVAILABLE'
    if owes.startswith('/TEST/'):
        try:
            jlab.set_metadata(JLAB_SCOPE, owes, 'lifetime', TEST_LIFETIME_S)
        except Exception as e:                                # noqa: BLE001
            _log(f'WARNING: lifetime not set on the test output {owes}: {e}')

    # 4. The stash entry goes: the file at the door, then the catalog row.
    env = dict(os.environ, X509_USER_PROXY=bnl_proxy)
    try:
        rm = subprocess.run(['xrdfs', STASH_DOOR, 'rm', entry['path']],
                            capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return 'registered', f'home at {rse}, but the stash door did not answer the removal'
    if rm.returncode != 0 and 'no such file' not in (rm.stderr or rm.stdout).lower():
        return 'registered', (f'home at {rse}, but the stash file could not be removed: '
                              f'{(rm.stderr or rm.stdout).strip()[-200:]}')
    try:
        bnl.delete_replicas(rse=STASH_RSE, files=[{'scope': BNL_SCOPE,
                                                  'name': entry['stashed_as']}])
    except Exception as e:                                    # noqa: BLE001
        return 'registered', f'home at {rse}, but the stash catalog entry could not be removed: {e}'
    return 'home', ''


def bring_home(entries, present, state, rse, summary, bnl, bnl_proxy, dry_run=False):
    """Every present, catalogued, due entry to its destination, a few at a
    time through this host; each outcome kept in the drain's state."""
    evgen = _reg._evgen
    try:
        jlab_proxy, _ = evgen.resolve_proxy()
        jlab = evgen.rucio_client(jlab_proxy)
    except evgen.DoerError as e:
        summary['failed'].append(f'the catalog of record cannot be written: {e}')
        return
    try:
        write_base = write_prefix(jlab, rse)
        read_base = _reg.rse_pfn_prefix(jlab, rse)
    except Exception as e:                                    # noqa: BLE001
        summary['failed'].append(f'RSE {rse} protocols: {e}')
        return
    now = datetime.now(dt_timezone.utc).isoformat()
    for pandaid, entry, _report in entries:
        name = entry['stashed_as']
        if name not in present:
            continue
        size, adler, events = present[name]
        entry_state = state.setdefault(name, {})
        if not due(entry_state):
            summary['deferred'].append({'stashed_as': name, 'owes': entry['owes'],
                                        'reason': entry_state.get('reason', '')})
            continue
        if dry_run:
            summary['home'].append(entry['owes'])
            continue
        outcome, reason = move_home(entry, size, adler, events, jlab, jlab_proxy,
                                    write_base, read_base, rse, bnl, bnl_proxy)
        entry_state['attempts'] = int(entry_state.get('attempts') or 0) + 1
        entry_state['last_attempt'] = now
        entry_state['outcome'] = outcome
        entry_state['reason'] = reason
        entry_state['owes'] = entry['owes']
        if outcome == 'home':
            entry_state['home_at'] = now
            summary['moved'] += 1
            summary['home'].append(entry['owes'])
        else:
            summary['failed'].append(f'{name}: {reason}')
        _log(f"{pandaid} {name} -> {entry['owes']} at {rse}: {outcome}"
             f"{' — ' + reason if reason else ''}")


def jlab_reachable():
    """Whether the catalog of record is answering at all."""
    try:
        from pcs.services import _jlab_rucio_auth
        return bool(_jlab_rucio_auth(timeout=20))
    except Exception as e:                                    # noqa: BLE001
        _log(f'JLab probe failed: {e}')
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--rse', default=DEST_RSE,
                        help='the destination RSE, the one the payload uploads to')
    parser.add_argument('--dry-run', action='store_true',
                        help='read and probe; catalogue nothing, move nothing')
    args = parser.parse_args()

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'entries': 0, 'catalogued': 0, 'missing_at_stash': 0,
               'jlab_reachable': None, 'rse': args.rse, 'moved': 0, 'home': [],
               'deferred': [], 'failed': [], 'dry_run': bool(args.dry_run)}
    state = load_state()

    reported = stashed_entries(since, limit=args.limit)
    proxy = resolve_proxy()
    client = None
    if not proxy:
        summary['failed'].append('no usable BNL credential')
    else:
        try:
            client = bnl_client(proxy)
            client.whoami()
        except Exception as e:                                # noqa: BLE001
            summary['failed'].append(f'BNL catalog unreachable: {e}')
    entries = list(reported)
    if client is not None:
        # The catalog's own listing is the authority when a report is lost.
        entries += catalogued_entries(client, {e['stashed_as'] for _, e, _ in reported})
    summary['entries'] = len(entries)
    if not entries:
        # An empty stash is the good state and still worth recording: the
        # page must be able to say "nothing is waiting, as of this pass"
        # rather than "the drain has never run", which is what an early
        # return left it saying.
        summary['jlab_reachable'] = jlab_reachable()
        if not args.dry_run:
            store_state(summary, [], state)
        print(json.dumps(summary))
        return 0 if client is not None else (5 if not proxy else 6)
    if client is None:
        if not args.dry_run:
            store_state(summary, entries, state)
        print(json.dumps(summary))
        return 5 if not proxy else 6

    present = {}
    for pandaid, entry, report in entries:
        found = stored_at(STASH_DOOR, entry.get('path', ''), proxy)
        if found is None:
            summary['missing_at_stash'] += 1
            _log(f"{pandaid} {entry.get('stashed_as')}: not at the stash")
            continue
        events = entry.get('events')
        if events is None and report is not None:
            events = _reg._events_for(report, entry.get('owes', ''))
        present[entry['stashed_as']] = (found[0], found[1], events)
        if args.dry_run:
            summary['catalogued'] += 1
            continue
        problem = catalogue_stash(client, entry, found[0], found[1], events)
        if problem:
            summary['failed'].append(f"{entry.get('stashed_as')}: {problem}")
            _log(f"{pandaid} {entry.get('stashed_as')}: {problem}")
            present.pop(entry['stashed_as'], None)
            continue
        summary['catalogued'] += 1
        _log(f"{pandaid} {entry.get('stashed_as')}: catalogued at {STASH_RSE}, "
             f"owes {entry.get('owes')}")

    # The move home. Not attempted while the catalog of record is silent:
    # a stash that cannot be drained is a backlog, which is what it is for.
    summary['jlab_reachable'] = jlab_reachable()
    if not summary['jlab_reachable']:
        _log('JLab is not answering; the stash keeps its entries for a later pass')
    elif present:
        bring_home(entries, present, state, args.rse, summary, client, proxy,
                   dry_run=args.dry_run)

    if not args.dry_run:
        save_state(state)
        store_state(summary, entries, state)
    print(json.dumps(summary))
    return 0


def store_state(summary, entries, state):
    """Keep the pass and what is stashed where a page can read it.

    The page shows what the stash holds, what it owes, and how far each
    entry got; reading the catalog to render that would be a remote call
    in a render, so the drain leaves its own account behind instead. An
    entry brought home this pass is listed as such once, then gone.
    """
    from monitor_app.cached_product import get_product
    rows = []
    for pandaid, entry, _report in entries:
        name = entry.get('stashed_as', '')
        entry_state = state.get(name) or {}
        rows.append({
            'pandaid': pandaid,
            'stashed_as': name,
            'path': entry.get('path', ''),
            'owes': entry.get('owes', ''),
            'reason': entry.get('reason', ''),
            'outcome': entry_state.get('outcome', ''),
            'attempts': int(entry_state.get('attempts') or 0),
            'last_attempt': entry_state.get('last_attempt', ''),
            'last_error': entry_state.get('reason', '') if entry_state.get('outcome') != 'home' else '',
        })
    payload = {
        'built_at': datetime.now(dt_timezone.utc).isoformat(),
        'rse': STASH_RSE,
        'door': STASH_DOOR,
        'destination_rse': summary.get('rse', DEST_RSE),
        'jlab_reachable': summary.get('jlab_reachable'),
        'entries': rows,
        'catalogued': summary.get('catalogued', 0),
        'missing_at_stash': summary.get('missing_at_stash', 0),
        'moved': summary.get('moved', 0),
        'home': summary.get('home', []),
        'deferred': len(summary.get('deferred', [])),
        'failed': summary.get('failed', []),
    }
    try:
        get_product('stash_state', lambda: payload,
                    ttl_seconds=24 * 3600, refresh=True)
    except Exception as e:                                    # noqa: BLE001
        _log(f'WARNING: the stash state was not stored: {e}')


if __name__ == '__main__':
    sys.exit(main())
