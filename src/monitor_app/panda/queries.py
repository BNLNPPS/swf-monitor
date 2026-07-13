"""
PanDA database query functions for ePIC production monitoring.

All functions are synchronous — they use django.db.connections['panda']
directly. Callers in async contexts should wrap with sync_to_async.
"""

import logging
import json
import re
from datetime import datetime, timedelta
from urllib.parse import unquote
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import connections

from .constants import (
    PANDA_SCHEMA, LIST_FIELDS, ERROR_FIELDS, DIAGNOSE_EXTRA_FIELDS,
    ERROR_COMPONENTS, FAULTY_STATUSES, TASK_LIST_FIELDS,
    STUDY_FIELDS, FILE_FIELDS, JOB_STATUS_CATEGORIES,
)
from .sql import (
    build_union_query, build_count_query,
    build_task_query, build_task_count_query,
    build_union_query_dt, build_union_count, build_union_count_by_field,
    build_task_query_dt, build_task_count, build_task_count_by_field,
    build_search_clauses,
    row_to_dict, extract_errors, like_or_eq,
)

logger = logging.getLogger(__name__)

TERMINAL_TASK_STATUSES = ('done', 'failed', 'aborted', 'broken', 'finished')
STALE_TASK_DAYS = 60
PSEUDO_TASK_DATASETS = {'seq_number', 'pseudo_dataset'}

# NERSC Perlmutter jobs publish their per-job pilot & slurm logs here.
# Pattern: <base>/<queue>/<pandaid>/{pilotlog.txt, slurm-<id>-task<N>-panda<pid>.out}
_NERSC_PORTAL_BASE = "https://portal.nersc.gov/cfs/m3763/panda/jobs"
_NERSC_SLURM_RE = re.compile(r'href="(slurm-\d+-task\d+-panda\d+\.out)"')
_PANDA_CLIENT_PROCESSING_RE = re.compile(r'^panda-client-[0-9][A-Za-z0-9._-]*-(jedi-.+)$')
_PANDA_USER_EQUIVALENCES = {
    # Canonical monitor display name -> equivalent login/name variants.
    'Torre Wenaus': ('wenaus',),
}
_PANDA_USER_TO_CANONICAL = {
    name: canonical
    for canonical, names in _PANDA_USER_EQUIVALENCES.items()
    for name in (canonical, *names)
}


def _display_processing_type(value):
    value = value or ''
    m = _PANDA_CLIENT_PROCESSING_RE.match(value)
    if m:
        return f'panda-client-{m.group(1)}'
    return value or 'None'


def _processing_type_filter_value(value):
    value = value or ''
    m = _PANDA_CLIENT_PROCESSING_RE.match(value)
    if m:
        return f'panda-client-%-{m.group(1)}'
    return value or '__blank__'


def _aggregate_processing_type_counts(rows):
    counts = {}
    filter_values = {}
    for raw_value, count in rows:
        label = _display_processing_type(raw_value)
        counts[label] = counts.get(label, 0) + count
        filter_values[label] = _processing_type_filter_value(raw_value)
    return [
        (label, count, filter_values[label])
        for label, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
    ]


def _canonical_user(value):
    return _PANDA_USER_TO_CANONICAL.get(value, value)


def _user_filter_values(value):
    canonical = _canonical_user(value)
    values = {canonical}
    values.update(_PANDA_USER_EQUIVALENCES.get(canonical, ()))
    return sorted(v for v in values if v)


def _pcs_owner_map(jeditaskids):
    """Return {jediTaskID: PCS created_by} for PanDA tasks linked to PCS."""
    ids = [int(tid) for tid in set(jeditaskids or []) if tid]
    if not ids:
        return {}
    try:
        from pcs.models import PandaTasks
        return {
            int(jedi_task_id): _canonical_user(created_by)
            for jedi_task_id, created_by in PandaTasks.objects
            .filter(jedi_task_id__in=ids)
            .values_list('jedi_task_id', 'prod_task__created_by')
            if jedi_task_id and created_by
        }
    except Exception as e:
        logger.warning("_pcs_owner_map failed: %s", e)
        return {}


def _pcs_taskids_for_owner(username):
    """Return linked PanDA task IDs owned, and linked IDs owned by others."""
    if not username:
        return [], []
    try:
        from pcs.models import PandaTasks
        linked = PandaTasks.objects.filter(jedi_task_id__isnull=False)
        if '%' in username:
            variants = [v.replace('%', '') for v in _user_filter_values(username)]
            owner_ids = set()
            for needle in variants:
                owner_ids.update(
                    int(jedi_task_id)
                    for jedi_task_id in linked
                    .filter(prod_task__created_by__contains=needle)
                    .values_list('jedi_task_id', flat=True)
                    if jedi_task_id
                )
        else:
            owner_ids = {
                int(jedi_task_id)
                for jedi_task_id in linked
                .filter(prod_task__created_by__in=_user_filter_values(username))
                .values_list('jedi_task_id', flat=True)
                if jedi_task_id
            }
        other_ids = {
            int(jedi_task_id)
            for jedi_task_id in linked.values_list('jedi_task_id', flat=True)
            if jedi_task_id and int(jedi_task_id) not in owner_ids
        }
        return sorted(owner_ids), sorted(other_ids)
    except Exception as e:
        logger.warning("_pcs_taskids_for_owner failed: %s", e)
        return [], []


def _in_clause(field, values):
    placeholders = ', '.join(['%s'] * len(values))
    return f'"{field}" IN ({placeholders})', list(values)


def _effective_username_filter(db_field, username):
    """Filter by displayed owner: PCS created_by for linked tasks, raw PanDA otherwise."""
    raw_values = _user_filter_values(username)
    owner_ids, other_ids = _pcs_taskids_for_owner(username)
    clauses = []
    params = []

    if owner_ids:
        owner_clause, owner_params = _in_clause('jeditaskid', owner_ids)
        clauses.append(owner_clause)
        params.extend(owner_params)

    raw_parts = []
    raw_params = []
    for raw_value in raw_values:
        raw_clause, raw_val = like_or_eq(db_field, raw_value)
        raw_parts.append(raw_clause)
        raw_params.append(raw_val)
    raw_clause = '(' + ' OR '.join(raw_parts) + ')'
    if other_ids:
        other_clause, other_params = _in_clause('jeditaskid', other_ids)
        raw_clause = f'({raw_clause} AND NOT {other_clause})'
        raw_params.extend(other_params)
    clauses.append(raw_clause)
    params.extend(raw_params)

    return '(' + ' OR '.join(clauses) + ')', params


def _apply_effective_owners(rows, user_field):
    owner_map = _pcs_owner_map([row.get('jeditaskid') for row in rows])
    if not owner_map:
        return rows
    for row in rows:
        owner = owner_map.get(row.get('jeditaskid'))
        if owner:
            row[user_field] = owner
        elif row.get(user_field):
            row[user_field] = _canonical_user(row[user_field])
    return rows


def _apply_processing_type_display(rows):
    for row in rows:
        if 'processingtype' in row:
            row['processingtype_raw'] = row.get('processingtype')
            row['processingtype'] = _display_processing_type(row.get('processingtype'))
    return rows


def _aggregate_effective_user_counts(rows):
    owner_map = _pcs_owner_map([row[1] for row in rows])
    counts = {}
    for raw_user, jeditaskid, count in rows:
        user = owner_map.get(jeditaskid) or _canonical_user(raw_user)
        counts[user] = counts.get(user, 0) + count
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)


def _get_task_record(jeditaskid):
    sql = f"""
        SELECT *
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE "jeditaskid" = %s
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [jeditaskid])
            row = cursor.fetchone()
            if not row:
                return {}
            columns = [col[0] for col in cursor.description]
            return row_to_dict(row, columns)
    except Exception as e:
        logger.error("_get_task_record failed: %s", e)
        return {}


def _get_task_datasets(jeditaskid):
    fields = [
        'datasetname', 'type', 'streamname', 'status',
        'nfiles', 'nfilesfinished', 'nfilesfailed', 'nfilesused',
        'nevents', 'neventsused', 'site', 'destination',
    ]
    field_list = ', '.join(f'"{field}"' for field in fields)
    sql = f"""
        SELECT {field_list}
        FROM "{PANDA_SCHEMA}"."jedi_datasets"
        WHERE "jeditaskid" = %s
        ORDER BY "datasetid"
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [jeditaskid])
            datasets = [row_to_dict(row, fields) for row in cursor.fetchall()]
    except Exception as e:
        logger.error("_get_task_datasets failed: %s", e)
        return []
    visible = []
    for dataset in datasets:
        if (
            dataset.get('type') == 'pseudo_input'
            or (dataset.get('type') or '').startswith('tmpl_')
            or dataset.get('datasetname') in PSEUDO_TASK_DATASETS
        ):
            continue
        nfiles = dataset.get('nfiles') or 0
        finished = dataset.get('nfilesfinished') or 0
        failed = dataset.get('nfilesfailed') or 0
        dataset['files_percent'] = round(100 * (finished + failed) / nfiles, 1) if nfiles else None
        dataset['rse'] = dataset.get('destination') or dataset.get('site') or ''
        visible.append(dataset)
    return visible


