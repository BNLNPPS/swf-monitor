"""Harvester worker records: our copy of PanDA's, kept past its window.

PanDA keeps a harvester worker row three months after its last update
(``harvester_workers``, partitions dropped by date); the job-worker links
and the jobs themselves stay. A batch allocation's start, end and cores are
in the worker row alone, so the allocation page
(``panda.queries.allocation_timeline``) loses the allocation's clock when
the row goes. This module copies the rows of the queues whose allocations
the page draws into ``HarvesterWorkerRecord``, nightly by the prod-ops
agent (``worker_record_capture``), and the timeline reads the copy once
PanDA no longer has the row (swf-epicprod docs/EPICPROD_OPS.md, Harvester
worker records).

Which queues: those whose site name starts with ``SITE_PREFIXES``, the
NERSC Perlmutter queues, whose allocations run many jobs each; about a
hundred rows a day. A row is copied whole but its JDL, and copied again
whenever PanDA's ``lastupdate`` is newer than ours, so a worker captured
while it ran ends with its final state.
"""
import logging
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.db import connections
from django.utils import timezone

from .panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

SITE_PREFIXES = ('NERSC',)
# The first pass reaches the oldest row PanDA still holds.
DEFAULT_DAYS = 100
TYPED = ('computingsite', 'status', 'batchid', 'ncore',
         'submittime', 'starttime', 'endtime', 'lastupdate')


def _aware(t):
    if t is None:
        return None
    return t.replace(tzinfo=dt_timezone.utc) if t.tzinfo is None else t


def _jsonable(v):
    if hasattr(v, 'isoformat'):
        return _aware(v).isoformat()
    if isinstance(v, (bytes, memoryview)):
        return None
    return v


def capture(days=DEFAULT_DAYS, prefixes=SITE_PREFIXES):
    """Copy the worker rows updated in the last ``days`` at the sites.
    Returns counts: read, created, updated, unchanged."""
    from .models import HarvesterWorkerRecord

    since = timezone.now() - timedelta(days=days)
    like = ' OR '.join('"computingsite" LIKE %s' for _ in prefixes)
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT * FROM "{PANDA_SCHEMA}"."harvester_workers" '
            f'WHERE "lastupdate" > %s AND ({like})',
            [since.replace(tzinfo=None)] + [p + '%' for p in prefixes])
        cols = [c[0].lower() for c in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    have = {(h, w): lu for h, w, lu in HarvesterWorkerRecord.objects
            .filter(computingsite__regex=r'^(' + '|'.join(prefixes) + ')')
            .values_list('harvesterid', 'workerid', 'lastupdate')}
    counts = {'read': len(rows), 'created': 0, 'updated': 0, 'unchanged': 0}
    for row in rows:
        key = (row['harvesterid'], int(row['workerid']))
        lastupdate = _aware(row.get('lastupdate'))
        if key in have and have[key] is not None and lastupdate is not None \
                and have[key] >= lastupdate:
            counts['unchanged'] += 1
            continue
        fields = {k: (_aware(row.get(k)) if k.endswith('time') or k == 'lastupdate'
                      else row.get(k)) for k in TYPED}
        fields['status'] = fields['status'] or ''
        fields['batchid'] = str(fields['batchid'] or '')
        fields['record'] = {k: _jsonable(v) for k, v in row.items() if k != 'jdl'}
        _, created = HarvesterWorkerRecord.objects.update_or_create(
            harvesterid=key[0], workerid=key[1], defaults=fields)
        counts['created' if created else 'updated'] += 1
    return counts


def worker_row(harvesterid, workerid):
    """Our copy of one worker row in the shape PanDA's has (naive UTC
    datetimes), or None."""
    from .models import HarvesterWorkerRecord
    rec = HarvesterWorkerRecord.objects.filter(
        harvesterid=harvesterid, workerid=int(workerid)).first()
    if rec is None:
        return None
    out = dict(rec.record or {})
    for k in ('submittime', 'starttime', 'endtime', 'lastupdate'):
        t = getattr(rec, k)
        out[k] = t.astimezone(dt_timezone.utc).replace(tzinfo=None) if t else None
    out['ncore'] = rec.ncore
    out['from_copy'] = True
    return out
