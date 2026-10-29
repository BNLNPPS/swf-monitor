"""Successful wall-time efficiency history (docs/COMPUTE_EFFICIENCY.md)."""
from collections import defaultdict
import re
from datetime import date, datetime, timedelta, timezone

from django.db import connections, transaction

from .cached_product import get_product
from .models import CachedProduct
from .panda.queries import PANDA_SCHEMA, execute_site_key_sql, execute_site_label, osg_pool_queues
from .short_names import short_name

PRODUCT_KEY = 'compute_efficiency:v1'
CURRENT_KEY = 'compute_efficiency_current:v2'
START = date(2026, 5, 1)
TTL = 3600
INCIDENT_TASK = 40856
MIN_HOURS = 50
# An Event Service queue's jobs from before its move to production were its
# commissioning trials, whatever their source label (Torre, 2026-10-26), and
# stay out of the production sample. SysConfig
# ``compute_efficiency.commissioning_until`` {queue: ISO time}.
DEFAULT_COMMISSIONING_UNTIL = {
    'BNL_ePIC_GOOGLE_es': '2026-10-25T21:36:37+00:00',
    'NERSC_Perlmutter_epic_es': '2026-10-26T12:20:35+00:00',
}
# Trials after a queue's move to production stay out of the production
# sample too (Torre Wenaus, 2026-10-29: "MARK THEM AS TRIALS"): on Perlmutter,
# 28 October's return to commissioning, the gun tasks of the serialized-handoff
# defect and the first tasks of the new EVGEN Event Service workflow, none of
# them production runs. SysConfig ``compute_efficiency.commissioning_tasks``
# [JEDI task id].
DEFAULT_COMMISSIONING_TASKS = [41463, 41469, 41479, 41480]
# Jobs whose loss came from a commissioning-grade defect of ours stay out of
# the production sample (Torre Wenaus, 2026-10-29: "DO NOT ACCOUNT
# INEFFICIENCIES ARISING TO PROD"): the four Google Event Service jobs the
# broken close-out step could not close on 28-29 October. SysConfig
# ``compute_efficiency.commissioning_jobs`` [PandaID].
DEFAULT_COMMISSIONING_JOBS = [6341816, 6359213, 6359214, 6359215]
DASHBOARD_SITES = (('osg', 'OSG', '#b48ee8'),
                   ('perlmutter', 'Perlmutter', '#e3b954'),
                   ('grex', 'GREX', '#7fce95'),
                   ('google', 'Google', '#6fb7f2'))
# A site drawn only on some of the dashboard's plots: Google's few cores make
# flat lines on the core plots, while its efficiency is meaningful (Torre,
# 2026-10-27). A site absent here is drawn on all three.
DASHBOARD_SITE_METRICS = {'google': ('efficiency',)}


def commissioning_until():
    """{queue: naive UTC time} from SysConfig; malformed entries are skipped."""
    from .models import SysConfig
    value = SysConfig.get_setting('compute_efficiency.commissioning_until',
                                  DEFAULT_COMMISSIONING_UNTIL)
    out = {}
    for queue, stamp in (value.items() if isinstance(value, dict) else ()):
        try:
            at = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if re.fullmatch(r'[A-Za-z0-9_.-]+', str(queue)):
            out[queue] = (at.astimezone(timezone.utc).replace(tzinfo=None)
                          if at.tzinfo else at)
    return out


def commissioning_tasks():
    """[JEDI task id] from SysConfig; entries that are not ids are skipped."""
    from .models import SysConfig
    value = SysConfig.get_setting('compute_efficiency.commissioning_tasks',
                                  DEFAULT_COMMISSIONING_TASKS)
    return [int(t) for t in (value if isinstance(value, list) else ())
            if str(t).isdigit()]


def commissioning_jobs():
    """[PandaID] from SysConfig; entries that are not ids are skipped."""
    from .models import SysConfig
    value = SysConfig.get_setting('compute_efficiency.commissioning_jobs',
                                  DEFAULT_COMMISSIONING_JOBS)
    return [int(j) for j in (value if isinstance(value, list) else ())
            if str(j).isdigit()]


