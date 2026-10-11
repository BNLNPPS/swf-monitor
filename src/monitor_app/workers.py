"""The queue's own workers in the batch pool: how many wait, how long.

The pool reading (``pools.py``) says how full somebody's batch pool is;
this reading says what that costs us at one queue: the harvester's
workers submitted there and not yet started, the oldest of them, and
how long the ones that did start waited. It is read from the
harvester's worker table in the PanDA database, which every queue has
whether or not its pool is one we can see, so it is the half of the
wait question that never goes dark.
"""
import logging
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.db import connections
from django.utils import timezone

from .cached_product import get_product
from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

# Workers change by the minute; the page serves the last reading and
# rebuilds behind it.
TTL_S = 120
WINDOW_H = 24


def _reading(queue_name):
    """Build the reading for one queue."""
    window = timezone.now() - timedelta(hours=WINDOW_H)
    out = {'queue': queue_name, 'window_hours': WINDOW_H}
    sql_now = f"""
        SELECT "status", count(*), min("submittime"), max("ncore")
        FROM "{PANDA_SCHEMA}"."harvester_workers"
        WHERE "computingsite" = %s
          AND "status" IN ('submitted', 'running')
        GROUP BY "status"
    """
    # Workers that started in the window, with what they waited: the
    # interval from the harvester's submission to the batch start.
    sql_started = f"""
        SELECT count(*),
               percentile_cont(0.5) WITHIN GROUP
                   (ORDER BY extract(epoch FROM ("starttime" - "submittime"))),
               percentile_cont(0.9) WITHIN GROUP
                   (ORDER BY extract(epoch FROM ("starttime" - "submittime"))),
               max("starttime"),
               sum(CASE WHEN "starttime" > now() - interval '1 hour' THEN 1 ELSE 0 END)
        FROM "{PANDA_SCHEMA}"."harvester_workers"
        WHERE "computingsite" = %s
          AND "starttime" IS NOT NULL
          AND "starttime" > %s
    """
    sql_ended = f"""
        SELECT "status", count(*)
        FROM "{PANDA_SCHEMA}"."harvester_workers"
        WHERE "computingsite" = %s
          AND "endtime" IS NOT NULL
          AND "endtime" > %s
        GROUP BY "status"
    """
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql_now, [queue_name])
        now_rows = {status: (count, oldest, ncore)
                    for status, count, oldest, ncore in cursor.fetchall()}
        cursor.execute(sql_started, [queue_name, window])
        started, wait_p50, wait_p90, last_start, started_last_hour = cursor.fetchone()
        cursor.execute(sql_ended, [queue_name, window])
        ended = {status: count for status, count in cursor.fetchall()}

    pending, oldest, ncore = now_rows.get('submitted', (0, None, None))
    running, _, ncore_running = now_rows.get('running', (0, None, None))
    out['pending'] = pending
    out['running'] = running
    out['cores_per_worker'] = ncore or ncore_running
    # PanDA DB timestamps are naive UTC.
    oldest = oldest.replace(tzinfo=dt_timezone.utc) if oldest else None
    last_start = last_start.replace(tzinfo=dt_timezone.utc) if last_start else None
    out['oldest_pending_at'] = oldest.isoformat() if oldest else None
    out['oldest_pending_s'] = (
        (timezone.now() - oldest).total_seconds() if oldest else None)
    out['started'] = started or 0
    out['started_last_hour'] = started_last_hour or 0
    out['wait_p50_s'] = float(wait_p50) if wait_p50 is not None else None
    out['wait_p90_s'] = float(wait_p90) if wait_p90 is not None else None
    out['last_start_at'] = last_start.isoformat() if last_start else None
    out['ended'] = ended
    return out


def worker_reading(queue_name, refresh=False):
    """One queue's worker reading with its build time, or None on failure.

    Returns the reading with ``built_at`` and ``age_seconds`` from the
    product store; an absent harvester record reads as zero workers,
    which is a fact about the queue, not a failure.
    """
    key = 'harvester_workers:v1:{}'.format(queue_name)
    try:
        product = get_product(key, lambda: _reading(queue_name),
                              ttl_seconds=TTL_S, refresh=refresh)
    except Exception as e:                                    # noqa: BLE001
        logger.error('worker reading failed for %s: %s', queue_name, e)
        return {'queue': queue_name, 'error': str(e)}
    value = dict((product or {}).get('value') or {})
    value['built_at'] = (product or {}).get('built_at')
    value['age_seconds'] = (product or {}).get('age_seconds')
    return value


# A worker in one of these states has ended; any other state is a worker
# that exists now (waiting to start, or running).
FINAL_STATES = ('finished', 'failed', 'cancelled', 'missed')
# A worker that has not ended is still updated by its harvester; rows not
# updated for this long are left out of the present count as abandoned.
STALE_DAYS = 7
MAX_HOURS = 168


