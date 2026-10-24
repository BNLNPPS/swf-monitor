"""The comp snapshot: the computing centre room's live record
(docs/COMP_SNAPSHOT.md).

One builder over the PanDA and swf databases, served as the cached
product ``comp_snapshot:v1`` (docs/CACHED_PRODUCTS.md) by REST at
``panda/comp/snapshot.json`` and by the MCP tool ``swf_comp_snapshot``.
No remote call and no LLM: the build reads tables only. A part that
cannot be read is listed in ``errors`` and left out, never shown as zero.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode

from django.db import connections
from django.utils import timezone

from monitor_app.short_names import short_name
from monitor_app import error_corrections
from monitor_app.panda.constants import ERROR_COMPONENTS
from monitor_app.panda.queries import INCOMPLETE_REGISTRATION_OUTCOMES
from monitor_app.panda.error_labels import category_label
from monitor_app.snapper_errors import _classify_sql

logger = logging.getLogger(__name__)

PRODUCT_KEY = 'comp_snapshot:v1'
TTL_SECONDS = 60
SCHEMA = 'comp-snapshot/1'
PUBLIC = 'https://epic-devcloud.org/prod'

# The room's site groups, seeded into SysConfig ``comp.sites`` on first
# read so a site is added or regrouped without a deploy. Each site's queues
# and their status are the queue registry's (swf-epicprod
# docs/QUEUE_REGISTRY.md): a status there other than production overrides
# what PanDA says, and a retired queue leaves the room.
DEFAULT_SITES = [
    {'id': 'osg', 'name': 'OSG', 'execute_sites': True},
    {'id': 'grex', 'name': 'GREX'},
    {'id': 'compute-canada', 'name': 'Compute Canada'},
    {'id': 'nersc', 'name': 'NERSC'},
    {'id': 'google', 'name': 'Google'},
    {'id': 'jlab', 'name': 'JLab'},
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
REGISTRATION_OWED = INCOMPLETE_REGISTRATION_OUTCOMES

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
# The floor's trend plots: twelve hours (Torre, 2026-10-22) in quarter-hour
# bins, fine enough to show a ramp or a dip.
TREND_HOURS = 12
TREND_BIN_MINUTES = 15
# A state sample older than this before a bin end is not the state then: the
# PanDA reading refreshes every five minutes.
TREND_STALE_MINUTES = 15
# The JLab output flow's kept bins, and how many before the newest kept one
# each build reads again (jobs reach the archive shortly after they end).
JLAB_FLOW_KEY = 'jlab_flow:v1'
JLAB_FLOW_REREAD_BINS = 2
JLAB_FLOW_BACKFILL_BINS = 4
# The floor is the control room: live-now records only, the last 12 hours;
# the logs page holds the history (Torre, 2026-10-22).
LIVE_WINDOW_H = 12


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


# A PanDA task status after which the task delivers nothing more.
TASK_ENDED = ('done', 'finished', 'failed', 'broken', 'aborted', 'exhausted')

# PanDA keeps a job's destinationsite in 36 characters, so a longer
# GLIDEIN_Site arrives cut ('Center for Advanced Research Computi').
PANDA_SITE_CHARS = 36


def _uncut_site(name):
    """An execute site's full name: one PanDA cut at its 36 characters is
    restored from the full names the short-name table knows; any other
    name passes unchanged."""
    from monitor_app.short_names import SHORT_NAMES
    if not name or len(name) != PANDA_SITE_CHARS:
        return name
    full = [n for n in SHORT_NAMES if len(n) > PANDA_SITE_CHARS and n.startswith(name)]
    return full[0] if len(full) == 1 else name


def _task_pcs(tasks):
    """Each PanDA task's physics configuration as PCS records it:
    ``{jeditaskid: (pc label, one-line physics)}``, the line PCS's summary
    (process, beams, Q², generator, sample, background, requesters).
    ``tasks`` is a set of (jeditaskid, taskname); a task PCS links nowhere
    is found by the composed identity its name carries, and one PCS does
    not know is left out. Local reads only."""
    try:
        from pcs.models import Dataset, PandaTasks
        from pcs.name_tokens import logical_name_from_physical_name
        from pcs.physics_config import summary
    except ImportError:
        return {}
    related = ('physics_config__physics_tag', 'physics_config__background_tag')
    out = {}
    for link in (PandaTasks.objects.filter(jedi_task_id__in={t for t, _ in tasks})
                 .select_related(*(f'prod_task__dataset__{r}' for r in related))):
        ds = link.prod_task.dataset if link.prod_task.dataset_id else None
        if ds is not None and ds.physics_config_id:
            out[link.jedi_task_id] = (ds.physics_config.label, summary(ds.physics_config))
    for tid, name in tasks:
        if tid in out:
            continue
        ds = (Dataset.objects.filter(composed_name=logical_name_from_physical_name(name or ''))
              .select_related(*related).first())
        if ds is not None and ds.physics_config_id:
            out[tid] = (ds.physics_config.label, summary(ds.physics_config))
    return out


def _task_physics(tasks):
    """What each PanDA task simulates, its tags read as physics:
    ``{jeditaskid: one-line physics}`` (_task_pcs)."""
    return {tid: line for tid, (_pc, line) in _task_pcs(tasks).items()}


def _physics_label(line):
    """The physics in a row's width: PCS's summary without its tail
    (generator, sample, requesters), the nucleus its beam species already
    names, and the units ('DIS Au197 eAu 9x100 GeV Q² 10–100 · beagle ...'
    -> 'DIS eAu 9x100 Q² 10–100')."""
    import re
    head = (line or '').split(' · ', 1)[0].replace('_', ' ').replace(' GeV', '')
    return re.sub(r'\b[A-Z][a-z]?\d{1,3}\s(?=e[A-Z])', '', head).strip()


def _plan_progress():
    """The campaign plan's delivered and target events per physics
    configuration, from the stored completion record
    (``prod_hub_campaign_completion``) as it stands: never built here."""
    from monitor_app.models import CachedProduct
    row = CachedProduct.objects.filter(key='prod_hub_campaign_completion').values('value').first()
    out = {}
    for entry in ((row or {}).get('value') or []):
        for pc in entry.get('pcs') or []:
            if pc.get('pc'):
                out[pc['pc']] = {'target': pc.get('target'),
                                 'delivered_events': pc.get('delivered_events')}
    return out


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
        """The last hour's failures per queue by Snapper error category,
        largest first: component x code, the job's first nonzero error
        component in ERROR_COMPONENTS order (SNAPPER_ERRORS.md), labelled
        from the code catalogs and read through the error-correction root
        (ERROR_ATTRIBUTION.md), so a label a rule marks unreliable shows
        its corrected reading. A failed job with no error code in any of
        the seven components reads as such; nothing reads "unattributed"
        (Torre, 2026-10-23: the OSG failures of 41039 and 41040 were DDM
        200, the log-name collision, and the room called them that)."""
        comp_case, code_case, diag_case = _classify_sql()
        fields = ', '.join(f'"{c["code"]}", "{c["diag"]}"' for c in ERROR_COMPONENTS)
        sql = f'''
            SELECT computingsite, component, code, MIN(diag), transexitcode, COUNT(*) FROM (
              SELECT computingsite, transexitcode,
                     CASE {comp_case} ELSE 'none' END AS component,
                     CASE {code_case} ELSE 0 END AS code,
                     CASE {diag_case} ELSE '' END AS diag
                FROM (
                  SELECT computingsite, transexitcode, {fields} FROM "{self.S}"."jobsactive4"
                   WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'failed'
                  UNION ALL
                  SELECT computingsite, transexitcode, {fields} FROM "{self.S}"."jobsarchived4"
                   WHERE endtime >= %s AND processingtype = 'epicproduction' AND jobstatus = 'failed') j) t
            GROUP BY 1, 2, 3, 5'''
        cats = {}
        for site, component, code, diag, exitcode, n in _rows(sql, (self.hour_ago, self.hour_ago)):
            cat = cats.setdefault((site, component, int(code or 0)),
                                  {'n': 0, 'diag': diag or '', 'exits': {}})
            cat['n'] += int(n)
            if exitcode not in (None, ''):
                cat['exits'][str(exitcode)] = cat['exits'].get(str(exitcode), 0) + int(n)
        out = {}
        for (site, component, code), cat in cats.items():
            if component == 'none':
                label = 'failed with no error code in any component'
            else:
                label = category_label(component, code)
                try:
                    rule = error_corrections.match(component, code, cat['diag'], sites=[site])
                    if rule is not None:
                        label = error_corrections.correction(rule, cat['exits'])['label']
                except Exception as e:  # a correction must never take the room down
                    logger.error('error-correction read failed for %s %s: %s', component, code, e)
            out.setdefault(site, []).append((label, cat['n']))
        for site in out:
            out[site].sort(key=lambda t: -t[1])
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

    def floor_tasks(self, site_name, alarms, site_of):
        """The task board (COMP_SNAPSHOT.md, Tasks): every production task
        with jobs running, starting or queued at the floor's queues, newest
        first; a task with none there is off the board."""
        from monitor_app.panda.census import (NOT_STARTED, RUNNING,
                                              TASK_TERMINAL_STATUSES,
                                              ungenerated_by_task)
        from pcs.models import Dataset
        from pcs.views import physics_description
        terminal = list(TASK_TERMINAL_STATUSES)
        sql = f'''
            SELECT a.jeditaskid, t.taskname, t.status,
                   COALESCE(t.starttime, t.creationdate),
                   SUM(CASE WHEN a.jobstatus = ANY(%s) THEN 1 ELSE 0 END), COUNT(*),
                   MODE() WITHIN GROUP (ORDER BY a.computingsite),
                   ARRAY_AGG(DISTINCT a.computingsite)
            FROM "{self.S}"."jobsactive4" a
            JOIN "{self.S}"."jedi_tasks" t ON t.jeditaskid = a.jeditaskid
            WHERE a.jobstatus = ANY(%s)
              AND t.processingtype = 'epicproduction'
              AND (t.status IS NULL OR NOT t.status = ANY(%s))
            GROUP BY a.jeditaskid, t.taskname, t.status, t.starttime, t.creationdate'''
        # Every production task in flight is on the board wherever it is
        # placed, by the same rules as any other (Torre Wenaus, 2026-10-22,
        # when two tasks sat on a queue taken off the floor).
        rows = _rows(sql, (list(RUNNING), list(NOT_STARTED + RUNNING), terminal))
        # A task with rows JEDI has not yet made into jobs is on the board
        # though none of its jobs waits or runs now.
        ungenerated = {tid: (queue, n) for tid, (queue, n) in ungenerated_by_task().items()
                       if n > 0}
        to_generate = {tid: n for tid, (_queue, n) in ungenerated.items()}
        waiting = sorted(set(to_generate) - {int(r[0]) for r in rows})
        if waiting:
            sql = f'''
                SELECT jeditaskid, taskname, status, COALESCE(starttime, creationdate),
                       0, 0, site, NULL
                FROM "{self.S}"."jedi_tasks" WHERE jeditaskid = ANY(%s)'''
            rows = list(rows) + list(_rows(sql, (waiting,)))
        ids = [int(r[0]) for r in rows]
        files = {}
        if ids:
            sql = f'''SELECT jeditaskid, SUM(nfiles), SUM(nfilesfinished), SUM(nfilesfailed)
                      FROM "{self.S}"."jedi_datasets"
                      WHERE jeditaskid = ANY(%s) AND masterid IS NULL
                        AND type IN ('input', 'pseudo_input')
                      GROUP BY jeditaskid'''
            files = {int(t): (int(n or 0), int(f or 0), int(x or 0))
                     for t, n, f, x in _rows(sql, (ids,))}
        alarmed = {int(m.group(1)) for m in (TASK_ALARM_RE.match(a['title']) for a in alarms) if m}
        names = {r[1]: re.sub(r'\.try\d+$', '', r[1]) for r in rows}
        datasets = {d.composed_name: d for d in
                    Dataset.objects.filter(composed_name__in=set(names.values()))
                    .select_related('physics_tag', 'evgen_tag')
                    .order_by('composed_name', 'block_num').distinct('composed_name')}
        items = []
        for tid, name, status, at, running, active, queue, queues in sorted(
                rows, key=lambda r: r[3], reverse=True):
            tid, running, active = int(tid), int(running or 0), int(active or 0)
            # The floor's queues the task has jobs at, and the queue of its
            # rows still to generate.
            queues = sorted(set(queues or ())
                            | ({ungenerated[tid][0]} if tid in ungenerated else set()))
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=dt_timezone.utc)  # PanDA stores UTC
            n, done, lost = files.get(tid, (0, 0, 0))
            d = datasets.get(names[name])
            physics = physics_description(d, concise=True) if d else ''
            lost_pct = 100.0 * lost / (done + lost) if done + lost else 0.0
            local = at.astimezone(ZoneInfo('America/New_York')) if at else None
            detail = (' at '.join(x for x in (physics, site_name.get(queue, queue)) if x)
                      + f". {name}, {status} on {queue}"
                      + (f" since {local:%m-%d %H:%M} ET" if local else '')
                      + f"; {done:,} of {n:,} files done, {lost:,} lost after retries"
                      + (f" ({lost_pct:.1f}%), in alarm" if tid in alarmed else '') + '.')
            items.append({
                'id': tid, 'at': at.isoformat() if at else None,
                'level': ('red' if tid in alarmed else 'amber' if lost
                          else 'green' if running else 'grey'),
                'title': str(tid), 'name': name,
                'counts': {'ungenerated': to_generate.get(tid, 0),
                           'queued': active - running, 'running': running,
                           'finished': done, 'failed': lost},
                'detail': detail,
                'sites': sorted({site_of[q] for q in queues if q in site_of}),
                'queues': queues,
                'link': f'{PUBLIC}/panda/tasks/{tid}/'})
        return {'link': f'{PUBLIC}/panda/tasks/?processingtype=epicproduction',
                'items': items}

    def paused_tasks(self, site_name):
        """The paused pillar (COMP_SNAPSHOT.md, Paused): every production
        task paused in PanDA, wherever it is placed, oldest pause first.
        The pause time is the task's last state change."""
        sql = f'''SELECT t.jeditaskid, t.taskname, t.site,
                         COALESCE(t.statechangetime, t.modificationtime),
                         SUM(d.nfiles), SUM(d.nfilesfinished), SUM(d.nfilesfailed)
                  FROM "{self.S}"."jedi_tasks" t
                  LEFT JOIN "{self.S}"."jedi_datasets" d ON d.jeditaskid = t.jeditaskid
                       AND d.masterid IS NULL AND d.type IN ('input', 'pseudo_input')
                  WHERE t.status = 'paused' AND t.processingtype = 'epicproduction'
                  GROUP BY 1, 2, 3, 4 ORDER BY 4, 1'''
        items, oldest = [], None
        for tid, name, queue, since, n, done, lost in _rows(sql, ()):
            if since is not None and since.tzinfo is None:
                since = since.replace(tzinfo=dt_timezone.utc)  # PanDA stores UTC
            oldest = oldest or since
            local = since.astimezone(ZoneInfo('America/New_York')) if since else None
            n, done, lost = int(n or 0), int(done or 0), int(lost or 0)
            where = site_name.get(queue, queue) or 'no queue'
            items.append({
                'id': int(tid), 'name': name, 'queue': queue or '',
                'since': since.isoformat() if since else None,
                'link': f'{PUBLIC}/panda/tasks/{int(tid)}/',
                'detail': (f"{int(tid)} paused on {where}"
                           + (f" ({queue})" if queue and queue != where else '')
                           + (f" since {local:%m-%d %H:%M} ET" if local else '')
                           + f"; {done:,} of {n:,} files done, {lost:,} lost. {name}")})
        # The task list's window reaches back to the oldest pause.
        days = max(7, (self.now - oldest).days + 1) if oldest else 7
        listed = ', '.join(f"{i['id']} on {site_name.get(i['queue'], i['queue'])}"
                           + (f" since {datetime.fromisoformat(i['since']).astimezone(ZoneInfo('America/New_York')):%m-%d %H:%M} ET"
                              if i['since'] else '')
                           for i in items)
        return {'tasks': len(items),
                'link': f'{PUBLIC}/panda/tasks/?status=paused&processingtype=epicproduction&days={days}',
                'detail': (f"{len(items)} task{'s' if len(items) != 1 else ''} paused: {listed}"
                           if items else 'no production task is paused'),
                'items': items}

    def pc_arrivals(self, site_name, hours=24):
        """Science arriving at JLab over the last ``hours`` (24, as the
        completed tasks; Torre Wenaus, 2026-10-24), per physics
        configuration (COMP_SNAPSHOT.md, Arrivals): the RECO files the
        finished production jobs registered and their events, from each
        job's payload report, grouped by task and the task's PC. Newest
        arrival first. Each PC carries its physics in words and the plan's
        delivered and target events, read from the stored completion
        record without building it."""
        since = (self.now - timedelta(hours=hours)).astimezone(dt_timezone.utc).replace(tzinfo=None)
        sql = f'''SELECT j.jeditaskid, t.taskname, t.status, j.computingsite, COUNT(*),
                         SUM(COALESCE((m.metadata::jsonb -> 'payload' -> 'outputs' -> 'reco' ->> 'events')::bigint, 0)),
                         MIN(j.endtime), MAX(j.endtime)
                  FROM "{self.S}"."jobsarchived4" j
                  JOIN "{self.S}"."jedi_tasks" t ON t.jeditaskid = j.jeditaskid
                  LEFT JOIN "{self.S}"."metatable" m ON m.pandaid = j.pandaid
                  WHERE j.endtime >= %s AND j.processingtype = 'epicproduction'
                    AND j.jobstatus = 'finished' AND j.jobmetrics LIKE %s
                  GROUP BY 1, 2, 3, 4'''
        rows = _rows(sql, (since, '%payloadRegistration=registered%'))
        pcs = _task_pcs({(int(r[0]), r[1]) for r in rows})
        progress = _plan_progress()
        by_pc = {}
        for tid, name, status, queue, files, events, first, last in rows:
            pc, physics = pcs.get(int(tid), (None, ''))
            key = pc or f'task {int(tid)}'
            b = by_pc.setdefault(key, {'pc': pc, 'physics': physics, 'files': 0, 'events': 0,
                                       'tasks': set(), 'sites': set(), 'first': None, 'last': None,
                                       'running': False})
            # A configuration is finished when every task delivering to it
            # in the window has ended (Torre Wenaus, 2026-10-24: green on
            # the dashboard is for finished).
            b['running'] = b['running'] or status not in TASK_ENDED
            b['files'] += int(files or 0)
            b['events'] += int(events or 0)
            b['tasks'].add(int(tid))
            b['sites'].add(site_name.get(queue, queue) or queue or '')
            first, last = _aware(first), _aware(last)
            b['first'] = min(x for x in (b['first'], first) if x) if (b['first'] or first) else None
            b['last'] = max(x for x in (b['last'], last) if x) if (b['last'] or last) else None
        et = ZoneInfo('America/New_York')
        items = []
        for key, b in by_pc.items():
            plan = progress.get(b['pc']) or {}
            delivered, target = plan.get('delivered_events'), plan.get('target')
            # Finished for this view (Torre Wenaus, 2026-10-24): every task
            # delivering in the window has ended, or the target is met; past
            # the target is met, not a fault, and the detail says by how much.
            met = bool(target) and delivered is not None and delivered >= target
            finished = met or not b['running']
            sites = ', '.join(sorted(s for s in b['sites'] if s))
            items.append({
                'pc': b['pc'] or '', 'physics': b['physics'], 'label': _physics_label(b['physics']),
                'state': 'finished' if finished else 'running',
                'files': b['files'],
                'events': b['events'], 'tasks': sorted(b['tasks']), 'sites': sorted(s for s in b['sites'] if s),
                'first_at': b['first'].isoformat() if b['first'] else None,
                'last_at': b['last'].isoformat() if b['last'] else None,
                'delivered_events': delivered, 'target_events': target,
                'link': (f"{PUBLIC}/pcs/config/{b['pc']}/" if b['pc']
                         else f"{PUBLIC}/panda/tasks/{min(b['tasks'])}/"),
                'detail': (f"{b['pc'] or key}: {b['physics'] or 'physics not recorded in PCS'}. "
                           f"{b['files']:,} RECO file{'s' if b['files'] != 1 else ''}, "
                           f"{b['events']:,} events in {hours} h"
                           + (f", at {b['last'].astimezone(et):%H:%M} ET"
                              if b['first'] and b['first'] == b['last'] else
                              f", {b['first'].astimezone(et):%H:%M} to {b['last'].astimezone(et):%H:%M} ET"
                              if b['first'] and b['last'] else '')
                           + f", from task{'s' if len(b['tasks']) != 1 else ''} "
                           + ', '.join(str(t) for t in sorted(b['tasks']))
                           + (f" at {sites}" if sites else '') + '.'
                           + (f" Delivered {delivered:,} of {target:,} events ({100 * delivered / target:.0f}%)."
                              if delivered is not None and target else '')
                           + (' Target met.' if met else ' Finished.' if finished else ' Still running.'))})
        items.sort(key=lambda i: i['last_at'] or '', reverse=True)
        files = sum(i['files'] for i in items)
        events = sum(i['events'] for i in items)
        return {'pcs': len(items), 'files': files, 'events': events, 'window_hours': hours,
                'link': f'{PUBLIC}/snapper/epicprod/campaign/',
                'detail': (f"{len(items)} physics configuration{'s' if len(items) != 1 else ''} "
                           f"received {files:,} RECO files, {events:,} events at JLab in {hours} h"
                           if items else f'no science arrived at JLab in {hours} h'),
                'items': items}

    def completed_tasks(self, site_name, hours=24):
        """The production tasks that completed in the last ``hours``
        (COMP_SNAPSHOT.md, Completed): done (every input file finished) or
        finished (some lost), newest first, the completion time the
        task's end time."""
        since = self.now - timedelta(hours=hours)
        sql = f'''SELECT t.jeditaskid, t.taskname, t.site, t.status,
                         COALESCE(t.endtime, t.statechangetime),
                         SUM(d.nfiles), SUM(d.nfilesfinished), SUM(d.nfilesfailed)
                  FROM "{self.S}"."jedi_tasks" t
                  LEFT JOIN "{self.S}"."jedi_datasets" d ON d.jeditaskid = t.jeditaskid
                       AND d.masterid IS NULL AND d.type IN ('input', 'pseudo_input')
                  WHERE t.status IN ('done', 'finished') AND t.processingtype = 'epicproduction'
                    AND COALESCE(t.endtime, t.statechangetime) >= %s
                  GROUP BY 1, 2, 3, 4, 5 ORDER BY 5 DESC, 1 DESC'''
        items = []
        rows = _rows(sql, (since.astimezone(dt_timezone.utc).replace(tzinfo=None),))
        physics = _task_physics({(int(r[0]), r[1]) for r in rows})
        for tid, name, queue, state, at, n, done, lost in rows:
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=dt_timezone.utc)  # PanDA stores UTC
            local = at.astimezone(ZoneInfo('America/New_York')) if at else None
            n, done, lost = int(n or 0), int(done or 0), int(lost or 0)
            where = site_name.get(queue, queue) or 'no queue'
            what = physics.get(int(tid), '')
            items.append({
                'id': int(tid), 'name': name, 'queue': queue or '', 'status': state,
                'at': at.isoformat() if at else None, 'physics': what,
                'link': f'{PUBLIC}/panda/tasks/{int(tid)}/',
                'detail': (f"{int(tid)} {state} on {where}"
                           + (f" ({queue})" if queue and queue != where else '')
                           + (f" at {local:%m-%d %H:%M} ET" if local else '')
                           + f"; {done:,} of {n:,} files done, {lost:,} lost."
                           + (f" {what}." if what else '') + f" {name}")})
        return {'tasks': len(items), 'window_hours': hours,
                'link': f'{PUBLIC}/panda/tasks/?processingtype=epicproduction&days=1',
                'detail': (f"{len(items)} task{'s' if len(items) != 1 else ''} completed in {hours} h"
                           if items else f'no production task completed in {hours} h'),
                'items': items}

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

    def jlab_worker_outcomes(self):
        """Commissioning evidence, including failures before a pilot gets a job.

        Harvester can stamp starttime when it discovers a finished worker, so
        this deliberately reports statuses, not inferred starts or runtimes.
        """
        rows = _rows(f'''SELECT status, COUNT(*)
                        FROM "{self.S}"."harvester_workers"
                        WHERE computingsite = %s AND submittime >= %s
                        GROUP BY status''',
                     ('JLAB_PanDA_1', self.now - timedelta(hours=48)))
        counts = {status: int(n) for status, n in rows}
        latest = _rows(f'''SELECT workerid, status, diagmessage, submittime
                          FROM "{self.S}"."harvester_workers"
                          WHERE computingsite = %s ORDER BY submittime DESC LIMIT 1''',
                       ('JLAB_PanDA_1',))
        last = latest[0] if latest else None
        detail = ', '.join(f'{n} {status}' for status, n in sorted(counts.items()))
        reason = f'commissioning; workers submitted in 48 h: {detail or "none"}'
        if last:
            reason += f'; latest worker {last[0]}: {last[1]}'
            if last[2]:
                reason += f' ({str(last[2])[:240]})'
        return {'counts_48h': counts, 'latest_worker': last[0] if last else None,
                'latest_submitted_at': last[3].isoformat() if last and last[3] else None,
                'reason': reason,
                'level': 'red' if counts.get('failed') or counts.get('cancelled') else 'grey'}

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
        record = found.get('record') or {}
        pool = record.get('pool') or {}
        return {'admitted': pool.get('slots_admitted'), 'total': pool.get('slots_total'),
                'age_seconds': found.get('age_seconds'),
                'fair_share': _fair_share(record.get('negotiators'), found.get('age_seconds'))}

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
            label = _uncut_site(name) or 'not reported'
            out.append({
                'id': re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-'),
                'name': label,
                'short': short_name(label),
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

    def science_arrivals(self, since=None, until=None, bin_seconds=None):
        """Per queue and output kind, the science files the last hour's
        finished production jobs registered in JLab Rucio, with their bytes
        and events: each DID the payload report's registration lists,
        joined by file name to the output it reports. A clash registered
        under a derived name (outcome ``diverted``) is a registered file
        whose name matches no output, so it counts with kind ``other`` and
        no bytes. PanDA's own file table holds no science output, since
        production tasks register their outputs themselves. ``since`` and
        ``until`` set another window; ``bin_seconds`` keys the result by
        the end of the job's end-time bin (epoch seconds) in place of the
        queue."""
        key = (f'((FLOOR(EXTRACT(EPOCH FROM d.endtime) / {int(bin_seconds)}) + 1)'
               f' * {int(bin_seconds)})::bigint' if bin_seconds else 'd.site')
        sql = f'''
            WITH r AS (
              SELECT j.computingsite AS site, j.endtime, m.metadata::jsonb -> 'payload' AS p
              FROM "{self.S}"."metatable" m JOIN "{self.S}"."jobsarchived4" j ON j.pandaid = m.pandaid
              WHERE j.endtime >= %s AND j.endtime < %s
                AND j.processingtype = 'epicproduction' AND j.jobstatus = 'finished'
                AND (j.jobmetrics LIKE %s OR j.jobmetrics LIKE %s)),
            d AS (
              SELECT r.site, r.endtime, r.p, did FROM r, jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(r.p -> 'registration' -> 'dids') = 'array'
                     THEN r.p -> 'registration' -> 'dids' ELSE '[]'::jsonb END) did)
            SELECT {key}, COALESCE(o.key, 'other'), COUNT(*),
                   SUM((o.value ->> 'bytes')::bigint), SUM((o.value ->> 'events')::bigint)
            FROM d LEFT JOIN LATERAL (
              SELECT o.key, o.value FROM jsonb_each(
                CASE WHEN jsonb_typeof(d.p -> 'outputs') = 'object'
                     THEN d.p -> 'outputs' ELSE '{{}}'::jsonb END) o
              WHERE d.did LIKE '%%/' || (o.value ->> 'file') LIMIT 1) o ON true
            GROUP BY 1, 2'''
        out = {}
        for k, kind, n, nbytes, events in _rows(sql, (since or self.hour_ago, until or self.now,
                                                      '%payloadRegistration=registered%',
                                                      '%payloadRegistration=diverted%')):
            out.setdefault(int(k) if bin_seconds else (k or ''), {})[kind] = (
                int(n), int(nbytes or 0), int(events or 0))
        return out

    def registration_outcomes(self, since=None, until=None, bin_seconds=None):
        """Per queue, the last hour's finished production jobs by the
        payload's registration outcome (``payloadRegistration``), and the
        end time of the newest that registered. ``since``, ``until`` and
        ``bin_seconds`` as for ``science_arrivals``."""
        key = (f'((FLOOR(EXTRACT(EPOCH FROM endtime) / {int(bin_seconds)}) + 1)'
               f' * {int(bin_seconds)})::bigint' if bin_seconds else 'computingsite')
        sql = f'''
            SELECT {key}, substring(jobmetrics from 'payloadRegistration=([A-Za-z_]+)'),
                   COUNT(*), MAX(endtime)
            FROM "{self.S}"."jobsarchived4"
            WHERE endtime >= %s AND endtime < %s
              AND processingtype = 'epicproduction' AND jobstatus = 'finished'
            GROUP BY 1, 2'''
        out, last = {}, None
        for k, outcome, n, end in _rows(sql, (since or self.hour_ago, until or self.now)):
            out.setdefault(int(k) if bin_seconds else (k or ''), {})[outcome or ''] = int(n)
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

    def jlab_flow(self, edges):
        """The JLab output flow on the trends axis (COMP_SNAPSHOT.md,
        Storage): per bin, the science files finished production jobs
        registered and their bytes, and the jobs whose registration did not
        complete, grouped by the job's end, through the hour's own
        reducers. A bin's jobs are final once archived, so the bins are
        kept (cached product ``jlab_flow:v1``) and each build reads again
        the JLAB_FLOW_REREAD_BINS bins before the newest kept one, and
        backfills JLAB_FLOW_BACKFILL_BINS of the window's bins not yet
        kept: a read of the whole window at once takes minutes. A bin not
        yet read is null."""
        from .models import CachedProduct

        width = int((edges[1] - edges[0]).total_seconds())
        ends = [int(e.timestamp()) for e in edges[1:]]
        row = CachedProduct.objects.filter(key=JLAB_FLOW_KEY).first()
        kept = {int(k): v for k, v in ((row.value or {}).get('bins') or {}).items()} if row else {}
        first = int(edges[0].timestamp())
        # Spans (start, end] of bin ends to read: the newest bins again, and
        # one hour of the window's bins not yet kept, before the oldest kept.
        if kept:
            spans = [(max(first, max(kept) - JLAB_FLOW_REREAD_BINS * width), ends[-1])]
            oldest = min(t for t in kept if t >= ends[0]) if any(t >= ends[0] for t in kept) else ends[-1]
            if oldest - width > first:
                spans.append((max(first, oldest - width - JLAB_FLOW_BACKFILL_BINS * width),
                              oldest - width))
        else:
            spans = [(max(first, ends[-1] - JLAB_FLOW_BACKFILL_BINS * width), ends[-1])]
        for start, end in spans:
            since = datetime.fromtimestamp(start, dt_timezone.utc)
            until = datetime.fromtimestamp(end, dt_timezone.utc)
            arrivals = self.science_arrivals(since=since, until=until, bin_seconds=width)
            outcomes, _last = self.registration_outcomes(since=since, until=until,
                                                         bin_seconds=width)
            for t in ends:
                if start < t <= end:
                    kinds = arrivals.get(t, {}).values()
                    kept[t] = [sum(n for n, _b, _e in kinds), sum(b for _n, b, _e in kinds),
                               sum(outcomes.get(t, {}).get(k, 0) for k in REGISTRATION_OWED)]
        kept = {t: v for t, v in kept.items() if t >= ends[0]}
        from .cached_product import stores
        if stores():
            CachedProduct.objects.update_or_create(
                key=JLAB_FLOW_KEY, defaults={'value': {'bins': {str(t): v for t, v in kept.items()}},
                                             'built_at': timezone.now()})
        column = {t: v for t, v in kept.items()}
        return {
            'files': [column[t][0] if t in column else None for t in ends],
            'bytes': [column[t][1] if t in column else None for t in ends],
            'registration_incomplete_jobs': [column[t][2] if t in column else None for t in ends],
        }

    def storage(self, site_of, edges=None):
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
        flow = self.part('jlab_flow', lambda: self.jlab_flow(edges), None) if edges else None

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
             'flow': flow,
             'by_site': site_rows(jlab_sites),
             'capacity': (capacity or {}).get('output'),
             'rses': (capacity or {}).get('rses') or [],
             'health': _health({'registration': reg_part, 'latest_arrival': arrival}),
             'links': {'storage': f'{PUBLIC}/panda/storage-doors/',
                       'arrived': f'{PUBLIC}/snapper/epicprod/storage/',
                       'failed': incomplete_registration_link(self.now),
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
    """The site groups of ``comp.sites``, each with its queues from the
    queue registry in the registry's order, shown with the registry's
    status; a registry site with no group is shown under its own id."""
    from monitor_app.models import SysConfig
    from swf_epicprod import queues as registry
    value = SysConfig.get_setting('comp.sites', DEFAULT_SITES)
    groups = [g for g in (value if isinstance(value, list) else DEFAULT_SITES)
              if isinstance(g, dict) and g.get('id')]
    by_site = registry.room_queues()
    out = []
    for g in [*groups, *({'id': s, 'name': s} for s in by_site if s not in {g['id'] for g in groups})]:
        site = {k: v for k, v in g.items() if k != 'queues'}
        site['queues'] = [{'id': e['queue'], 'status': registry.ROOM_STATUS.get(e['status']),
                           'status_reason': e['reason']} for e in by_site.get(g['id'], [])]
        out.append(site)
    return out


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
    drift = b.part('queue_drift', _queue_drift, [])
    jlab_workers = (b.part('jlab_workers', b.jlab_worker_outcomes, None)
                    if 'JLAB_PanDA_1' in names else None)

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
            if q == 'JLAB_PanDA_1' and status != 'live':
                reason = (jlab_workers or {}).get('reason',
                           'commissioning; worker status unavailable')

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
                if q == 'JLAB_PanDA_1':
                    health = _health({'status': _part(
                        (jlab_workers or {}).get('level', 'amber'), status, reason)})

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
            site['fair_share'] = (pool or {}).get('fair_share')
            live = [q['id'] for q in queues if q['status'] == 'live']
            if live:
                site['execute_sites'] = b.part('execute_sites', lambda: b.execute_sites(live), [])
        sites.append(site)

    site_of = {q['id']: s['id'] for s in sites_cfg for q in s.get('queues', [])}
    trend = b.part('trend_series', lambda: _trend_series(sites), None)
    storage = b.part('storage', lambda: b.storage(
        site_of, edges=trend['edges'] if trend else None), [])

    ready = b.part('ready_tasks', lambda: _ready_tasks(), None)
    released = b.part('released', lambda: _released(b.hour_ago), None)
    front = {
        'ready_tasks': ready, 'released_1h': released,
        'enabled': cfg.get('front.enabled'), 'mode': cfg.get('front.mode'),
        'jedi_throttled': cfg.get('front.jedi_throttled'),
        'link': f'{PUBLIC}/panda/front/#ready',
        **(b.part('front_board', lambda: _front_board(ready, released), None) or {}),
    }

    alarms = b.part('alarms', b.production_alarms, [])
    site_name = {q['id']: short_name(s.get('name') or s['id'])
                 for s in sites_cfg for q in s.get('queues', [])}
    record = {
        'schema': SCHEMA,
        'observed_at': b.now.isoformat(),
        'units': {'capacity': 'cores', 'core_hours': 'core-hours in the last hour'},
        'pressure_front': front,
        'sites': sites,
        'storage': storage,
        'report': {'text': _report_text(front, sites, storage),
                   'items': _drift_items(drift) + _report_items(
                       front, sites, storage, alarms,
                       b.part('assessment', _assessment_freshness, None)),
                   'assessment': b.part('daily_assessment', _latest_daily_assessment, None)},
        'live': b.part('live', _live_records, None),
        'tasks': b.part('tasks', lambda: b.floor_tasks(site_name, alarms, site_of), None),
        'paused': b.part('paused', lambda: b.paused_tasks(site_name), None),
        'completed': b.part('completed', lambda: b.completed_tasks(site_name), None),
        'arrivals': b.part('arrivals', lambda: b.pc_arrivals(site_name), None),
        'trends': b.part('trends', lambda: _trends(trend), None),
        'errors': b.errors,
    }
    record['build_seconds'] = round(time.monotonic() - t0, 2)
    return record


def _ready_tasks():
    with connections['default'].cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM pcs_prod_task WHERE overrides ? 'queued'")
        return int(cursor.fetchone()[0])


def _front_board(ready, released):
    """What the pressure-front pillar shows on hover and opens on click
    (Torre Wenaus, 2026-10-22): the ready buffer task by task, in each
    queue's feed order, and each production queue's latest decision, from
    the state the front's cycle stored (cached product ``front_state``)."""
    from monitor_app.models import CachedProduct
    from swf_epicprod.front import STATE_KEY, closed_queues

    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    state = (row.value if row else None) or {}
    closed = closed_queues()
    items = []
    for queue, entries in (state.get('backlog') or {}).items():
        for order, e in enumerate(entries or [], 1):
            items.append({
                'task': e.get('task'), 'queue': None if queue == 'unplaced' else queue,
                'order': order, 'kind': e.get('kind'), 'by_hand': bool(e.get('hand')),
                'priority': e.get('level'), 'jobs': e.get('rows'),
                'eligible': not e.get('problems') and queue not in closed,
                'problems': list(e.get('problems') or [])
                + ([f'queue closed: {closed[queue]}'] if queue in closed else []),
                'link': f"{PUBLIC}/pcs/tasks/{e.get('task')}/"})
    queues = []
    for name, d in (state.get('queues') or {}).items():
        queues.append({
            'queue': name, 'state': d.get('state'), 'reason': d.get('reason'),
            'committed_h': d.get('committed_h'), 'h_low': d.get('h_low'),
            'h_high': d.get('h_high'), 'running': d.get('running'),
            'ready_total': d.get('ready_total'), 'ready_eligible': d.get('ready_eligible'),
            'last_feed_age_h': d.get('last_feed_age_h'), 'closed': name in closed})
    lines = [f"{ready if ready is not None else '?'} tasks ready, "
             f"{released if released is not None else '?'} released in the last hour"]
    for q in queues:
        waiting = [i for i in items if i['queue'] == q['queue']]
        if not waiting and q['closed']:
            continue
        lines.append(
            f"{short_name(q['queue'])}: {q['state'] or '?'}"
            + (f", {q['committed_h']:.1f} h committed of {q['h_low']:g}-{q['h_high']:g} h"
               if isinstance(q['committed_h'], (int, float)) and q['h_low'] is not None else '')
            + (f"; {len(waiting)} ready, next {waiting[0]['task']}" if waiting else '')
            + (' (closed)' if q['closed'] else ''))
    decided = {q['queue'] for q in queues}
    for queue in dict.fromkeys(i['queue'] for i in items if i['queue'] not in decided):
        waiting = [i for i in items if i['queue'] == queue]
        lines.append(f"{short_name(queue) or 'no queue yet'}: {len(waiting)} ready, next {waiting[0]['task']}"
                     + (' (queue closed, held)' if queue in closed else ''))
    return {'items': items, 'queues': queues, 'cycle_at': state.get('cycle_at'),
            'detail': '. '.join(lines) + '.'}


def _released(since):
    from monitor_app.models import AppLog
    return AppLog.objects.filter(app_name='epicprod', timestamp__gte=since,
                                 extra_data__action='front_decision',
                                 extra_data__outcome='fed').count()


def _fair_share_notice(share, link):
    """The notice shared by every display of the pool's fair-share state."""
    if not share or not share.get('limited'):
        return None
    pools = ' and '.join(share.get('pools') or ['Pool'])
    cycle = share.get('cycle_at')
    when = ''
    if cycle:
        try:
            at = datetime.fromisoformat(cycle.replace('Z', '+00:00'))
            when = f" ({at.astimezone(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M:%S} ET)"
        except (TypeError, ValueError):
            logger.warning('Unusable fair-share cycle time: %r', cycle)
    return {
        'text': 'fair share limited', 'level': 'amber', 'link': link,
        'detail': (f'{pools}: fair-share limit reached in the last reported '
                   f'negotiation cycle{when}. Existing pilots keep their slots; '
                   'later cycles may grant more.'),
    }


def _fair_share(negotiators, age_seconds):
    """Whether a pool's negotiator held our OSG submitter at its
    fair-share limit in its last cycle, from the submit host reporter's
    record (docs/OSG_SUBMIT_REPORTER.md).
    None when the record lacks the reading or is stale."""
    if (not isinstance(negotiators, dict) or not negotiators.get('negotiators')
            or age_seconds is None or age_seconds > REPORT_STALE_SECONDS):
        return None
    held = [n for n in negotiators['negotiators'] if n.get('share_limited')]
    return {'limited': bool(held),
            'pools': sorted({short_name(n['pool']) for n in held}),
            'cycle_at': max((n['cycle_at'] for n in held if n.get('cycle_at')), default=None),
            'unread': sorted({short_name(p) for p in negotiators.get('errors') or {}})}


def _queue_drift():
    """Where a host's copy of a queue's status disagrees with the queue
    registry, from the host reporters' records (swf-epicprod
    docs/QUEUE_REGISTRY.md, The drift check)."""
    from monitor_app.host_reports import latest
    from swf_epicprod import queues as registry
    return registry.drift({key: latest(key) for key in registry.DRIFT_REPORTS})


def _drift_items(drift):
    """One red report line per disagreement: the copy must be put right."""
    return [{'level': 'red',
             'text': (f"Queue drift, {d['queue']}: {d['copy']} holds {d['holds']}, "
                      f"the queue registry says {d['expected']}"),
             'link': f'{PUBLIC}/panda/front/'} for d in drift or []]


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
    'degraded': ('red', 'holds new releases, a gate is red'),
    'half_open': ('amber', 'trying one task after a gate cleared'),
    'held': ('grey', 'held'),
    'oversize': ('amber', 'the next task alone is too large, waiting for the operator'),
    'no_work': ('amber', 'no eligible task in the ready buffer'),
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
# The alarm engine's titles: 'task 40962 (failed) final-failure rate 100.0% —
# <task name>'.
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
    page is behind the login wall, so the records ride here. The floor is
    live now: records from the window only, a node guard trip only for a
    node new to the blacklist, and off it a record a ``live_cleared``
    record names, a failure whose action has since run ok, and a record a
    later one on its subject supersedes or repeats (COMP_SNAPSHOT.md,
    Live)."""
    from monitor_app.epicprod_logging import EPICPROD_APP_NAME, live_stream_q
    from monitor_app.live_notices import (FAILURE_OUTCOMES, board_text, failure_key,
                                         site_node_guard_fields)
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
    cutoff = timezone.now() - timedelta(hours=LIVE_WINDOW_H)
    ranks = None
    last_ok = {}
    shown = set()
    items = []
    for r in _pages(rows):
        if r['timestamp'] < cutoff:
            break
        extra = r['extra_data'] or {}
        # A node guard trip is news only for a node not already on the
        # blacklist; a re-trip from half open is a node the floor knows.
        if (extra.get('action') == 'node_guard_decision'
                and extra.get('old_state') not in (None, '', 'clear')):
            continue
        subject = _subject(r['instance_name'], extra)
        if subject in shown:
            continue
        if str(extra.get('outcome') or '') in FAILURE_OUTCOMES:
            key = failure_key(r['instance_name'], extra)
            if key not in last_ok:
                last_ok[key] = _last_ok(r['instance_name'], extra, key)
            if last_ok[key] and last_ok[key] > r['timestamp']:
                continue
        if _superseded(r['instance_name'], extra, r['timestamp']):
            continue
        shown.add(subject)
        if extra.get('action') == 'node_guard_decision':
            if ranks is None:
                from monitor_app.panda.node_guard import exclusion_ranks
                try:
                    ranks = exclusion_ranks()
                except Exception:
                    logger.exception('live screen: node guard blacklist unreadable')
                    ranks = {}
            extra = site_node_guard_fields(extra, ranks)
        title, detail = board_text(r['message'], extra)
        items.append({'id': r['id'], 'at': r['timestamp'].isoformat(),
                      'level': r['levelname'], 'title': title, 'detail': detail,
                      'link': f"{PUBLIC}/logs/{r['id']}/"})
        if len(items) == LIVE_ITEMS:
            break
    return {'link': f'{PUBLIC}/logs/?app_name=epicprod&live=1',
            'numbering': 'site', 'items': items}


def _coverage_gaps(scope, start):
    """Snapper's own record of the spans its collection missed since
    ``start``, ``[(gap start, gap end)]``: each gap a recovering snap closed
    (one whose start is unknown runs from the snap before it), and the open
    gap the capture cursor holds, to now."""
    from django.db.models import Q
    from snapper_ai.models import CaptureCursor, SystemSnap

    gaps = []
    rows = (SystemSnap.objects.filter(scope=scope, snap_time__gte=start)
            .filter(Q(recovered_gap_started_at__isnull=False)
                    | ~Q(recovered_gap_start_unknown=False))
            .values_list('snap_time', 'recovered_gap_started_at',
                         'recovered_gap_start_unknown'))
    for snap_time, gap_start, unknown in rows:
        if gap_start is None or unknown is not False:
            before = (SystemSnap.objects.filter(scope=scope, snap_time__lt=snap_time)
                      .order_by('-snap_time').values_list('snap_time', flat=True).first())
            gap_start = before or start
        gaps.append((gap_start, snap_time))
    cursor = CaptureCursor.objects.filter(scope=scope).first()
    if cursor is not None and cursor.coverage_gap_started_at:
        gaps.append((cursor.coverage_gap_started_at, timezone.now()))
    return sorted(gaps)


def _trend_series(sites):
    """The floor's trend record (COMP_SNAPSHOT.md, Trends): the Snapper
    Site view's series product for the floor's live queues, with the axis
    of 48 whole quarter-hour bins ending at or before the product's own
    newest sample, and Snapper's coverage gaps over it. The task board's
    output flow shares the axis. None when no queue is live or the product
    holds no sample."""
    from snapper_ai.products import series_product

    live = [(s, [q['id'] for q in s['queues'] if q['status'] == 'live']) for s in sites]
    live = [(s, queues) for s, queues in live if queues]
    if not live:
        return None
    product = series_product('epicprod', 'site', window='24h',
                             selection=','.join(q for _, qs in live for q in qs))
    curves = product.get('curves') or {}

    def stamps(cid):
        return [datetime.fromisoformat(x) for x, _ in
                (curves.get(cid) or {}).get('points') or []]

    # Each sample of a queue's PanDA state carries its finished counter, so
    # the counter's stamps are the queue's sample times.
    samples = {q: stamps(f'sjfw_{q}') for _, qs in live for q in qs}
    newest = max((x[-1] for x in samples.values() if x), default=None)
    if newest is None:
        return None
    step_s = TREND_BIN_MINUTES * 60
    bins = TREND_HOURS * 60 // TREND_BIN_MINUTES
    aligned = datetime.fromtimestamp(newest.timestamp() // step_s * step_s, dt_timezone.utc)
    edges = [aligned - timedelta(seconds=step_s * k) for k in range(bins, -1, -1)]
    gaps = [g for g in _coverage_gaps('epicprod', edges[0]) if g[1] >= edges[0]]
    return {'live': live, 'curves': curves, 'samples': samples, 'newest': newest,
            'edges': edges, 'gaps': gaps}


def _trends(series):
    """The floor's trend plots (COMP_SNAPSHOT.md, Trends): per site with a
    live queue, on the trend record's axis, running and queued jobs at each
    bin's end and jobs finished and failed in the bin, summed over the
    site's live queues. A value is read at the queue's newest sample at or
    before the edge: a job state absent from that sample was zero then (the
    PanDA state lists only the states that hold jobs), and a sample older
    than TREND_STALE_MINUTES before the edge, or an edge inside a coverage
    gap, gives null, never a value carried forward. Finished and failed
    count up, so a bin's ended jobs are the difference of the count at its
    two ends, exact across a gap inside the bin, null where an end is
    unknown. The series rebases those counters by summing their nonnegative
    increments, so a raw counter reset reads as no increment, not as a
    detectable fall. A queue with no sample is null throughout."""
    from bisect import bisect_right

    from monitor_app.panda.census import NOT_STARTED
    from snapper_ai.models import CurrentComponent

    if series is None:
        return None
    curves, samples, edges, gaps = (series['curves'], series['samples'],
                                    series['edges'], series['gaps'])
    stale = timedelta(minutes=TREND_STALE_MINUTES)

    def values(cid):
        return {datetime.fromisoformat(x): v for x, v in
                (curves.get(cid) or {}).get('points') or []}

    def at_edges(q, cid, absent_is_zero):
        """The curve at each edge, from the queue's newest sample at or
        before it."""
        times = samples.get(q) or []
        by_time = values(cid)
        out = []
        for edge in edges:
            i = bisect_right(times, edge)
            if not i or edge - times[i - 1] > stale or any(a <= edge <= b for a, b in gaps):
                out.append(None)
                continue
            v = by_time.get(times[i - 1])
            out.append(v if v is not None else (0 if absent_is_zero else None))
        return out

    def total(rows):
        return [None if any(v is None for v in col) else sum(col) for col in zip(*rows)]

    def ended(counts):
        # The counters are cumulative: a gap inside a bin loses none of its
        # ended jobs while the count at both ends is known. The series has
        # already rebased them (nonnegative increments), so b < a cannot
        # occur from a reset; it is guarded, not relied on.
        return [None if a is None or b is None or b < a else round(b - a)
                for a, b in zip(counts, counts[1:])]

    def known_sum(xs):
        return None if any(v is None for v in xs) else sum(xs)

    def num(v):
        return 'unknown' if v is None else f'{v:,}'

    hour = 60 // TREND_BIN_MINUTES
    to = edges[-1].astimezone(ZoneInfo('America/New_York')).strftime('%H:%M')
    out = []
    for site, queues in series['live']:
        running = total([at_edges(q, f'sj_{q}_running', True) for q in queues])[1:]
        queued = total([at_edges(q, f'sj_{q}_{s}', True) for q in queues
                        for s in NOT_STARTED])[1:]
        finished = ended(total([at_edges(q, f'sjfw_{q}', False) for q in queues]))
        failed = ended(total([at_edges(q, f'sjxw_{q}', False) for q in queues]))
        name = short_name(site['name'])
        detail = (f'{name} at {to} ET: {num(running[-1])} running, {num(queued[-1])} queued; '
                  f'{num(known_sum(finished[-hour:]))} finished, '
                  f'{num(known_sum(failed[-hour:]))} failed in the hour before')
        out.append({'id': site['id'], 'name': name, 'running': running,
                    'queued': queued, 'finished': finished, 'failed': failed,
                    'detail': detail})
    panda = CurrentComponent.objects.filter(scope='epicprod', name='panda').first()

    def iso(x):
        return x.astimezone(dt_timezone.utc).isoformat() if x else None

    return {
        'window_hours': TREND_HOURS, 'bin_minutes': TREND_BIN_MINUTES,
        't': [iso(x) for x in edges[1:]],
        'sampled_at': iso(series['newest']),
        'source_as_of': iso(panda.source_as_of) if panda else None,
        'gaps': [[iso(a), iso(b)] for a, b in gaps],
        'sites': out,
        'link': f'{PUBLIC}/snapper/epicprod/site/',
    }


def clear_live_records(ids, reason, username, instance='mcp'):
    """Take records off the room's epicprod-live screen: one live_cleared
    action naming them and why (COMP_SNAPSHOT.md, Live). An id outside the
    epicprod stream is refused, never recorded."""
    from monitor_app.epicprod_logging import EPICPROD_APP_NAME, log_epicprod_action
    from monitor_app.models import AppLog
    ids = sorted({int(i) for i in ids or ()})
    reason = str(reason or '').strip()
    if not ids:
        raise ValueError('no record ids')
    if not reason:
        raise ValueError('a reason is required')
    found = set(AppLog.objects.filter(id__in=ids, app_name=EPICPROD_APP_NAME)
                .values_list('id', flat=True))
    unknown = [i for i in ids if i not in found]
    if unknown:
        raise ValueError(f'not epicprod records: {unknown}')
    summary = (f"cleared {len(ids)} record(s) from the epicprod-live screen: "
               f"{', '.join(str(i) for i in ids)}; {reason}")
    row_id = log_epicprod_action(instance, 'live_cleared', username=username,
                                 message=summary, cleared_ids=ids,
                                 reason=reason, summary=summary)
    if row_id is None:
        raise RuntimeError('the live_cleared record was not written')
    return {'record_id': row_id, 'cleared_ids': ids, 'reason': reason}


def _subject(instance_name, extra):
    """What a live record is about: its action on its subject, or for an
    action with no subject (a maintenance pass), the action from its
    source. The screen shows one record per subject, the newest."""
    if extra.get('subject_key'):
        return json.dumps([extra.get('action'), extra.get('subject_type'),
                           extra.get('subject_key')])
    return json.dumps([extra.get('action'), instance_name])


def _superseded(instance_name, extra, at):
    """True when a later record of the same action on the same subject
    reports another outcome: a node guard decision the node has moved on
    from, a task operation followed by another. A later record with the
    same outcome (the node guard's hourly standing record) continues it."""
    from monitor_app.epicprod_logging import EPICPROD_APP_NAME
    from monitor_app.models import AppLog
    later = AppLog.objects.filter(app_name=EPICPROD_APP_NAME, timestamp__gt=at,
                                  extra_data__action=extra.get('action'))
    if extra.get('subject_key'):
        later = later.filter(extra_data__subject_type=extra.get('subject_type'),
                             extra_data__subject_key=extra['subject_key'])
    else:
        later = later.filter(instance_name=instance_name)
    if extra.get('outcome') is not None:
        later = later.exclude(extra_data__outcome=extra['outcome'])
    return later.exists()


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
    for s in sites:
        live = [q for q in s['queues'] if q['status'] == 'live']
        if not live:
            continue
        for q in live:
            f = q.get('front') or {}
            level, words = FRONT_STATES.get(f.get('state'), ('grey', f.get('state') or ''))
            if f.get('state') and f.get('state') not in ('supplied', 'fed'):
                reason = FRONT_REASONS.get(f.get('reason'), '')
                items.append({'level': level,
                              'text': f"Front, {short_name(q['id'])}: {words}{', ' + reason if reason else ''}",
                              'link': front.get('link')})
            for name, p in q['health']['parts'].items():
                if p['level'] not in ('amber', 'red'):
                    continue
                link = (q['links'].get('slots') if name == 'pilot_supply' else
                        q['links'].get('canary') if name == 'canary' else
                        q['links'].get('snapper'))
                items.append({'level': p['level'],
                              'text': f"{short_name(q['id'])} {PART_NAMES.get(name, name)}: {p['note']}",
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
        # The alarm engine selects approaching pings by their lead time.
        # Their recorded title's countdown ages; display the due date.
        ping = a.get('severity') == 'ping'
        due = DUE_RE.match(title) if ping else None
        if task:
            number, state, rate, name = task.groups()
            text = f'Final failures {rate}, task {number} ({state}): {name}'
        elif due:
            _days, date, what = due.groups()
            text = f'Due {date}: {what}'
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
        if level in ('amber', 'red'):
            items.append({'level': level,
                          'text': f"Campaign assessment: {assessment['summary']}",
                          'link': f'{PUBLIC}/ai/assessments/?subject_type=campaign_task,campaign'})
    items.sort(key=lambda i: LEVEL_ORDER.get(i['level'], 2))
    return items


def incomplete_registration_link(observed_at):
    """The same completion window and registration subset as the count."""
    if isinstance(observed_at, str):
        observed_at = datetime.fromisoformat(observed_at)
    return f'{PUBLIC}/panda/jobs/?' + urlencode({
        'status': 'finished', 'registration': 'incomplete', 'hours': 1,
        'ended_after': (observed_at - timedelta(hours=1)).isoformat(),
        'ended_before': observed_at.isoformat(),
    })


def _dashboard_campaign():
    """Current campaign, from the same cached completion product as Nav."""
    from pcs.models import Campaign
    from monitor_app.cached_product import get_product
    from swf_epicprod.analytics.completion import completion_product_value

    name = Campaign.objects.filter(lifecycle='current').values_list('name', flat=True).first()
    if not name:
        return None
    product = get_product('prod_hub_campaign_completion', completion_product_value,
                          ttl_seconds=600)
    row = next((row for row in product['value'] or [] if row['campaign'] == name), {})
    totals = row.get('overall') or {}
    return {'id': name, 'delivered_since': totals.get('delivered_since'),
            'events': totals.get('delivered_events'), 'bytes': totals.get('bytes'),
            'configurations': totals.get('configurations')}


def get_snapshot(refresh=False):
    """The stored record, rebuilt behind the request when older than the
    TTL (docs/CACHED_PRODUCTS.md)."""
    from monitor_app.cached_product import get_product
    result = get_product(PRODUCT_KEY, build, TTL_SECONDS, refresh=refresh)
    value = dict(result.get('value') or {})
    # Correct the old queue-wide labels immediately, without rebuilding
    # the cached census, storage or histories.
    if value.get('live') and value['live'].get('numbering') != 'site':
        value['live'] = _live_records()
    # Labels follow the current shared vocabulary even while counts are cached.
    value['sites'] = [dict(site, execute_sites=[
        dict(item, short=short_name(item.get('name')))
        for item in site.get('execute_sites', [])
    ]) for site in value.get('sites', [])]
    for site in value['sites']:
        if isinstance(site.get('fair_share'), dict):
            share = site['fair_share']
            site['fair_share'] = dict(share, notice=_fair_share_notice(
                share, (site.get('links') or {}).get('panda_queue')))
    if value.get('observed_at'):
        value['storage'] = [dict(row, links=dict(row.get('links') or {},
            failed=incomplete_registration_link(value['observed_at'])))
            if row.get('id') == 'jlab-rucio' else row
            for row in value.get('storage', [])]
    try:
        value['campaign'] = _dashboard_campaign()
    except Exception:
        logger.exception('Dashboard campaign summary unavailable')
        value['campaign'] = None
    value['age_seconds'] = result.get('age_seconds')
    value['refreshing'] = result.get('refreshing')
    return value
