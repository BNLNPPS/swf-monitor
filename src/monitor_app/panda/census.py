"""The per-queue job census: the pool of not-yet-running work at each
PanDA queue, in jobs and in hours at the queue's capacity, with the
six-hour completion and failure counts that gate it (swf-epicprod
docs/CONTINUOUS_PRODUCTION.md, The dispatcher: The pressure measure).

The dispatcher's loop and the ready-queue page read one record from
``queue_census()``. The live counts read ``jobsactive4`` on every call.
The calibration per queue (median and p90 finished walltime of
production jobs, peak concurrent running, over the last 14 days) reads
``jobsarchived4`` and is cached for an hour: it moves slowly and its
two queries cost about a second each. The six-hour window (finished,
failed, fast failures) is read on every call.

Hours at capacity is the depth the dispatcher regulates on: the
not-started jobs of a queue times the queue's median finished walltime,
divided by its running ceiling (the 14-day peak, or the current running
count when that is higher). It is None where the queue has no
calibration.
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import connections
from django.utils import timezone

from .constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

NOT_STARTED = ('defined', 'waiting', 'assigned', 'activated', 'sent', 'starting')
RUNNING = ('running',)
FINISHING = ('holding', 'transferring', 'merging')
CALIBRATION_DAYS = 14
GATE_HOURS = 6
# A failure is fast when it dies in under this fraction of the queue's
# median finished walltime (20 minutes where no median exists).
FAST_FAILURE_FRACTION = 0.25
FAST_FAILURE_FLOOR_S = 20 * 60
CALIBRATION_CACHE_KEY = 'panda:queue-census:calibration:v2'
CALIBRATION_TTL_S = 3600
# JEDI task statuses that end a task (as monitor_app.snapper_panda reads them).
TASK_TERMINAL_STATUSES = ('done', 'finished', 'failed', 'broken', 'aborted',
                          'exhausted', 'passed')


def _live_counts():
    """Per queue and status, the jobs in ``jobsactive4``: all jobs, their
    cores, and the production subset."""
    sql = f"""
        SELECT COALESCE("computingsite", 'unknown'), "jobstatus",
               COUNT(*),
               COALESCE(SUM(COALESCE("actualcorecount", "corecount", 1)), 0),
               COUNT(*) FILTER (WHERE "processingtype" = 'epicproduction')
        FROM "{PANDA_SCHEMA}"."jobsactive4"
        GROUP BY 1, 2
    """
    out = {}
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql)
        for site, status, jobs, cores, prod in cursor.fetchall():
            out.setdefault(str(site), {})[str(status)] = {
                'jobs': int(jobs or 0), 'cores': int(cores or 0),
                'production': int(prod or 0)}
    return out


def _tasks_active():
    """Per queue, the production tasks pinned to it that have not
    ended (canary and other test tasks excluded). By processing type,
    not VO: PCS submits under ``epic``, the production team's own
    submissions carry the client default ``wlcg``, and both are the
    queue's production."""
    placeholders = ', '.join(['%s'] * len(TASK_TERMINAL_STATUSES))
    sql = f"""
        SELECT COALESCE("site", 'unknown'), COUNT(*)
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE "processingtype" = 'epicproduction'
          AND ("status" IS NULL OR "status" NOT IN ({placeholders}))
        GROUP BY 1
    """
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, list(TASK_TERMINAL_STATUSES))
        return {str(site): int(n or 0) for site, n in cursor.fetchall()}


