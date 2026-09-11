#!/usr/bin/env python3
"""segfault-inventory.py — the crash-class inventory from the PanDA record.

Stage 1 of swf-epicprod docs/SEGFAULT_DIAGNOSIS.md. A payload crash reaches
the record as the payload's exit code: 128 plus the signal, stored on every
failed job as ``transexitcode`` (134 SIGABRT, 135 SIGBUS, 136 SIGFPE, 139
SIGSEGV), under whatever pilot label the pilot last read from stderr. This
reads those jobs from ``jobsarchived4`` for a window and writes one
``EpicProdJob`` row per crashed job, ``phase = 'payload_crash'``, with the
crash record under ``data['crash']``: exit code and signal, site and host,
time to death, memory, the pilot label as stored, the task name, the
sequence number and the manifest row it ran when the attempt's manifest
record exists, and the stage from the payload digest when the job carried
one.

The record, as verified 2026-09-11 on one PCS job (2723039) and one storm
job (1768411):

  * ``jobsarchived4`` holds the whole campaign (rows back to 2025-09);
    ``doma_pandaarch.jobsarchived`` holds nothing in the window, so the
    live table is read alone.
  * The job's manifest row is its ``pseudo_input`` file in ``filestable4``
    (dataset ``seq_number``, LFN = the row), and that table is purged after
    about 30 days (6,874 of 116,334 crashed jobs still had one), as is
    ``jobparamstable``. Beyond the purge the row comes from the tables
    JEDI keeps with the task: ``jedi_job_retry_history`` links every
    retried job to its successor, and the ``seq_number`` dataset's
    contents row carries the row's last attempt, so a job's row is the
    row of the last job in its retry chain (116,245 of 116,334 resolve
    this way; chains run to depth 9). The log LFN's six-digit serial is
    JEDI's job counter, not the row (task 39623: 4 rows, serials to 11),
    and is not used.
  * The payload digest in ``jobmetrics`` (``payloadStage=...``) exists on
    payload 0.11 jobs only, so the stage is mostly unknown for the
    campaign to date; the dig (stage 3) supplies it.

Nothing on an existing row outside the crash fields is touched: the
payload report the sweep files under ``data['payload_report']`` and the
registrar's notes stay as they are.

Django-bootstrap standalone script — usable by hand and as the nightly
chain step::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-inventory.py \\
        --since 2026-07-01            # the campaign back-fill
    ... --days 3                      # the nightly top-up (overlap intended)
    ... --check --since 2026-07-01    # the record's count against swfdb's

Every run records one ``segfault_inventory`` action on the epicprod action
stream with the window and the counts; a failure records outcome ``error``
and exits non-zero. The last stdout line is a JSON summary; progress and
errors go to stderr.
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from monitor_app.epicprod_inventory import _prod_task_for_jeditaskid  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.models import EpicProdJob  # noqa: E402
from monitor_app.panda.constants import PANDA_SCHEMA  # noqa: E402
from pcs.manifests import expand, record_of  # noqa: E402
from pcs.models import PandaTasks  # noqa: E402

CRASH_EXITS = ('134', '135', '136', '139')
SIGNAL_NAMES = {6: 'SIGABRT', 7: 'SIGBUS', 8: 'SIGFPE', 11: 'SIGSEGV'}
BATCH = 1000
PHASE = 'payload_crash'

JOB_FIELDS = (
    'pandaid', 'jeditaskid', 'jobname', 'computingsite', 'modificationhost',
    'transexitcode', 'piloterrorcode', 'piloterrordiag', 'starttime',
    'endtime', 'modificationtime', 'maxrss', 'maxpss', 'cpuconsumptiontime',
    'attemptnr', 'jobmetrics',
)
# The dispatcher exec in the job parameters: evgen_job_dispatcher.py <seq> <task>
DISPATCHER_SEQ_RE = re.compile(r'evgen_job_dispatcher\.py(?:%20|\s)+(\d+)')
DIGEST_RE = re.compile(r'payload(Stage|StageStatus|Trail|Events|Version|Exit)=(\S+)')

log = logging.getLogger('segfault-inventory')


def _panda(sql, params=None):
    with connections['panda'].cursor() as cur:
        cur.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _window(args):
    """(since, until) in UTC from --since or --days; until is open."""
    if args.since:
        since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=dt_timezone.utc)
    else:
        since = datetime.now(dt_timezone.utc) - timedelta(days=args.days)
    return since


# ------------------------------------------------------------- the record

def crashed_jobs(since, limit=None):
    """Crash-class failed jobs in the window, oldest first, with the task name."""
    fields = ', '.join(f'j."{f}"' for f in JOB_FIELDS)
    sql = f"""
        SELECT {fields}, t."taskname"
        FROM "{PANDA_SCHEMA}"."jobsarchived4" j
        LEFT JOIN "{PANDA_SCHEMA}"."jedi_tasks" t ON t."jeditaskid" = j."jeditaskid"
        WHERE j."jobstatus" = 'failed'
          AND j."transexitcode" = ANY(%s)
          AND j."modificationtime" > %s
        ORDER BY j."modificationtime", j."pandaid"
    """
    params = [list(CRASH_EXITS), since]
    if limit:
        sql += ' LIMIT %s'
        params.append(int(limit))
    return _panda(sql, params)


def record_count(since):
    """The appendix count: crash-class failed jobs in the window."""
    rows = _panda(
        f"""SELECT count(*) AS n FROM "{PANDA_SCHEMA}"."jobsarchived4"
            WHERE "jobstatus" = 'failed' AND "transexitcode" = ANY(%s)
              AND "modificationtime" > %s""",
        [list(CRASH_EXITS), since])
    return int(rows[0]['n'])


def seq_numbers_from_files(pandaids):
    """{pandaid: seq} from the jobs' own pseudo_input file rows, for jobs
    still inside filestable4's retention."""
    if not pandaids:
        return {}
    rows = _panda(
        f"""SELECT "pandaid", "lfn" FROM "{PANDA_SCHEMA}"."filestable4"
            WHERE "pandaid" = ANY(%s) AND "type" = 'pseudo_input' AND "lfn" <> 'pseudo_lfn'""",
        [list(pandaids)])
    return {int(r['pandaid']): int(r['lfn']) for r in rows if str(r['lfn']).isdigit()}