def _format_task_job_parameter(param):
    if not isinstance(param, dict):
        return {'label': 'Parameter', 'value': str(param)}
    if param.get('type') == 'template':
        if (
            param.get('param_type') == 'pseudo_input'
            or param.get('dataset') in PSEUDO_TASK_DATASETS
        ):
            return None
        bits = []
        if param.get('dataset'):
            bits.append(f"dataset={param.get('dataset')}")
        if param.get('value'):
            bits.append(f"value={param.get('value')}")
        return {'label': f"{param.get('param_type') or 'Template'} template", 'value': ', '.join(bits)}
    if param.get('type') == 'constant':
        value = (param.get('value') or '').strip()
        if not value or value in {'"', '-p "'}:
            return None
        source_match = re.search(r'--sourceURL\s+(\S+)', value)
        if source_match:
            return {'label': 'Source URL', 'value': source_match.group(1)}
        if value.startswith('-r '):
            return {'label': 'Work directory', 'value': value[3:].strip()}
        if value.startswith('-a '):
            return {'label': 'Sandbox', 'value': value[3:].strip()}
        return {'label': 'Payload command', 'value': unquote(value).strip('"')}
    return {'label': param.get('type') or 'Parameter', 'value': str(param)}


def _get_task_parameters(jeditaskid):
    sql = f"""
        SELECT "taskparams"
        FROM "{PANDA_SCHEMA}"."jedi_taskparams"
        WHERE "jeditaskid" = %s
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, [jeditaskid])
            row = cursor.fetchone()
    except Exception as e:
        logger.error("_get_task_parameters failed: %s", e)
        return {}, []
    if not row or not row[0]:
        return {}, []
    raw = row[0]
    try:
        params = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {'raw': raw}, [raw]
    job_params = params.get('jobParameters') if isinstance(params, dict) else None
    if not isinstance(job_params, list):
        return params, []
    items = [
        item for item in (_format_task_job_parameter(param) for param in job_params)
        if item and item.get('value') not in (None, '')
    ]
    if not any(item.get('label') == 'Source URL' for item in items) and params.get('sourceURL'):
        items.insert(0, {'label': 'Source URL', 'value': params.get('sourceURL')})
    return params, items


def _nersc_portal_log_urls(computingsite, pandaid):
    """Build Perlmutter log URLs by scraping the NERSC portal dir listing.

    The slurm task filename contains a per-allocation task index not stored in
    our DB, so we have to scrape the Apache autoindex to find it. Returns
    ``None`` if the dir is unreachable or empty.
    """
    import requests
    log_dir = f"{_NERSC_PORTAL_BASE}/{computingsite}/{pandaid}/"
    try:
        resp = requests.get(log_dir, timeout=5)
        if resp.status_code != 200:
            return None
    except Exception as e:
        logger.warning("NERSC portal dir fetch failed for %s: %s", pandaid, e)
        return None
    result = {
        'nersc_log_dir': log_dir,
        'pilot_stdout': log_dir + 'pilotlog.txt',
    }
    m = _NERSC_SLURM_RE.search(resp.text)
    if m:
        result['slurm_task_stdout'] = log_dir + m.group(1)
    return result


def _bulk_destinationse(pandaids):
    """Look up destinationse (destination storage element) for a batch of jobs.

    The destination SE — the Rucio storage element where output files are
    written — is stored per-file in filestable4, not in the jobs table.
    Returns {pandaid: destinationse} for jobs that have one.
    """
    if not pandaids:
        return {}
    conn = connections['panda']
    placeholders = ','.join(['%s'] * len(pandaids))
    sql = f"""
        SELECT DISTINCT "pandaid", "destinationse"
        FROM "{PANDA_SCHEMA}"."filestable4"
        WHERE "pandaid" IN ({placeholders})
          AND "destinationse" IS NOT NULL
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, list(pandaids))
            return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        logger.exception("_bulk_destinationse failed")
        return {}


def job_completion_details(pandaids):
    """Return lightweight completion/error details for visible jobs.

    This reads only jobsactive4/jobsarchived4 columns. It deliberately does not
    fetch files, harvester records, pilot logs, or run log analysis; the single
    job detail page owns that deeper path.
    """
    ids = [int(pandaid) for pandaid in (pandaids or []) if pandaid]
    if not ids:
        return {}
    fields = ['pandaid', 'attemptnr', 'maxattempt', *ERROR_FIELDS]
    placeholders = ','.join(['%s'] * len(ids))
    field_list = ', '.join(f'"{field}"' for field in fields)
    sql = f"""
        SELECT {field_list} FROM "{PANDA_SCHEMA}"."jobsactive4"
            WHERE "pandaid" IN ({placeholders})
        UNION ALL
        SELECT {field_list} FROM "{PANDA_SCHEMA}"."jobsarchived4"
            WHERE "pandaid" IN ({placeholders})
    """
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, ids + ids)
            details = {}
            for row in cursor.fetchall():
                item = row_to_dict(row, fields)
                errors = extract_errors(item)
                attempt = item.get('attemptnr')
                maxattempt = item.get('maxattempt') or 3
                try:
                    final_attempt = bool(attempt and int(attempt) >= int(maxattempt))
                except (TypeError, ValueError):
                    final_attempt = False
                details[item['pandaid']] = {
                    'attemptnr': attempt,
                    'maxattempt': maxattempt,
                    'final_attempt': final_attempt,
                    'errors': errors,
                }
            return details
    except Exception:
        logger.exception("job_completion_details failed")
        return {}


def _stale_task_filter():
    """Exclude non-terminal tasks created more than STALE_TASK_DAYS ago."""
    cutoff = timezone.now() - timedelta(days=STALE_TASK_DAYS)
    placeholders = ', '.join(['%s'] * len(TERMINAL_TASK_STATUSES))
    clause = f'NOT ("creationdate" < %s AND "status" NOT IN ({placeholders}))'
    return {'clause': clause, 'params': [cutoff, *TERMINAL_TASK_STATUSES]}


def list_jobs(days=7, status=None, username=None, site=None,
              taskid=None, reqid=None, limit=200, before_id=None):
    """List PanDA jobs with summary statistics and cursor-based pagination."""
    # When scoped to a specific task, return everything — don't truncate
    if taskid:
        limit = 100000
    cutoff = timezone.now() - timedelta(days=days)
    where = ['"modificationtime" >= %s']
    params = [cutoff]

    if status:
        where.append('"jobstatus" = %s')
        params.append(status)
    if username:
        clause, vals = _effective_username_filter('produsername', username)
        where.append(clause)
        params.extend(vals)
    if site:
        clause, val = like_or_eq('computingsite', site)
        where.append(clause)
        params.append(val)
    if taskid:
        where.append('"jeditaskid" = %s')
        params.append(taskid)
    if reqid:
        where.append('"reqid" = %s')
        params.append(reqid)
    if before_id:
        where.append('"pandaid" < %s')
        params.append(before_id)

    conn = connections['panda']

    # Summary counts (without pagination cursor)
    if before_id:
        count_where = where[:-1]
        count_params = params[:-1]
    else:
        count_where = where
        count_params = params
    count_sql, count_full_params = build_count_query(count_where, count_params)

    summary = {}
    total = 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(count_sql, count_full_params)
            for row in cursor.fetchall():
                summary[row[0]] = row[1]
                total += row[1]
    except Exception as e:
        logger.error(f"list_jobs count query failed: {e}")
        return {"error": str(e)}

    fetch_limit = limit + 1
    sql, full_params = build_union_query(
        LIST_FIELDS, where, params,
        order_by='"pandaid" DESC',
        limit=fetch_limit,
    )

    jobs = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, full_params)
            rows = cursor.fetchall()
            for row in rows[:limit]:
                jobs.append(row_to_dict(row, LIST_FIELDS))
    except Exception as e:
        logger.error(f"list_jobs query failed: {e}")
        return {"error": str(e)}

    has_more = len(rows) > limit
    next_before_id = jobs[-1]['pandaid'] if jobs and has_more else None

    # Annotate with destinationse from filestable4
    dest_map = _bulk_destinationse([j['pandaid'] for j in jobs])
    for j in jobs:
        j['destinationse'] = dest_map.get(j['pandaid'])
    _apply_effective_owners(jobs, 'produsername')

    return {
        "summary": summary,
        "total_in_window": total,
        "jobs": jobs,
        "count": len(jobs),
        "pagination": {
            "before_id": before_id,
            "has_more": has_more,
            "next_before_id": next_before_id,
            "limit": limit,
        },
        "filters": {
            "days": days,
            "status": status,
            "username": username,
            "site": site,
            "taskid": taskid,
            "reqid": reqid,
        },
    }


