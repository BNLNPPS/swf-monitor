#!/usr/bin/env python3
"""node-measure-ingest.py — fold finished jobs' measures into the node
measurement store (site-canary docs/MEASUREMENTS.md).

Every finished ePIC production job leaves a payload report with what
prmon measured per stage and the events it produced; its PanDA record
says where it ran. This reads the jobs finished since its cursor, resolves
each job's workload through PCS, computes per stage the measures the store
keeps, and folds them into the running distributions of that queue,
processor and workload. The cursor is the newest job modification time
folded, kept in a state file beside the storage store, so a job is folded
once.

The ePIC-specific part is here: which report fields mean what, and how
the workload keys are found. The store and its fold know nothing of PCS
(canary.store.measure).

Django-bootstrap standalone script — also usable by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/node-measure-ingest.py \
        [--hours 168] [--limit 5000] [--dry-run] [--reset]

The last stdout line is a JSON summary; progress goes to stderr.
Exit codes: 0 ok · 6 the PanDA record could not be read.
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

from monitor_app.panda.constants import PANDA_SCHEMA  # noqa: E402

STATE_PATH = os.environ.get('NODE_MEASURE_STATE',
                            '/data/wenauseic/swf-delivery/node-measure-state.json')
DEFAULT_HOURS = 168
DEFAULT_LIMIT = 5000
# The stages whose event count is the simulated one; every other stage's
# per-event measure divides by the events the job delivered.
SIMULATION_STAGES = ('simulation', 'background', 'evgen', 'input', 'geometry')


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_state():
    try:
        with open(STATE_PATH) as handle:
            return json.load(handle) or {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        _log(f'WARNING: state at {STATE_PATH} unreadable: {e}')
        return {}


def save_state(state):
    try:
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w') as handle:
            json.dump(state, handle, indent=1, sort_keys=True, default=str)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        _log(f'ERROR: state not written to {STATE_PATH}: {e}')


def finished_jobs(since, limit):
    """Finished production jobs with a report, oldest first:
    (pandaid, jeditaskid, queue, processor, cores, modificationtime,
    container, report)."""
    sql = f"""
        SELECT j."pandaid", j."jeditaskid", j."computingsite",
               COALESCE(j."cpuconsumptionunit", 'unknown'),
               GREATEST(COALESCE(j."actualcorecount", j."corecount", 1), 1),
               j."modificationtime", COALESCE(j."container_name", ''),
               m."metadata"
        FROM "{PANDA_SCHEMA}"."jobsarchived4" j
        JOIN "{PANDA_SCHEMA}"."metatable" m ON m."pandaid" = j."pandaid"
        WHERE j."jobstatus" = 'finished' AND j."processingtype" = 'epicproduction'
          AND j."modificationtime" > %s
        ORDER BY j."modificationtime"
        LIMIT %s
    """
    rows = []
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, [since, int(limit)])
        for (pandaid, jeditaskid, queue, processor, cores, modtime,
             container, raw) in cursor.fetchall():
            try:
                metadata = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                metadata = None
            report = (metadata or {}).get('payload') if isinstance(metadata, dict) else None
            # PanDA times are naive UTC; the cursor and the store are aware.
            if modtime is not None and modtime.tzinfo is None:
                modtime = modtime.replace(tzinfo=dt_timezone.utc)
            rows.append((int(pandaid), jeditaskid, queue, processor, int(cores),
                         modtime, container, report))
    return rows


def stage_measures(report, cores):
    """Per stage, the measures the store keeps, from the report's prmon
    summaries and event counts. A stage with no events to divide by keeps
    its efficiency and memory and no per-event figure."""
    prmon = report.get('prmon') or {}
    events = report.get('events') or {}
    simulated = events.get('simulated')
    delivered = events.get('reconstructed') or simulated
    out = {}
    for stage, rec in prmon.items():
        if not isinstance(rec, dict) or 'error' in rec:
            continue
        wall = rec.get('wall_s')
        cpu = (rec.get('cpu_user_s') or 0) + (rec.get('cpu_sys_s') or 0)
        count = simulated if stage in SIMULATION_STAGES else delivered
        measures = {}
        if count and wall:
            measures['cpu_s_per_event'] = cpu / count
            measures['wall_s_per_event'] = wall / count
        if wall:
            measures['cpu_efficiency'] = cpu / (wall * max(int(cores or 1), 1))
        if rec.get('rss_max_kb') is not None:
            measures['rss_max_kb'] = rec['rss_max_kb']
        if measures:
            out[stage] = measures
    return out


class Workloads:
    """The workload keys of a task through PCS, once per task."""

    def __init__(self):
        self._cache = {}

    def for_task(self, jeditaskid):
        if jeditaskid in self._cache:
            return self._cache[jeditaskid]
        from pcs.models import PandaTasks
        row = (PandaTasks.objects.filter(jedi_task_id=jeditaskid)
               .select_related('prod_task__dataset__physics_config').first())
        keys = None
        if row is not None:
            dataset = row.prod_task.dataset if row.prod_task_id else None
            keys = {
                'detector_version': (dataset.detector_version if dataset else '') or '',
                'physics_config': ((dataset.physics_config.label
                                    if dataset is not None and dataset.physics_config_id
                                    else '') or ''),
            }
        self._cache[jeditaskid] = keys
        return keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hours', type=float, default=DEFAULT_HOURS,
                        help='the window when there is no cursor yet')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                        help='jobs per batch; batches run until one comes back short')
    parser.add_argument('--dry-run', action='store_true',
                        help='read and compute; fold nothing, move no cursor')
    parser.add_argument('--reset', action='store_true',
                        help='forget the cursor and start from --hours ago')
    args = parser.parse_args()

    from canary.store import measure

    state = {} if args.reset else load_state()
    cursor = state.get('cursor')
    if cursor:
        since = datetime.fromisoformat(cursor)
    else:
        since = datetime.now(dt_timezone.utc) - timedelta(hours=args.hours)
    summary = {'since': since.isoformat(), 'jobs': 0, 'folded': 0, 'rows': 0,
               'no_report': 0, 'no_task': 0, 'no_measures': 0,
               'cursor': cursor, 'dry_run': bool(args.dry_run)}
    workloads = Workloads()
    newest = since

    while True:
        try:
            batch = finished_jobs(since if newest == since else newest, args.limit)
        except Exception as e:                                # noqa: BLE001
            summary['error'] = f'the PanDA record could not be read: {e}'
            print(json.dumps(summary, default=str))
            return 6
        for pandaid, jeditaskid, queue, processor, cores, modtime, container, report in batch:
            summary['jobs'] += 1
            if modtime and modtime > newest:
                newest = modtime
            if not isinstance(report, dict) or not report.get('prmon'):
                summary['no_report'] += 1
                continue
            keys = workloads.for_task(jeditaskid) if jeditaskid else None
            if keys is None:
                summary['no_task'] += 1
                continue
            stages = stage_measures(report, cores)
            if not stages:
                summary['no_measures'] += 1
                continue
            workload = dict(keys, container_image=container or '',
                            payload_version=str(report.get('payload_version') or ''))
            if args.dry_run:
                summary['folded'] += 1
                summary['rows'] += len(stages)
                continue
            try:
                summary['rows'] += measure.record_job(
                    queue_name=queue, processor=processor, workload=workload,
                    stages=stages, job_at=modtime)
                summary['folded'] += 1
            except Exception as e:                            # noqa: BLE001
                _log(f'ERROR: job {pandaid} not folded: {e}')
        if len(batch) < args.limit:
            break
        _log(f"{summary['jobs']} jobs read, cursor at {newest.isoformat()}")

    if not args.dry_run and newest > since:
        state['cursor'] = newest.isoformat()
        state['last_run'] = datetime.now(dt_timezone.utc).isoformat()
        state['last_summary'] = {k: v for k, v in summary.items() if k != 'cursor'}
        save_state(state)
    summary['cursor'] = newest.isoformat()
    print(json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
