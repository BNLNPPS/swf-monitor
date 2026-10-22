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
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import quote

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
from swf_epicprod.regulation import PILOTS_PER_WORKER
OSG_SUBMIT_HOST = 'osgsub01'
# The harvester reporters (docs/HARVESTER_REPORTER.md); each record names
# the queues its harvester serves. osgsub01's own key is the OSG submit
# reporter's, which carries the schedd's idle pilots per queue.
HARVESTER_REPORTS = ('osgsub01-harvester', 'pandaharvester01', 'pandaharvester02')
# The reporters run every five minutes; three missed runs make a record stale.
REPORT_STALE_SECONDS = 900
LEVELS = ('grey', 'green', 'amber', 'red')
# The pilot's log stage-out error codes.
LOG_STAGEOUT_CODES = (1137, 1151, 1152)
# The registration outcomes that leave a file owed to JLab Rucio: pending
# (the registrar completes it) and unfinished. ``diverted`` is a clash
# registered under a derived name (swf-epicprod RUCIO_RESILIENCE.md,
# Measure 3), so it is registered.
REGISTRATION_OWED = ('pending', 'unfinished')
# The production output RSE: every production configuration names it as
# OUT_RSE (swf-epicprod RUCIO_FAILOVER_STASH.md).
OUTPUT_RSE = 'BNL-XRD'
# A daily assessment's verdict as a health level (swf-epicprod
# EPICPROD_ASSESSMENTS.md: ok | attention | alarm).
VERDICT_LEVELS = {'ok': 'green', 'attention': 'amber', 'alarm': 'red'}
# The epicprod-live screen: warning and error records only, five to ten
# on screen (Torre, 2026-10-22).
LIVE_LEVELS = ('WARNING', 'ERROR', 'CRITICAL')
LIVE_ITEMS = 10


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


def effective_waiting_limit(config, applied, now):
    """Use the fixed DB limit or the last applied value carried by the reporter.

    The adjuster skips its limit log when the waiting queue is already full.
    Its timestamp records an event, not a heartbeat: freshness belongs to the
    enclosing report, checked separately by ``_pilot_supply_part``.
    """
    config = config or {}
    try:
        if int(config['n_queue_limit_worker_ratio']) == 0:
            return int(config['n_queue_limit_worker'])
    except (KeyError, ValueError, TypeError):
        pass
    try:
        at = datetime.fromisoformat(applied['at'].replace('Z', '+00:00'))
        limit = int(applied['queue_limit'])
        if at <= now and limit >= 0:
            return limit
    except (KeyError, ValueError, TypeError):
        pass
    return None


def _pilot_supply_part(p, queued, started_1h, submitted_1h):
    """Whether pilots keep reaching the queue (docs/COMP_SNAPSHOT.md, Pilot
    supply). Harvester holds new pilots back against its own count of
    those waiting, so a count the schedd no longer bears out stops supply
    while the queue reads full."""
    if not p:
        return _part('amber' if queued else 'grey', None, 'no harvester report names the queue')
    age = p.get('report_age_seconds')
    stale_seconds = p.get('report_stale_seconds', REPORT_STALE_SECONDS)
    if age is None or age > stale_seconds:
        return _part('amber' if queued else 'grey', None, f"harvester report {int((age or 0) // 60)} min old")
    if p.get('error'):
        return _part('amber' if queued else 'grey', None, p['error'])
    demand_age = p.get('demand_age_seconds')
    if queued and p.get('demand_max_age_seconds') is not None and (
            demand_age is None or demand_age > p['demand_max_age_seconds']):
        return _part('amber', demand_age, 'job-demand cache missing or stale')
    waiting, limit = p['waiting'], p.get('limit')
    if queued and not waiting and not started_1h and not submitted_1h:
        return _part('red', 0, f"{queued} jobs queued, no pilot waiting, submitted "
                               f"or started in the last hour")
    if limit is None:
        return _part('amber' if queued else 'grey', waiting, f"harvester counts {waiting} waiting; the limit "
                                      f"it applies is not reported")
    label = 'last applied limit' if p.get('limit_source') == 'adjuster' else 'limit'
    seen = f"harvester counts {waiting} waiting (its {label} {limit})"
    idle = p.get('waiting_schedd')
    if idle is not None and (p.get('schedd_age_seconds') or 0) <= stale_seconds:
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


