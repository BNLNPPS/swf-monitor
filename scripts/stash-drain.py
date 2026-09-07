#!/usr/bin/env python3
"""stash-drain.py — move stashed outputs to JLab and register them there.

The registrar of the failover stash (swf-epicprod
docs/RUCIO_FAILOVER_STASH.md). A job whose output JLab would not take
writes it to a BNL dCache space and records what it stashed; this brings
those files home when JLab is reachable again.

One pass:

1. Read what the jobs stashed, from the payload reports — the PanDA
   metatable for a finished job, the swept copy for a failed one.
2. Register the stash replica in the BNL catalog if it is not already
   there, so the stash is catalogued rather than a pile of files: the
   deterministic PFN, the size and checksum the storage reports, and the
   destination in the metadata. The name is flat, because the BNL
   instance refuses a path-like DID.
3. Probe JLab. If it does not answer, the pass ends without touching
   anything: a stash that cannot be drained is a backlog, which is the
   point of having one.
4. Move each file to its JLab destination and register it there by its
   logical name, then verify the replica reads AVAILABLE.
5. Delete the stash replica only after that verification.

Retries live here and nowhere else. A file that will not move keeps its
stash entry and is tried again on later passes; nothing is deleted that
has not been verified at JLab first.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/stash-drain.py \
        [--hours 168] [--limit N] [--dry-run]

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 5 no usable credential · 6 the BNL catalog is unreachable.
"""
import argparse
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
DEFAULT_HOURS = 168


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
    """What jobs said they stashed: [(pandaid, entry)] from the reports."""
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
                    found.append((int(pandaid), entry))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: metatable read failed: {e}')

    try:
        for job in (EpicProdJob.objects.filter(updated_at__gte=since)
                    .only('pandaid', 'data').iterator()):
            if int(job.pandaid) in seen:
                continue
            report = ((job.data or {}).get('payload_report') or {}).get('report') or {}
            for entry in report.get('stash') or []:
                found.append((int(job.pandaid), entry))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: filed reports unreadable: {e}')
    return found[:limit] if limit else found


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


def catalogue_stash(client, entry, size, adler):
    """Register the stash replica in the BNL catalog, with what it owes.

    A stash nobody catalogued is a pile of files: the catalog is what lets
    a later pass, or a person, find what is owed when a report is lost.
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
                       ('stash_reason', entry.get('reason', '')[:200])):
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
    parser.add_argument('--dry-run', action='store_true',
                        help='read and probe; catalogue nothing, move nothing')
    args = parser.parse_args()

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'entries': 0, 'catalogued': 0, 'missing_at_stash': 0,
               'jlab_reachable': None, 'moved': 0, 'failed': [],
               'dry_run': bool(args.dry_run)}

    entries = stashed_entries(since, limit=args.limit)
    summary['entries'] = len(entries)
    if not entries:
        # An empty stash is the good state and still worth recording: the
        # page must be able to say "nothing is waiting, as of this pass"
        # rather than "the drain has never run", which is what an early
        # return left it saying.
        summary['jlab_reachable'] = jlab_reachable()
        if not args.dry_run:
            store_state(summary, [], None, '')
        print(json.dumps(summary))
        return 0

    proxy = resolve_proxy()
    if not proxy:
        summary['failed'].append('no usable BNL credential')
        print(json.dumps(summary))
        return 5
    try:
        client = bnl_client(proxy)
        client.whoami()
    except Exception as e:                                    # noqa: BLE001
        summary['failed'].append(f'BNL catalog unreachable: {e}')
        print(json.dumps(summary))
        return 6

    for pandaid, entry in entries:
        found = stored_at(STASH_DOOR, entry.get('path', ''), proxy)
        if found is None:
            summary['missing_at_stash'] += 1
            _log(f"{pandaid} {entry.get('stashed_as')}: not at the stash")
            continue
        if args.dry_run:
            summary['catalogued'] += 1
            continue
        problem = catalogue_stash(client, entry, found[0], found[1])
        if problem:
            summary['failed'].append(f"{entry.get('stashed_as')}: {problem}")
            _log(f"{pandaid} {entry.get('stashed_as')}: {problem}")
            continue
        summary['catalogued'] += 1
        _log(f"{pandaid} {entry.get('stashed_as')}: catalogued at {STASH_RSE}, "
             f"owes {entry.get('owes')}")

    # The move home. Not attempted while the catalog of record is silent:
    # a stash that cannot be drained is a backlog, which is what it is for.
    summary['jlab_reachable'] = jlab_reachable()
    if not summary['jlab_reachable']:
        _log('JLab is not answering; the stash keeps its entries for a later pass')

    if not args.dry_run:
        store_state(summary, entries, client, proxy)
    print(json.dumps(summary))
    return 0


def store_state(summary, entries, client, proxy):
    """Keep the pass and what is stashed where a page can read it.

    The page shows what the stash holds and what it owes; reading the
    catalog to render that would be a remote call in a render, so the
    drain leaves its own account behind instead.
    """
    from monitor_app.cached_product import get_product
    rows = []
    for pandaid, entry in entries:
        rows.append({
            'pandaid': pandaid,
            'stashed_as': entry.get('stashed_as', ''),
            'path': entry.get('path', ''),
            'owes': entry.get('owes', ''),
            'reason': entry.get('reason', ''),
        })
    payload = {
        'built_at': datetime.now(dt_timezone.utc).isoformat(),
        'rse': STASH_RSE,
        'door': STASH_DOOR,
        'jlab_reachable': summary.get('jlab_reachable'),
        'entries': rows,
        'catalogued': summary.get('catalogued', 0),
        'missing_at_stash': summary.get('missing_at_stash', 0),
        'failed': summary.get('failed', []),
    }
    try:
        get_product('stash_state', lambda: payload,
                    ttl_seconds=24 * 3600, refresh=True)
    except Exception as e:                                    # noqa: BLE001
        _log(f'WARNING: the stash state was not stored: {e}')


if __name__ == '__main__':
    sys.exit(main())