def _ungenerated():
    """Per queue, the rows of its non-terminal production tasks that
    JEDI has not generated jobs for: ``nFilesToBeUsed - nFilesUsed`` on
    each task's input dataset (the largest input where a task carries
    several, as an EVGEN task carries two pseudo inputs with one count).
    The work committed to a queue beyond its activated pool; the
    pressure front's phase-two depth counts it once the ePIC job
    throttler paces generation (swf-epicprod CONTINUOUS_PRODUCTION.md,
    Two regulators)."""
    placeholders = ', '.join(['%s'] * len(TASK_TERMINAL_STATUSES))
    sql = f"""
        SELECT site, SUM(remaining) FROM (
            SELECT COALESCE(t."site", 'unknown') AS site, t."jeditaskid",
                   MAX(GREATEST(COALESCE(d."nfilestobeused", 0) - COALESCE(d."nfilesused", 0), 0)) AS remaining
            FROM "{PANDA_SCHEMA}"."jedi_tasks" t
            JOIN "{PANDA_SCHEMA}"."jedi_datasets" d ON d."jeditaskid" = t."jeditaskid"
            WHERE t."processingtype" = 'epicproduction'
              AND (t."status" IS NULL OR t."status" NOT IN ({placeholders}))
              AND d."type" IN ('input', 'pseudo_input') AND d."masterid" IS NULL
            GROUP BY 1, 2
        ) per_task
        GROUP BY 1
    """
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, list(TASK_TERMINAL_STATUSES))
        return {str(site): int(n or 0) for site, n in cursor.fetchall()}


def _calibration():
    """Per queue over the last CALIBRATION_DAYS days: the finished
    production jobs' median and p90 walltime in hours, their p90 start
    latency (creation to start) in hours and their count, and the peak
    number of concurrently running jobs of any type (a sweep over start
    and end times, the currently running jobs included)."""
    days = f"{CALIBRATION_DAYS} days"
    walltime_sql = f"""
        SELECT "computingsite", COUNT(*),
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM ("endtime" - "starttime"))),
               PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM ("endtime" - "starttime"))),
               PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM ("starttime" - "creationtime")))
        FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "jobstatus" = 'finished' AND "processingtype" = 'epicproduction'
          AND "starttime" IS NOT NULL AND "endtime" > NOW() - INTERVAL %s
        GROUP BY 1
    """
    peak_sql = f"""
        WITH ev AS (
            SELECT "computingsite" AS s, "starttime" AS t, 1 AS d
            FROM "{PANDA_SCHEMA}"."jobsarchived4"
            WHERE "starttime" IS NOT NULL AND "endtime" > NOW() - INTERVAL %s
            UNION ALL
            SELECT "computingsite", "endtime", -1
            FROM "{PANDA_SCHEMA}"."jobsarchived4"
            WHERE "starttime" IS NOT NULL AND "endtime" > NOW() - INTERVAL %s
            UNION ALL
            SELECT "computingsite", "starttime", 1
            FROM "{PANDA_SCHEMA}"."jobsactive4"
            WHERE "jobstatus" = 'running' AND "starttime" IS NOT NULL
        )
        SELECT s, MAX(run) FROM (
            SELECT s, SUM(d) OVER (PARTITION BY s ORDER BY t, d ROWS UNBOUNDED PRECEDING) AS run
            FROM ev) x
        GROUP BY s
    """
    out = {}
    with connections['panda'].cursor() as cursor:
        cursor.execute(walltime_sql, [days])
        for site, n, med, p90, start_p90 in cursor.fetchall():
            out.setdefault(str(site), {}).update({
                'finished_jobs': int(n or 0),
                'median_walltime_h': round(float(med) / 3600.0, 3) if med is not None else None,
                'p90_walltime_h': round(float(p90) / 3600.0, 3) if p90 is not None else None,
                'p90_start_latency_h': (round(float(start_p90) / 3600.0, 2)
                                        if start_p90 is not None else None),
            })
        cursor.execute(peak_sql, [days, days])
        for site, peak in cursor.fetchall():
            out.setdefault(str(site), {})['peak_running'] = int(peak or 0)
    return {'built_at': timezone.now().isoformat(), 'days': CALIBRATION_DAYS,
            'queues': out}