def _aware(t):
    """A PanDA time (naive UTC) made aware."""
    return timezone.make_aware(t, dt_timezone.utc) if t is not None and timezone.is_naive(t) else t


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

    def production_alarms(self):
        """The active alarm events that concern production: a task's
        failure-rate alarm only when the task is a production task
        (processingtype epicproduction). The failure-rate alarms watch
        every EIC task, a user's included, and the room reports
        production."""
        alarms = _active_alarms()
        ids = {int(m.group(1)) for m in (TASK_ALARM_RE.match(a['title']) for a in alarms) if m}
        types = {}
        if ids:
            sql = f'''SELECT jeditaskid, processingtype FROM "{self.S}"."jedi_tasks"
                      WHERE jeditaskid = ANY(%s)'''
            types = {int(t): p for t, p in _rows(sql, (list(ids),))}
        out = []
        for a in alarms:
            m = TASK_ALARM_RE.match(a['title'])
            if m and types.get(int(m.group(1))) != 'epicproduction':
                continue
            out.append(a)
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
            db = record.get('harvester_db') or {}
            now = db.get('workers_now') or {}
            try:
                demand_at = datetime.fromisoformat(db['cache_updates']['job_statistics.json']).replace(tzinfo=dt_timezone.utc)
                demand_age = (self.now - demand_at).total_seconds()
            except (KeyError, ValueError, TypeError):
                demand_age = None
            # the limit harvester applied, from its adjuster log; pq_table's
            # n_queue_limit_worker is only the cap it is computed under
            applied = (record.get('adjustment') or {}).get('queues') or {}
            limits = {r['queue']: r for r in (record.get('harvester_db') or {}).get('launch_limits', [])
                      if isinstance(r, dict) and 'queue' in r}
            for q in (set(now) - {'error'}) | set(applied) | set(limits):
                w = now.get(q) or {}
                a = applied.get(q) or {}
                limit = effective_waiting_limit(limits.get(q), a, self.now)
                fixed = str((limits.get(q) or {}).get('n_queue_limit_worker_ratio')) == '0'
                adjustment_error = ((record.get('adjustment') or {}).get('read') or {}).get('error')
                out[q] = {
                    'error': db.get('error') or now.get('error') or (
                        f'cannot read adjuster log: {adjustment_error}' if adjustment_error and not fixed else None),
                    'demand_age_seconds': demand_age,
                    'demand_max_age_seconds': 90 if key == 'osgsub01-harvester' else 900,
                    'report_stale_seconds': 180 if key == 'osgsub01-harvester' else REPORT_STALE_SECONDS,
                    # harvester's idle is a pilot evicted back to waiting
                    'waiting': int(w.get('submitted') or 0) + int(w.get('idle') or 0),
                    'waiting_schedd': None,
                    'limit': limit,
                    'limit_source': 'database' if fixed else 'adjuster',
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
                # the execute site's jobs, by the site its pilot reported
                'links': ({'jobs': f'{PUBLIC}/panda/jobs/?execsite={quote(name)}&days=1'}
                          if name else {}),
            })
        return out

    def science_arrivals(self):
        """Per queue and output kind, the science files the last hour's
        finished production jobs registered in JLab Rucio, with their bytes
        and events: each DID the payload report's registration lists,
        joined by file name to the output it reports. A clash registered
        under a derived name (outcome ``diverted``) is a registered file
        whose name matches no output, so it counts with kind ``other`` and
        no bytes. PanDA's own file table holds no science output, since
        production tasks register their outputs themselves."""
        sql = f'''
            WITH r AS (
              SELECT j.computingsite AS site, m.metadata::jsonb -> 'payload' AS p
              FROM "{self.S}"."metatable" m JOIN "{self.S}"."jobsarchived4" j ON j.pandaid = m.pandaid
              WHERE j.endtime >= %s AND j.processingtype = 'epicproduction' AND j.jobstatus = 'finished'
                AND (j.jobmetrics LIKE %s OR j.jobmetrics LIKE %s)),
            d AS (
              SELECT r.site, r.p, did FROM r, jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(r.p -> 'registration' -> 'dids') = 'array'
                     THEN r.p -> 'registration' -> 'dids' ELSE '[]'::jsonb END) did)
            SELECT d.site, COALESCE(o.key, 'other'), COUNT(*),
                   SUM((o.value ->> 'bytes')::bigint), SUM((o.value ->> 'events')::bigint)
            FROM d LEFT JOIN LATERAL (
              SELECT o.key, o.value FROM jsonb_each(
                CASE WHEN jsonb_typeof(d.p -> 'outputs') = 'object'
                     THEN d.p -> 'outputs' ELSE '{{}}'::jsonb END) o
              WHERE d.did LIKE '%%/' || (o.value ->> 'file') LIMIT 1) o ON true
            GROUP BY 1, 2'''
        out = {}
        for site, kind, n, nbytes, events in _rows(sql, (self.hour_ago,
                                                         '%payloadRegistration=registered%',
                                                         '%payloadRegistration=diverted%')):
            out.setdefault(site or '', {})[kind] = (int(n), int(nbytes or 0), int(events or 0))
        return out

    def registration_outcomes(self):
        """Per queue, the last hour's finished production jobs by the
        payload's registration outcome (``payloadRegistration``), and the
        end time of the newest that registered."""
        sql = f'''
            SELECT computingsite, substring(jobmetrics from 'payloadRegistration=([A-Za-z_]+)'),
                   COUNT(*), MAX(endtime)
            FROM "{self.S}"."jobsarchived4"
            WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'finished'
            GROUP BY 1, 2'''
        out, last = {}, None
        for site, outcome, n, end in _rows(sql, (self.hour_ago,)):
            out.setdefault(site or '', {})[outcome or ''] = int(n)
            if outcome == 'registered' and end is not None and (last is None or end > last):
                last = end
        return out, _aware(last)

    def log_arrivals(self):
        """Per queue, the log tarballs of the last hour's ended production
        jobs that reached BNL Rucio, with their bytes, and the jobs that
        failed at log stage-out. A failed job's log is registered too
        (PanDA marks every file of a failed job failed, but its log is at
        BNL_PROD_DISK_1, checked 2026-10-22), so only a job failed at log
        stage-out, or one whose pilot held its log in the stage-out bucket
        (``logHeld``), has no log there."""
        stageout = (f"(j.piloterrorcode IN ({','.join(str(c) for c in LOG_STAGEOUT_CODES)}) "
                    f"OR j.piloterrordiag ILIKE %s)")
        missing = f"((j.jobstatus = 'failed' AND {stageout}) OR COALESCE(j.jobmetrics, '') LIKE %s)"
        sql = f'''
            SELECT j.computingsite,
                   SUM(CASE WHEN {missing} THEN 0 ELSE 1 END),
                   SUM(CASE WHEN {missing} THEN 0 ELSE f.fsize END),
                   SUM(CASE WHEN j.jobstatus = 'failed' AND {stageout} THEN 1 ELSE 0 END),
                   MAX(CASE WHEN {missing} THEN NULL ELSE j.endtime END)
            FROM (
              SELECT pandaid, computingsite, jobstatus, jobmetrics, piloterrorcode, piloterrordiag, endtime
                FROM "{self.S}"."jobsarchived4"
               WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus IN ('finished', 'failed')
              UNION ALL
              SELECT pandaid, computingsite, jobstatus, jobmetrics, piloterrorcode, piloterrordiag, endtime
                FROM "{self.S}"."jobsactive4"
               WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus IN ('finished', 'failed')) j
            JOIN "{self.S}"."filestable4" f ON f.pandaid = j.pandaid AND f.type = 'log'
            GROUP BY 1'''
        diag, held = '%stage-out%', '%logHeld=%'
        rows = _rows(sql, (diag, held, diag, held, diag, diag, held, self.hour_ago, self.hour_ago))
        out, last = {}, None
        for site, n, nbytes, failed, end in rows:
            out[site or ''] = (int(n or 0), int(nbytes or 0), int(failed or 0))
            if end is not None and (last is None or end > last):
                last = end
        return out, _aware(last)

    def latest_finished(self, metrics_like, negate=False):
        """The end time of the newest finished production job in two days
        whose job metrics match (or, negated, do not match) the pattern:
        the newest arrival when none arrived in the last hour."""
        last = _rows(f'''
            SELECT MAX(endtime) FROM "{self.S}"."jobsarchived4"
            WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'finished'
              AND COALESCE(jobmetrics, '') {'NOT ' if negate else ''}LIKE %s''',
                     (self.now - timedelta(days=2), metrics_like))[0][0]
        return _aware(last)

    def storage(self, site_of):
        """The E1s' Rucio storage (docs/COMP_SNAPSHOT.md, Storage): what
        arrived in the last hour, what did not, what is still owed, the
        output RSE's capacity, and the health."""
        def site_id(queue):
            return site_of.get(queue) or re.sub(r'[^a-z0-9]+', '-', (queue or 'not reported').lower()).strip('-')

        # JLab Rucio: science data
        arrivals = self.part('science_arrivals', self.science_arrivals, None)
        outcomes, last = self.part('registration_outcomes', self.registration_outcomes, (None, None))
        reg = {}
        for per in (outcomes or {}).values():
            for k, n in per.items():
                reg[k] = reg.get(k, 0) + n
        if outcomes is None:
            reg_part = _part('grey', None, 'registration outcomes not read')
        else:
            registered = reg.get('registered', 0) + reg.get('diverted', 0)
            unsettled = sum(reg.get(k, 0) for k in REGISTRATION_OWED)
            done = registered + unsettled
            if done == 0:
                reg_part = _part('grey', 0, 'no finished jobs in the last hour')
            else:
                share = unsettled / done
                reg_part = _part('red' if share > 0.5 else 'amber' if share > 0.05 else 'green',
                                 round(share, 3),
                                 f'{registered} registered, {unsettled} not registered (pending or unfinished)')
        if last is None:
            last = self.part('latest_registration',
                             lambda: self.latest_finished('%payloadRegistration=registered%'), None)
        if last is None:
            arrival = _part('red', None, 'no output registered in two days')
            jlab_age = None
        else:
            jlab_age = max(0, int((self.now - last).total_seconds()))
            age_min = round(jlab_age / 60, 1)
            arrival = _part('green' if age_min <= 30 else 'amber' if age_min <= 120 else 'red',
                            age_min, f'latest output registered {age_min} min ago')
        jlab_sites, by_kind = {}, {}
        for queue, kinds in (arrivals or {}).items():
            s = jlab_sites.setdefault(site_id(queue), {'arrived_1h': 0, 'bytes_1h': 0, 'failed_1h': 0})
            for kind, (n, nbytes, events) in kinds.items():
                s['arrived_1h'] += n
                s['bytes_1h'] += nbytes
                k = by_kind.setdefault(kind, {'files': 0, 'bytes': 0, 'events': 0})
                k['files'] += n
                k['bytes'] += nbytes
                k['events'] += events
        for queue, per in (outcomes or {}).items():
            owed = sum(per.get(k, 0) for k in REGISTRATION_OWED)
            if owed:
                jlab_sites.setdefault(site_id(queue), {'arrived_1h': 0, 'bytes_1h': 0,
                                                       'failed_1h': 0})['failed_1h'] += owed
        jlab_pending = self.part('jlab_pending', _jlab_pending, None)
        capacity = self.part('storage_record', _storage_capacity, None)

        # BNL Rucio: logs
        logs, last_log = self.part('log_arrivals', self.log_arrivals, (None, None))
        if logs is None:
            log_part = _part('grey', None, 'log stage-out not read')
            log_failed = None
        else:
            log_failed = sum(f for _n, _b, f in logs.values())
            log_part = _part('red' if log_failed >= 50 else 'amber' if log_failed >= 5 else 'green',
                             log_failed, f'{log_failed} jobs failed at log stage-out in the last hour')
        bnl_sites = {}
        for queue, (n, nbytes, failed) in (logs or {}).items():
            s = bnl_sites.setdefault(site_id(queue), {'arrived_1h': 0, 'bytes_1h': 0, 'failed_1h': 0})
            s['arrived_1h'] += n
            s['bytes_1h'] += nbytes
            s['failed_1h'] += failed
        if last_log is None:
            last_log = self.part('latest_log',
                                 lambda: self.latest_finished('%logHeld=%', negate=True), None)
        log_pending = self.part('log_pending', _log_pending, None)

        def site_rows(sites):
            return [dict(site=k, **v) for k, v in
                    sorted(sites.items(), key=lambda kv: -kv[1]['arrived_1h'])]

        return [
            {'id': 'jlab-rucio', 'name': 'JLab Rucio', 'holds': 'science data',
             'status': 'live', 'counts': 'files',
             'arrived_1h': (sum(k['files'] for k in by_kind.values()) if arrivals is not None else None),
             'bytes_1h': (sum(k['bytes'] for k in by_kind.values()) if arrivals is not None else None),
             'events_1h': (sum(k['events'] for k in by_kind.values()) if arrivals is not None else None),
             'arrived_by_kind_1h': ({k: v['files'] for k, v in by_kind.items()}
                                    if arrivals is not None else None),
             'failed_1h': (sum(reg.get(k, 0) for k in REGISTRATION_OWED) if outcomes is not None else None),
             'pending': (jlab_pending or {}).get('pending'),
             'pending_parts': (jlab_pending or {}).get('parts'),
             'pending_at': (jlab_pending or {}).get('at'),
             'latest_arrival_s': jlab_age,
             'by_site': site_rows(jlab_sites),
             'capacity': (capacity or {}).get('output'),
             'rses': (capacity or {}).get('rses') or [],
             'health': _health({'registration': reg_part, 'latest_arrival': arrival}),
             'links': {'storage': f'{PUBLIC}/panda/storage-doors/',
                       'arrived': f'{PUBLIC}/snapper/epicprod/storage/',
                       'failed': None,
                       'pending': f'{PUBLIC}/pcs/stash/'}},
            {'id': 'bnl-rucio', 'name': 'BNL Rucio', 'holds': 'logs',
             'status': 'live', 'counts': 'logs',
             'arrived_1h': (sum(n for n, _b, _f in logs.values()) if logs is not None else None),
             'bytes_1h': (sum(b for _n, b, _f in logs.values()) if logs is not None else None),
             'events_1h': None,
             'arrived_by_kind_1h': None,
             'failed_1h': log_failed,
             'pending': (log_pending or {}).get('pending'),
             'pending_parts': (log_pending or {}).get('parts'),
             'pending_at': (log_pending or {}).get('at'),
             'latest_arrival_s': (max(0, int((self.now - last_log).total_seconds()))
                                  if last_log is not None else None),
             'by_site': site_rows(bnl_sites),
             'capacity': None,
             'rses': [],
             'health': _health({'log_stageout': log_part}),
             'links': {'storage': f'{PUBLIC}/panda/storage-doors/',
                       'arrived': f'{PUBLIC}/panda/jobs/?status=finished&days=1',
                       'failed': f'{PUBLIC}/panda/errors/?error_source=pilot&days=1',
                       'pending': None}},
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
                    # the pillars' drilldowns: the queue's running jobs, its
                    # queued jobs (the census's not-started set, `queued`
                    # here), and its harvester workers
                    'running': f'{PUBLIC}/panda/jobs/?site={q}&status=running&days=2',
                    'queued': f'{PUBLIC}/panda/jobs/?site={q}&status=queued&days=30',
                    'slots': f'{PUBLIC}/panda/epic-queues/{q}/#workers',
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
            # a site's links are those of its busiest live queue: for the
            # OSG pool, the queue production runs on
            'links': (dict(max((q for q in queues if q['status'] == 'live'),
                               key=lambda q: q['running'], default=queues[0])['links'])
                      if queues else {'panda_queue': None}),
            'queues': queues,
            'execute_sites': [],
        }
        if s.get('execute_sites'):
            live = [q['id'] for q in queues if q['status'] == 'live']
            if live:
                site['execute_sites'] = b.part('execute_sites', lambda: b.execute_sites(live), [])
        sites.append(site)

    site_of = {q['id']: s['id'] for s in sites_cfg for q in s.get('queues', [])}
    storage = b.part('storage', lambda: b.storage(site_of), [])

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
        'report': {'text': _report_text(front, sites, storage),
                   'items': _report_items(front, sites, storage,
                                          b.part('alarms', b.production_alarms, []),
                                          b.part('assessment', _assessment_freshness, None)),
                   'assessment': b.part('daily_assessment', _latest_daily_assessment, None)},
        'live': b.part('live', _live_records, None),
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


