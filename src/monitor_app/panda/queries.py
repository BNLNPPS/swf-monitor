"""
PanDA database query functions for ePIC production monitoring.

All functions are synchronous — they use django.db.connections['panda']
directly. Callers in async contexts should wrap with sync_to_async.
"""

import logging
from datetime import timedelta
from django.utils import timezone
from django.db import connections

from .constants import (
    PANDA_SCHEMA, LIST_FIELDS, ERROR_FIELDS, DIAGNOSE_EXTRA_FIELDS,
    ERROR_COMPONENTS, FAULTY_STATUSES, TASK_LIST_FIELDS,
)
from .sql import (
    build_union_query, build_count_query,
    build_task_query, build_task_count_query,
    row_to_dict, extract_errors, like_or_eq,
)

logger = logging.getLogger(__name__)


def list_jobs(days=7, status=None, username=None, site=None,
              taskid=None, reqid=None, limit=200, before_id=None):
    """List PanDA jobs with summary statistics and cursor-based pagination."""
    cutoff = timezone.now() - timedelta(days=days)
    where = ['"modificationtime" >= %s']
    params = [cutoff]

    if status:
        where.append('"jobstatus" = %s')
        params.append(status)
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
    if before_id:
        where.append('"pandaid" < %s')
        params.append(before_id)

    conn = connections['panda']

    # Summary counts (without pagination cursor)
    count_where = [w for w in where if '"pandaid" <' not in w]
    count_params = [p for i, p in enumerate(params) if '"pandaid" <' not in where[i]]
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


def list_tasks(days=7, status=None, username=None, taskname=None,
               reqid=None, workinggroup=None, taskid=None,
               limit=25, before_id=None):
    """List JEDI tasks with summary statistics and cursor-based pagination."""
    cutoff = timezone.now() - timedelta(days=days)
    where = ['"modificationtime" >= %s']
    params = [cutoff]

    if status:
        where.append('"status" = %s')
        params.append(status)
    if username:
        clause, val = like_or_eq('username', username)
        where.append(clause)
        params.append(val)
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
    if before_id:
        where.append('"jeditaskid" < %s')
        params.append(before_id)

    conn = connections['panda']

    # Summary counts (without pagination cursor)
    count_where = [w for w in where if '"jeditaskid" <' not in w]
    count_params = [p for i, p in enumerate(params) if '"jeditaskid" <' not in where[i]]
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


def error_summary(days=10, username=None, site=None, taskid=None,
                  error_source=None, limit=20):
    """Aggregate error summary across failed PanDA jobs, ranked by frequency."""
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
    for comp in components_to_query:
        for table in ['jobsactive4', 'jobsarchived4']:
            parts.append(f"""
                SELECT '{comp['name']}' as error_source,
                       "{comp['code']}" as error_code,
                       "{comp['diag']}" as error_diag,
                       "jeditaskid",
                       "produsername",
                       "computingsite"
                FROM "{PANDA_SCHEMA}"."{table}"
                WHERE "modificationtime" >= %s
                  AND "jobstatus" IN ('failed','cancelled','closed')
                  AND "{comp['code']}" IS NOT NULL
                  AND "{comp['code']}" != 0
                  {filters}
            """)
            all_params.extend([cutoff] + extra_params)

    union_sql = ' UNION ALL '.join(parts)
    sql = f"""
        SELECT error_source, error_code,
               LEFT(error_diag, 256) as error_diag,
               COUNT(*) as count,
               COUNT(DISTINCT jeditaskid) as task_count,
               array_agg(DISTINCT produsername) as users,
               array_agg(DISTINCT computingsite) as sites
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
        if '%' in username:
            job_filters += ' AND "produsername" LIKE %s'
        else:
            job_filters += ' AND "produsername" = %s'
        job_params.append(username)
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

        user_rows = _job_agg('produsername')
        user_map = {}
        for status_val, user_val, count in user_rows:
            if user_val not in user_map:
                user_map[user_val] = {'user': user_val, 'total': 0}
            user_map[user_val][status_val] = count
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
    task_where = ['"modificationtime" >= %s']
    task_params = [cutoff]

    if username:
        if '%' in username:
            task_where.append('"username" LIKE %s')
        else:
            task_where.append('"username" = %s')
        task_params.append(username)
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

        task_user_sql = f"""
            SELECT "status", "username", COUNT(*)
            FROM "{PANDA_SCHEMA}"."jedi_tasks"
            WHERE {task_where_sql}
            GROUP BY "status", "username"
            ORDER BY COUNT(*) DESC
        """
        with conn.cursor() as cursor:
            cursor.execute(task_user_sql, task_params)
            task_user_rows = cursor.fetchall()

        task_user_map = {}
        for status_val, user_val, count in task_user_rows:
            if user_val not in task_user_map:
                task_user_map[user_val] = {'user': user_val, 'total': 0}
            task_user_map[user_val][status_val] = count
            task_user_map[user_val]['total'] += count
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
            "by_user": task_by_user,
        },
        "filters": {
            "days": days,
            "username": username,
            "site": site,
            "workinggroup": workinggroup,
        },
    }
