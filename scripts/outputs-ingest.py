#!/usr/bin/env python3
"""outputs-ingest.py — the production record of what a task produced.

One pass over a PCS task's jobs, reading each job's payload report and
posting what it delivered to the production record (swf-epicprod
docs/RUCIO_RESILIENCE.md, Measure 3). Rucio holds the bytes; this record
holds the truth, and it is what says whether a sample is complete.

The same pass yields the registrar's worklist, because a report that says
its registration is pending is exactly a row in ``pending``.

Reports come from two places, since a pending job now exits success:
  * the PanDA metatable, which holds a finished job's report;
  * the copy the sweeper filed on EpicProdJob, for a failed job.

Writing goes through ``POST /pcs/api/outputs/`` and nowhere else — the
endpoint is the single writer of that record, keyed by task and DID, so
re-running updates rather than duplicates and a re-run is always safe.

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/outputs-ingest.py \
        [--hours 48] [--task <jeditaskid>] [--dry-run]

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 2 nothing to write · 6 the record refused a batch.
"""
import argparse
import json
import os
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

OUTPUTS_URL = os.environ.get(
    'PCS_OUTPUTS_URL', 'https://localhost/swf-monitor/pcs/api/outputs/')
POST_TIMEOUT_S = 60
DEFAULT_HOURS = 48


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def pcs_tasks(since, jedi_task_id=None):
    """The JEDI tasks PCS owns whose jobs moved in the window.

    A task PCS does not own has no production record to write, and the
    endpoint refuses it rather than storing against a guess, so the pass
    does not offer it.
    """
    from pcs.models import PandaTasks
    rows = PandaTasks.objects.filter(jedi_task_id__isnull=False)
    if jedi_task_id:
        rows = rows.filter(jedi_task_id=jedi_task_id)
    ids = sorted({int(t) for t in rows.values_list('jedi_task_id', flat=True)})
    if jedi_task_id or not ids:
        return ids
    sql = f"""
        SELECT DISTINCT "jeditaskid" FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "jeditaskid" = ANY(%s) AND "modificationtime" >= %s
        UNION
        SELECT DISTINCT "jeditaskid" FROM "{PANDA_SCHEMA}"."jobsactive4"
        WHERE "jeditaskid" = ANY(%s) AND "modificationtime" >= %s
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [ids, since, ids, since])
            return sorted({int(r[0]) for r in cursor.fetchall()})
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: active-task query failed: {e}')
        return []


def reports_for_task(jedi_task_id):
    """(pandaid, report, reported_at) for every job of the task that has one."""
    found, seen = [], set()
    sql = f"""
        SELECT m."pandaid", m."metadata", j."endtime"
        FROM "{PANDA_SCHEMA}"."metatable" m
        JOIN "{PANDA_SCHEMA}"."jobsarchived4" j ON j."pandaid" = m."pandaid"
        WHERE j."jeditaskid" = %s
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [jedi_task_id])
            for pandaid, raw, endtime in cursor.fetchall():
                try:
                    metadata = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, TypeError):
                    continue
                report = (metadata or {}).get('payload')
                if isinstance(report, dict):
                    seen.add(int(pandaid))
                    found.append((int(pandaid), report, endtime))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: metatable read failed for task {jedi_task_id}: {e}')

    try:
        for job in (EpicProdJob.objects.filter(jeditaskid=jedi_task_id)
                    .only('pandaid', 'data', 'updated_at').iterator()):
            if int(job.pandaid) in seen:
                continue
            filed = (job.data or {}).get('payload_report') or {}
            report = filed.get('report')
            if isinstance(report, dict):
                found.append((int(job.pandaid), report, job.updated_at))
    except Exception as e:                                    # noqa: BLE001
        _log(f'ERROR: filed reports unreadable for task {jedi_task_id}: {e}')
    return found


def segment_of(did):
    """The unit of work a file belongs to: its name without the stage.

    FULL and RECO of one manifest row share a stem, so grouping on it is
    what makes a work unit's state derivable rather than stored.
    """
    stem = did.rsplit('/', 1)[-1]
    for suffix in ('.eicrecon.edm4eic.root', '.edm4hep.root', '.root'):
        if stem.endswith(suffix):
            return stem[:-len(suffix)]
    return stem