# What a front decision means, in the words CONTINUOUS_PRODUCTION.md gives
# its states and reason codes, with the level the report shows it at.
FRONT_STATES = {
    'supplied': ('green', 'enough work queued, nothing to feed'),
    'fed': ('green', 'fed a task in its last cycle'),
    'awaiting_observation': ('grey', 'waiting for its last submission to activate'),
    'idle_capacity': ('amber', 'capacity idle while work is queued: the queue is not '
                               'pulling it (worker supply or site, not the front)'),
    'degraded': ('red', 'held, a gate is red'),
    'half_open': ('amber', 'trying one task after a gate cleared'),
    'held': ('grey', 'held'),
    'oversize': ('amber', 'the next task alone is too large, waiting for the operator'),
    'no_work': ('grey', 'no eligible task in the ready buffer'),
}
FRONT_REASONS = {
    'queue_off': 'its feed is off', 'front_off': 'the front is off',
    'declared': 'a declared downtime', 'would_exceed_high': 'a feed would pass the high mark',
    'task_cap': 'the task cap', 'job_cap': 'the job cap',
    'unsized_task': 'a task that cannot be sized', 'no_calibration': 'no calibration yet',
    'canary': 'the canary is failing', 'burn_through': 'jobs burning through',
    'failure_window': 'the failure rate over its threshold', 'credential': 'a credential',
}
PART_NAMES = {'prod_activity': 'jobs', 'canary': 'canary',
              'snapper_errors': 'failures', 'pilot_supply': 'pilot supply'}
