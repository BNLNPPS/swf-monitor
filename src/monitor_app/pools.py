"""Batch pools behind the PanDA queues: how full, and how deep the queue.

A PanDA queue's workers wait in somebody's batch pool, and the wait is
decided there rather than in PanDA: what share of the pool is claimed,
and how many jobs are already idle ahead of ours. Two pools are visible
to us and are read by reporters, never in a request path:

- ``bnl-scdf``, the SCDF shared pool, read from the monitor host by
  ``scripts/pool-reporter.py`` (docs/POOL_REPORTER.md);
- ``osg``, the pool the OSG submit host sees, carried in the osgsub01
  reporter's record (docs/OSG_SUBMIT_REPORTER.md).

Which pool a queue draws on is not declared anywhere: it is derived from
the queue's own finished jobs. Every job record names the worker node it
ran on, and a node's domain says whose pool it was in, so the mapping is
evidence rather than a table someone maintains.
"""
import logging
from collections import Counter

from .cached_product import get_product
from .host_reports import latest

logger = logging.getLogger(__name__)

POOLS = {
    'bnl-scdf': {'label': 'SCDF shared pool',
                 'source': 'report',
                 'report_key': 'bnl-scdf'},
    'osg': {'label': 'OSG pool',
            'source': 'osgsub01',
            'report_key': 'osgsub01'},
}

# How long a queue's pool attribution is kept before the jobs are read
# again. Where a queue's workers run changes when a site is added or a
# queue is repointed, which is a matter of days, not minutes.
ATTRIBUTION_TTL_S = 6 * 3600
ATTRIBUTION_KEY = 'queue_pool_attribution:v1'


def domain_of(machine):
    """The pool-identifying part of a worker node name.

    Job records carry ``slot1_14@spool1499.sdcc.bnl.gov``; the pool
    reporter carries the machine names its collector advertises. The
    domain is what the two have in common. A bare name contributes its
    leading letters, which groups the OSG pool's ``compute34`` names.
    """
    name = (machine or '').split('@')[-1].strip().lower()
    if not name:
        return ''
    if '.' in name:
        return name.split('.', 1)[1]
    return name.rstrip('0123456789') or name


def _pool_domains():
    """The machine domains each readable pool advertises."""
    out = {}
    for pool, spec in POOLS.items():
        record = (latest(spec['report_key']) or {}).get('record') or {}
        if pool == 'osg':
            by_site = ((record.get('pool') or {}).get('slots_by_site')) or {}
            # The OSG pool reports slots by site rather than by machine;
            # its own reading of the pool is what identifies it, and the
            # site names are not node domains, so the OSG pool is matched
            # by the queues the submit host itself says it submits.
            out[pool] = set()
            out[pool + ':queues'] = set((record.get('queues') or {}).keys())
            del by_site
            continue
        domains = ((record.get('reading') or {}).get('machine_domains')) or {}
        out[pool] = set(domains)
    return out


def _attribution():
    """Queue to pool, from the worker nodes the queue's jobs ran on."""
    from django.db import connections
    from .panda.constants import PANDA_SCHEMA

    domains = _pool_domains()
    osg_queues = domains.get('osg:queues') or set()
    # Only jobs that actually ran name a worker node: a job that never
    # started carries the harvester host instead, and those rows would
    # put every queue in the pool the harvester happens to sit in.
    sql = """
        SELECT "computingsite", "modificationhost", count(*)
        FROM "{}"."jobsarchived4"
        WHERE "modificationtime" > now() - interval '2 days'
          AND "modificationhost" IS NOT NULL
          AND "starttime" IS NOT NULL
        GROUP BY 1, 2
    """.format(PANDA_SCHEMA)
    seen = {}
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql)
            for site, host, count in cursor.fetchall():
                seen.setdefault(site, Counter())[domain_of(host)] += count
    except Exception as e:                                    # noqa: BLE001
        logger.error("queue pool attribution query failed: %s", e)
        return {'queues': {}, 'error': str(e)}

    out = {}
    for site, counts in seen.items():
        if site in osg_queues:
            out[site] = {'pool': 'osg', 'evidence': 'the submit host submits it'}
            continue
        for pool, pool_domains in domains.items():
            if pool.endswith(':queues') or not pool_domains:
                continue
            hit = sum(n for d, n in counts.items() if d in pool_domains)
            # A strong majority, so a stray node of another facility does
            # not carry a queue into a pool it does not run in.
            if hit and hit >= 0.8 * sum(counts.values()):
                top = counts.most_common(1)[0][0]
                out[site] = {'pool': pool,
                             'evidence': 'its workers ran on {}'.format(top)}
                break
    return {'queues': out}


def queue_pools():
    """The queue-to-pool map, from the cached product."""
    product = get_product(ATTRIBUTION_KEY, _attribution,
                          ttl_seconds=ATTRIBUTION_TTL_S,
                          async_first_fill=True)
    return ((product or {}).get('value') or {}).get('queues') or {}


def pool_reading(pool):
    """One pool's latest reading with its age, or None.

    Returns ``{pool, label, slots, claimed_fraction, queue, age_seconds,
    error}``. A pool whose reporter has never run returns None; a pool
    whose reading carried an error keeps the error, because a stated
    failure and an absent reading are different facts.
    """
    spec = POOLS.get(pool)
    if not spec:
        return None
    report = latest(spec['report_key'])
    if not report:
        return None
    record = report.get('record') or {}
    out = {'pool': pool, 'label': spec['label'],
           'age_seconds': report.get('age_seconds'),
           'reported_at': report.get('reported_at')}
    if pool == 'osg':
        block = record.get('pool') or {}
        schedd = (record.get('schedd') or {}).get('workers') or {}
        total = block.get('slots_total')
        # The OSG submit host counts the slots its queue's requirements
        # admit, not the pool's claimed share: the pool is not ours and
        # the collector does not answer state to us. What it does know
        # is its own workers, which is the queue ahead that we made.
        out['slots'] = {'total': total, 'admitted': block.get('slots_admitted')}
        out['queue'] = {'idle': schedd.get('idle'), 'running': schedd.get('running'),
                        'held': schedd.get('held'), 'scope': 'our workers'}
        if block.get('error'):
            out['error'] = block['error']
        return out
    reading = record.get('reading') or {}
    if reading.get('error'):
        out['error'] = reading['error']
        return out
    out['slots'] = reading.get('slots') or {}
    out['cores'] = reading.get('cores') or {}
    out['claimed_fraction'] = reading.get('claimed_fraction')
    queue = dict(reading.get('queue') or {})
    queue['scope'] = 'the whole pool'
    out['queue'] = queue
    out['collector'] = reading.get('collector')
    return out


def readings_by_queue(queue_names):
    """Pool readings keyed by queue name, for the queues asked about."""
    mapping = queue_pools()
    cache = {}
    out = {}
    for name in queue_names:
        entry = mapping.get(name)
        if not entry:
            continue
        pool = entry.get('pool')
        if pool not in cache:
            cache[pool] = pool_reading(pool)
        reading = cache[pool]
        if reading:
            out[name] = dict(reading, evidence=entry.get('evidence'))
    return out
