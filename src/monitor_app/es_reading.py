"""The Event Service reading of one queue: what the node harness reported
across the queue's Event Service jobs over a window, from the job
reports (swf-epicprod docs/NODE_EVENT_DISPATCHER.md, The record), as a
cached product on the uniform mechanism (docs/CACHED_PRODUCTS.md): the
queue detail page serves the stored reading and rebuilds behind the
response.

The reading: jobs with a harness report and their slots, units done and
failed with their events, the unit wall's middle and tail and the
seconds an event costs on this queue, the closes with their events and
wall, the jobs that stopped taking ranges at the deadline, and the
events done per hour of job wall on the queue's Event Service jobs.
"""
import json
import logging

from django.db import connections

from .cached_product import get_product
from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

TTL_S = 300
WINDOW_DAYS = 7
JOB_CAP = 3000


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 1)


def _reading(queue_name, days=WINDOW_DAYS):
    """The queue's Event Service reading over ``days``; pure database."""
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT j."pandaid", j."jobstatus", j."jobsubstatus", '
            f'EXTRACT(EPOCH FROM (j."endtime" - j."starttime")), m."metadata" '
            f'FROM "{PANDA_SCHEMA}"."jobsarchived4" j '
            f'LEFT JOIN "{PANDA_SCHEMA}"."metatable" m ON m."pandaid" = j."pandaid" '
            f'WHERE j."computingsite" = %s AND j."eventservice" IN (1, 6) '
            f'AND j."modificationtime" > NOW() - INTERVAL %s '
            f'ORDER BY j."pandaid" DESC LIMIT %s',
            [queue_name, f'{int(days)} days', JOB_CAP])
        rows = cursor.fetchall()
    out = {'queue': queue_name, 'window_days': int(days), 'es_jobs': len(rows),
           'capped': len(rows) >= JOB_CAP, 'by_status': {}, 'by_substatus': {}}
    reports = 0
    slots = []
    unit_walls, unit_events, spe = [], [], []
    done = failed = events_done = 0
    closes = close_failed = close_events = 0
    close_walls = []
    deadline_marks = 0
    job_wall_s = 0.0
    for pandaid, status, substatus, wall, raw in rows:
        out['by_status'][status] = out['by_status'].get(status, 0) + 1
        if substatus:
            out['by_substatus'][substatus] = out['by_substatus'].get(substatus, 0) + 1
        if wall is not None:
            job_wall_s += float(wall)
        if not raw:
            continue
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        es = metadata.get('es') if isinstance(metadata, dict) else None
        if not isinstance(es, dict):
            continue
        reports += 1
        if es.get('slots'):
            slots.append(int(es['slots']))
        if es.get('untaken_at_deadline'):
            deadline_marks += 1
        for u in es.get('ranges_done') or []:
            if not isinstance(u, dict):
                continue
            done += 1
            ev = int(u.get('events_reconstructed') or u.get('events') or 0)
            events_done += ev
            w = u.get('wall_s')
            if w is not None:
                unit_walls.append(float(w))
                unit_events.append(ev)
                if ev:
                    spe.append(float(w) / ev)
        failed += len(es.get('ranges_failed') or [])
        for c in es.get('closes') or []:
            if not isinstance(c, dict):
                continue
            closes += 1
            if c.get('ok'):
                close_events += int(c.get('events') or 0)
            else:
                close_failed += 1
            if c.get('wall_s') is not None:
                close_walls.append(float(c['wall_s']))
    walls = sorted(unit_walls)
    out.update({
        'reports': reports,
        'slots_min': min(slots) if slots else None, 'slots_max': max(slots) if slots else None,
        'units_done': done, 'units_failed': failed, 'events_done': events_done,
        'unit_events_median': _median(unit_events),
        'unit_wall_median_s': _median(unit_walls),
        'unit_wall_p90_s': round(walls[min(len(walls) - 1, int(0.9 * len(walls)))], 1) if walls else None,
        's_per_event_median': round(_median(spe), 2) if spe else None,
        'closes': closes, 'closes_failed': close_failed, 'close_events': close_events,
        'close_wall_median_s': _median(close_walls),
        'deadline_marks': deadline_marks,
        'job_wall_h': round(job_wall_s / 3600.0, 2),
        'events_per_job_hour': round(events_done / (job_wall_s / 3600.0), 1) if job_wall_s else None,
    })
    return out


def es_reading(queue_name, refresh=False):
    """One queue's Event Service reading with its build time; None when
    the queue ran no Event Service job in the window; an error record
    when the read fails."""
    key = 'es_reading:v1:{}'.format(queue_name)
    try:
        product = get_product(key, lambda: _reading(queue_name),
                              ttl_seconds=TTL_S, refresh=refresh)
    except Exception as e:                                    # noqa: BLE001
        logger.error('event service reading failed for %s: %s', queue_name, e)
        return {'queue': queue_name, 'error': str(e)}
    value = dict((product or {}).get('value') or {})
    if not value.get('es_jobs'):
        return None
    value['built_at'] = (product or {}).get('built_at')
    value['age_seconds'] = (product or {}).get('age_seconds')
    return value