def diagnose_jobs(days=7, username=None, site=None, taskid=None,
                  reqid=None, error_component=None, limit=500, before_id=None):
    """Diagnose failed PanDA jobs with full error details."""
    # When scoped to a specific task, return everything — don't truncate
    if taskid:
        limit = 100000
    cutoff = timezone.now() - timedelta(days=days)
    where = [
        '"modificationtime" >= %s',
        '"jobstatus" IN %s',
    ]
    params = [cutoff, tuple(FAULTY_STATUSES)]

    if username:
        clause, val = like_or_eq('produsername', username)
        where.append(clause)
        params.append(val)
    if site:
        clause, val = like_or_eq('computingsite', site)
        where.append(clause)
        params.append(val)
    if taskid:
        where.append('"jeditaskid" = %s')
        params.append(taskid)
    if reqid:
        where.append('"reqid" = %s')
        params.append(reqid)
    if error_component:
        comp = next((c for c in ERROR_COMPONENTS if c['name'] == error_component), None)
        if comp:
            where.append(f'"{comp["code"]}" != 0')
    if before_id:
        where.append('"pandaid" < %s')
        params.append(before_id)

    conn = connections['panda']

    # Build deduplicated field list
    seen = set()
    fields = []
    for f in LIST_FIELDS + [f for f in ERROR_FIELDS if f not in LIST_FIELDS] + DIAGNOSE_EXTRA_FIELDS:
        if f not in seen:
            seen.add(f)
            fields.append(f)

    fetch_limit = limit + 1
    sql, full_params = build_union_query(
        fields, where, params,
        order_by='"pandaid" DESC',
        limit=fetch_limit,
    )

    jobs = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, full_params)
            rows = cursor.fetchall()
            for row in rows[:limit]:
                job = row_to_dict(row, fields)
                job['errors'] = extract_errors(job)
                jobs.append(job)
    except Exception as e:
        logger.error(f"diagnose_jobs query failed: {e}")
        return {"error": str(e)}

    has_more = len(rows) > limit
    next_before_id = jobs[-1]['pandaid'] if jobs and has_more else None

    # Annotate with destinationse from filestable4
    dest_map = _bulk_destinationse([j['pandaid'] for j in jobs])
    for j in jobs:
        j['destinationse'] = dest_map.get(j['pandaid'])

    # Error summary: count by component and top codes
    component_counts = {}
    code_counts = {}
    for job in jobs:
        for err in job['errors']:
            comp = err['component']
            component_counts[comp] = component_counts.get(comp, 0) + 1
            key = f"{comp}:{err['code']}"
            if key not in code_counts:
                code_counts[key] = {'component': comp, 'code': err['code'], 'count': 0, 'sample_diag': err['diag']}
            code_counts[key]['count'] += 1

    top_errors = sorted(code_counts.values(), key=lambda x: x['count'], reverse=True)[:20]

    return {
        "error_summary": {
            "total_faulty_jobs": len(jobs),
            "by_component": component_counts,
            "top_error_codes": top_errors,
        },
        "jobs": jobs,
        "count": len(jobs),
        "pagination": {
            "before_id": before_id,
            "has_more": has_more,
            "next_before_id": next_before_id,
            "limit": limit,
        },
        "filters": {
            "days": days,
            "username": username,
            "site": site,
            "taskid": taskid,
            "reqid": reqid,
            "error_component": error_component,
        },
    }


def _compute_failurerate(nfailed, nfinished):
    """Compute file-like failure rate from job-level counts.

    Useful because the native JEDI `failurerate` column is commonly NULL in
    this PanDA instance — the upstream post-processing that populates it
    isn't running for ePIC task types. Returns None when no jobs have
    reached a terminal success/fail state (avoids 0/0 noise).
    """
    denom = (nfailed or 0) + (nfinished or 0)
    if denom == 0:
        return None
    return round((nfailed or 0) / denom, 4)


def _compute_progress(nactive, nfinished, nfailed):
    """Integer-percent progress derived from job-level counts.

    Substitute for the native JEDI `progress` column, which is NULL here for
    the same reason as `failurerate`. Semantics: fraction of known jobs that
    have reached a terminal state (finished or failed), as an integer %.
    Returns None when there are no known jobs yet.
    """
    total = (nactive or 0) + (nfinished or 0) + (nfailed or 0)
    if total == 0:
        return None
    return round(100 * ((nfinished or 0) + (nfailed or 0)) / total)


def _get_task_job_counts(jeditaskids):
    """Return per-task job counts:
    {jeditaskid: {nactive, nfinished, nfailed, nrunning, nretries,
    nfinalfailed, maxattempt}}.

    Aggregates over jobsactive4 + jobsarchived4 bucketed by JOB_STATUS_CATEGORIES.
    Cancelled and closed are deliberately not reported — alarms surface what
    operators don't know. Missing tasks get zero counts across all keys.

    Extras beyond JOB_STATUS_CATEGORIES:
    - nrunning: count of job records with jobstatus='running' (subset of nactive).
    - nretries: count of job records with attemptnr > 1. In the ePIC PanDA
      schema every retry creates a new job record, so this is the total
      retry count for the task.
    - nfinalfailed: count of job records with jobstatus='failed' AND
      attemptnr >= maxattempt. These are final failures — the job exhausted
      its retry budget. Distinguishes true failures from transient-fail-then-
      retry-succeeds, which matters for alarms.
    - maxattempt: maximum job-level maxattempt currently seen for the task.
    """
    zero_counts = {'nactive': 0, 'nfinished': 0, 'nfailed': 0,
                   'nrunning': 0, 'nretries': 0, 'nfinalfailed': 0,
                   'maxattempt': None}
    if not jeditaskids:
        return {}

    # Build flat {status: category} lookup once
    status_to_cat = {}
    for cat, statuses in JOB_STATUS_CATEGORIES.items():
        for s in statuses:
            status_to_cat[s] = cat

    placeholders = ','.join(['%s'] * len(jeditaskids))
    sql = f"""
        SELECT "jeditaskid", "jobstatus",
               COUNT(*) AS n,
               SUM(CASE WHEN "attemptnr" > 1 THEN 1 ELSE 0 END) AS nretries_part,
               SUM(CASE WHEN "jobstatus"='failed' AND "attemptnr" >= COALESCE("maxattempt", 3) THEN 1 ELSE 0 END) AS nfinalfailed_part,
               MAX(COALESCE("maxattempt", 3)) AS maxattempt_part
        FROM (
            SELECT "jeditaskid", "jobstatus", "attemptnr", "maxattempt"
                FROM "{PANDA_SCHEMA}"."jobsactive4"
                WHERE "jeditaskid" IN ({placeholders})
            UNION ALL
            SELECT "jeditaskid", "jobstatus", "attemptnr", "maxattempt"
                FROM "{PANDA_SCHEMA}"."jobsarchived4"
                WHERE "jeditaskid" IN ({placeholders})
        ) combined
        GROUP BY "jeditaskid", "jobstatus"
    """
    params = list(jeditaskids) + list(jeditaskids)

    counts = {tid: dict(zero_counts) for tid in jeditaskids}
    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, params)
            for tid, jobstatus, n, nretries_part, nfinalfailed_part, maxattempt_part in cursor.fetchall():
                cat = status_to_cat.get(jobstatus)
                if cat is not None:
                    counts[tid][f'n{cat}'] += n
                # else: cancelled, closed, unknown — skipped by design
                if jobstatus == 'running':
                    counts[tid]['nrunning'] += n
                counts[tid]['nretries'] += nretries_part or 0
                counts[tid]['nfinalfailed'] += nfinalfailed_part or 0
                if maxattempt_part is not None:
                    current = counts[tid].get('maxattempt')
                    counts[tid]['maxattempt'] = max(
                        current or 0,
                        maxattempt_part,
                    )
    except Exception as e:
        logger.error(f"_get_task_job_counts failed: {e}")
        # On failure, return zeros so caller still gets a consistent shape.
    return counts


