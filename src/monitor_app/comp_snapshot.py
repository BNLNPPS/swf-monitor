"""The comp snapshot: the computing centre room's live record
(docs/COMP_SNAPSHOT.md).

One builder over the PanDA and swf databases, served as the cached
product ``comp_snapshot:v1`` (docs/CACHED_PRODUCTS.md) by REST at
``panda/comp/snapshot.json`` and by the MCP tool ``swf_comp_snapshot``.
No remote call and no LLM: the build reads tables only. A part that
cannot be read is listed in ``errors`` and left out, never shown as zero.
"""
import logging
import re
import time
from datetime import timedelta, timezone as dt_timezone

from django.db import connections
from django.utils import timezone

logger = logging.getLogger(__name__)

PRODUCT_KEY = 'comp_snapshot:v1'
TTL_SECONDS = 60
SCHEMA = 'comp-snapshot/1'
PUBLIC = 'https://epic-devcloud.org/prod'

# The room's grouping, seeded into SysConfig ``comp.sites`` on first read
# so a site or queue is added or regrouped without a deploy. A queue's
# ``status`` here overrides what PanDA says: an operator's word that a
# queue is off or not yet built.
DEFAULT_SITES = [
    {'id': 'osg', 'name': 'OSG', 'execute_sites': True,
     'queues': [{'id': 'BNL_OSG_PanDA_1'}, {'id': 'BNL_OSG_EPIC_PROD_1'}]},
    {'id': 'grex', 'name': 'GREX',
     'queues': [{'id': 'UM_GREX_PanDA_1'}]},
    {'id': 'compute-canada', 'name': 'Compute Canada',
     'queues': [{'id': 'ComputeCanada_PanDA_1', 'status': 'not_built',
                 'status_reason': 'awaiting SFU identity mapping at the CEs'}]},
    {'id': 'nersc', 'name': 'NERSC',
     'queues': [{'id': 'NERSC_Perlmutter_epic', 'status': 'off'},
                {'id': 'NERSC_Perlmutter_epic_es', 'status': 'off'}]},
    {'id': 'google', 'name': 'Google',
     'queues': [{'id': 'BNL_ePIC_GOOGLE', 'status': 'off'},
                {'id': 'BNL_ePIC_GOOGLE_es', 'status': 'off'}]},
    {'id': 'jlab', 'name': 'JLab',
     'queues': [{'id': 'JLAB_PanDA_1', 'status': 'off',
                 'status_reason': "waiting for JLab's CE to route our jobs"}]},
]

# GREX runs eight pilots per Slurm worker (2026-10-20).
PILOTS_PER_WORKER = {'UM_GREX_PanDA_1': 8}
OSG_SUBMIT_HOST = 'osgsub01'
# The harvester reporters (docs/HARVESTER_REPORTER.md); each record names
# the queues its harvester serves. osgsub01's own key is the OSG submit
# reporter's, which carries the schedd's idle pilots per queue.
HARVESTER_REPORTS = ('osgsub01-harvester', 'pandaharvester01', 'pandaharvester02')
# The reporters run every five minutes; three missed runs make a record stale.
REPORT_STALE_SECONDS = 900
LEVELS = ('grey', 'green', 'amber', 'red')


def _worst(levels):
    levels = [lv for lv in levels if lv in LEVELS]
    if not levels:
        return 'grey'
    real = [lv for lv in levels if lv != 'grey']
    return max(real, key=LEVELS.index) if real else 'grey'


def _health(parts):
    return {'level': _worst(p['level'] for p in parts.values()), 'parts': parts}


def _part(level, value=None, note=''):
    return {'level': level, 'value': value, 'note': note}


def _activity_part(finished, failed):
    ended = finished + failed
    if ended == 0:
        return _part('grey', 0, 'no jobs ended in the last hour')
    if finished == 0 and failed >= 10:
        level = 'red'
    elif failed * 4 > ended:
        level = 'amber'
    else:
        level = 'green'
    return _part(level, round(failed / ended, 3),
                 f'{finished} finished, {failed} failed in the last hour')