class RetryChains:
    """The row of a job through its retry chain, per task: the last job in
    the chain is the pandaid on the row's ``seq_number`` contents entry.
    Both tables are fetched once per task."""

    def __init__(self):
        self._next = {}      # jeditaskid -> {oldpandaid: newpandaid}
        self._last_seq = {}  # jeditaskid -> {pandaid of last attempt: seq}

    def _load(self, tid):
        nxt = {}
        for r in _panda(
                f"""SELECT "oldpandaid", "newpandaid" FROM "{PANDA_SCHEMA}"."jedi_job_retry_history"
                    WHERE "jeditaskid" = %s AND "relationtype" = 'retry'""", [tid]):
            nxt[int(r['oldpandaid'])] = int(r['newpandaid'])
        last = {}
        for r in _panda(
                f"""SELECT c."pandaid", c."lfn"
                    FROM "{PANDA_SCHEMA}"."jedi_dataset_contents" c
                    JOIN "{PANDA_SCHEMA}"."jedi_datasets" d
                      ON d."datasetid" = c."datasetid" AND d."jeditaskid" = c."jeditaskid"
                    WHERE c."jeditaskid" = %s AND d."type" = 'pseudo_input'
                      AND d."datasetname" = 'seq_number' AND c."pandaid" IS NOT NULL""", [tid]):
            if str(r['lfn']).isdigit():
                last[int(r['pandaid'])] = int(r['lfn'])
        self._next[tid], self._last_seq[tid] = nxt, last

    def seq(self, tid, pandaid):
        if tid not in self._next:
            self._load(tid)
        nxt, last = self._next[tid], self._last_seq[tid]
        cur, hops = pandaid, 0
        while cur not in last and cur in nxt and hops < 100:
            cur, hops = nxt[cur], hops + 1
        return last.get(cur)


