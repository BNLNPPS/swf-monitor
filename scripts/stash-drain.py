#!/usr/bin/env python3
"""stash-drain.py — register stashed outputs where they lie.

The registrar of the failover stash (swf-epicprod
docs/RUCIO_FAILOVER_STASH.md). A job whose output JLab would not take
writes it to the BNL science-data RSE, BNL-XRD, at the path Rucio's
deterministic naming gives its logical name, and records what it
stashed. The file is therefore already home; what the catalog of record
is owed is the replica row. This pass supplies it when JLab answers.

One pass:

1. Read what the jobs stashed, from the payload reports — the PanDA
   metatable for a finished job, the swept copy for a failed one.
2. Confirm each file at the door, with its size and checksum.
3. Probe JLab. If it does not answer, the pass ends without touching
   anything: a stash that cannot be registered is a backlog, which is
   the point of having one.
4. Register each file in the JLab catalog by logical name at the stash
   RSE, with its event count, through the registrar's own registration;
   verify the replica reads AVAILABLE. Nothing is copied and nothing is
   removed: the file stays where it is, a replica like any other, and a
   later move is Rucio's to make.

Retries live here and nowhere else. A file that will not register keeps
its entry and is tried again on later passes, once an hour and a bounded
number of times, with its reason kept in the drain's state file.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/stash-drain.py \
        [--hours 168] [--limit N] [--entry /TEST/stash-probe/x.txt] [--dry-run]

``--entry`` names a logical file already at its deterministic path on the
stash RSE and registers it as a stash entry would be: the hand test of
the path, and the way to register a stashed file no report names.

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 5 no usable credential.
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

# The stash: the BNL science-data RSE of the JLab catalog, its write door,
# and its deterministic prefix, so a stashed file sits at the PFN its
# logical name resolves to. The same values run.sh writes with
# (STASH_DOOR, STASH_PREFIX).
STASH_RSE = os.environ.get('STASH_RSE', 'BNL-XRD')
STASH_DOOR = os.environ.get('STASH_DOOR', 'root://epicxrd1.sdcc.bnl.gov:1094')
STASH_PREFIX = os.environ.get('STASH_PREFIX', '/eic/EPIC')
DEFAULT_HOURS = 168
STATE_PATH = os.environ.get('STASH_DRAIN_STATE',
                            '/data/wenauseic/swf-delivery/stash-drain-state.json')
RETRY_INTERVAL_S = int(os.environ.get('STASH_RETRY_INTERVAL_S', 3600))
MAX_ATTEMPTS = int(os.environ.get('STASH_MAX_ATTEMPTS', 8))
# A test output (under /TEST/) registered from the stash removes itself, as
# the payload canaries' do.
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


def stash_path(owes):
    """The path at the stash door of a logical name: the RSE's deterministic
    prefix and the name."""
    return '/' + '/'.join(p for p in f'{STASH_PREFIX}/{owes}'.split('/') if p)


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


def hand_entry(owes):
    """A stash entry for a logical name given by hand: the file is expected
    at its deterministic path on the stash RSE."""
    owes = '/' + owes.lstrip('/')
    return (0, {'stashed_as': owes, 'path': stash_path(owes), 'owes': owes,
                'reason': 'registered by hand from the stash'}, None)


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


def load_state():
    """The drain's own record of every entry it has tried to register:
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


def register_in_place(entry, events, jlab, proxy, rse):
    """One stashed file registered by logical name at the RSE it lies on,
    the registrar's way, and verified AVAILABLE. Returns ('home', '') or
    ('failed', reason)."""
    owes = entry['owes']
    outcome, reason = _reg.complete(jlab, rse, owes, events, proxy)
    if outcome != 'registered':
        return 'failed', f'registration {outcome}: {reason}'
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
    return 'home', ''


def register_all(entries, present, state, rse, summary, proxy, dry_run=False):
    """Every present, due entry registered where it lies; each outcome kept
    in the drain's state."""
    evgen = _reg._evgen
    try:
        jlab = evgen.rucio_client(proxy)
    except evgen.DoerError as e:
        summary['failed'].append(f'the catalog of record cannot be written: {e}')
        return
    now = datetime.now(dt_timezone.utc).isoformat()
    for pandaid, entry, _report in entries:
        name = entry['stashed_as']
        if name not in present:
            continue
        entry_state = state.setdefault(name, {})
        if not due(entry_state):
            summary['deferred'].append({'stashed_as': name, 'owes': entry['owes'],
                                        'reason': entry_state.get('reason', '')})
            continue
        if dry_run:
            summary['home'].append(entry['owes'])
            continue
        _size, _adler, events = present[name]
        outcome, reason = register_in_place(entry, events, jlab, proxy, rse)
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
        _log(f"{pandaid} {name} at {rse}: {outcome}{' — ' + reason if reason else ''}")


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
    parser.add_argument('--rse', default=STASH_RSE,
                        help='the stash RSE, where the files lie and are registered')
    parser.add_argument('--entry', action='append', default=[],
                        help='a logical name already at its deterministic path '
                             'on the stash RSE, registered as a stash entry')
    parser.add_argument('--dry-run', action='store_true',
                        help='read and probe; register nothing')
    args = parser.parse_args()

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'entries': 0, 'catalogued': 0, 'missing_at_stash': 0,
               'jlab_reachable': None, 'rse': args.rse, 'moved': 0, 'home': [],
               'deferred': [], 'failed': [], 'dry_run': bool(args.dry_run)}
    state = load_state()

    entries = list(stashed_entries(since, limit=args.limit))
    entries += [hand_entry(owes) for owes in args.entry]
    summary['entries'] = len(entries)

    evgen = _reg._evgen
    try:
        proxy, _ = evgen.resolve_proxy()
    except evgen.DoerError as e:
        summary['failed'].append(f'no usable credential: {e}')
        if not args.dry_run:
            store_state(summary, entries, state)
        print(json.dumps(summary))
        return 5

    if not entries:
        # An empty stash is the good state and still worth recording: the
        # page must be able to say "nothing is waiting, as of this pass"
        # rather than "the drain has never run".
        summary['jlab_reachable'] = jlab_reachable()
        if not args.dry_run:
            store_state(summary, [], state)
        print(json.dumps(summary))
        return 0

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
        summary['catalogued'] += 1
        _log(f"{pandaid} {entry.get('stashed_as')}: at {STASH_RSE}, "
             f"{found[0]} bytes, owes {entry.get('owes')}")

    # Not attempted while the catalog of record is silent: a stash that
    # cannot be registered is a backlog, which is what it is for.
    summary['jlab_reachable'] = jlab_reachable()
    if not summary['jlab_reachable']:
        _log('JLab is not answering; the stash keeps its entries for a later pass')
    else:
        register_all(entries, present, state, args.rse, summary, proxy,
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
    in a render, so the drain leaves its own account behind instead.
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
        'destination_rse': summary.get('rse', STASH_RSE),
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