def _pilot_supply_part(p, queued, started_1h, submitted_1h):
    """Whether pilots keep reaching the queue (docs/COMP_SNAPSHOT.md, Pilot
    supply). Harvester holds new pilots back against its own count of
    those waiting, so a count the schedd no longer bears out stops supply
    while the queue reads full."""
    if not p:
        return _part('grey', None, 'no harvester report names the queue')
    age = p.get('report_age_seconds')
    if age is None or age > REPORT_STALE_SECONDS:
        return _part('grey', None, f"harvester report {int((age or 0) // 60)} min old")
    waiting, limit = p['waiting'], p.get('limit')
    if queued and not waiting and not started_1h and not submitted_1h:
        return _part('red', 0, f"{queued} jobs queued, no pilot waiting, submitted "
                               f"or started in the last hour")
    if limit is None:
        return _part('grey', waiting, f"harvester counts {waiting} waiting; the limit "
                                      f"it applies is not reported")
    seen = f"harvester counts {waiting} waiting (its limit {limit})"
    idle = p.get('waiting_schedd')
    if idle is not None and (p.get('schedd_age_seconds') or 0) <= REPORT_STALE_SECONDS:
        seen += f", the schedd holds {idle}"
        if waiting >= 2 * limit and idle < limit:
            return _part('red' if queued else 'amber', waiting - idle,
                         f"{seen}: harvester counts pilots the schedd no longer has "
                         f"and submits none")
        # what the pool can start: the pilots the schedd holds idle and
        # those harvester will still submit before its count reaches its limit
        startable = idle + max(limit - waiting, 0)
        if startable < limit / 2:
            return _part('red' if queued else 'amber', startable,
                         f"{seen}: harvester will add {max(limit - waiting, 0)}, so the "
                         f"pool can start {startable}, under half the limit; harvester "
                         f"has not yet seen its waiting pilots start")
    return _part('green', waiting, seen)


def _rows(sql, params=()):
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