LEVEL_ORDER = {'red': 0, 'amber': 1, 'grey': 2, 'green': 3}
# Queues production no longer uses, left out of the report by name (Torre,
# 2026-10-22): BNL_OSG_EPIC_PROD_1 since production moved to BNL_OSG_PanDA_1
# on 2026-10-21 (swf-epicprod OSG_SUBMISSION.md).
NOT_IN_PRODUCTION = ('BNL_OSG_EPIC_PROD_1',)
# The alarm engine's titles: 'task 40962 (failed) final-failure rate 100.0% —
# <task name>' and a ping's 'due in 7 days (2026-10-28): <what>'.
TASK_ALARM_RE = re.compile(r'^task (\d+) \(([^)]*)\) final-failure rate ([\d.]+%) — (.*)$')
DUE_RE = re.compile(r'^due in (\d+) days? \((\d{4}-\d\d-\d\d)\): (.*)$')


def _active_alarms():
    """The alarm engine's events not cleared (docs/alarms.md)."""
    from monitor_app.alarms_data import CONTEXT_NAME
    from monitor_app.models import Entry
    rows = (Entry.objects.filter(context_id=CONTEXT_NAME, kind='event',
                                 archived=False, deleted_at__isnull=True)
            .order_by('-timestamp_created')[:500])
    return [{'id': str(e.id), 'title': e.title or (e.data or {}).get('subject') or '',
             'severity': (e.data or {}).get('severity') or 'alarm'}
            for e in rows if (e.data or {}).get('clear_time') is None]