def list_tasks(days=7, status=None, username=None, taskname=None,
               reqid=None, workinggroup=None, taskid=None,
               processingtype=None, limit=25, before_id=None):
    """List JEDI tasks with summary statistics and cursor-based pagination.

    Each task dict is augmented with per-task job counts (nactive, nfinished,
    nfailed) via _get_task_job_counts. See JOB_STATUS_CATEGORIES in
    constants.py for the bucketing; cancelled and closed are excluded.
    """
    cutoff = timezone.now() - timedelta(days=days)
    where = ['COALESCE("modificationtime", "creationdate") >= %s']
    params = [cutoff]

    # Exclude stale non-terminal tasks (created >60 days ago, still pending)
    _stale = _stale_task_filter()
    where.append(_stale['clause'])
    params.extend(_stale['params'])

    if status:
        where.append('"status" = %s')
        params.append(status)
    if username:
        clause, vals = _effective_username_filter('username', username)
        where.append(clause)
        params.extend(vals)
    if taskname:
        clause, val = like_or_eq('taskname', taskname)
        where.append(clause)
        params.append(val)
    if reqid:
        where.append('"reqid" = %s')
        params.append(reqid)
    if workinggroup:
        where.append('"workinggroup" = %s')
        params.append(workinggroup)
    if taskid:
        where.append('"jeditaskid" = %s')
        params.append(taskid)
    if processingtype:
        if processingtype == '__blank__':
            where.append('("processingtype" IS NULL OR "processingtype" = %s)')
            params.append('')
        else:
            clause, val = like_or_eq('processingtype', processingtype)
            where.append(clause)
            params.append(val)
    if before_id:
        where.append('"jeditaskid" < %s')
        params.append(before_id)

    conn = connections['panda']

    # Summary counts (without pagination cursor)
    # Remove the before_id clause and its param (always the last pair if present)
    if before_id:
        count_where = where[:-1]
        count_params = params[:-1]
    else:
        count_where = where
        count_params = params
    count_sql, count_full_params = build_task_count_query(count_where, count_params)

    summary = {}
    total = 0
    try:
        with conn.cursor() as cursor:
            cursor.execute(count_sql, count_full_params)
            for row in cursor.fetchall():
                summary[row[0]] = row[1]
                total += row[1]
    except Exception as e:
        logger.error(f"list_tasks count query failed: {e}")
        return {"error": str(e)}

    fetch_limit = limit + 1
    sql, full_params = build_task_query(
        TASK_LIST_FIELDS, where, params,
        order_by='"jeditaskid" DESC',
        limit=fetch_limit,
    )

    tasks = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, full_params)
            rows = cursor.fetchall()
            for row in rows[:limit]:
                tasks.append(row_to_dict(row, TASK_LIST_FIELDS))
    except Exception as e:
        logger.error(f"list_tasks query failed: {e}")
        return {"error": str(e)}

    # Per-task job counts (nactive, nfinished, nfailed, nrunning, nretries,
    # nfinalfailed) — one extra query. computed_failurerate (all failures)
    # and computed_finalfailurerate (attemptnr>=maxattempt — retry-exhausted)
    # serve as usable substitutes for the native JEDI failurerate column,
    # which is NULL in this deployment. Alarms use the final-failure rate.
    zero = {'nactive': 0, 'nfinished': 0, 'nfailed': 0, 'nrunning': 0,
            'nretries': 0, 'nfinalfailed': 0}
    job_counts = _get_task_job_counts([t['jeditaskid'] for t in tasks])
    for t in tasks:
        c = job_counts.get(t['jeditaskid'], dict(zero))
        t.update(c)
        t['computed_failurerate'] = _compute_failurerate(c['nfailed'], c['nfinished'])
        t['computed_finalfailurerate'] = _compute_failurerate(c['nfinalfailed'], c['nfinished'])
        t['computed_progress'] = _compute_progress(c['nactive'], c['nfinished'], c['nfailed'])
    _apply_effective_owners(tasks, 'username')
    _apply_processing_type_display(tasks)

    has_more = len(rows) > limit
    next_before_id = tasks[-1]['jeditaskid'] if tasks and has_more else None

    return {
        "summary": summary,
        "total_in_window": total,
        "tasks": tasks,
        "count": len(tasks),
        "pagination": {
            "before_id": before_id,
            "has_more": has_more,
            "next_before_id": next_before_id,
            "limit": limit,
        },
        "filters": {
            "days": days,
            "status": status,
            "username": username,
            "taskname": taskname,
            "reqid": reqid,
            "workinggroup": workinggroup,
            "taskid": taskid,
        },
    }


def error_summary(days=10, username=None, site=None, destinationse=None,
                  taskid=None, error_source=None, limit=20):
    """Aggregate error summary across failed PanDA jobs, ranked by frequency."""
    # When scoped to a specific task, return all error patterns
    if taskid:
        limit = 10000
    cutoff = timezone.now() - timedelta(days=days)
    conn = connections['panda']

    extra_params = []
    filters = ''

    if username:
        if '%' in username:
            filters += ' AND "produsername" LIKE %s'
        else:
            filters += ' AND "produsername" = %s'
        extra_params.append(username)
    if site:
        if '%' in site:
            filters += ' AND "computingsite" LIKE %s'
        else:
            filters += ' AND "computingsite" = %s'
        extra_params.append(site)
    destse_filter = ''
    destse_params = []
    if destinationse:
        if '%' in destinationse:
            destse_filter = ' AND f."destinationse" LIKE %s'
        else:
            destse_filter = ' AND f."destinationse" = %s'
        destse_params.append(destinationse)
    if taskid:
        filters += ' AND "jeditaskid" = %s'
        extra_params.append(taskid)

    components_to_query = ERROR_COMPONENTS
    if error_source:
        components_to_query = [c for c in ERROR_COMPONENTS if c['name'] == error_source]
        if not components_to_query:
            return {"error": f"Unknown error_source '{error_source}'. Valid: {[c['name'] for c in ERROR_COMPONENTS]}"}

    parts = []
    all_params = []
    join_type = 'JOIN' if destinationse else 'LEFT JOIN'
    for comp in components_to_query:
        for table in ['jobsactive4', 'jobsarchived4']:
            parts.append(f"""
                SELECT '{comp['name']}' as error_source,
                       j."{comp['code']}" as error_code,
                       j."{comp['diag']}" as error_diag,
                       j."jeditaskid",
                       j."produsername",
                       j."computingsite",
                       f."destinationse"
                FROM "{PANDA_SCHEMA}"."{table}" j
                {join_type} (
                    SELECT DISTINCT "pandaid", "destinationse"
                    FROM "{PANDA_SCHEMA}"."filestable4"
                    WHERE "destinationse" IS NOT NULL
                ) f ON f."pandaid" = j."pandaid"
                WHERE j."modificationtime" >= %s
                  AND j."jobstatus" IN ('failed','cancelled','closed')
                  AND j."{comp['code']}" IS NOT NULL
                  AND j."{comp['code']}" != 0
                  {filters}
                  {destse_filter}
            """)
            all_params.extend([cutoff] + extra_params + destse_params)

    union_sql = ' UNION ALL '.join(parts)
    sql = f"""
        SELECT error_source, error_code,
               LEFT(error_diag, 256) as error_diag,
               COUNT(*) as count,
               COUNT(DISTINCT jeditaskid) as task_count,
               array_agg(DISTINCT produsername) as users,
               array_agg(DISTINCT computingsite) as sites,
               array_agg(DISTINCT destinationse) as destination_sites
        FROM ({union_sql}) errs
        GROUP BY error_source, error_code, LEFT(error_diag, 256)
        ORDER BY count DESC
        LIMIT %s
    """
    all_params.append(limit)

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, all_params)
            rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"error_summary query failed: {e}")
        return {"error": str(e)}

    errors = []
    total = 0
    for row in rows:
        entry = {
            'error_source': row[0],
            'error_code': row[1],
            'error_diag': row[2] or '',
            'count': row[3],
            'task_count': row[4],
            'users': row[5],
            'sites': row[6],
            'destination_sites': row[7],
        }
        total += row[3]
        errors.append(entry)

    return {
        "total_errors": total,
        "errors": errors,
        "count": len(errors),
        "filters": {
            "days": days,
            "username": username,
            "site": site,
            "taskid": taskid,
            "error_source": error_source,
        },
    }


