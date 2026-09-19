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
import json
import logging
from collections import Counter
from datetime import datetime

from django.utils import timezone

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
    # A pool no collector answers for: its reading is the sample a
    # worker took on the node (squeue and sinfo, before the container
    # started) and the payload carried in its report, the newest one
    # among the finished jobs of the pool's queues (swf-epicprod
    # docs/NERSC_PERLMUTTER.md, the pool sample). The queues are the
    # pool's by name; nothing else runs there.
    'nersc-perlmutter': {'label': 'NERSC Perlmutter, allocation m3763',
                         'source': 'sample',
                         'queue_prefix': 'NERSC_Perlmutter'},
}

# The newest sample a pool's jobs carried, looked for over this window
# and served as a cached product: the page never reads the PanDA record.
SAMPLE_WINDOW_DAYS = 3
SAMPLE_TTL_S = 300
SAMPLE_SCAN_LIMIT = 200

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


def _sample_pool(queue_name):
    """The sample-fed pool a queue belongs to by name, or None."""
    for pool, spec in POOLS.items():
        prefix = spec.get('queue_prefix')
        if prefix and (queue_name or '').startswith(prefix):
            return pool
    return None


def _newest_sample(pool):
    """The newest pool sample among the finished jobs of the pool's
    queues in the window: the sample, and the job that carried it. The
    payload writes the sample under ``payload.pool`` of the job report
    the pilot ships as job metadata; the text match keeps the scan to
    rows that carry one."""
    from django.db import connections
    from .panda.constants import PANDA_SCHEMA

    spec = POOLS[pool]
    sql = """
        SELECT j."pandaid", j."computingsite", j."modificationtime", m."metadata"
        FROM "{schema}"."jobsarchived4" j
        JOIN "{schema}"."metatable" m ON m."pandaid" = j."pandaid"
        WHERE j."computingsite" LIKE %s
          AND j."jobstatus" = 'finished'
          AND j."modificationtime" > now() - interval '{days} days'
          AND m."metadata" LIKE '%%pool-sample/%%'
        ORDER BY j."modificationtime" DESC
        LIMIT %s
    """.format(schema=PANDA_SCHEMA, days=int(SAMPLE_WINDOW_DAYS))
    best = None
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, [spec['queue_prefix'] + '%', SAMPLE_SCAN_LIMIT])
        for pandaid, queue, modtime, raw in cursor.fetchall():
            try:
                metadata = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                continue
            sample = ((metadata or {}).get('payload') or {}).get('pool') \
                if isinstance(metadata, dict) else None
            if not isinstance(sample, dict) or 'taken_at' not in sample:
                continue
            taken = sample.get('taken_at')
            if best is None or str(taken) > str(best['taken_at']):
                best = {'taken_at': taken, 'sample': sample,
                        'pandaid': int(pandaid), 'queue': queue,
                        'job_modified': modtime.isoformat() if modtime else None}
    if best is None:
        # No job of the pool's queues has carried a sample in the window:
        # a fact the page states. The read succeeded and found none, which
        # is not the empty-in-place-of-missing case the contract forbids.
        return {'sample': None, 'searched_days': SAMPLE_WINDOW_DAYS,
                'searched_queues': spec['queue_prefix'] + '*'}
    return best


def _sample_product(pool):
    """The pool's newest sample as a cached product."""
    return get_product('pool_sample:v1:{}'.format(pool),
                       lambda: _newest_sample(pool),
                       ttl_seconds=SAMPLE_TTL_S, async_first_fill=True)


def _sample_reading(pool, spec):
    """The reading of a sample-fed pool in the shape the pages render."""
    product = _sample_product(pool)
    value = (product or {}).get('value') or {}
    sample = value.get('sample')
    if not sample:
        if value.get('searched_days'):
            reason = ('no finished job of the {} queues has carried a pool sample '
                      'in the last {} days').format(value['searched_queues'],
                                                    value['searched_days'])
        else:
            reason = 'the sample record is building; reload shortly'
        return {'pool': pool, 'label': spec['label'], 'error': reason,
                'absent': True, 'reported_at': None, 'age_seconds': None,
                'slots_unit': 'nodes'}
    taken_at = None
    try:
        taken_at = datetime.fromisoformat(str(value['taken_at']).replace('Z', '+00:00'))
    except (ValueError, TypeError, KeyError):
        pass
    out = {'pool': pool, 'label': spec['label'],
           'reported_at': taken_at,
           'age_seconds': ((timezone.now() - taken_at).total_seconds()
                           if taken_at else None),
           'sampled_by': {'pandaid': value.get('pandaid'),
                          'queue': value.get('queue'),
                          'host': sample.get('host')},
           'slots_unit': 'nodes'}
    errors = sample.get('errors') or []
    if sample.get('error'):
        errors = [sample['error']] + list(errors)
    nodes = sample.get('partition_nodes') or {}
    total = nodes.get('total')
    allocated = (nodes.get('allocated') or 0) + (nodes.get('mixed') or 0)
    if total:
        out['slots'] = {'total': total, 'claimed': allocated,
                        'unclaimed': nodes.get('idle') or 0}
        out['claimed_fraction'] = allocated / total
    machine = sample.get('machine') or {}
    if machine:
        out['queue'] = {'idle': machine.get('pending_jobs'),
                        'running': machine.get('running_jobs'),
                        'idle_nodes': machine.get('pending_nodes'),
                        'running_nodes': machine.get('running_nodes'),
                        'scope': 'the whole machine, partition {}'.format(
                            sample.get('partition') or '?')}
    ours = sample.get('ours') or {}
    if ours:
        out['ours'] = {'idle': ours.get('pending_jobs'),
                       'running': ours.get('running_jobs'),
                       'oldest_pending_s': ours.get('oldest_pending_s'),
                       'account': sample.get('account')}
    if errors:
        out['error'] = '; '.join(str(e) for e in errors)
    out['collector'] = 'squeue and sinfo on {}, carried by job {}'.format(
        sample.get('host') or '?', value.get('pandaid') or '?')
    return out


def _pool_domains():
    """The machine domains each readable pool advertises."""
    out = {}
    for pool, spec in POOLS.items():
        if spec.get('source') == 'sample':
            # Attributed by queue name (queue_prefix), not by domain.
            continue
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
    if spec.get('source') == 'sample':
        return _sample_reading(pool, spec)
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
            sample_pool = _sample_pool(name)
            if not sample_pool:
                continue
            entry = {'pool': sample_pool,
                     'evidence': 'its name; the pool is read by its own jobs'}
        pool = entry.get('pool')
        if pool not in cache:
            cache[pool] = pool_reading(pool)
        reading = cache[pool]
        if reading:
            out[name] = dict(reading, evidence=entry.get('evidence'))
    return out