def calibration(refresh=False):
    """The cached calibration, rebuilt when absent, expired, or asked."""
    if not refresh:
        cached = cache.get(CALIBRATION_CACHE_KEY)
        if cached:
            return cached
    built = _calibration()
    cache.set(CALIBRATION_CACHE_KEY, built, CALIBRATION_TTL_S)
    return built


def _gate_window(calib_queues):
    """Per queue over the last GATE_HOURS hours: finished and failed
    production jobs, and the failures that died fast."""
    rows = []
    with connections['panda'].cursor() as cursor:
        cursor.execute(f"""
            SELECT "computingsite", "jobstatus",
                   EXTRACT(EPOCH FROM ("endtime" - "starttime"))
            FROM "{PANDA_SCHEMA}"."jobsarchived4"
            WHERE "processingtype" = 'epicproduction'
              AND "endtime" > NOW() - INTERVAL %s
              AND "jobstatus" IN ('finished', 'failed')
        """, [f"{GATE_HOURS} hours"])
        rows = cursor.fetchall()
    out = {}
    for site, status, seconds in rows:
        site = str(site)
        g = out.setdefault(site, {'finished': 0, 'failed': 0, 'fast_failed': 0})
        if status == 'finished':
            g['finished'] += 1
            continue
        g['failed'] += 1
        med_h = (calib_queues.get(site) or {}).get('median_walltime_h')
        threshold = med_h * 3600.0 * FAST_FAILURE_FRACTION if med_h else FAST_FAILURE_FLOOR_S
        if seconds is not None and float(seconds) < threshold:
            g['fast_failed'] += 1
    return out


def queue_census(refresh=False):
    """The census record: per queue the live pool by status, the
    calibration, the gate window, and the derived depth."""
    observed_at = timezone.now()
    live = _live_counts()
    tasks = _tasks_active()
    ungenerated = _ungenerated()
    calib = calibration(refresh=refresh)
    calib_queues = calib.get('queues') or {}
    gate = _gate_window(calib_queues)
    names = set(live) | set(calib_queues) | set(gate) | set(tasks)
    queues = {}
    for name in sorted(names):
        by_status = live.get(name, {})
        not_started = sum(v['jobs'] for s, v in by_status.items() if s in NOT_STARTED)
        not_started_prod = sum(v['production'] for s, v in by_status.items() if s in NOT_STARTED)
        running = sum(v['jobs'] for s, v in by_status.items() if s in RUNNING)
        finishing = sum(v['jobs'] for s, v in by_status.items() if s in FINISHING)
        c = calib_queues.get(name) or {}
        g = gate.get(name) or {'finished': 0, 'failed': 0, 'fast_failed': 0}
        peak = int(c.get('peak_running') or 0)
        ceiling = max(peak, running)
        med_h = c.get('median_walltime_h')
        hours = (round(not_started * med_h / ceiling, 2)
                 if med_h and ceiling > 0 else None)
        queues[name] = {
            'by_status': by_status,
            'not_started': not_started,
            'not_started_production': not_started_prod,
            'running': running,
            'finishing': finishing,
            'tasks_active': int(tasks.get(name) or 0),
            'ungenerated': int(ungenerated.get(name) or 0),
            'calibration': {
                'median_walltime_h': med_h,
                'p90_walltime_h': c.get('p90_walltime_h'),
                'p90_start_latency_h': c.get('p90_start_latency_h'),
                'finished_jobs': c.get('finished_jobs', 0),
                'peak_running': peak,
            },
            'ceiling': ceiling,
            'hours_at_capacity': hours,
            'gate': {
                'hours': GATE_HOURS,
                'finished': g['finished'],
                'failed': g['failed'],
                'fast_failed': g['fast_failed'],
                'useful_rate_per_h': round(g['finished'] / GATE_HOURS, 2),
            },
        }
    return {
        'observed_at': observed_at.isoformat(),
        'calibration_built_at': calib.get('built_at'),
        'calibration_days': calib.get('days', CALIBRATION_DAYS),
        'queues': queues,
    }
