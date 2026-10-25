"""Payload parallelism of running production jobs, separate from allocation."""
import re
from collections import defaultdict
from datetime import datetime, timezone

from django.db import connections, transaction

from .cached_product import get_product
from .models import EpicProdJob
from .panda.queries import PANDA_SCHEMA

KEY = 'processing_activity:v1'
TTL = 60
ES_STALE_SECONDS = 600


def payload_threads(corecount, actualcorecount, eventservice, metrics, record, now):
    """ES busy work lanes, or the native payload's selected thread width.

    Each ES work lane runs one single-threaded payload. Native multicore
    allocations need the chosen payloadThreads report; allocation alone
    cannot establish that the payload uses those cores.
    """
    if eventservice:
        if not isinstance(record, dict) or not record.get('written_at'):
            return None
        if record.get('ended_at'):
            return 0
        age = now - float(record['written_at'])
        if not 0 <= age <= ES_STALE_SECONDS or 'in_flight' not in record:
            return None
        return len({unit['slot'] for unit in record['in_flight'] if 'slot' in unit})
    chosen = re.search(r'(?:^|\s)payloadThreads=(\d+)(?:\s|$)', metrics or '')
    if chosen:
        return int(chosen[1])
    # A native one-core job launches one processing thread. Do not turn an
    # unmeasured multicore reservation into an equivalent thread count.
    if int(corecount or 1) == 1 and int(actualcorecount or 1) == 1:
        return 1
    return None


def build():
    now = datetime.now(timezone.utc)
    sql = f'''SELECT pandaid, computingsite, COALESCE(destinationsite,''),
                     corecount, actualcorecount, eventservice, jobmetrics
              FROM "{PANDA_SCHEMA}".jobsactive4
              WHERE jobstatus='running' AND workinggroup='EIC'
                AND (prodsourcelabel='managed' OR processingtype='epicproduction')
                AND COALESCE(processingtype,'') <> 'canary' '''
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '20s'")
            cursor.execute(sql)
            rows = cursor.fetchall()
    records = dict(EpicProdJob.objects.filter(pandaid__in=[r[0] for r in rows if r[5]])
                   .values_list('pandaid', 'data'))
    def empty():
        return {'known_threads': 0, 'jobs': 0, 'unknown_jobs': 0}
    queues, executors = defaultdict(empty), defaultdict(empty)
    for pid, queue, destination, cores, actual, es, metrics in rows:
        record = ((records.get(pid) or {}).get('es_record') or {}).get('record')
        threads = payload_threads(cores, actual, es, metrics, record, now.timestamp())
        for bucket in (queues[queue], executors[destination] if queue.startswith('BNL_OSG') else None):
            if bucket is None:
                continue
            bucket['jobs'] += 1
            bucket['unknown_jobs'] += threads is None
            bucket['known_threads'] += threads or 0
    for bucket in [*queues.values(), *executors.values()]:
        bucket['threads'] = None if bucket['unknown_jobs'] else bucket['known_threads']
    return {'queues': dict(queues), 'executors': dict(executors), 'observed_at': now.isoformat()}


def product(refresh=False):
    return get_product(KEY, build, TTL, refresh=refresh, async_first_fill=True)