def _assessment_freshness():
    """Whether the scheduled campaign assessments arrived (the System
    page's campaign-assessments row). The verdicts themselves are held
    by corun-ai, which this DB-only build does not call."""
    from swf_epicprod.assessment.freshness import assessment_freshness
    status, summary, _data = assessment_freshness()
    return {'status': status, 'summary': summary}


def _latest_daily_assessment():
    """The newest daily campaign assessment (swf-epicprod
    EPICPROD_ASSESSMENTS.md), from its ``assessment_register`` record: the
    verdict, the verdict as a health level, and the assessment's page."""
    from monitor_app.models import AppLog
    from swf_epicprod.assessment.freshness import DAILY_KINDS
    rec = (AppLog.objects.filter(app_name='epicprod', extra_data__action='assessment_register',
                                 extra_data__subject_type='campaign',
                                 extra_data__assessment_kind__in=DAILY_KINDS)
           .order_by('-timestamp').values('timestamp', 'extra_data').first())
    if not rec:
        return None
    x = rec['extra_data'] or {}
    path = x.get('url') or x.get('report_path')
    verdict = x.get('verdict') or ''
    return {'verdict': verdict, 'level': VERDICT_LEVELS.get(verdict, 'grey'),
            'at': rec['timestamp'].isoformat(),
            'link': f'{PUBLIC}{path}' if path else None,
            'campaign': x.get('subject_key'), 'title': x.get('report_title'),
            'narration': x.get('narration')}