def get_activity(days=1, username=None, site=None, workinggroup=None):
    """Pre-digested overview of PanDA activity — aggregate counts only."""
    cutoff = timezone.now() - timedelta(days=days)
    conn = connections['panda']

    # ── Job filters ──
    job_where = '"modificationtime" >= %s'
    job_params = [cutoff]
    job_filters = ''

    if username:
        clause, vals = _effective_username_filter('produsername', username)
        job_filters += f' AND {clause}'
        job_params.extend(vals)
    if site:
        if '%' in site:
            job_filters += ' AND "computingsite" LIKE %s'
        else:
            job_filters += ' AND "computingsite" = %s'
        job_params.append(site)

    base_job_where = f'{job_where}{job_filters}'

    def _job_agg(group_col):
        sql = f"""
            SELECT "jobstatus", "{group_col}", COUNT(*) FROM (
                SELECT "jobstatus", "{group_col}"
                FROM "{PANDA_SCHEMA}"."jobsactive4"
                WHERE {base_job_where}
                UNION ALL
                SELECT "jobstatus", "{group_col}"
                FROM "{PANDA_SCHEMA}"."jobsarchived4"
                WHERE {base_job_where}
            ) combined
            GROUP BY "jobstatus", "{group_col}"
            ORDER BY COUNT(*) DESC
        """
        full_params = job_params + job_params
        with conn.cursor() as cursor:
            cursor.execute(sql, full_params)
            return cursor.fetchall()

    try:
        status_sql = f"""
            SELECT "jobstatus", COUNT(*) FROM (
                SELECT "jobstatus" FROM "{PANDA_SCHEMA}"."jobsactive4"
                WHERE {base_job_where}
                UNION ALL
                SELECT "jobstatus" FROM "{PANDA_SCHEMA}"."jobsarchived4"
                WHERE {base_job_where}
            ) combined
            GROUP BY "jobstatus"
            ORDER BY COUNT(*) DESC
        """
        with conn.cursor() as cursor:
            cursor.execute(status_sql, job_params + job_params)
            job_by_status = {row[0]: row[1] for row in cursor.fetchall()}

        job_total = sum(job_by_status.values())

        job_user_sql = f"""
            SELECT "jobstatus", "produsername", "jeditaskid", COUNT(*)
            FROM (
                SELECT "jobstatus", "produsername", "jeditaskid"
                FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {base_job_where}
                UNION ALL
                SELECT "jobstatus", "produsername", "jeditaskid"
                FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {base_job_where}
            ) combined
            GROUP BY "jobstatus", "produsername", "jeditaskid"
        """
        with conn.cursor() as cursor:
            cursor.execute(job_user_sql, job_params + job_params)
            job_user_rows = cursor.fetchall()

        job_owner_map = _pcs_owner_map([row[2] for row in job_user_rows])
        user_map = {}
        for status_val, user_val, jeditaskid, count in job_user_rows:
            user_val = job_owner_map.get(jeditaskid) or _canonical_user(user_val)
            if user_val not in user_map:
                user_map[user_val] = {'user': user_val, 'total': 0}
            user_map[user_val][status_val] = user_map[user_val].get(status_val, 0) + count
            user_map[user_val]['total'] += count
        by_user = sorted(user_map.values(), key=lambda x: x['total'], reverse=True)

        site_rows = _job_agg('computingsite')
        site_map = {}
        for status_val, site_val, count in site_rows:
            if site_val not in site_map:
                site_map[site_val] = {'site': site_val, 'total': 0}
            site_map[site_val][status_val] = count
            site_map[site_val]['total'] += count
        by_site = sorted(site_map.values(), key=lambda x: x['total'], reverse=True)

    except Exception as e:
        logger.error(f"get_activity job queries failed: {e}")
        return {"error": str(e)}

    # ── Task aggregation ──
    task_where = ['COALESCE("modificationtime", "creationdate") >= %s']
    task_params = [cutoff]

    _stale = _stale_task_filter()
    task_where.append(_stale['clause'])
    task_params.extend(_stale['params'])

    if username:
        clause, vals = _effective_username_filter('username', username)
        task_where.append(clause)
        task_params.extend(vals)
    if workinggroup:
        task_where.append('"workinggroup" = %s')
        task_params.append(workinggroup)

    task_where_sql = ' AND '.join(task_where)

    try:
        task_status_sql = f"""
            SELECT "status", COUNT(*)
            FROM "{PANDA_SCHEMA}"."jedi_tasks"
            WHERE {task_where_sql}
            GROUP BY "status"
            ORDER BY COUNT(*) DESC
        """
        with conn.cursor() as cursor:
            cursor.execute(task_status_sql, task_params)
            task_by_status = {row[0]: row[1] for row in cursor.fetchall()}

        task_total = sum(task_by_status.values())

        task_type_sql = f"""
            SELECT COALESCE("processingtype", ''), COUNT(*)
            FROM "{PANDA_SCHEMA}"."jedi_tasks"
            WHERE {task_where_sql}
            GROUP BY COALESCE("processingtype", '')
            ORDER BY COUNT(*) DESC
        """
        with conn.cursor() as cursor:
            cursor.execute(task_type_sql, task_params)
            task_by_type = [
                {
                    'type': label,
                    'count': count,
                    'is_test': 'test' in label.lower(),
                    'filter_value': filter_value,
                }
                for label, count, filter_value
                in _aggregate_processing_type_counts(cursor.fetchall())
            ]

        task_user_sql = f"""
            SELECT "status", "username", "jeditaskid"
            FROM "{PANDA_SCHEMA}"."jedi_tasks"
            WHERE {task_where_sql}
        """
        with conn.cursor() as cursor:
            cursor.execute(task_user_sql, task_params)
            task_user_rows = cursor.fetchall()

        task_owner_map = _pcs_owner_map([row[2] for row in task_user_rows])
        task_user_map = {}
        for status_val, user_val, jeditaskid in task_user_rows:
            user_val = task_owner_map.get(jeditaskid) or _canonical_user(user_val)
            if user_val not in task_user_map:
                task_user_map[user_val] = {'user': user_val, 'total': 0}
            task_user_map[user_val][status_val] = task_user_map[user_val].get(status_val, 0) + 1
            task_user_map[user_val]['total'] += 1
        task_by_user = sorted(task_user_map.values(), key=lambda x: x['total'], reverse=True)

    except Exception as e:
        logger.error(f"get_activity task queries failed: {e}")
        return {"error": str(e)}

    return {
        "jobs": {
            "total": job_total,
            "by_status": job_by_status,
            "by_user": by_user,
            "by_site": by_site,
        },
        "tasks": {
            "total": task_total,
            "by_status": task_by_status,
            "by_type": task_by_type,
            "by_user": task_by_user,
        },
        "filters": {
            "days": days,
            "username": username,
            "site": site,
            "workinggroup": workinggroup,
        },
    }


QUEUE_SUMMARY_FIELDS = [
    'status', 'state', 'vo_name', 'resource_type', 'type', 'capability',
    'corepower', 'atlas_site', 'region', 'country', 'tier', 'cloud',
    'container_type', 'pilot_version', 'maxrss', 'maxtime', 'maxwdir',
]


def list_queues(vo=None, status=None, state=None, search=None):
    """List PanDA queues from schedconfig_json with summary fields."""
    conn = connections['panda']
    where = []
    params = []

    if vo:
        where.append(""""data"->>'vo_name' = %s""")
        params.append(vo)
    if status:
        where.append(""""data"->>'status' = %s""")
        params.append(status)
    if state:
        where.append(""""data"->>'state' = %s""")
        params.append(state)
    if search:
        where.append(""""panda_queue" ILIKE %s""")
        params.append(f'%{search}%')

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''

    sql = f"""
        SELECT "panda_queue", "data", "last_update"
        FROM "{PANDA_SCHEMA}"."schedconfig_json"
        {where_sql}
        ORDER BY "panda_queue"
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"list_queues failed: {e}")
        return {"error": str(e)}

    queues = []
    for row in rows:
        data = row[1] or {}
        summary = {'panda_queue': row[0]}
        for f in QUEUE_SUMMARY_FIELDS:
            val = data.get(f)
            if val is not None:
                summary[f] = val
        summary['last_update'] = row[2].isoformat() if row[2] else None
        queues.append(summary)

    return {
        "queues": queues,
        "count": len(queues),
        "filters": {"vo": vo, "status": status, "state": state, "search": search},
    }


def get_queue(panda_queue):
    """Get full configuration for a single PanDA queue."""
    conn = connections['panda']

    sql = f"""
        SELECT "panda_queue", "data", "last_update"
        FROM "{PANDA_SCHEMA}"."schedconfig_json"
        WHERE "panda_queue" = %s
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, [panda_queue])
            row = cursor.fetchone()
    except Exception as e:
        logger.error(f"get_queue failed: {e}")
        return {"error": str(e)}

    if not row:
        return {"error": f"Queue '{panda_queue}' not found"}

    data = row[1] or {}
    # Strip None values for readability
    config = {k: v for k, v in data.items() if v is not None}
    config['panda_queue'] = row[0]
    config['last_update'] = row[2].isoformat() if row[2] else None

    return {"queue": config}