class _Build:
    def __init__(self):
        from monitor_app.panda.census import PANDA_SCHEMA
        self.S = PANDA_SCHEMA
        self.now = timezone.now()
        self.hour_ago = self.now - timedelta(hours=1)
        self.errors = []

    def part(self, name, fn, fallback=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.exception('comp snapshot: %s failed', name)
            self.errors.append({'part': name, 'error': f'{type(exc).__name__}: {exc}'})
            return fallback

    # -- readings -------------------------------------------------------

    def ended(self, column):
        """Production jobs ended in the last hour by ``column``: counts and
        core-hours, finished and failed."""
        out = {}
        sql = f'''
            SELECT {column}, jobstatus, COUNT(*),
                   SUM(GREATEST(COALESCE(corecount, 1), 1)
                       * EXTRACT(EPOCH FROM (endtime - starttime))) / 3600.0
            FROM (
              SELECT {column}, jobstatus, corecount, endtime, starttime FROM "{self.S}"."jobsactive4"
               WHERE endtime >= %s AND processingtype = 'epicproduction'
                 AND jobstatus IN ('finished', 'failed')
              UNION ALL
              SELECT {column}, jobstatus, corecount, endtime, starttime FROM "{self.S}"."jobsarchived4"
               WHERE endtime >= %s AND processingtype = 'epicproduction'
                 AND jobstatus IN ('finished', 'failed')) t
            GROUP BY 1, 2'''
        for key, status, n, core_h in _rows(sql, (self.hour_ago, self.hour_ago)):
            entry = out.setdefault(key or '', {'finished': 0, 'failed': 0,
                                               'finished_core_h': 0.0, 'failed_core_h': 0.0})
            entry[status] = int(n)
            entry[f'{status}_core_h'] = round(float(core_h or 0), 1)
        return out

    def top_failures(self):
        """The last hour's failures per queue by error, largest first."""
        out = {}
        sql = f'''
            SELECT computingsite, exeerrorcode, piloterrorcode, COUNT(*) FROM (
              SELECT computingsite, exeerrorcode, piloterrorcode FROM "{self.S}"."jobsactive4"
               WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'failed'
              UNION ALL
              SELECT computingsite, exeerrorcode, piloterrorcode FROM "{self.S}"."jobsarchived4"
               WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'failed') t
            GROUP BY 1, 2, 3 ORDER BY 4 DESC'''
        for site, exe, pilot, n in _rows(sql, (self.hour_ago, self.hour_ago)):
            label = (f'payload exit {exe}' if exe else
                     f'pilot error {pilot}' if pilot else 'unattributed')
            out.setdefault(site, []).append((label, int(n)))
        return out

    def opened(self):
        """Harvester workers per queue in the last hour: started (a slot
        opened: the pilot got a slot at the site) and submitted (sent to
        the site's batch system or pool, not yet a slot)."""
        sql = f'''SELECT computingsite,
                         SUM(CASE WHEN starttime >= %s THEN 1 ELSE 0 END),
                         SUM(CASE WHEN submittime >= %s THEN 1 ELSE 0 END)
                  FROM "{self.S}"."harvester_workers"
                  WHERE starttime >= %s OR submittime >= %s GROUP BY 1'''
        h = self.hour_ago
        return {site: (int(s or 0), int(n or 0)) for site, s, n in _rows(sql, (h, h, h, h))}

    def queue_status(self, names):
        sql = f'''SELECT panda_queue, data->>'status' FROM "{self.S}"."schedconfig_json"
                  WHERE panda_queue = ANY(%s)'''
        return {q: (st or '') for q, st in _rows(sql, (list(names),))}

    def front_decisions(self, names):
        from monitor_app.models import AppLog
        out = {}
        since = self.now - timedelta(hours=3)
        for q in names:
            rec = (AppLog.objects.filter(app_name='epicprod', timestamp__gte=since,
                                         extra_data__action='front_decision',
                                         extra_data__subject_key=q)
                   .order_by('-timestamp').values_list('extra_data', flat=True).first())
            if rec:
                out[q] = rec
        return out

    def osg_pool(self):
        from monitor_app.host_reports import latest
        found = latest(OSG_SUBMIT_HOST)
        if not found:
            return None
        pool = (found.get('record') or {}).get('pool') or {}
        return {'admitted': pool.get('slots_admitted'), 'total': pool.get('slots_total'),
                'age_seconds': found.get('age_seconds')}

    def pilots(self):
        """Per queue, the pilot slots waiting at the site and running by
        harvester's own count, its waiting limit, and for the OSG queues
        the idle pilots the submit schedd holds, from the host reporters'
        records (no remote call)."""
        from monitor_app.host_reports import latest
        out = {}
        for key in HARVESTER_REPORTS:
            found = latest(key)
            if not found:
                continue
            record = found.get('record') or {}
            now = (record.get('harvester_db') or {}).get('workers_now') or {}
            # the limit harvester applied, from its adjuster log; pq_table's
            # n_queue_limit_worker is only the cap it is computed under
            applied = (record.get('adjustment') or {}).get('queues') or {}
            for q in set(now) | set(applied):
                w = now.get(q) or {}
                a = applied.get(q) or {}
                out[q] = {
                    # harvester's idle is a pilot evicted back to waiting
                    'waiting': int(w.get('submitted') or 0) + int(w.get('idle') or 0),
                    'waiting_schedd': None,
                    'limit': a.get('queue_limit'),
                    'limit_at': a.get('at'),
                    'running': int(w.get('running') or 0),
                    'per_slot': PILOTS_PER_WORKER.get(q, 1),
                    'report': key,
                    'report_age_seconds': found.get('age_seconds'),
                }
        found = latest(OSG_SUBMIT_HOST)
        idle = ((found or {}).get('record') or {}).get('idle_analysis')
        if isinstance(idle, dict) and 'error' not in idle:
            # the reporter lists every queue that has an idle pilot
            for q, p in out.items():
                if p['report'] == f'{OSG_SUBMIT_HOST}-harvester':
                    p['waiting_schedd'] = int((idle.get(q) or {}).get('idle') or 0)
                    p['schedd_age_seconds'] = found.get('age_seconds')
        return out

    def execute_sites(self, queues):
        sql = f'''SELECT COALESCE(NULLIF(destinationsite, ''), ''), COUNT(*),
                         SUM(GREATEST(COALESCE(corecount, 1), 1))
                  FROM "{self.S}"."jobsactive4"
                  WHERE jobstatus = 'running' AND computingsite = ANY(%s) GROUP BY 1'''
        running = {d: (int(n), int(c or 0)) for d, n, c in _rows(sql, (list(queues),))}
        sql = f'''
            SELECT COALESCE(NULLIF(destinationsite, ''), ''), jobstatus, COUNT(*),
                   SUM(GREATEST(COALESCE(corecount, 1), 1)
                       * EXTRACT(EPOCH FROM (endtime - starttime))) / 3600.0
            FROM (
              SELECT destinationsite, jobstatus, corecount, endtime, starttime FROM "{self.S}"."jobsactive4"
               WHERE endtime >= %s AND computingsite = ANY(%s) AND jobstatus IN ('finished', 'failed')
              UNION ALL
              SELECT destinationsite, jobstatus, corecount, endtime, starttime FROM "{self.S}"."jobsarchived4"
               WHERE endtime >= %s AND computingsite = ANY(%s) AND jobstatus IN ('finished', 'failed')) t
            GROUP BY 1, 2'''
        ended = {}
        for d, status, n, core_h in _rows(sql, (self.hour_ago, list(queues), self.hour_ago, list(queues))):
            e = ended.setdefault(d, {'finished': 0, 'failed': 0,
                                     'finished_core_h': 0.0, 'failed_core_h': 0.0})
            e[status] = int(n)
            e[f'{status}_core_h'] = round(float(core_h or 0), 1)
        out = []
        for name in sorted(set(running) | set(ended),
                           key=lambda s: -(running.get(s, (0, 0))[0])):
            r_jobs, r_cores = running.get(name, (0, 0))
            e = ended.get(name, {'finished': 0, 'failed': 0,
                                 'finished_core_h': 0.0, 'failed_core_h': 0.0})
            label = name or 'not reported'
            out.append({
                'id': re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-'),
                'name': label,
                'running': r_jobs,
                'finished_1h': e['finished'], 'failed_1h': e['failed'],
                'capacity': None, 'capacity_source': None,
                'in_use': r_cores,
                'finished_core_h_1h': e['finished_core_h'],
                'failed_core_h_1h': e['failed_core_h'],
                'health': _health({'prod_activity': _activity_part(e['finished'], e['failed'])}),
            })
        return out

    def storage(self):
        sql = f'''
            SELECT substring(jobmetrics from 'payloadRegistration=([A-Za-z_]+)'), COUNT(*), MAX(endtime)
            FROM "{self.S}"."jobsarchived4"
            WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'finished'
            GROUP BY 1'''
        reg = {k or '': (int(n), t) for k, n, t in _rows(sql, (self.hour_ago,))}
        registered = reg.get('registered', (0, None))
        unsettled = sum(reg.get(k, (0, None))[0] for k in ('unfinished', 'pending', 'diverted'))
        done = registered[0] + unsettled
        if done == 0:
            reg_part = _part('grey', 0, 'no finished jobs in the last hour')
        else:
            share = unsettled / done
            reg_part = _part('red' if share > 0.5 else 'amber' if share > 0.05 else 'green',
                             round(share, 3),
                             f'{registered[0]} registered, {unsettled} not registered or diverted to the BNL stash')
        last = _rows(f'''
            SELECT MAX(endtime) FROM "{self.S}"."jobsarchived4"
            WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'finished'
              AND jobmetrics LIKE %s''', (self.now - timedelta(days=2), '%payloadRegistration=registered%'))[0][0]
        if last is None:
            arrival = _part('red', None, 'no output registered in two days')
        else:
            if timezone.is_naive(last):
                last = timezone.make_aware(last, dt_timezone.utc)
            age_min = max(0.0, round((self.now - last).total_seconds() / 60, 1))
            arrival = _part('green' if age_min <= 30 else 'amber' if age_min <= 120 else 'red',
                            age_min, f'latest output registered {age_min} min ago')
        logs = _rows(f'''
            SELECT COUNT(*) FROM (
              SELECT piloterrordiag FROM "{self.S}"."jobsarchived4"
               WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'failed'
                 AND (piloterrorcode IN (1137, 1151, 1152) OR piloterrordiag ILIKE %s)
              UNION ALL
              SELECT piloterrordiag FROM "{self.S}"."jobsactive4"
               WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'failed'
                 AND (piloterrorcode IN (1137, 1151, 1152) OR piloterrordiag ILIKE %s)) t''',
                     (self.hour_ago, '%stage-out%', self.hour_ago, '%stage-out%'))[0][0]
        logs = int(logs or 0)
        log_part = _part('red' if logs >= 50 else 'amber' if logs >= 5 else 'green', logs,
                         f'{logs} jobs failed at log stage-out in the last hour')
        return [
            {'id': 'jlab-rucio', 'name': 'JLab Rucio', 'holds': 'science data',
             'health': _health({'registration': reg_part, 'latest_arrival': arrival}),
             'links': {'storage': f'{PUBLIC}/panda/storage-doors/'}},
            {'id': 'bnl-rucio', 'name': 'BNL Rucio', 'holds': 'logs',
             'health': _health({'log_stageout': log_part}),
             'links': {'storage': f'{PUBLIC}/panda/storage-doors/'}},
        ]


def _sites_config():
    from monitor_app.models import SysConfig
    value = SysConfig.get_setting('comp.sites', DEFAULT_SITES)
    return value if isinstance(value, list) else DEFAULT_SITES


def build():
    """Build the record (docs/COMP_SNAPSHOT.md, schema 1)."""
    from monitor_app.models import SysConfig
    from monitor_app.panda.census import queue_census

    t0 = time.monotonic()
    b = _Build()
    sites_cfg = _sites_config()
    names = [q['id'] for s in sites_cfg for q in s.get('queues', [])]
    cfg = SysConfig.get_config()

    census = b.part('census', lambda: (queue_census() or {}).get('queues') or {}, {})
    ended = b.part('ended', lambda: b.ended('computingsite'), {})
    failures = b.part('failures', b.top_failures, {})
    opened = b.part('harvester', b.opened, {})
    pandastatus = b.part('queue_status', lambda: b.queue_status(names), {})
    decisions = b.part('front', lambda: b.front_decisions(names), {})
    pool = b.part('osg_pool', b.osg_pool, None)
    pilots = b.part('pilots', b.pilots, {})

    sites = []
    for s in sites_cfg:
        queues = []
        for qc in s.get('queues', []):
            q = qc['id']
            c = census.get(q) or {}
            by_status = c.get('by_status') or {}
            e = ended.get(q) or {'finished': 0, 'failed': 0, 'finished_core_h': 0.0, 'failed_core_h': 0.0}
            d = decisions.get(q) or {}
            dc = d.get('counts') or d
            running = int((by_status.get('running') or {}).get('jobs', 0))
            running_cores = int((by_status.get('running') or {}).get('cores', running))
            starting = int((by_status.get('starting') or {}).get('jobs', 0))
            in_panda = q in pandastatus
            status = qc.get('status') or ('live' if in_panda else 'not_built')
            reason = qc.get('status_reason') or ('' if in_panda else 'not in PanDA')

            # capacity, best source first
            capacity, source = None, None
            configured = cfg.get(f'comp.capacity.{q}')
            if configured:
                capacity, source = int(configured), 'configured'
            elif s.get('execute_sites') and pool and pool.get('admitted') is not None:
                # one pool behind every OSG queue: the site carries it once
                capacity, source = int(pool['admitted']), 'pool, shared'
            elif (c.get('calibration') or {}).get('peak_running'):
                capacity, source = int(c['calibration']['peak_running']), 'observed peak'

            if status != 'live':
                capacity, source = None, None
            if status == 'live':
                activity = _activity_part(e['finished'], e['failed'])
                canary = dc.get('canary') or 'unknown'
                canary_part = _part({'healthy': 'green', 'failing': 'red'}.get(canary, 'grey'), canary,
                                    f'canary {canary}')
                top = (failures.get(q) or [])
                ended_n = e['finished'] + e['failed']
                if not ended_n:
                    snap = _part('grey', 0, 'no jobs ended in the last hour')
                elif top and top[0][1] * 2 > ended_n and e['failed'] >= 10:
                    snap = _part('red', top[0][1], f'{top[0][1]} x {top[0][0]}')
                elif e['failed'] * 4 > ended_n:
                    snap = _part('amber', e['failed'], f'top: {top[0][1]} x {top[0][0]}' if top else '')
                else:
                    snap = _part('green', e['failed'],
                                 f'top: {top[0][1]} x {top[0][0]}' if top else 'no failures')
                supply = _pilot_supply_part(pilots.get(q), int(c.get('not_started') or 0),
                                            *opened.get(q, (0, 0)))
                health = _health({'prod_activity': activity, 'canary': canary_part,
                                  'snapper_errors': snap, 'pilot_supply': supply})
            else:
                health = _health({'status': _part('grey', status, reason or status)})

            queues.append({
                'id': q, 'name': qc.get('name') or q,
                'status': status, 'status_reason': reason,
                'running': running, 'starting': starting,
                'queued': int(c.get('not_started') or 0),
                'ungenerated': int(c.get('ungenerated') or 0),
                'harvester_slots_opened_1h': opened.get(q, (0, 0))[0],
                'harvester_pilots_submitted_1h': opened.get(q, (0, 0))[1],
                'pilots': pilots.get(q),
                'finished_1h': e['finished'], 'failed_1h': e['failed'],
                'capacity': capacity, 'capacity_source': source,
                'in_use': running_cores,
                'finished_core_h_1h': e['finished_core_h'],
                'failed_core_h_1h': e['failed_core_h'],
                'front': {'state': d.get('outcome'), 'reason': d.get('reason'),
                          'feed': dc.get('feed_switch')},
                'health': health,
                'links': {
                    'panda_queue': f'{PUBLIC}/panda/epic-queues/{q}/',
                    'snapper': f'{PUBLIC}/panda/errors/?site={q}',
                    # the canary root redirects, which the proxy cannot relay
                    'canary': f'{PUBLIC}/canary/probes/',
                },
            })
        if s.get('execute_sites') and pool and pool.get('admitted') is not None:
            site_capacity, site_source = int(pool['admitted']), 'pool'
        else:
            caps = [q['capacity'] for q in queues if q['capacity'] is not None]
            site_capacity = sum(caps) if caps else None
            site_source = ('+'.join(sorted({q['capacity_source'] for q in queues if q['capacity_source']}))
                           or None)
        site = {
            'id': s['id'], 'name': s['name'],
            'capacity': site_capacity, 'capacity_source': site_source,
            'in_use': sum(q['in_use'] for q in queues),
            'status': 'live' if any(q['status'] == 'live' for q in queues) else
                      (queues[0]['status'] if queues else 'off'),
            'health': {'level': _worst(q['health']['level'] for q in queues),
                       'parts': {q['id']: q['health']['level'] for q in queues}},
            'links': {'panda_queue': queues[0]['links']['panda_queue'] if queues else None},
            'queues': queues,
            'execute_sites': [],
        }
        if s.get('execute_sites'):
            live = [q['id'] for q in queues if q['status'] == 'live']
            if live:
                site['execute_sites'] = b.part('execute_sites', lambda: b.execute_sites(live), [])
        sites.append(site)

    storage = b.part('storage', b.storage, [])

    ready = b.part('ready_tasks', lambda: _ready_tasks(), None)
    released = b.part('released', lambda: _released(b.hour_ago), None)
    front = {
        'ready_tasks': ready, 'released_1h': released,
        'enabled': cfg.get('front.enabled'), 'mode': cfg.get('front.mode'),
        'jedi_throttled': cfg.get('front.jedi_throttled'),
        'link': f'{PUBLIC}/panda/front/',
    }

    record = {
        'schema': SCHEMA,
        'observed_at': b.now.isoformat(),
        'units': {'capacity': 'cores', 'core_hours': 'core-hours in the last hour'},
        'pressure_front': front,
        'sites': sites,
        'storage': storage,
        'report': {'text': _report_text(front, sites, storage), 'assessment': None},
        'errors': b.errors,
    }
    record['build_seconds'] = round(time.monotonic() - t0, 2)
    return record


def _ready_tasks():
    with connections['default'].cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM pcs_prod_task WHERE overrides ? 'queued'")
        return int(cursor.fetchone()[0])


def _released(since):
    from monitor_app.models import AppLog
    return AppLog.objects.filter(app_name='epicprod', timestamp__gte=since,
                                 extra_data__action='front_decision',
                                 extra_data__outcome='fed').count()


def _report_text(front, sites, storage):
    lines = []
    ready, released = front.get('ready_tasks'), front.get('released_1h')
    lines.append(f"Pressure front: {ready if ready is not None else '?'} tasks ready, "
                 f"{released if released is not None else '?'} released in the last hour.")
    for s in sites:
        live = [q for q in s['queues'] if q['status'] == 'live']
        if not live:
            continue
        running = sum(q['running'] for q in live)
        fin = sum(q['finished_1h'] for q in live)
        fail = sum(q['failed_1h'] for q in live)
        queued = sum(q['queued'] for q in live)
        lines.append(f"{s['name']}: {running} running, {queued} queued; last hour "
                     f"{fin} finished, {fail} failed ({s['health']['level']}).")
        for q in live:
            for name, p in q['health']['parts'].items():
                if p['level'] in ('amber', 'red'):
                    lines.append(f"  {q['id']} {name} {p['level']}: {p['note']}")
    off = [s['name'] for s in sites if not any(q['status'] == 'live' for q in s['queues'])]
    if off:
        lines.append(f"Off or not yet built: {', '.join(off)}.")
    for st in storage:
        for name, p in st['health']['parts'].items():
            if p['level'] in ('amber', 'red'):
                lines.append(f"{st['name']} {name} {p['level']}: {p['note']}")
    return '\n'.join(lines)


def get_snapshot(refresh=False):
    """The stored record, rebuilt behind the request when older than the
    TTL (docs/CACHED_PRODUCTS.md)."""
    from monitor_app.cached_product import get_product
    result = get_product(PRODUCT_KEY, build, TTL_SECONDS, refresh=refresh)
    value = dict(result.get('value') or {})
    value['age_seconds'] = result.get('age_seconds')
    value['refreshing'] = result.get('refreshing')
    return value