def seq_numbers_from_params(pandaids):
    """{pandaid: seq} from the dispatcher argument in the job parameters,
    for jobs whose parameters are still in the live table."""
    if not pandaids:
        return {}
    rows = _panda(
        f"""SELECT "pandaid", "jobparameters" FROM "{PANDA_SCHEMA}"."jobparamstable"
            WHERE "pandaid" = ANY(%s)""", [list(pandaids)])
    out = {}
    for r in rows:
        text = r['jobparameters'] or ''
        if not isinstance(text, str):
            text = text.read()
        m = DISPATCHER_SEQ_RE.search(text)
        if m:
            out[int(r['pandaid'])] = int(m.group(1))
    return out


# ----------------------------------------------------------- the reading

class ManifestRows:
    """The manifest rows of an attempt from its record on PandaTasks, by
    JEDI task id, fetched once per task; None when no record exists."""

    def __init__(self):
        self._rows = {}
        self._tasks = {}

    def prod_task(self, jeditaskid):
        if jeditaskid not in self._tasks:
            self._tasks[jeditaskid] = _prod_task_for_jeditaskid(jeditaskid)
        return self._tasks[jeditaskid]

    def row(self, jeditaskid, seq):
        if not jeditaskid or not seq:
            return None
        if jeditaskid not in self._rows:
            pt = PandaTasks.objects.filter(jedi_task_id=int(jeditaskid)).first()
            rec = record_of(pt) if pt else None
            self._rows[jeditaskid] = expand(rec) if rec else None
        rows = self._rows[jeditaskid]
        if rows is None or seq < 1 or seq > len(rows):
            return None
        f, e, n, c = rows[seq - 1]
        return {'file': f, 'ext': e, 'nevents': n, 'ichunk': c}


def _minutes(start, end):
    if not start or not end:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def _digest(jobmetrics):
    """The payload digest fields in the job metrics, when the job carried one."""
    out = {}
    for key, value in DIGEST_RE.findall(jobmetrics or ''):
        out[key.lower()] = value
    return out


def _summary(stage, signal, minutes, site):
    what = stage or 'payload'
    name = SIGNAL_NAMES.get(signal, f'signal {signal}')
    when = f' after {minutes:g} min' if minutes is not None else ''
    return f'{what} died on {name} ({signal}){when} at {site or "unknown site"}'


def _num(value):
    """A numeric column as a JSON number: PanDA's numerics arrive as Decimal."""
    if value is None or value == '':
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def crash_record(j, seq, row, seq_source):
    exit_code = int(j['transexitcode'])
    signal = exit_code - 128
    digest = _digest(j.get('jobmetrics'))
    stage = digest.get('stage') or ''
    minutes = _minutes(j.get('starttime'), j.get('endtime'))
    maxrss = _num(j.get('maxrss'))
    maxpss = _num(j.get('maxpss'))
    return {
        'exit_code': exit_code,
        'signal': signal,
        'computingsite': j.get('computingsite') or '',
        'modificationhost': j.get('modificationhost') or '',
        'starttime': j['starttime'].isoformat() if j.get('starttime') else None,
        'endtime': j['endtime'].isoformat() if j.get('endtime') else None,
        'modificationtime': j['modificationtime'].isoformat() if j.get('modificationtime') else None,
        'minutes': minutes,
        'maxrss_mb': round(maxrss / 1024.0, 1) if maxrss else None,
        'maxpss_mb': round(maxpss / 1024.0, 1) if maxpss else None,
        'cpuconsumptiontime': _num(j.get('cpuconsumptiontime')),
        'piloterrorcode': _num(j.get('piloterrorcode')),
        'piloterrordiag': (j.get('piloterrordiag') or '')[:300],
        'taskname': j.get('taskname') or '',
        'jobname': j.get('jobname') or '',
        'attemptnr': _num(j.get('attemptnr')),
        'seq_source': seq_source,
        'row': row,
        'stage': stage,
        'digest': digest or None,
        'trace_status': 'unknown',
        'recorded_at': datetime.now(dt_timezone.utc).isoformat(timespec='seconds'),
    }, stage, signal, minutes


# ------------------------------------------------------------ the writing