def resource_usage(days=30, site=None, username=None, taskid=None,
                   start_time=None, end_time=None, bucket=None):
    """Aggregate resource usage for finished jobs.

    Reports two core-hour metrics:
    - allocated: actualcorecount × wall time (cores allocated to the job)
    - used: cpuconsumptiontime (CPU time the job actually consumed)

    Only counts jobs that actually ran: jobstatus='finished' with both
    starttime and endtime set. Pre-running queue time is excluded.
    """
    def _time(value, label):
        if value in (None, ''):
            return None
        parsed = value if isinstance(value, datetime) else parse_datetime(str(value))
        if parsed is None:
            raise ValueError(f'{label} must be an ISO-8601 timestamp')
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    explicit_start = start_time not in (None, '')
    try:
        window_end = _time(end_time, 'end_time') or timezone.now()
        window_start = _time(start_time, 'start_time')
        if window_start is None:
            window_start = window_end - timedelta(days=float(days))
        if window_start >= window_end:
            raise ValueError('start_time must be before end_time')
    except (TypeError, ValueError) as exc:
        return {'error': str(exc)}

    bucket = str(bucket or '').strip().lower()
    if bucket not in {'', 'day', 'week'}:
        return {'error': "bucket must be 'day' or 'week'"}
    conn = connections['panda']

    filters = ''
    extra_params = []

    if site:
        clause, val = like_or_eq('computingsite', site)
        filters += f' AND {clause}'
        extra_params.append(val)
    if username:
        clause, val = like_or_eq('produsername', username)
        filters += f' AND {clause}'
        extra_params.append(val)
    if taskid:
        filters += ' AND "jeditaskid" = %s'
        extra_params.append(taskid)

    base_where = (
        '"endtime" >= %s'
        ' AND "endtime" < %s'
        ' AND "jobstatus" = \'finished\''
        ' AND "starttime" IS NOT NULL'
        ' AND "endtime" IS NOT NULL'
        + filters
    )
    base_params = [window_start, window_end] + extra_params

    inner_fields = (
        '"computingsite", "produsername", '
        '"cpuconsumptiontime", "actualcorecount", "corecount", '
        '"starttime", "endtime"'
    )

    agg_cols = """
        COUNT(*) as job_count,
        COALESCE(SUM(
            EXTRACT(EPOCH FROM ("endtime" - "starttime"))
            * COALESCE("actualcorecount", "corecount", 1)
        ), 0) / 3600.0 as allocated_core_hours,
        COALESCE(SUM("cpuconsumptiontime"), 0) / 3600.0 as used_core_hours,
        COALESCE(SUM(
            EXTRACT(EPOCH FROM ("endtime" - "starttime"))
        ), 0) / 3600.0 as wall_hours
    """

    def _run(group_col=None):
        select = f'"{group_col}", {agg_cols}' if group_col else agg_cols
        group = f'GROUP BY "{group_col}" ORDER BY allocated_core_hours DESC' if group_col else ''
        sql = f"""
            SELECT {select} FROM (
                SELECT {inner_fields}
                FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {base_where}
                UNION ALL
                SELECT {inner_fields}
                FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {base_where}
            ) combined
            {group}
        """
        with conn.cursor() as cursor:
            cursor.execute(sql, base_params + base_params)
            return cursor.fetchall()

    def _parse(row, offset=0):
        return {
            'job_count': row[offset],
            'allocated_core_hours': round(float(row[offset + 1]), 1),
            'used_core_hours': round(float(row[offset + 2]), 1),
            'wall_hours': round(float(row[offset + 3]), 1),
        }

    try:
        rows = _run()
        totals = _parse(rows[0]) if rows else {
            'job_count': 0, 'allocated_core_hours': 0,
            'used_core_hours': 0, 'wall_hours': 0,
        }

        by_site = []
        for row in _run('computingsite'):
            entry = _parse(row, offset=1)
            entry['site'] = row[0] or 'unknown'
            by_site.append(entry)

        by_user = []
        for row in _run('produsername'):
            entry = _parse(row, offset=1)
            entry['user'] = row[0]
            by_user.append(entry)

        series = []
        if bucket:
            bucket_timezone = timezone.get_current_timezone_name()
            sql = f"""
                SELECT date_trunc(
                           '{bucket}',
                           ("endtime" AT TIME ZONE 'UTC') AT TIME ZONE %s
                       ) AS bucket_start,
                       "computingsite", {agg_cols}
                FROM (
                    SELECT {inner_fields}
                    FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {base_where}
                    UNION ALL
                    SELECT {inner_fields}
                    FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {base_where}
                ) combined
                GROUP BY bucket_start, "computingsite"
                ORDER BY bucket_start, "computingsite"
            """
            with conn.cursor() as cursor:
                cursor.execute(sql, [bucket_timezone] + base_params + base_params)
                time_rows = cursor.fetchall()
            for row in time_rows:
                entry = _parse(row, offset=2)
                entry['bucket_start'] = row[0].isoformat()
                entry['site'] = row[1] or 'unknown'
                series.append(entry)

    except Exception as e:
        logger.error(f"resource_usage query failed: {e}")
        return {"error": str(e)}

    return {
        "totals": totals,
        "by_site": by_site,
        "by_user": by_user,
        "series": series,
        "window": {
            "start_time": window_start.isoformat(),
            "end_time": window_end.isoformat(),
            "bucket": bucket or None,
            "bucket_timezone": (timezone.get_current_timezone_name()
                                if bucket else None),
            "time_field": "endtime",
        },
        "filters": {
            "days": None if explicit_start else days,
            "site": site,
            "username": username,
            "taskid": taskid,
            "start_time": window_start.isoformat(),
            "end_time": window_end.isoformat(),
            "bucket": bucket or None,
        },
    }


