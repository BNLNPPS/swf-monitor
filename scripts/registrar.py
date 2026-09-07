#!/usr/bin/env python3
"""registrar.py — complete the registrations the payload left pending.

The production-ops agent's doer for the second half of Measure 2
(swf-epicprod docs/RUCIO_RESILIENCE.md). A job whose output is written and
uploaded but whose catalog entry could not be made no longer fails: it
records the registration pending in its payload report and exits on its
physics. This reads those reports and finishes the job's bookkeeping.

Retry lives here and only here, so a struggling catalog sees one gentle
attempt per job and then a bounded number of hour-scale attempts from one
process, instead of thousands of jobs retrying at once. An outage
therefore produces a registration backlog that costs no compute.

Worklist, from two places, because a pending job now exits success:
  * the PanDA metatable, which holds the payload report of every finished
    job — the normal case;
  * the reports the sweeper filed on EpicProdJob for failed jobs, for a
    job that went pending and then died for another reason.

For each pending DID the file is looked for at the RSE's deterministic
path. Present means the upload stood and only the catalog entry is
missing: the replica is registered with the size and checksum the storage
reports, attached to its dataset, and given the event count the report
carries. Absent means the upload itself did not complete, which is not a
pending registration but an undelivered output, and it is reported as
such and left to the residual rerun rather than invented into the
catalog.

Attempts are recorded on the job record under
``data['payload_report']['registrar']``, so a DID is retried at most once
an hour and gives up after a bounded number of tries with its reason kept.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/registrar.py \
        [--hours 48] [--limit N] [--dry-run]

The last stdout line is a JSON summary; progress and errors go to stderr.
Exit codes: 0 ok · 5 proxy unusable · 6 Rucio unreachable.
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

# The credential and the client are the EVGEN registration doer's, so the
# two writers into the JLab catalog cannot drift on identity, proxy renewal
# or account checking. Imported rather than copied for that reason.
_spec = importlib.util.spec_from_file_location(
    'register_evgen_rucio', os.path.join(THIS_DIR, 'register-evgen-rucio.py'))
_evgen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_evgen)

RUCIO_SCOPE = _evgen.RUCIO_SCOPE
EXIT_PROXY, EXIT_RUCIO = 5, 6
# One attempt an hour, and a DID that has refused eight times is a case for
# a person rather than a ninth attempt.
RETRY_INTERVAL_S = int(os.environ.get('REGISTRAR_RETRY_INTERVAL_S', 3600))
MAX_ATTEMPTS = int(os.environ.get('REGISTRAR_MAX_ATTEMPTS', 8))
DEFAULT_HOURS = 48


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _pending_from_report(report):
    """The DIDs a payload report says are pending, or []."""
    registration = (report or {}).get('registration') or {}
    if (registration.get('outcome') or '') != 'pending':
        return []
    return [d for d in (registration.get('dids') or []) if str(d).startswith('/')]


def _events_for(report, did):
    """The event count the report gives for the output named by this DID."""
    outputs = (report or {}).get('outputs') or {}
    stem = did.rsplit('/', 1)[-1]
    for entry in outputs.values():
        if isinstance(entry, dict) and entry.get('file') == stem:
            events = entry.get('events')
            return int(events) if isinstance(events, int) else None
    return None


def worklist(since, limit=None):
    """Jobs with a pending registration: [(pandaid, report, [did, ...])]."""
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
                report = (metadata or {}).get('payload')
                dids = _pending_from_report(report)
                if dids:
                    seen.add(int(pandaid))
                    found.append((int(pandaid), report, dids))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: metatable worklist query failed: {e}')

    try:
        for job in EpicProdJob.objects.filter(updated_at__gte=since).only(
                'pandaid', 'data').iterator():
            if int(job.pandaid) in seen:
                continue
            report = ((job.data or {}).get('payload_report') or {}).get('report')
            dids = _pending_from_report(report)
            if dids:
                found.append((int(job.pandaid), report, dids))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: filed-report worklist query failed: {e}')

    return found[:limit] if limit else found


def _registrar_state(pandaid):
    job = EpicProdJob.objects.filter(pandaid=pandaid).only('data').first()
    filed = ((job.data if job else None) or {}).get('payload_report') or {}
    return filed.get('registrar') or {}


def _record_attempt(pandaid, did, outcome, reason=''):
    """Keep the attempt on the job record. No table, no migration: the
    registrar's state is a few fields beside the report it acts on."""
    try:
        job, _ = EpicProdJob.objects.get_or_create(pandaid=pandaid)
        data = dict(job.data or {})
        filed = dict(data.get('payload_report') or {})
        state = dict(filed.get('registrar') or {})
        entry = dict(state.get(did) or {})
        entry['attempts'] = int(entry.get('attempts') or 0) + 1
        entry['last_attempt'] = datetime.now(dt_timezone.utc).isoformat()
        entry['outcome'] = outcome
        entry['reason'] = reason
        state[did] = entry
        filed['registrar'] = state
        data['payload_report'] = filed
        job.data = data
        job.save(update_fields=['data', 'updated_at'])
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: attempt not recorded for {pandaid} {did}: {e}')


