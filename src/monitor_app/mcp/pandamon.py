"""
PanDA Monitor MCP tools.

Provides LLM access to the PanDA job database (doma_panda schema) for
ePIC production monitoring. Queries jobsactive4 (current jobs) and
jobsarchived4 (recently completed jobs) via the 'panda' database connection.

Error diagnostics are the core value — 7 error components (pilot, executor,
DDM, brokerage, dispatcher, supervisor, taskbuffer) each with code + diag text.
"""

import logging
from datetime import timedelta
from django.utils import timezone
from django.db import connections
from asgiref.sync import sync_to_async

from mcp_server import mcp_server as mcp
from .common import _monitor_url

logger = logging.getLogger(__name__)

PANDA_SCHEMA = 'doma_panda'

# Fields for list_jobs overview
LIST_FIELDS = [
    'pandaid', 'jeditaskid', 'reqid', 'produsername', 'jobstatus',
    'jobsubstatus', 'computingsite', 'transformation', 'processingtype',
    'currentpriority', 'creationtime', 'starttime', 'endtime',
    'modificationtime', 'attemptnr', 'maxattempt', 'corecount',
    'cpuconsumptiontime', 'nevents', 'transexitcode',
]

# Error diagnostic fields — the 80% of the point
ERROR_FIELDS = [
    'brokerageerrorcode', 'brokerageerrordiag',
    'ddmerrorcode', 'ddmerrordiag',
    'exeerrorcode', 'exeerrordiag',
    'jobdispatchererrorcode', 'jobdispatchererrordiag',
    'piloterrorcode', 'piloterrordiag',
    'superrorcode', 'superrordiag',
    'taskbuffererrorcode', 'taskbuffererrordiag',
    'transexitcode',
]

# Additional fields for diagnose (richer context for failed jobs)
DIAGNOSE_EXTRA_FIELDS = [
    'jobname', 'pilotid', 'computingelement', 'jobmetrics',
    'specialhandling', 'commandtopilot', 'maxrss', 'maxpss',
]

ERROR_COMPONENTS = [
    {'name': 'brokerage', 'code': 'brokerageerrorcode', 'diag': 'brokerageerrordiag'},
    {'name': 'ddm', 'code': 'ddmerrorcode', 'diag': 'ddmerrordiag'},
    {'name': 'executor', 'code': 'exeerrorcode', 'diag': 'exeerrordiag'},
    {'name': 'dispatcher', 'code': 'jobdispatchererrorcode', 'diag': 'jobdispatchererrordiag'},
    {'name': 'pilot', 'code': 'piloterrorcode', 'diag': 'piloterrordiag'},
    {'name': 'supervisor', 'code': 'superrorcode', 'diag': 'superrordiag'},
    {'name': 'taskbuffer', 'code': 'taskbuffererrorcode', 'diag': 'taskbuffererrordiag'},
]

FAULTY_STATUSES = ('failed', 'cancelled', 'closed')


def _build_union_query(fields, where_clauses, params, order_by, limit):
    """
    Build a UNION ALL query across jobsactive4 and jobsarchived4.
    Returns (sql, params).
    """
    field_list = ', '.join(f'"{f}"' for f in fields)
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)

    sql = f"""
        SELECT * FROM (
            SELECT {field_list} FROM "{PANDA_SCHEMA}"."jobsactive4"{where_sql}
            UNION ALL
            SELECT {field_list} FROM "{PANDA_SCHEMA}"."jobsarchived4"{where_sql}
        ) combined
        ORDER BY {order_by}
        LIMIT {limit}
    """
    # params are used twice (once per table in the UNION)
    full_params = list(params) + list(params)
    return sql, full_params


def _build_count_query(where_clauses, params):
    """Build a count-by-status query across both tables."""
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)

    sql = f"""
        SELECT "jobstatus", COUNT(*) FROM (
            SELECT "jobstatus" FROM "{PANDA_SCHEMA}"."jobsactive4"{where_sql}
            UNION ALL
            SELECT "jobstatus" FROM "{PANDA_SCHEMA}"."jobsarchived4"{where_sql}
        ) combined
        GROUP BY "jobstatus"
        ORDER BY COUNT(*) DESC
    """
    full_params = list(params) + list(params)
    return sql, full_params


def _row_to_dict(row, fields):
    """Convert a database row to a dict, formatting datetimes."""
    result = {}
    for i, field in enumerate(fields):
        val = row[i]
        if val is not None and hasattr(val, 'isoformat'):
            val = val.isoformat()
        result[field] = val
    return result


def _extract_errors(job_dict):
    """
    Extract non-zero error components from a job dict.
    Returns a list of {component, code, diag} for non-zero errors.
    """
    errors = []
    for comp in ERROR_COMPONENTS:
        code = job_dict.get(comp['code'])
        if code and int(code) != 0:
            errors.append({
                'component': comp['name'],
                'code': int(code),
                'diag': job_dict.get(comp['diag'], ''),
            })
    transexitcode = job_dict.get('transexitcode')
    if transexitcode and str(transexitcode).strip() not in ('', '0'):
        errors.append({
            'component': 'transformation',
            'code': transexitcode,
            'diag': '',
        })
    return errors


# -----------------------------------------------------------------------------
# panda_list_jobs
# -----------------------------------------------------------------------------