def study_job(pandaid):
    """Deep study of a single PanDA job — full record, files, harvester logs, errors."""
    conn = connections['panda']

    # 1. Full job record from both tables
    field_list = ', '.join(f'"{f}"' for f in STUDY_FIELDS)
    job_sql = f"""
        SELECT {field_list} FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE "pandaid" = %s
        UNION ALL
        SELECT {field_list} FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE "pandaid" = %s
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(job_sql, [pandaid, pandaid])
            row = cursor.fetchone()
    except Exception as e:
        logger.error(f"study_job query failed: {e}")
        return {"error": str(e)}

    if not row:
        return {"error": f"Job {pandaid} not found"}

    full_job = row_to_dict(row, STUDY_FIELDS)
    job = dict(full_job)
    job['errors'] = extract_errors(job)

    # Strip null fields for readability
    job = {k: v for k, v in job.items() if v is not None}

    # Parse pilotid for log URLs
    log_urls = {}
    pilotid = job.get('pilotid', '')
    if pilotid and '|' in pilotid:
        parts = pilotid.split('|')
        stdout_url = parts[0]
        log_urls['pilot_stdout'] = stdout_url
        log_urls['pilot_stderr'] = stdout_url.replace('.out', '.err')
        log_urls['batch_log'] = stdout_url.replace('.out', '.log')
        if len(parts) >= 4:
            job['pilot_type'] = parts[1]
            job['pilot_version'] = parts[3]

    # NERSC Perlmutter pilotid ends in literal 'None' so the synthesized URLs
    # 404. The NERSC portal exposes per-job log dirs instead.
    site = job.get('computingsite') or ''
    if site.startswith('NERSC_Perlmutter'):
        portal_urls = _nersc_portal_log_urls(site, pandaid)
        if portal_urls:
            # Drop the broken stderr/batch entries; Perlmutter has a single
            # combined pilot log.
            log_urls.pop('pilot_stderr', None)
            log_urls.pop('batch_log', None)
            log_urls.update(portal_urls)

    # 2. Files from filestable4
    file_field_list = ', '.join(f'"{f}"' for f in FILE_FIELDS)
    files_sql = f"""
        SELECT {file_field_list}
        FROM "{PANDA_SCHEMA}"."filestable4"
        WHERE "pandaid" = %s
        ORDER BY "type", "lfn"
    """

    files = []
    log_file = None
    try:
        with conn.cursor() as cursor:
            cursor.execute(files_sql, [pandaid])
            for frow in cursor.fetchall():
                fd = row_to_dict(frow, FILE_FIELDS)
                fd = {k: v for k, v in fd.items() if v is not None}
                files.append(fd)
                if fd.get('type') == 'log':
                    log_file = fd
    except Exception as e:
        logger.error(f"study_job files query failed: {e}")
        # Non-fatal — continue with what we have

    # 3. Harvester worker info (condor log URLs)
    harvester = None
    harvester_sql = f"""
        SELECT w."workerid", w."harvesterid", w."stdout", w."stderr", w."batchlog",
               w."errorcode", w."diagmessage", w."status"
        FROM "{PANDA_SCHEMA}"."harvester_workers" w
        JOIN "{PANDA_SCHEMA}"."harvester_rel_jobs_workers" r
            ON w."workerid" = r."workerid" AND w."harvesterid" = r."harvesterid"
        WHERE r."pandaid" = %s
    """

    try:
        with conn.cursor() as cursor:
            cursor.execute(harvester_sql, [pandaid])
            hrow = cursor.fetchone()
            if hrow:
                hcols = ['workerid', 'harvesterid', 'stdout', 'stderr', 'batchlog',
                         'errorcode', 'diagmessage', 'status']
                harvester = row_to_dict(hrow, hcols)
                harvester = {k: v for k, v in harvester.items() if v is not None}
                # Use harvester URLs if available (more authoritative than parsed pilotid)
                if harvester.get('stdout'):
                    log_urls['pilot_stdout'] = harvester['stdout']
                if harvester.get('stderr'):
                    log_urls['pilot_stderr'] = harvester['stderr']
                if harvester.get('batchlog'):
                    log_urls['batch_log'] = harvester['batchlog']
    except Exception as e:
        logger.error(f"study_job harvester query failed: {e}")
        # Non-fatal

    # 4. Task context (parent task name and status)
    task_info = None
    jeditaskid = job.get('jeditaskid')
    if jeditaskid:
        task_sql = f"""
            SELECT "jeditaskid", "taskname", "status", "username", "errordialog",
                   "failurerate", "workinggroup"
            FROM "{PANDA_SCHEMA}"."jedi_tasks"
            WHERE "jeditaskid" = %s
        """
        try:
            with conn.cursor() as cursor:
                cursor.execute(task_sql, [jeditaskid])
                trow = cursor.fetchone()
                if trow:
                    tcols = ['jeditaskid', 'taskname', 'status', 'username',
                             'errordialog', 'failurerate', 'workinggroup']
                    task_info = row_to_dict(trow, tcols)
                    task_info = {k: v for k, v in task_info.items() if v is not None}
                    _apply_effective_owners([task_info], 'username')
        except Exception as e:
            logger.error(f"study_job task query failed: {e}")

    # Assemble result
    result = {
        "pandaid": pandaid,
        "job": job,
        "job_record": full_job,
        "files": files,
        "log_urls": log_urls,
    }

    if log_file:
        result["log_file"] = log_file

    if harvester:
        result["harvester"] = harvester

    if task_info:
        result["task"] = task_info

    # Official PanDA monitoring page URL.
    result["monitor_url"] = f"https://pandamon01.sdcc.bnl.gov/job?pandaid={pandaid}"

    # 5. Log analysis for failure-adjacent statuses. 'closed' covers
    # lost-heartbeat (pilot killed at slot boundary before reporting back);
    # its pilot log on NERSC CFS is the only window into what happened.
    jobstatus = job.get('jobstatus', '')
    if jobstatus in ('failed', 'holding', 'cancelled', 'closed'):
        try:
            from askpanda_atlas.log_analysis_impl import (
                _select_log_filename, _fetch_log_text,
                extract_log_excerpt, classify_failure,
            )
            from decouple import config
            base_url = config('PANDA_BASE_URL', default='https://pandamon01.sdcc.bnl.gov')
            log_filename = _select_log_filename(job)
            log_text = _fetch_log_text(pandaid, log_filename, base_url, timeout=30)
            log_source = 'filebrowser'

            # Fallback: fetch pilot log directly from its URL (NERSC portal for
            # Perlmutter, Harvester-published URL elsewhere).
            if not log_text:
                direct_url = (
                    log_urls.get('pilot_stdout')
                    or (harvester or {}).get('stdout')
                )
                if direct_url:
                    import requests as _requests
                    try:
                        resp = _requests.get(
                            direct_url, timeout=30, verify=False,
                        )
                        if resp.status_code == 200 and resp.text:
                            log_text = resp.text
                            log_source = (
                                'nersc_portal'
                                if 'portal.nersc.gov' in direct_url
                                else 'harvester'
                            )
                    except Exception as exc:
                        logger.warning("Direct log fetch failed: %s", exc)

            if log_text:
                piloterrorcode = int(job.get('piloterrorcode') or 0)
                piloterrordiag = str(job.get('piloterrordiag') or '')
                excerpt = extract_log_excerpt(
                    log_text, log_filename, piloterrorcode, piloterrordiag
                )
                failure_type = classify_failure(job, excerpt)
                result['log_analysis'] = {
                    'failure_type': failure_type,
                    'log_filename': log_filename,
                    'log_source': log_source,
                    'log_excerpt': excerpt,
                    'log_available': True,
                }
            else:
                result['log_analysis'] = {
                    'log_available': False,
                    'log_filename': log_filename,
                }
        except Exception as e:
            logger.error(f"study_job log analysis failed: {e}")
            result['log_analysis'] = {'error': str(e)}

    return result


# ── DataTables query functions ───────────────────────────────────────────────

# Orderable columns for jobs and tasks (maps column name to SQL expression)
JOB_ORDER_COLUMNS = {f: f'"{f}"' for f in LIST_FIELDS}
TASK_ORDER_COLUMNS = {f: f'"{f}"' for f in TASK_LIST_FIELDS}

# Searchable columns for DataTables global search
JOB_SEARCH_FIELDS = ['pandaid', 'jeditaskid', 'produsername', 'jobstatus',
                     'computingsite', 'transformation']
TASK_SEARCH_FIELDS = ['jeditaskid', 'taskname', 'status', 'username',
                      'processingtype', 'workinggroup', 'transpath']


def list_jobs_dt(days=7, status=None, username=None, site=None,
                 taskid=None, reqid=None,
                 order_by='"pandaid" DESC', limit=100, offset=0, search=None):
    """List PanDA jobs for DataTables (returns rows, total, filtered counts)."""
    cutoff = timezone.now() - timedelta(days=days)
    where = ['"modificationtime" >= %s']
    params = [cutoff]

    if status:
        where.append('"jobstatus" = %s')
        params.append(status)
    if username:
        clause, vals = _effective_username_filter('produsername', username)
        where.append(clause)
        params.extend(vals)
    if site:
        clause, val = like_or_eq('computingsite', site)
        where.append(clause)
        params.append(val)
    if taskid:
        where.append('"jeditaskid" = %s')
        params.append(int(taskid))
    if reqid:
        where.append('"reqid" = %s')
        params.append(int(reqid))

    conn = connections['panda']

    # Total count (no search filter)
    count_sql, count_params = build_union_count(where, params)
    try:
        with conn.cursor() as cursor:
            cursor.execute(count_sql, count_params)
            total = cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"list_jobs_dt count failed: {e}")
        return [], 0, 0

    # Apply search filter
    filtered_where = list(where)
    filtered_params = list(params)
    if search:
        search_clause, search_params = build_search_clauses(JOB_SEARCH_FIELDS, search)
        filtered_where.append(search_clause)
        filtered_params.extend(search_params)

    # Filtered count
    if search:
        fcount_sql, fcount_params = build_union_count(filtered_where, filtered_params)
        try:
            with conn.cursor() as cursor:
                cursor.execute(fcount_sql, fcount_params)
                filtered = cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"list_jobs_dt filtered count failed: {e}")
            return [], total, 0
    else:
        filtered = total

    # Data query
    sql, full_params = build_union_query_dt(
        LIST_FIELDS, filtered_where, filtered_params,
        order_by=order_by, limit=limit, offset=offset,
    )

    rows = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, full_params)
            for row in cursor.fetchall():
                rows.append(row_to_dict(row, LIST_FIELDS))
    except Exception as e:
        logger.error(f"list_jobs_dt query failed: {e}")
        return [], total, filtered

    _apply_effective_owners(rows, 'produsername')
    return rows, total, filtered


def list_tasks_dt(days=7, status=None, username=None, taskname=None,
                  processingtype=None,
                  workinggroup=None, order_by='"jeditaskid" DESC',
                  limit=100, offset=0, search=None):
    """List JEDI tasks for DataTables (returns rows, total, filtered counts)."""
    cutoff = timezone.now() - timedelta(days=days)
    where = ['COALESCE("modificationtime", "creationdate") >= %s']
    params = [cutoff]

    _stale = _stale_task_filter()
    where.append(_stale['clause'])
    params.extend(_stale['params'])

    if status:
        where.append('"status" = %s')
        params.append(status)
    if username:
        clause, vals = _effective_username_filter('username', username)
        where.append(clause)
        params.extend(vals)
    if taskname:
        clause, val = like_or_eq('taskname', taskname)
        where.append(clause)
        params.append(val)
    if processingtype:
        if processingtype == '__blank__':
            where.append('("processingtype" IS NULL OR "processingtype" = %s)')
            params.append('')
        else:
            clause, val = like_or_eq('processingtype', processingtype)
            where.append(clause)
            params.append(val)
    if workinggroup:
        where.append('"workinggroup" = %s')
        params.append(workinggroup)

    conn = connections['panda']

    # Total count
    count_sql, count_params = build_task_count(where, params)
    try:
        with conn.cursor() as cursor:
            cursor.execute(count_sql, count_params)
            total = cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"list_tasks_dt count failed: {e}")
        return [], 0, 0

    # Apply search filter
    filtered_where = list(where)
    filtered_params = list(params)
    if search:
        search_clause, search_params = build_search_clauses(TASK_SEARCH_FIELDS, search)
        filtered_where.append(search_clause)
        filtered_params.extend(search_params)

    # Filtered count
    if search:
        fcount_sql, fcount_params = build_task_count(filtered_where, filtered_params)
        try:
            with conn.cursor() as cursor:
                cursor.execute(fcount_sql, fcount_params)
                filtered = cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"list_tasks_dt filtered count failed: {e}")
            return [], total, 0
    else:
        filtered = total

    # Data query
    sql, full_params = build_task_query_dt(
        TASK_LIST_FIELDS, filtered_where, filtered_params,
        order_by=order_by, limit=limit, offset=offset,
    )

    rows = []
    n_base = len(TASK_LIST_FIELDS)
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, full_params)
            for row in cursor.fetchall():
                # build_task_query_dt returns TASK_LIST_FIELDS + 9 aggregate
                # columns (in order): nactive, nfinished, nfailed, nrunning,
                # nretries, computed_failurerate, computed_progress,
                # nfinalfailed, computed_finalfailurerate.
                task = row_to_dict(row[:n_base], TASK_LIST_FIELDS)
                task['nactive'] = row[n_base]
                task['nfinished'] = row[n_base + 1]
                task['nfailed'] = row[n_base + 2]
                task['nrunning'] = row[n_base + 3]
                task['nretries'] = row[n_base + 4]
                fr = row[n_base + 5]
                task['computed_failurerate'] = float(fr) if fr is not None else None
                task['computed_progress'] = row[n_base + 6]  # already integer or None
                task['nfinalfailed'] = row[n_base + 7]
                ffr = row[n_base + 8]
                task['computed_finalfailurerate'] = float(ffr) if ffr is not None else None
                rows.append(task)
    except Exception as e:
        logger.error(f"list_tasks_dt query failed: {e}")
        return [], total, filtered

    _apply_effective_owners(rows, 'username')
    _apply_processing_type_display(rows)
    return rows, total, filtered


def job_filter_counts(days=7, status=None, username=None, site=None,
                      taskid=None, reqid=None):
    """Get filter option counts for job list (status, user, site)."""
    cutoff = timezone.now() - timedelta(days=days)
    base_where = ['"modificationtime" >= %s']
    base_params = [cutoff]

    if taskid:
        base_where.append('"jeditaskid" = %s')
        base_params.append(int(taskid))
    if reqid:
        base_where.append('"reqid" = %s')
        base_params.append(int(reqid))

    conn = connections['panda']
    result = {}

    filter_config = [
        ('jobstatus', 'status', status),
        ('produsername', 'username', username),
        ('computingsite', 'site', site),
    ]

    for db_field, filter_name, current_value in filter_config:
        # Apply all other filters except this one
        where = list(base_where)
        params = list(base_params)
        for other_db_field, other_name, other_value in filter_config:
            if other_name != filter_name and other_value:
                if other_name == 'username':
                    clause, vals = _effective_username_filter(other_db_field, other_value)
                    where.append(clause)
                    params.extend(vals)
                    continue
                clause, val = like_or_eq(other_db_field, other_value)
                where.append(clause)
                params.append(val)

        if filter_name == 'username':
            where_sql = ' WHERE ' + ' AND '.join(where) if where else ''
            sql = f"""
                SELECT "produsername", "jeditaskid", COUNT(*) FROM (
                    SELECT "produsername", "jeditaskid"
                    FROM "{PANDA_SCHEMA}"."jobsactive4"{where_sql}
                    UNION ALL
                    SELECT "produsername", "jeditaskid"
                    FROM "{PANDA_SCHEMA}"."jobsarchived4"{where_sql}
                ) combined
                GROUP BY "produsername", "jeditaskid"
            """
            full_params = list(params) + list(params)
        else:
            sql, full_params = build_union_count_by_field(db_field, where, params)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, full_params)
                rows = cursor.fetchall()
                if filter_name == 'username':
                    result[filter_name] = _aggregate_effective_user_counts(rows)
                else:
                    result[filter_name] = [(row[0], row[1]) for row in rows]
        except Exception as e:
            logger.error(f"job_filter_counts {filter_name} failed: {e}")
            result[filter_name] = []

    return result


def task_filter_counts(days=7, status=None, username=None,
                       processingtype=None, workinggroup=None):
    """Get filter option counts for task list."""
    cutoff = timezone.now() - timedelta(days=days)
    base_where = ['COALESCE("modificationtime", "creationdate") >= %s']
    base_params = [cutoff]

    _stale = _stale_task_filter()
    base_where.append(_stale['clause'])
    base_params.extend(_stale['params'])

    conn = connections['panda']
    result = {}

    filter_config = [
        ('status', 'status', status),
        ('username', 'username', username),
        ('processingtype', 'processingtype', processingtype),
        ('workinggroup', 'workinggroup', workinggroup),
    ]

    for db_field, filter_name, current_value in filter_config:
        where = list(base_where)
        params = list(base_params)
        for other_db_field, other_name, other_value in filter_config:
            if other_name != filter_name and other_value:
                if other_name == 'username':
                    clause, vals = _effective_username_filter(other_db_field, other_value)
                    where.append(clause)
                    params.extend(vals)
                elif other_db_field == 'processingtype' and other_value == '__blank__':
                    where.append('("processingtype" IS NULL OR "processingtype" = %s)')
                    params.append('')
                else:
                    where.append(f'"{other_db_field}" = %s')
                    params.append(other_value)

        if filter_name == 'username':
            where_sql = ' WHERE ' + ' AND '.join(where) if where else ''
            sql = f"""
                SELECT "username", "jeditaskid", COUNT(*)
                FROM "{PANDA_SCHEMA}"."jedi_tasks"{where_sql}
                GROUP BY "username", "jeditaskid"
            """
            full_params = list(params)
        else:
            sql, full_params = build_task_count_by_field(db_field, where, params)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, full_params)
                rows = cursor.fetchall()
                if filter_name == 'username':
                    result[filter_name] = _aggregate_effective_user_counts(rows)
                elif filter_name == 'processingtype':
                    result[filter_name] = [
                        (label, count, filter_value)
                        for label, count, filter_value
                        in _aggregate_processing_type_counts(rows)
                    ]
                else:
                    result[filter_name] = [(row[0], row[1]) for row in rows]
        except Exception as e:
            logger.error(f"task_filter_counts {filter_name} failed: {e}")
            result[filter_name] = []

    return result


def get_task(jeditaskid):
    """Get a single JEDI task record."""
    conn = connections['panda']
    field_list = ', '.join(f'"{f}"' for f in TASK_LIST_FIELDS)
    sql = f"""
        SELECT {field_list}
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE "jeditaskid" = %s
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, [jeditaskid])
            row = cursor.fetchone()
    except Exception as e:
        logger.error(f"get_task query failed: {e}")
        return {"error": str(e)}

    if not row:
        return {"error": f"Task {jeditaskid} not found"}

    task = row_to_dict(row, TASK_LIST_FIELDS)
    zero = {'nactive': 0, 'nfinished': 0, 'nfailed': 0, 'nrunning': 0,
            'nretries': 0, 'nfinalfailed': 0}
    c = _get_task_job_counts([jeditaskid]).get(jeditaskid, dict(zero))
    task.update(c)
    task['computed_failurerate'] = _compute_failurerate(c['nfailed'], c['nfinished'])
    task['computed_finalfailurerate'] = _compute_failurerate(c['nfinalfailed'], c['nfinished'])
    task['computed_progress'] = _compute_progress(c['nactive'], c['nfinished'], c['nfailed'])
    _apply_effective_owners([task], 'username')
    task['datasets'] = _get_task_datasets(jeditaskid)
    task['taskparams'], task['job_parameters'] = _get_task_parameters(jeditaskid)
    task['task_record'] = _get_task_record(jeditaskid)
    return task
