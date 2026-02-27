"""
SQL builders and row helpers for PanDA database queries.

Pure functions that construct SQL strings and transform database rows.
No database I/O — callers execute the returned (sql, params) tuples.
"""

from .constants import PANDA_SCHEMA, ERROR_COMPONENTS


# ── SQL builders ─────────────────────────────────────────────────────────────

def build_union_query(fields, where_clauses, params, order_by, limit):
    """Build a UNION ALL query across jobsactive4 and jobsarchived4."""
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
    full_params = list(params) + list(params)
    return sql, full_params


def build_count_query(where_clauses, params):
    """Build a count-by-status query across both job tables."""
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


def build_task_query(fields, where_clauses, params, order_by, limit):
    """Build a query against the jedi_tasks table."""
    field_list = ', '.join(f'"{f}"' for f in fields)
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)
    sql = f"""
        SELECT {field_list}
        FROM "{PANDA_SCHEMA}"."jedi_tasks"{where_sql}
        ORDER BY {order_by}
        LIMIT {limit}
    """
    return sql, list(params)


def build_task_count_query(where_clauses, params):
    """Build a count-by-status query for jedi_tasks."""
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)
    sql = f"""
        SELECT "status", COUNT(*)
        FROM "{PANDA_SCHEMA}"."jedi_tasks"{where_sql}
        GROUP BY "status"
        ORDER BY COUNT(*) DESC
    """
    return sql, list(params)


# ── Row helpers ──────────────────────────────────────────────────────────────

def row_to_dict(row, fields):
    """Convert a database row to a dict, formatting datetimes."""
    result = {}
    for i, field in enumerate(fields):
        val = row[i]
        if val is not None and hasattr(val, 'isoformat'):
            val = val.isoformat()
        result[field] = val
    return result


def extract_errors(job_dict):
    """Extract non-zero error components from a job dict."""
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


def like_or_eq(field, value):
    """Return (where_clause, param) using LIKE if value contains %, else =."""
    if '%' in value:
        return f'"{field}" LIKE %s', value
    return f'"{field}" = %s', value


# ── DataTables SQL builders ──────────────────────────────────────────────────

def build_union_query_dt(fields, where_clauses, params, order_by, limit, offset):
    """Build a UNION ALL query with OFFSET for DataTables pagination."""
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
        LIMIT {limit} OFFSET {offset}
    """
    full_params = list(params) + list(params)
    return sql, full_params


def build_union_count(where_clauses, params):
    """Build a total count query across both job tables."""
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)

    sql = f"""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM "{PANDA_SCHEMA}"."jobsactive4"{where_sql}
            UNION ALL
            SELECT 1 FROM "{PANDA_SCHEMA}"."jobsarchived4"{where_sql}
        ) combined
    """
    full_params = list(params) + list(params)
    return sql, full_params


def build_union_count_by_field(field, where_clauses, params):
    """Build a GROUP BY count for a single field across both job tables."""
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)

    sql = f"""
        SELECT "{field}", COUNT(*) FROM (
            SELECT "{field}" FROM "{PANDA_SCHEMA}"."jobsactive4"{where_sql}
            UNION ALL
            SELECT "{field}" FROM "{PANDA_SCHEMA}"."jobsarchived4"{where_sql}
        ) combined
        WHERE "{field}" IS NOT NULL
        GROUP BY "{field}"
        ORDER BY COUNT(*) DESC
    """
    full_params = list(params) + list(params)
    return sql, full_params


def build_task_query_dt(fields, where_clauses, params, order_by, limit, offset):
    """Build a task query with OFFSET for DataTables pagination."""
    field_list = ', '.join(f'"{f}"' for f in fields)
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)
    sql = f"""
        SELECT {field_list}
        FROM "{PANDA_SCHEMA}"."jedi_tasks"{where_sql}
        ORDER BY {order_by}
        LIMIT {limit} OFFSET {offset}
    """
    return sql, list(params)


def build_task_count(where_clauses, params):
    """Build a total count query for jedi_tasks."""
    where_sql = ''
    if where_clauses:
        where_sql = ' WHERE ' + ' AND '.join(where_clauses)
    sql = f"""
        SELECT COUNT(*)
        FROM "{PANDA_SCHEMA}"."jedi_tasks"{where_sql}
    """
    return sql, list(params)


def build_task_count_by_field(field, where_clauses, params):
    """Build a GROUP BY count for a single field in jedi_tasks."""
    all_clauses = list(where_clauses) + [f'"{field}" IS NOT NULL']
    where_sql = ' WHERE ' + ' AND '.join(all_clauses)
    sql = f"""
        SELECT "{field}", COUNT(*)
        FROM "{PANDA_SCHEMA}"."jedi_tasks"{where_sql}
        GROUP BY "{field}"
        ORDER BY COUNT(*) DESC
    """
    return sql, list(params)


def build_search_clauses(fields, search_value):
    """Build ILIKE search clauses across multiple fields."""
    clauses = []
    params = []
    for f in fields:
        clauses.append(f'CAST("{f}" AS TEXT) ILIKE %s')
        params.append(f'%{search_value}%')
    return f"({' OR '.join(clauses)})", params