@mcp.tool()
async def panda_list_jobs(
    days: int = 7,
    status: str = None,
    username: str = None,
    site: str = None,
    taskid: int = None,
    reqid: int = None,
    limit: int = 500,
    before_id: int = None,
) -> dict:
    """
    List PanDA jobs from the ePIC production database with summary statistics.

    Returns jobs in reverse time order (newest first) with cursor-based pagination.
    Use before_id to page through results: pass the last pandaid from the previous
    call to get the next batch.

    For error diagnostics on failed jobs, use panda_diagnose_jobs instead.

    Args:
        days: Time window in days (default 7). Jobs with modificationtime within this window.
        status: Filter by jobstatus (e.g. 'failed', 'finished', 'running', 'activated').
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        site: Filter by computing site (computingsite). Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID (jeditaskid).
        reqid: Filter by request ID.
        limit: Maximum jobs to return (default 500).
        before_id: Pagination cursor — return jobs with pandaid < this value.

    Returns:
        summary: Job counts by status for the full query (not just this page).
        jobs: List of job records with key fields.
        pagination: {before_id, has_more, next_before_id} for incremental pulling.
        total_in_window: Total jobs matching filters in the time window.
    """
    @sync_to_async
    def fetch():
        cutoff = timezone.now() - timedelta(days=days)
        where = ['"modificationtime" >= %s']
        params = [cutoff]

        if status:
            where.append('"jobstatus" = %s')
            params.append(status)
        if username:
            if '%' in username:
                where.append('"produsername" LIKE %s')
            else:
                where.append('"produsername" = %s')
            params.append(username)
        if site:
            if '%' in site:
                where.append('"computingsite" LIKE %s')
            else:
                where.append('"computingsite" = %s')
            params.append(site)
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

        # Get summary counts (without pagination cursor, to show full picture)
        count_where = [w for w in where if '"pandaid" <' not in w]
        count_params = [p for i, p in enumerate(params) if '"pandaid" <' not in where[i]]
        count_sql, count_full_params = _build_count_query(count_where, count_params)

        summary = {}
        total = 0
        try:
            with conn.cursor() as cursor:
                cursor.execute(count_sql, count_full_params)
                for row in cursor.fetchall():
                    summary[row[0]] = row[1]
                    total += row[1]
        except Exception as e:
            logger.error(f"panda_list_jobs count query failed: {e}")
            return {"error": str(e)}

        # Fetch actual rows (with pagination)
        fetch_limit = limit + 1  # fetch one extra to detect has_more
        sql, full_params = _build_union_query(
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
                    jobs.append(_row_to_dict(row, LIST_FIELDS))
        except Exception as e:
            logger.error(f"panda_list_jobs query failed: {e}")
            return {"error": str(e)}

        has_more = len(rows) > limit
        next_before_id = jobs[-1]['pandaid'] if jobs and has_more else None

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

    return await fetch()


# -----------------------------------------------------------------------------
# panda_diagnose_jobs
# -----------------------------------------------------------------------------

@mcp.tool()
async def panda_diagnose_jobs(
    days: int = 7,
    username: str = None,
    site: str = None,
    taskid: int = None,
    reqid: int = None,
    error_component: str = None,
    limit: int = 500,
    before_id: int = None,
) -> dict:
    """
    Diagnose failed and faulty PanDA jobs with full error details.

    Pulls only jobs in failed/cancelled/closed status with non-zero error codes.
    Returns all 7 error component fields (pilot, executor, DDM, brokerage,
    dispatcher, supervisor, taskbuffer) plus transformation exit code, distilled
    into a structured errors list per job.

    Use this after panda_list_jobs shows failures you want to understand.

    Args:
        days: Time window in days (default 7).
        username: Filter by job owner (produsername). Supports SQL LIKE with %.
        site: Filter by computing site. Supports SQL LIKE with %.
        taskid: Filter by JEDI task ID.
        reqid: Filter by request ID.
        error_component: Filter to jobs with errors in this component
                         (pilot, executor, ddm, brokerage, dispatcher, supervisor, taskbuffer).
        limit: Maximum jobs to return (default 500).
        before_id: Pagination cursor — return jobs with pandaid < this value.

    Returns:
        error_summary: Counts by error component and top error codes.
        jobs: Failed jobs with full error details and structured errors list.
        pagination: {before_id, has_more, next_before_id} for incremental pulling.
    """
    @sync_to_async
    def fetch():
        cutoff = timezone.now() - timedelta(days=days)
        where = [
            '"modificationtime" >= %s',
            '"jobstatus" IN %s',
        ]
        params = [cutoff, tuple(FAULTY_STATUSES)]

        if username:
            if '%' in username:
                where.append('"produsername" LIKE %s')
            else:
                where.append('"produsername" = %s')
            params.append(username)
        if site:
            if '%' in site:
                where.append('"computingsite" LIKE %s')
            else:
                where.append('"computingsite" = %s')
            params.append(site)
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
        fields = LIST_FIELDS + [f for f in ERROR_FIELDS if f not in LIST_FIELDS] + DIAGNOSE_EXTRA_FIELDS
        # Deduplicate while preserving order
        seen = set()
        unique_fields = []
        for f in fields:
            if f not in seen:
                seen.add(f)
                unique_fields.append(f)
        fields = unique_fields

        fetch_limit = limit + 1
        sql, full_params = _build_union_query(
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
                    job = _row_to_dict(row, fields)
                    job['errors'] = _extract_errors(job)
                    jobs.append(job)
        except Exception as e:
            logger.error(f"panda_diagnose_jobs query failed: {e}")
            return {"error": str(e)}

        has_more = len(rows) > limit
        next_before_id = jobs[-1]['pandaid'] if jobs and has_more else None

        # Build error summary: count by component and top codes
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

        # Top error codes sorted by frequency
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

    return await fetch()
