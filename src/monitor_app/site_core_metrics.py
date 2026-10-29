"""Core completions and queued core capacity for the production dashboard."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from django.db import connections, transaction

from .cached_product import get_product
from .models import SysConfig
from .panda.census import NOT_STARTED
from .panda.queries import PANDA_SCHEMA

KEY = 'site_core_metrics:v1'
TTL = 60
PRODUCTION = """workinggroup='EIC'
    AND (prodsourcelabel='managed' OR processingtype='epicproduction')
    AND COALESCE(processingtype,'') <> 'canary'"""
CORES = """GREATEST(1, COALESCE(
    NULLIF(SUBSTRING(COALESCE(jobmetrics,'') FROM 'payloadThreads=([0-9]+)')::integer,0),
    NULLIF(actualcorecount,0), NULLIF(corecount,0), 1))"""


def ready_capacity(jobs, nodes, cores_per_node):
    return min(max(0, jobs), max(0, nodes)) * cores_per_node


def build():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=13)).replace(tzinfo=None)
    hour = (now - timedelta(hours=1)).replace(tzinfo=None)
    queues = defaultdict(lambda: {'finished_cores_1h': 0, 'failed_cores_1h': 0,
                                  'finished_jobs_1h': 0, 'failed_jobs_1h': 0,
                                  'queued_cores': 0, 'queued_jobs': 0})
    history = defaultdict(dict)
    fields = 'pandaid,computingsite,jobstatus,endtime,actualcorecount,corecount,jobmetrics,modificationtime'
    sql = f'''WITH jobs AS (
        SELECT {fields} FROM "{PANDA_SCHEMA}".jobsarchived4
        WHERE modificationtime >= %s AND endtime >= %s
          AND jobstatus IN ('finished','failed') AND {PRODUCTION}
        UNION ALL
        SELECT {fields} FROM "{PANDA_SCHEMA}".jobsactive4
        WHERE endtime >= %s AND jobstatus IN ('finished','failed') AND {PRODUCTION}
      ), unique_jobs AS (
        SELECT DISTINCT ON (pandaid) * FROM jobs ORDER BY pandaid,modificationtime DESC
      )
      SELECT computingsite,jobstatus,
        to_timestamp(CEIL(EXTRACT(EPOCH FROM endtime)/900)*900) AT TIME ZONE 'UTC',
        SUM({CORES}), COALESCE(SUM({CORES}) FILTER(WHERE endtime >= %s),0),
        COUNT(*) FILTER(WHERE endtime >= %s)
      FROM unique_jobs GROUP BY 1,2,3'''
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '25s'")
            cursor.execute(sql, [since, since, since, hour, hour])
            ended = cursor.fetchall()
            cursor.execute(f'''SELECT computingsite,COUNT(*),SUM({CORES})
                FROM "{PANDA_SCHEMA}".jobsactive4
                WHERE jobstatus=ANY(%s) AND {PRODUCTION} GROUP BY 1''', [list(NOT_STARTED)])
            pending = cursor.fetchall()
    for queue, state, edge, cores, recent, jobs in ended:
        queues[queue][f'{state}_cores_1h'] += int(recent)
        queues[queue][f'{state}_jobs_1h'] += int(jobs)
        stamp = edge.replace(tzinfo=timezone.utc).isoformat()
        bucket = history[stamp].setdefault(queue, {'finished': 0, 'failed': 0})
        bucket[state] += int(cores)
    for queue, jobs, cores in pending:
        queues[queue].update(queued_jobs=int(jobs), queued_cores=int(cores))
    cfg = SysConfig.get_config()
    google = {'queue': cfg.get('gke_pilots.queue') or 'BNL_ePIC_GOOGLE_es',
              'nodes': int(cfg.get('gke_pilots.max_pods') or 0),
              'cores_per_node': int(float(cfg.get('gke_pilots.cpu') or 14))}
    queued = queues[google['queue']]
    queued['queued_cores'] = ready_capacity(queued['queued_jobs'], google['nodes'], google['cores_per_node'])
    queued['queued_core_demand'] = queued['queued_jobs'] * google['cores_per_node']
    # Perlmutter's whole-node jobs declare 256 cores (two threads a core);
    # each runs 128 work lanes, the unit its running figure counts, so a
    # waiting job counts 128 (Torre Wenaus, 2026-10-29).
    from swf_epicprod import perlmutter_production
    perl = queues[perlmutter_production.QUEUE]
    perl['queued_cores'] = perl['queued_jobs'] * int(perlmutter_production.SHAPE['slots'])
    return {'queues': dict(queues), 'ended': dict(history), 'google': google,
            'since': since.replace(tzinfo=timezone.utc).isoformat(), 'observed_at': now.isoformat()}


def product(refresh=False):
    return get_product(KEY, build, TTL, refresh=refresh, async_first_fill=True)