def rows_from_report(pandaid, report, reported_at):
    """The record rows one job's report yields, one per file.

    A row exists for a file the job registered, owes a registration for,
    or failed to register — not for a file it merely produced. The trial
    path, for one, makes a FULL output and never uploads it: inventing a
    delivery state for a file that never left the worker would put the
    registrar on a hunt for bytes that are not there.

    The outcome is per report; the state is per file, because a job can
    register one output and fail the other.
    """
    registration = report.get('registration') or {}
    outcome = (registration.get('outcome') or '').strip()
    outputs = {}
    for kind, entry in (report.get('outputs') or {}).items():
        if isinstance(entry, dict) and entry.get('file'):
            outputs[entry['file']] = (str(kind).upper(), entry)

    def _row(did, state, reason):
        stem = str(did).rsplit('/', 1)[-1]
        kind, entry = outputs.get(stem, ('', {}))
        return {
            'pandaid': pandaid,
            'did': str(did),
            'kind': kind or ('RECO' if 'eicrecon' in stem else 'FULL'),
            'segment': segment_of(str(did)),
            'events': entry.get('events'),
            'bytes': entry.get('bytes'),
            'status': state,
            'reason': reason,
            'reported_at': (reported_at.isoformat()
                            if hasattr(reported_at, 'isoformat') else None),
        }

    state = {'registered': 'delivered', 'pending': 'pending',
             'diverted': 'diverted'}.get(outcome, 'lost')
    reason = '' if state == 'delivered' else (
        f'the job reported registration {outcome or "not reached"}')
    rows = [_row(did, state, reason)
            for did in (registration.get('dids') or [])]
    # What the job named as failed is lost to this attempt whatever the
    # overall outcome was: the catalog has nothing for it.
    rows.extend(_row(did, 'lost', 'the job reported this registration failed')
                for did in (registration.get('failed') or []))
    return rows


def post_outputs(jedi_task_id, rows, token):
    """Send one task's rows to the single writer. Returns (ok, detail)."""
    import urllib.error
    import urllib.request
    import ssl
    body = json.dumps({'jedi_task_id': jedi_task_id, 'outputs': rows}).encode()
    request = urllib.request.Request(
        OUTPUTS_URL, data=body, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Token {token}'})
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S,
                                    context=context) as response:
            return True, json.loads(response.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        return False, f'{e.code}: {e.read().decode()[:300]}'
    except Exception as e:                                    # noqa: BLE001
        return False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS)
    parser.add_argument('--task', type=int, default=None,
                        help='one JEDI task id, ignoring the window')
    parser.add_argument('--dry-run', action='store_true',
                        help='read and shape the rows, write nothing')
    args = parser.parse_args()

    token = os.environ.get('SWF_API_TOKEN', '')
    if not token and not args.dry_run:
        print(json.dumps({'error': 'SWF_API_TOKEN is not set'}))
        return 6

    since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'tasks': 0, 'jobs': 0, 'rows': 0, 'delivered': 0, 'pending': 0,
               'lost': 0, 'written': 0, 'refused': [], 'dry_run': args.dry_run}

    for jedi_task_id in pcs_tasks(since, jedi_task_id=args.task):
        reports = reports_for_task(jedi_task_id)
        if not reports:
            continue
        rows = []
        for pandaid, report, reported_at in reports:
            rows.extend(rows_from_report(pandaid, report, reported_at))
        if not rows:
            continue
        summary['tasks'] += 1
        summary['jobs'] += len(reports)
        summary['rows'] += len(rows)
        for row in rows:
            summary[row['status']] = summary.get(row['status'], 0) + 1
        _log(f'task {jedi_task_id}: {len(reports)} report(s), {len(rows)} row(s)')
        if args.dry_run:
            continue
        ok, detail = post_outputs(jedi_task_id, rows, token)
        if ok:
            summary['written'] += (detail or {}).get('written', len(rows))
            for refusal in (detail or {}).get('refused') or []:
                summary['refused'].append(f'{jedi_task_id}: {refusal}')
        else:
            summary['refused'].append(f'{jedi_task_id}: {detail}')
            _log(f'ERROR: task {jedi_task_id} refused: {detail}')

    print(json.dumps(summary))
    return 0 if summary['rows'] else 2


if __name__ == '__main__':
    sys.exit(main())