def worker_counts(site=None, hours=1):
    """Harvester workers (pilots) across the ePIC queues, read directly
    from PanDA's ``harvester_workers`` table.

    Two readings are kept apart, because confusing them has misled before
    (BigPanDA's tally of workers that ended was read as jobs, 25-fold, on
    10/6 and 10/7):

    - ``now``: the workers that exist at this moment, by state (a
      ``submitted`` worker waits in the batch system, a ``running`` one
      runs), by queue and resource type, with their cores;
    - ``window``: what happened in the last ``hours``: workers submitted,
      started, and ended, the ended ones by final state. A ``finished``
      worker is a batch job that ended normally; it says nothing about
      whether the PanDA jobs bound to it succeeded.

    ``site`` is a queue name (computingsite), a SQL LIKE pattern when it
    contains ``%``. Raises on a failed read.
    """
    hours = max(1, min(int(hours or 1), MAX_HOURS))
    now = timezone.now()
    cutoff = now - timedelta(hours=hours)
    stale = now - timedelta(days=STALE_DAYS)
    site_sql, site_args = '', []
    if site:
        site_sql = (' AND "computingsite" LIKE %s' if '%' in site
                    else ' AND "computingsite" = %s')
        site_args = [site]
    final = ', '.join(f"'{s}'" for s in FINAL_STATES)
    table = f'"{PANDA_SCHEMA}"."harvester_workers"'
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT "computingsite", "status", "resourcetype", count(*), '
            f'coalesce(sum("ncore"), 0) FROM {table} '
            f'WHERE "status" NOT IN ({final}) AND "lastupdate" > %s{site_sql} '
            f'GROUP BY 1, 2, 3', [stale] + site_args)
        present = cursor.fetchall()
        cursor.execute(
            f'SELECT "computingsite", count(*) FROM {table} '
            f'WHERE "submittime" > %s{site_sql} GROUP BY 1', [cutoff] + site_args)
        submitted = dict(cursor.fetchall())
        cursor.execute(
            f'SELECT "computingsite", count(*) FROM {table} '
            f'WHERE "starttime" > %s{site_sql} GROUP BY 1', [cutoff] + site_args)
        started = dict(cursor.fetchall())
        cursor.execute(
            f'SELECT "computingsite", "status", "resourcetype", count(*), '
            f'coalesce(sum("njobs"), 0) FROM {table} '
            f'WHERE "endtime" > %s AND "status" IN ({final}){site_sql} '
            f'GROUP BY 1, 2, 3', [cutoff] + site_args)
        ended = cursor.fetchall()

    def tally(rows):
        by_status, by_site, by_resourcetype = {}, {}, {}
        for computingsite, status, resourcetype, count, _ in rows:
            by_status[status] = by_status.get(status, 0) + count
            per_site = by_site.setdefault(computingsite, {})
            per_site[status] = per_site.get(status, 0) + count
            rt = resourcetype or 'unset'
            by_resourcetype[rt] = by_resourcetype.get(rt, 0) + count
        return by_status, by_site, by_resourcetype

    now_status, now_site, now_rt = tally(present)
    cores = {}
    for _, status, _, _, ncore in present:
        cores[status] = cores.get(status, 0) + int(ncore)
    ended_status, ended_site, ended_rt = tally(ended)
    return {
        'now': {
            'workers': sum(now_status.values()),
            'by_status': now_status,
            'cores_by_status': cores,
            'by_site': now_site,
            'by_resourcetype': now_rt,
        },
        'window': {
            'hours': hours,
            'from': cutoff.isoformat(),
            'to': now.isoformat(),
            'submitted': sum(submitted.values()),
            'started': sum(started.values()),
            'ended': sum(ended_status.values()),
            'ended_by_status': ended_status,
            'jobs_bound_to_ended': sum(int(r[4]) for r in ended),
            'by_site': {
                s: {'submitted': submitted.get(s, 0), 'started': started.get(s, 0),
                    'ended': ended_site.get(s, {})}
                for s in sorted(set(submitted) | set(started) | set(ended_site))},
            'ended_by_resourcetype': ended_rt,
        },
    }


# An allocation's occupancy timeline is kept as it is first built: the
# harvester's worker record ages out of PanDA after three months, and the
# plot of a slot must not lose its batch start and end with it.
ALLOCATION_LIVE_TTL_S = 120
ALLOCATION_KEPT_TTL_S = 10 * 365 * 86400


def _jsonable_timeline(tl):
    """The timeline with the worker's timestamps as ISO strings (UTC)."""
    worker = dict(tl.get('worker') or {})
    for key, value in list(worker.items()):
        if hasattr(value, 'isoformat'):
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt_timezone.utc)
            worker[key] = value.isoformat()
    return dict(tl, worker=worker or None)


def allocation_product(harvesterid, workerid, refresh=False):
    """One allocation's occupancy timeline (panda.queries.allocation_timeline)
    from the product store, ``allocation:v1:<harvester>:<worker>``.

    A live allocation rebuilds every two minutes; an ended one is kept and
    served without rebuilding. A rebuild never replaces a record that holds
    the worker's batch start and end with one that has lost them. Raises
    when the allocation cannot be read or ran no job."""
    from .models import CachedProduct
    from .panda.queries import allocation_timeline

    key = 'allocation:v1:{}:{}'.format(harvesterid, int(workerid))
    row = CachedProduct.objects.filter(key=key).first()
    stored = row.value if row is not None and row.built_at is not None else None

    def build():
        tl = allocation_timeline(harvesterid, workerid)
        if tl is None:
            raise ValueError(f'no PanDA job is linked to worker {workerid} of harvester {harvesterid}')
        if tl.get('error'):
            raise RuntimeError(tl['error'])
        if (tl.get('bounds_from_jobs') and isinstance(stored, dict)
                and not stored.get('bounds_from_jobs')):
            return stored
        return _jsonable_timeline(tl)

    kept = isinstance(stored, dict) and not stored.get('live')
    ttl = ALLOCATION_KEPT_TTL_S if kept else ALLOCATION_LIVE_TTL_S
    product = get_product(key, build, ttl_seconds=ttl, refresh=refresh)
    return product