def _due(state, did):
    """Whether this DID is due an attempt now."""
    entry = state.get(did) or {}
    if entry.get('outcome') == 'registered':
        return False
    if int(entry.get('attempts') or 0) >= MAX_ATTEMPTS:
        return False
    last = entry.get('last_attempt')
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(dt_timezone.utc) - when).total_seconds() >= RETRY_INTERVAL_S


def rse_pfn_prefix(client, rse):
    """The deterministic PFN prefix of an RSE, from its own protocol entry,
    so nothing here hardcodes a door."""
    protocols = client.get_protocols(rse)
    for protocol in sorted(protocols, key=lambda p: p.get('domains', {})
                           .get('wan', {}).get('read', 99)):
        scheme = protocol.get('scheme')
        host = protocol.get('hostname')
        if not (scheme and host):
            continue
        port = protocol.get('port')
        prefix = protocol.get('prefix') or '/'
        netloc = f'{host}:{port}' if port else host
        return f'{scheme}://{netloc}/{prefix.lstrip("/")}'
    raise _evgen.DoerError(EXIT_RUCIO, f'no readable protocol on RSE {rse}')


def stored_file(pfn, proxy):
    """(bytes, adler32) of the file at a PFN, or None when it is not there.

    The storage is asked, never the catalog: the question is whether the
    upload stood, and the catalog is the thing that could not answer.
    """
    door, _, path = pfn.partition('//')[2].partition('/')
    door_url = f"{pfn.split('//')[0]}//{door}"
    env = dict(os.environ, X509_USER_PROXY=proxy)
    try:
        stat = subprocess.run(['xrdfs', door_url, 'stat', f'/{path}'],
                              capture_output=True, text=True, timeout=120, env=env)
        if stat.returncode != 0:
            return None
        size = None
        for line in (stat.stdout or '').splitlines():
            if line.strip().startswith('Size:'):
                size = int(line.split(':', 1)[1].strip())
        check = subprocess.run(['xrdfs', door_url, 'query', 'checksum', f'/{path}'],
                               capture_output=True, text=True, timeout=300, env=env)
        adler = ''
        if check.returncode == 0:
            parts = (check.stdout or '').split()
            if len(parts) >= 2 and parts[0].startswith('adler32'):
                adler = parts[1]
        if size is None or not adler:
            return None
        return size, adler
    except Exception as e:                                    # noqa: BLE001
        _log(f'WARNING: storage probe failed for {pfn}: {e}')
        return None