def _live_records():
    """The epicprod-live screen: the newest warning and error records of
    the live stream, the logs page's live view (ACTION_STREAM.md), each
    with its own page. The room reads the record anonymously and the logs
    page is behind the login wall, so the records ride here. Off the
    screen: a record a ``live_cleared`` record names, and a failure whose
    action has since run ok (COMP_SNAPSHOT.md, Live)."""
    from monitor_app.epicprod_logging import EPICPROD_APP_NAME, live_stream_q
    from monitor_app.live_notices import FAILURE_OUTCOMES, failure_key
    from monitor_app.models import AppLog
    cleared = set()
    for ids in (AppLog.objects
                .filter(app_name=EPICPROD_APP_NAME,
                        extra_data__action='live_cleared')
                .values_list('extra_data__cleared_ids', flat=True)):
        cleared.update(int(i) for i in ids or ())
    rows = (AppLog.objects.filter(live_stream_q(), levelname__in=LIVE_LEVELS)
            .exclude(id__in=cleared)
            .order_by('-timestamp')
            .values('id', 'timestamp', 'levelname', 'message', 'instance_name',
                    'extra_data'))
    last_ok = {}
    items = []
    for r in _pages(rows):
        extra = r['extra_data'] or {}
        if str(extra.get('outcome') or '') in FAILURE_OUTCOMES:
            key = failure_key(r['instance_name'], extra)
            if key not in last_ok:
                last_ok[key] = _last_ok(r['instance_name'], extra, key)
            if last_ok[key] and last_ok[key] > r['timestamp']:
                continue
        items.append({'id': r['id'], 'at': r['timestamp'].isoformat(),
                      'level': r['levelname'], 'importance': extra.get('sublevel'),
                      'text': r['message'], 'link': f"{PUBLIC}/logs/{r['id']}/"})
        if len(items) == LIVE_ITEMS:
            break
    return {'link': f'{PUBLIC}/logs/?app_name=epicprod&live=1', 'items': items}