def write_batch(jobs, manifests, chains, dry_run, counts):
    pandaids = [int(j['pandaid']) for j in jobs]
    from_files = seq_numbers_from_files(pandaids)
    missing = [p for p in pandaids if p not in from_files]
    from_params = seq_numbers_from_params(missing) if missing else {}
    existing = {r.pandaid: r for r in EpicProdJob.objects.filter(pandaid__in=pandaids)}
    now = timezone.now()
    new, changed = [], []
    for j in jobs:
        pid = int(j['pandaid'])
        tid = int(j['jeditaskid']) if j.get('jeditaskid') else None
        seq, source = None, 'none'
        if pid in from_files:
            seq, source = from_files[pid], 'filestable4'
        elif tid:
            seq = chains.seq(tid, pid)
            if seq is not None:
                source = 'retry_chain'
        if seq is None and pid in from_params:
            seq, source = from_params[pid], 'jobparamstable'
        row = manifests.row(tid, seq)
        if seq is None:
            counts['seq_unresolved'] += 1
        if row is None:
            counts['rows_unresolved'] += 1
        crash, stage, signal, minutes = crash_record(j, seq, row, source)
        obj = existing.get(pid)
        if obj is None:
            obj = EpicProdJob(pandaid=pid)
            new.append(obj)
        else:
            changed.append(obj)
        obj.jeditaskid = tid
        obj.prod_task = manifests.prod_task(tid) if tid else None
        obj.seq_number = seq
        obj.job_index = seq - 1 if seq else None
        obj.status = 'failed'
        obj.phase = PHASE
        obj.failure_summary = _summary(stage, signal, minutes, j.get('computingsite'))
        data = dict(obj.data or {})
        data['crash'] = crash
        obj.data = data
        obj.last_refreshed_at = now
        obj.updated_at = now
    counts['jobs_seen'] += len(jobs)
    counts['rows_added'] += len(new)
    counts['rows_updated'] += len(changed)
    counts['tasks'].update(int(j['jeditaskid']) for j in jobs if j.get('jeditaskid'))
    if dry_run:
        return
    if new:
        EpicProdJob.objects.bulk_create(new, batch_size=BATCH)
    if changed:
        EpicProdJob.objects.bulk_update(
            changed,
            ['jeditaskid', 'prod_task', 'seq_number', 'job_index', 'status',
             'phase', 'failure_summary', 'data', 'last_refreshed_at', 'updated_at'],
            batch_size=BATCH)


def run(args):
    since = _window(args)
    t0 = time.monotonic()
    counts = {'jobs_seen': 0, 'rows_added': 0, 'rows_updated': 0,
              'seq_unresolved': 0, 'rows_unresolved': 0, 'tasks': set()}
    log.info('window since %s%s', since.isoformat(timespec='seconds'),
             ' (dry run)' if args.dry_run else '')
    jobs = crashed_jobs(since, args.limit)
    log.info('%d crash-class jobs in the record', len(jobs))
    manifests, chains = ManifestRows(), RetryChains()
    for i in range(0, len(jobs), BATCH):
        write_batch(jobs[i:i + BATCH], manifests, chains, args.dry_run, counts)
        log.info('  %d/%d written', min(i + BATCH, len(jobs)), len(jobs))
    summary = {
        'since': since.isoformat(timespec='seconds'),
        'jobs_seen': counts['jobs_seen'],
        'rows_added': counts['rows_added'],
        'rows_updated': counts['rows_updated'],
        'seq_unresolved': counts['seq_unresolved'],
        'rows_unresolved': counts['rows_unresolved'],
        'tasks': len(counts['tasks']),
        'dry_run': bool(args.dry_run),
    }
    if not args.dry_run and not args.no_signatures:
        summary.update(signature_pass(sorted(counts['tasks']), args))
    summary['seconds'] = round(time.monotonic() - t0, 1)
    return summary, t0