def complete(client, rse, did, events, proxy, dry_run=False):
    """Register one pending DID. Returns (outcome, reason)."""
    dataset = did.rsplit('/', 1)[0]
    try:
        replicas = list(client.list_replicas(
            [{'scope': RUCIO_SCOPE, 'name': did}], all_states=True))
    except Exception as e:                                    # noqa: BLE001
        return 'deferred', f'catalog unreachable: {e}'
    for replica in replicas:
        if 'AVAILABLE' in (replica.get('states') or {}).values():
            return 'registered', 'already registered with an available replica'

    prefix = rse_pfn_prefix(client, rse)
    pfn = f'{prefix.rstrip("/")}/{did.lstrip("/")}'
    found = stored_file(pfn, proxy)
    if found is None:
        return 'undelivered', ('the output is not at the storage, so the upload '
                               'did not complete; this is a residual rerun, not '
                               'a registration')
    size, adler = found
    if dry_run:
        return 'would register', f'{size} bytes, adler32 {adler}'

    entry = {'scope': RUCIO_SCOPE, 'name': did, 'bytes': size, 'adler32': adler}
    try:
        client.add_replicas(rse=rse, files=[entry], ignore_availability=True)
    except Exception as e:                                    # noqa: BLE001
        if 'Data identifier already added' not in str(e):
            return 'deferred', f'add_replicas: {e}'
    try:
        client.add_dataset(scope=RUCIO_SCOPE, name=dataset)
    except Exception as e:                                    # noqa: BLE001
        if 'Data identifier already added' not in str(e):
            _log(f'WARNING: add_dataset {dataset}: {e}')
    try:
        client.attach_dids(scope=RUCIO_SCOPE, name=dataset,
                           dids=[{'scope': RUCIO_SCOPE, 'name': did}])
    except Exception as e:                                    # noqa: BLE001
        if 'already attached' not in str(e).lower():
            return 'deferred', f'attach_dids: {e}'
    if events is not None:
        try:
            client.set_metadata(RUCIO_SCOPE, did, 'events', int(events))
        except Exception as e:                                # noqa: BLE001
            # The registration stands; the count is reported, never invented.
            _log(f'WARNING: events not set on {did}: {e}')
    return 'registered', ''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS,
                        help='how far back to look for pending registrations')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--rse', default=os.environ.get('REGISTRAR_RSE', 'BNL-XRD'),
                        help='the RSE the payload uploaded to')
    parser.add_argument('--dry-run', action='store_true',
                        help='find and probe, register nothing')
    args = parser.parse_args()

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'window_hours': args.hours, 'rse': args.rse, 'jobs': 0,
               'registered': [], 'deferred': [], 'undelivered': [],
               'skipped': 0, 'dry_run': bool(args.dry_run)}

    work = worklist(since, limit=args.limit)
    summary['jobs'] = len(work)
    if not work:
        print(json.dumps(summary))
        return 0

    try:
        proxy, summary['proxy'] = _evgen.resolve_proxy()
    except _evgen.DoerError as e:
        summary['error'] = e.msg
        print(json.dumps(summary))
        return EXIT_PROXY
    try:
        client = _evgen.rucio_client(proxy)
    except _evgen.DoerError as e:
        summary['error'] = e.msg
        print(json.dumps(summary))
        return EXIT_RUCIO

    for pandaid, report, dids in work:
        state = _registrar_state(pandaid)
        for did in dids:
            if not _due(state, did):
                summary['skipped'] += 1
                continue
            outcome, reason = complete(
                client, args.rse, did, _events_for(report, did), proxy,
                dry_run=args.dry_run)
            _log(f'{pandaid} {did}: {outcome}{" — " + reason if reason else ""}')
            if not args.dry_run:
                _record_attempt(pandaid, did, outcome, reason)
            if outcome == 'registered' or outcome == 'would register':
                summary['registered'].append(did)
            elif outcome == 'undelivered':
                summary['undelivered'].append({'did': did, 'reason': reason})
            else:
                summary['deferred'].append({'did': did, 'reason': reason})

    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