def query_days(start, end, production=False, finished_only=False):
    """One grouped database read; no per-job records leave the database."""
    pools = osg_pool_queues()
    key = execute_site_key_sql()
    fields = f'''computingsite, destinationsite AS dest_site,
        regexp_replace(COALESCE(modificationhost, ''), '^.*@', '') AS host,
        jobstatus, jeditaskid, endtime,
        CASE WHEN starttime IS NOT NULL AND endtime >= starttime THEN
          EXTRACT(EPOCH FROM (endtime-starttime)) *
          GREATEST(1, COALESCE(NULLIF(actualcorecount,0), NULLIF(corecount,0),1)) / 3600.0
        END AS hours,
        pandaid, piloterrorcode, starttime,
        COALESCE(eventservice, 0) IN ({','.join(map(str, ES_CONSUMERS))}) AS es'''
    where = '''endtime >= %s AND endtime < %s
        AND jobstatus IN ('finished','failed','closed','cancelled')'''
    if production:
        # Production used the test source label before the managed-label
        # transition. Keep that history without admitting canary probes.
        where += """ AND workinggroup='EIC'
            AND (prodsourcelabel='managed' OR processingtype='epicproduction')
            AND COALESCE(processingtype,'') <> 'canary'"""
        # Validated names and parsed times, so they are written in place.
        for queue, at in commissioning_until().items():
            where += (f" AND NOT (computingsite = '{queue}'"
                      f" AND creationtime < '{at.isoformat(sep=' ')}')")
        tasks = commissioning_tasks()
        if tasks:
            # integers by construction, so written in place
            where += f" AND COALESCE(jeditaskid, 0) NOT IN ({','.join(map(str, tasks))})"
        jobs = commissioning_jobs()
        if jobs:
            # integers by construction, so written in place
            where += f" AND pandaid NOT IN ({','.join(map(str, jobs))})"
    if finished_only:
        where += " AND jobstatus='finished'"
    # PanDA stores UTC timestamps without a timezone; datetime bounds retain
    # their time of day for the dashboard's exact trailing 24-hour window.
    def utc_bound(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        return datetime.combine(value, datetime.min.time())

    bounds = [utc_bound(start), utc_bound(end)]
    sql = f'''WITH jobs AS (
        SELECT {fields} FROM "{PANDA_SCHEMA}".jobsarchived4 WHERE {where}
          AND modificationtime >= %s
        UNION ALL
        SELECT {fields} FROM "{PANDA_SCHEMA}".jobsactive4 WHERE {where}
      ), located AS (
        SELECT *, (computingsite = ANY(%s) OR computingsite LIKE 'BNL_OSG%%') AS pool
        FROM jobs
      )
      SELECT endtime::date, computingsite, pool,
        CASE WHEN pool THEN {key} ELSE '' END AS execution,
        COALESCE(SUM(hours) FILTER (WHERE jobstatus='finished' AND NOT es),0),
        COALESCE(SUM(hours),0), COUNT(*), COUNT(*) FILTER (WHERE hours IS NULL),
        COALESCE(SUM(hours) FILTER (WHERE jobstatus='finished' AND jeditaskid={INCIDENT_TASK}),0),
        COALESCE(SUM(hours) FILTER (WHERE jeditaskid={INCIDENT_TASK}),0),
        COUNT(*) FILTER (WHERE jeditaskid={INCIDENT_TASK}),
        COUNT(*) FILTER (WHERE jeditaskid={INCIDENT_TASK} AND hours IS NULL),
        COUNT(*) FILTER (WHERE es AND hours IS NOT NULL),
        ARRAY_AGG(ARRAY[pandaid::text, jobstatus, COALESCE(piloterrorcode, 0)::text,
                        starttime::text, endtime::text, hours::text])
          FILTER (WHERE es AND hours IS NOT NULL)
      FROM located GROUP BY 1,2,3,4 ORDER BY 1,2,4'''
    with transaction.atomic(using='panda'):
        with connections['panda'].cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '180s'")
            cursor.execute(sql, bounds + [bounds[0]] + bounds + [pools])
            rows = cursor.fetchall()
    out = []
    for row in rows:
        values = [float(v or 0) for v in row[4:13]]
        # An Event Service job's productive core-hours are its allocation
        # times the share its slots spent processing events whose output
        # was saved, whatever PanDA's status says (docs/COMPUTE_EFFICIENCY.md,
        # Event Service jobs). Column 12 counts the Event Service jobs with
        # timings, column 13 those whose record does not measure the share;
        # for those PanDA's status stands, the only evidence there is.
        unmeasured = 0
        for pandaid, status, pilot_error, start, end, hours in row[13] or []:
            fraction = es_saved_fraction(int(pandaid), status, int(pilot_error), start, end)
            if fraction is None:
                unmeasured += 1
                fraction = 1.0 if status == 'finished' else 0.0
            values[0] += float(hours) * fraction
        values.append(float(unmeasured))
        out.append([str(row[0]), row[1] or 'Unknown queue', bool(row[2]), row[3], *values])
    return out


# The Event Service job flavors that process event ranges (consumers, not
# merges; panda/constants.py ES_JOB_FLAVORS).
ES_CONSUMERS = (1, 3, 4, 5, 6)
_es_fractions = {}


def es_saved_fraction(pandaid, status, pilot_error, start, end):
    """The share of an ended Event Service job's slot time spent processing
    events whose output was saved (the job page's measured saved
    processing, panda/queries.py es_slot_timeline), from the harness's own
    record; None when the job has no record that measures it. A measured
    value never changes, so it is kept for the process's life."""
    if pandaid in _es_fractions:
        return _es_fractions[pandaid]
    from .panda.queries import es_harness_report, es_slot_timeline
    job = {'pandaid': pandaid, 'jobstatus': status, 'piloterrorcode': pilot_error,
           'starttime': start, 'endtime': end}
    report = es_harness_report(connections['panda'], pandaid, job=job)
    timeline = es_slot_timeline(job, report['raw']) if report else None
    fraction = (timeline or {}).get('saved_processing_fraction')
    if fraction is not None:
        fraction = min(1.0, max(0.0, float(fraction)))
        _es_fractions[pandaid] = fraction
    return fraction


def merge_days(previous, recent, reread_from):
    """Replace entire recent days, including rows that disappeared on correction."""
    return [r for r in previous if r[0] < str(reread_from)] + recent


def build(full=False):
    now = datetime.now(timezone.utc)
    row = CachedProduct.objects.filter(key=PRODUCT_KEY).first()
    previous = (row.value or {}).get('daily', []) if row else []
    start = START if full or not previous else max(START, now.date() - timedelta(days=7))
    recent = query_days(start, now.date() + timedelta(days=1), production=True)
    if full and previous:
        if not recent:
            raise RuntimeError('Full efficiency reconciliation returned no source history')
        # Retain periods that have aged out of the PanDA archive.
        start = max(start, date.fromisoformat(min(r[0] for r in recent)))
    daily = merge_days(previous, recent, start)
    return {'daily': daily, 'start': str(START), 'through': now.isoformat(),
            'reread_from': str(start), 'schema': 1}


def product(refresh=False, full=False):
    return get_product(PRODUCT_KEY, lambda: build(full), TTL,
                       refresh=refresh or full, async_first_fill=True)


def _hours_summary(rows):
    finished, ended, jobs, missing = (sum(row[i] for row in rows) for i in range(4, 8))
    # Rows built before the Event Service measure carry no columns 12-13.
    es_jobs, es_unmeasured = (int(sum(row[i] for row in rows if len(row) > i)) for i in (12, 13))
    return {'finished': finished, 'ended': ended, 'jobs': int(jobs), 'missing': int(missing),
            'es_jobs': es_jobs, 'es_unmeasured': es_unmeasured,
            'efficiency': 100 * finished / ended if ended >= MIN_HOURS else None}


def _delivery_summary(rows, days):
    summary = _hours_summary(rows)
    measured = days > 0 and summary['jobs'] > summary['missing']
    return {**summary, 'days': days,
            'core_hours_per_day': summary['ended'] / days if measured else None,
            'productive_cores': summary['finished'] / (days * 24) if measured else None}


def _dashboard_site(row):
    if row[2]:
        return 'osg'
    if row[1].startswith('NERSC_Perlmutter_'):
        return 'perlmutter'
    if row[1] == 'UM_GREX_PanDA_1':
        return 'grex'
    if row[1].startswith('BNL_ePIC_GOOGLE'):
        return 'google'
    return None


def build_current():
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    rows = query_days(start, end, production=True)
    sites = {sid: _delivery_summary([row for row in rows if _dashboard_site(row) == sid], 1)
             for sid, _, _ in DASHBOARD_SITES}
    return {**_delivery_summary(rows, 1), 'sites': sites,
            'start': start.isoformat(), 'end': end.isoformat()}


def current_product():
    return get_product(CURRENT_KEY, build_current, 60, async_first_fill=True)


def dashboard_data(value, current):
    """Global averages and three site histories over the same ended-job sample."""
    now = datetime.now(timezone.utc)
    today = now.date()
    through = datetime.fromisoformat(value['through']) if value.get('through') else now
    through = min(through.astimezone(timezone.utc), now)
    grouped = defaultdict(list)
    for row in value.get('daily', []):
        day = date.fromisoformat(row[0])
        if START <= day <= today:
            grouped[str(day.replace(day=1))].append(row)
    months = []
    sites = [{'id': sid, 'label': label, 'color': color, 'months': [],
              'metrics': list(DASHBOARD_SITE_METRICS.get(sid, ()))}
             for sid, label, color in DASHBOARD_SITES]
    month = START
    while month <= today:
        following = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        begin = datetime.combine(month, datetime.min.time(), tzinfo=timezone.utc)
        end = min(datetime.combine(following, datetime.min.time(), tzinfo=timezone.utc), through)
        days = max(0, (end - begin).total_seconds() / 86400)
        period = {'period': str(month), 'label': month.strftime('%b'),
                  'partial': month.year == today.year and month.month == today.month}
        rows = grouped[str(month)]
        months.append({**period, **_delivery_summary(rows, days)})
        for site in sites:
            site['months'].append({**period, **_delivery_summary(
                [row for row in rows if _dashboard_site(row) == site['id']], days)})
        month = following
    return {'current': current, 'months': months, 'sites': sites, 'minimum_hours': MIN_HOURS,
            'history_through': value.get('through')}


def _queue_labels():
    from swf_epicprod.queues import entries
    from .comp_snapshot import DEFAULT_SITES
    from .models import SysConfig
    groups = SysConfig.get_setting('comp.sites', DEFAULT_SITES)
    names = {g['id']: short_name(g.get('name') or g['id']) for g in groups}
    return {q['queue']: names.get(q.get('site')) for q in entries()}


def display_data(value, start, end, bucket='month', exclude=False):
    """Sum hours into periods, then form ratios; retain unknown timing counts."""
    labels = _queue_labels()
    totals = defaultdict(lambda: [0.0, 0.0, 0, 0])
    sites = {}
    for row in value.get('daily', []):
        day = date.fromisoformat(row[0])
        if not start <= day <= end:
            continue
        queue, pool, execution = row[1:4]
        # These historical test/GPU/ES queues used the same physical system.
        family = 'NERSC_Perlmutter_epic' if queue.startswith('NERSC_Perlmutter_') else queue
        name = short_name(execute_site_label(execution)) if pool else (
            labels.get(family) or short_name(queue))
        sid = ('osg:' if pool else 'queue:') + str(name)
        label = 'OSG · ' + str(name) if pool else str(name)
        period = (day.replace(day=1) if bucket == 'month' else day - timedelta(days=day.weekday()))
        sites[sid] = label
        finished, ended, jobs, missing = row[4:8]
        if exclude:
            finished -= row[8]; ended -= row[9]; jobs -= row[10]; missing -= row[11]
        target = totals[(sid, str(period))]
        for i, n in enumerate((finished, ended, jobs, missing)):
            target[i] += n
    cells = []
    for (sid, period), (finished, ended, jobs, missing) in totals.items():
        cells.append({'site': sid, 'period': period, 'finished': finished, 'ended': ended,
                      'jobs': int(jobs), 'missing': int(missing),
                      'efficiency': 100 * finished / ended if ended >= MIN_HOURS else None})
    return {'sites': [{'id': sid, 'label': label} for sid, label in sites.items()],
            'cells': cells, 'start': str(start), 'end': str(end), 'bucket': bucket,
            'exclude_task': INCIDENT_TASK if exclude else None, 'minimum_hours': MIN_HOURS,
            'through': value.get('through')}