def signature_pass(jeditaskids, args):
    """The record-level signatures of the tasks touched (or of every task
    in the inventory when ``jeditaskids`` is None)."""
    from monitor_app.segfaults import build_record_signatures
    log.info('signatures for %s', f'{len(jeditaskids)} tasks' if jeditaskids else 'every task')
    result = build_record_signatures(
        jeditaskids=jeditaskids, rows_lost=not args.no_rows_lost,
        rows_lost_max_tasks=args.rows_lost_max_tasks)
    log.info('  %d signatures (%d new): %s', result['signatures'],
             result['created'], result['classes'])
    return {'signatures': result['signatures'],
            'signatures_new': result['created'],
            'signature_classes': result['classes'],
            'rows_lost_checked': result['rows_lost_checked']}


def check(args):
    """The record's crash-class count for the window against swfdb's rows."""
    since = _window(args)
    in_record = record_count(since)
    in_swfdb = EpicProdJob.objects.filter(
        phase=PHASE, data__crash__modificationtime__gt=since.replace(tzinfo=None).isoformat()).count()
    in_swfdb_any = EpicProdJob.objects.filter(phase=PHASE).count()
    return {'since': since.isoformat(timespec='seconds'),
            'record': in_record, 'swfdb_in_window': in_swfdb,
            'swfdb_total': in_swfdb_any, 'match': in_record == in_swfdb}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    win = ap.add_mutually_exclusive_group()
    win.add_argument('--since', help='window start, YYYY-MM-DD (UTC)')
    win.add_argument('--days', type=int, default=3,
                     help='window length in days back from now (default 3)')
    ap.add_argument('--limit', type=int, help='at most this many jobs (a trial run)')
    ap.add_argument('--dry-run', action='store_true', help='read and count, write nothing')
    ap.add_argument('--check', action='store_true',
                    help="compare the record's count for the window with swfdb's rows")
    ap.add_argument('--signatures', action='store_true',
                    help='rebuild the signatures of every task in the inventory, '
                         'no job pass')
    ap.add_argument('--no-signatures', action='store_true',
                    help='the job pass only')
    ap.add_argument('--no-rows-lost', action='store_true',
                    help='skip the delivery lookup behind rows_lost')
    ap.add_argument('--rows-lost-max-tasks', type=int, default=50,
                    help='delivery lookups per pass, largest signatures first (default 50)')
    ap.add_argument('--instance', default='segfault-inventory',
                    help="the action's instance name ('catalog-sync' under the chain)")
    ap.add_argument('--created-by', default='', help='the action record\'s username')
    ap.add_argument('--no-action', action='store_true',
                    help='record no action (the ops agent records it under the chain)')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    if args.check:
        result = check(args)
        print(json.dumps(result))
        return 0 if result['match'] else 3
    t0 = time.monotonic()
    try:
        if args.signatures:
            summary = signature_pass(None, args)
            summary['seconds'] = round(time.monotonic() - t0, 1)
        else:
            summary, t0 = run(args)
    except Exception as e:
        log.exception('segfault inventory failed')
        if not args.no_action:
            log_epicprod_action(
                args.instance, 'segfault_inventory', outcome='error',
                duration_ms=int((time.monotonic() - t0) * 1000),
                username=args.created_by, sublevel='normal', live_default=True,
                level=logging.ERROR, message=f'segfault inventory failed: {str(e)[:200]}',
                reason=str(e)[:300])
        print(json.dumps({'error': str(e)[:300]}))
        return 1
    if not args.dry_run and not args.no_action:
        log_epicprod_action(
            args.instance, 'segfault_inventory', outcome='ok',
            duration_ms=int((time.monotonic() - t0) * 1000),
            username=args.created_by, sublevel='normal', live_default=True,
            message=(f"segfault signatures rebuilt: {summary.get('signatures')} "
                     f"({summary.get('signatures_new')} new) {summary.get('signature_classes')}"
                     if args.signatures else
                     f"segfault inventory since {summary['since'][:10]}: "
                     f"{summary['jobs_seen']} crashed jobs, {summary['rows_added']} rows added, "
                     f"{summary['rows_updated']} updated, {summary['rows_unresolved']} rows "
                     f"unresolved, {summary['tasks']} tasks, "
                     f"{summary.get('signatures', 0)} signatures "
                     f"({summary.get('signatures_new', 0)} new)"),
            **{k: v for k, v in summary.items() if isinstance(v, int) and not isinstance(v, bool)})
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