def _last_ok(instance_name, extra, key):
    """Time of the newest ok record carrying a failure's identity
    (live_notices.failure_key), or None: the recovery the epicprod-live
    channel announces."""
    from monitor_app.epicprod_logging import EPICPROD_APP_NAME
    from monitor_app.live_notices import failure_key
    from monitor_app.models import AppLog
    oks = AppLog.objects.filter(app_name=EPICPROD_APP_NAME,
                                instance_name=instance_name,
                                extra_data__action=extra.get('action'),
                                extra_data__outcome='ok')
    for field in ('subject_type', 'subject_key', 'source', 'operation'):
        if extra.get(field) is not None:
            oks = oks.filter(**{f'extra_data__{field}': extra[field]})
    for ok in _pages(oks.order_by('-timestamp').values('timestamp', 'extra_data')):
        if failure_key(instance_name, ok['extra_data'] or {}) == key:
            return ok['timestamp']
    return None


def _pages(queryset, size=100):
    """Rows of an ordered queryset in LIMIT/OFFSET pages: each page is an
    index scan, where a server-side cursor plans a sort of the whole set."""
    start = 0
    while True:
        page = list(queryset[start:start + size])
        yield from page
        if len(page) < size:
            return
        start += size


def _jlab_pending():
    """What JLab Rucio is still owed: the failover stash's entries not yet
    home, from the drain's own record (the cached product ``stash_state``,
    swf-epicprod RUCIO_FAILOVER_STASH.md), and the registrations the
    registrar's last pass deferred, from its action record."""
    from monitor_app.models import AppLog, CachedProduct
    with connections['default'].cursor() as cursor:
        # counted in the database: after an outage the stash holds tens of
        # thousands of entries
        cursor.execute(f'''
            SELECT value ->> 'built_at',
                   (SELECT COUNT(*) FROM jsonb_array_elements(
                      CASE WHEN jsonb_typeof(value -> 'entries') = 'array'
                           THEN value -> 'entries' ELSE '[]'::jsonb END) e
                     WHERE e ->> 'outcome' IS DISTINCT FROM 'home')
            FROM {CachedProduct._meta.db_table} WHERE key = 'stash_state' ''')
        row = cursor.fetchone()
    stash, stash_at = (int(row[1]), row[0]) if row else (None, None)
    rec = (AppLog.objects.filter(app_name='epicprod', extra_data__action='registrar',
                                 extra_data__has_key='deferred')
           .order_by('-timestamp').values('timestamp', 'extra_data').first())
    deferred = int(rec['extra_data'].get('deferred') or 0) if rec else None
    known = [n for n in (stash, deferred) if n is not None]
    times = [t for t in (stash_at, rec['timestamp'].isoformat() if rec else None) if t]
    return {'pending': sum(known) if known else None,
            'parts': {'stash': stash, 'registrar': deferred},
            'at': min(times) if times else None}


def _log_pending():
    """The logs pilots held in the stage-out bucket that are not yet in BNL
    Rucio, from the hourly log-rescue pass's record: those held over its
    window less those it moved (swf-epicprod LOG_STAGEOUT_FALLBACK.md,
    Recovery)."""
    from monitor_app.models import AppLog
    rec = (AppLog.objects.filter(app_name='epicprod', extra_data__action='log_rescue',
                                 extra_data__has_key='held')
           .order_by('-timestamp').values('timestamp', 'extra_data').first())
    if not rec:
        return None
    held = int(rec['extra_data'].get('held') or 0)
    moved = int(rec['extra_data'].get('moved') or 0)
    return {'pending': max(0, held - moved), 'parts': {'held': held, 'moved': moved},
            'at': rec['timestamp'].isoformat()}


def _storage_capacity():
    """Each JLab disk RSE's usage against the production account's limit,
    from the latest storage component (swf-epicprod STORAGE.md, per RSE
    capacity), and the production output RSE's alone."""
    from monitor_app.snapper_storage import _current_data
    data = _current_data()
    if not data:
        raise LookupError('no storage component published')
    rses = []
    for name, r in sorted((data.get('rses') or {}).items()):
        c = (r or {}).get('capacity') or {}
        # the fraction is the account's usage over its limit where the
        # account figure is present, else the RSE-wide usage
        account = c.get('account_used') is not None
        rses.append({'id': name.lower(), 'name': name, 'type': (r or {}).get('type'),
                     'used_bytes': c.get('account_used') if account else c.get('used'),
                     'limit_bytes': c.get('limit'), 'fraction': c.get('fraction'),
                     'files': c.get('account_files') if account else c.get('files'),
                     'as_of': c.get('as_of')})
    output = next(({'rse': r['name'], 'used_bytes': r['used_bytes'], 'limit_bytes': r['limit_bytes'],
                    'fraction': r['fraction'], 'as_of': r['as_of']}
                   for r in rses if r['name'] == OUTPUT_RSE), None)
    return {'output': output, 'rses': rses}


def _report_items(front, sites, storage, alarms, assessment):
    """The report as items (docs/COMP_SNAPSHOT.md, Report): what the room's
    floor cannot show, each one line with a level and the page to open.
    No per-site counts: the pillars carry those."""
    items = []
    if not front.get('enabled'):
        items.append({'level': 'red', 'text': 'Pressure front: off, nothing is fed',
                      'link': front.get('link')})
    elif front.get('ready_tasks') == 0:
        items.append({'level': 'grey', 'text': 'Pressure front: no task is ready to release',
                      'link': front.get('link')})
    for s in sites:
        live = [q for q in s['queues'] if q['status'] == 'live']
        if not live:
            reason = next((q['status_reason'] for q in s['queues'] if q['status_reason']), '')
            items.append({'level': 'grey',
                          'text': f"{s['name']}: {'off' if not reason else reason}",
                          'link': (s.get('links') or {}).get('panda_queue')})
            continue
        for q in live:
            f = q.get('front') or {}
            if q['id'] in NOT_IN_PRODUCTION:
                continue
            level, words = FRONT_STATES.get(f.get('state'), ('grey', f.get('state') or ''))
            if f.get('state') and f.get('state') not in ('supplied', 'fed'):
                reason = FRONT_REASONS.get(f.get('reason'), '')
                items.append({'level': level,
                              'text': f"Front, {q['id']}: {words}{', ' + reason if reason else ''}",
                              'link': front.get('link')})
            for name, p in q['health']['parts'].items():
                if p['level'] not in ('amber', 'red'):
                    continue
                link = (q['links'].get('slots') if name == 'pilot_supply' else
                        q['links'].get('canary') if name == 'canary' else
                        q['links'].get('snapper'))
                items.append({'level': p['level'],
                              'text': f"{q['id']} {PART_NAMES.get(name, name)}: {p['note']}",
                              'link': link})
    seen = set()
    for a in alarms or []:  # newest first
        title = a['title']
        # one line per task: the failure-rate alarms fire per task, and
        # several alarms (and older events) can name the same one
        task = TASK_ALARM_RE.match(title)
        key = ('task', task.group(1)) if task else ('title', title)
        if key in seen:
            continue
        seen.add(key)
        # a ping is a dated obligation, an alarm only once overdue (PINGS.md)
        ping = a.get('severity') == 'ping'
        due = DUE_RE.match(title) if ping else None
        if task:
            number, state, rate, name = task.groups()
            text = f'Final failures {rate}, task {number} ({state}): {name}'
        elif due:
            days, date, what = due.groups()
            text = f'Due {date} ({days} days): {what}'
        else:
            text = f"{'Due' if ping else 'Alarm'}: {title.replace(' — ', ': ')}"
        items.append({'level': 'amber' if ping else 'red', 'text': text,
                      'link': f"{PUBLIC}/alarms/events/{a['id']}/"})
    for st in storage:
        for name, p in st['health']['parts'].items():
            if p['level'] in ('amber', 'red'):
                items.append({'level': p['level'], 'text': f"{st['name']}: {p['note']}",
                              'link': (st.get('links') or {}).get('storage')})
    if assessment:
        level = {'ok': 'green', 'green': 'green', 'warn': 'amber', 'warning': 'amber',
                 'amber': 'amber', 'fail': 'red', 'error': 'red', 'red': 'red'}.get(
                     assessment['status'], 'grey')
        items.append({'level': level,
                      'text': f"Campaign assessment: {assessment['summary']}",
                      'link': f'{PUBLIC}/ai/assessments/?subject_type=campaign_task,campaign'})
    items.sort(key=lambda i: LEVEL_ORDER.get(i['level'], 2))
    return items


def get_snapshot(refresh=False):
    """The stored record, rebuilt behind the request when older than the
    TTL (docs/CACHED_PRODUCTS.md)."""
    from monitor_app.cached_product import get_product
    result = get_product(PRODUCT_KEY, build, TTL_SECONDS, refresh=refresh)
    value = dict(result.get('value') or {})
    value['age_seconds'] = result.get('age_seconds')
    value['refreshing'] = result.get('refreshing')
    return value
