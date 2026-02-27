"""
PanDA Production Monitor views.

Web views for ePIC PanDA production monitoring — jobs, tasks, errors,
activity overview, and detail pages with rich cross-linking.
"""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse

from datetime import datetime
from zoneinfo import ZoneInfo

from ..utils import DataTablesProcessor
from ..panda import (
    get_activity, study_job, list_jobs,
    list_jobs_dt, list_tasks_dt,
    job_filter_counts, task_filter_counts,
    get_task, error_summary, diagnose_jobs,
)
from ..panda.constants import LIST_FIELDS, TASK_LIST_FIELDS


# ── Column definitions ───────────────────────────────────────────────────────

JOB_COLUMNS = [
    {'name': 'pandaid', 'title': 'PanDA ID', 'orderable': True},
    {'name': 'jeditaskid', 'title': 'Task ID', 'orderable': True},
    {'name': 'produsername', 'title': 'User', 'orderable': True},
    {'name': 'jobstatus', 'title': 'Status', 'orderable': True},
    {'name': 'computingsite', 'title': 'Site', 'orderable': True},
    {'name': 'transformation', 'title': 'Transformation', 'orderable': True},
    {'name': 'creationtime', 'title': 'Created', 'orderable': True},
    {'name': 'endtime', 'title': 'Ended', 'orderable': True},
    {'name': 'corecount', 'title': 'Cores', 'orderable': True},
]

JOB_FIELD_NAMES = [c['name'] for c in JOB_COLUMNS]

# Map DataTables column index to SQL ORDER BY expression
JOB_ORDER_MAP = {
    0: '"pandaid"', 1: '"jeditaskid"', 2: '"produsername"',
    3: '"jobstatus"', 4: '"computingsite"', 5: '"transformation"',
    6: '"creationtime"', 7: '"endtime"', 8: '"corecount"',
}

TASK_COLUMNS = [
    {'name': 'jeditaskid', 'title': 'Task ID', 'orderable': True},
    {'name': 'taskname', 'title': 'Task Name', 'orderable': True},
    {'name': 'status', 'title': 'Status', 'orderable': True},
    {'name': 'username', 'title': 'User', 'orderable': True},
    {'name': 'workinggroup', 'title': 'Working Group', 'orderable': True},
    {'name': 'creationdate', 'title': 'Created', 'orderable': True},
    {'name': 'modificationtime', 'title': 'Modified', 'orderable': True},
    {'name': 'progress', 'title': 'Progress', 'orderable': True},
    {'name': 'failurerate', 'title': 'Failure Rate', 'orderable': True},
]

TASK_FIELD_NAMES = [c['name'] for c in TASK_COLUMNS]

TASK_ORDER_MAP = {
    0: '"jeditaskid"', 1: '"taskname"', 2: '"status"',
    3: '"username"', 4: '"workinggroup"', 5: '"creationdate"',
    6: '"modificationtime"', 7: '"progress"', 8: '"failurerate"',
}

ERROR_COLUMNS = [
    {'name': 'error_source', 'title': 'Component', 'orderable': False},
    {'name': 'error_code', 'title': 'Code', 'orderable': False},
    {'name': 'error_diag', 'title': 'Diagnostic', 'orderable': False},
    {'name': 'count', 'title': 'Count', 'orderable': False},
    {'name': 'task_count', 'title': 'Tasks', 'orderable': False},
    {'name': 'users', 'title': 'Users', 'orderable': False},
    {'name': 'sites', 'title': 'Sites', 'orderable': False},
]

DIAG_COLUMNS = [
    {'name': 'pandaid', 'title': 'PanDA ID', 'orderable': False},
    {'name': 'jeditaskid', 'title': 'Task ID', 'orderable': False},
    {'name': 'produsername', 'title': 'User', 'orderable': False},
    {'name': 'jobstatus', 'title': 'Status', 'orderable': False},
    {'name': 'computingsite', 'title': 'Site', 'orderable': False},
    {'name': 'errors', 'title': 'Errors', 'orderable': False},
    {'name': 'endtime', 'title': 'Ended', 'orderable': False},
]


# ── Helpers ──────────────────────────────────────────────────────────────────

_EASTERN = ZoneInfo('America/New_York')


def _fmt_dt(val):
    """Format an ISO datetime string or datetime object for display."""
    if not val:
        return ''
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return val
    return val.astimezone(_EASTERN).strftime('%Y%m%d %H:%M:%S')


STATUS_COLORS = {
    'finished': '#28a745', 'done': '#28a745',
    'failed': '#dc3545', 'broken': '#dc3545',
    'running': '#007bff',
    'activated': '#17a2b8', 'ready': '#17a2b8',
    'cancelled': '#6c757d', 'aborted': '#6c757d', 'closed': '#6c757d',
    'exhausted': '#fd7e14',
}


def _status_badge(status, url=None):
    """Render a status as a colored badge, optionally linked."""
    color = STATUS_COLORS.get(status, '#6c757d')
    badge = (f'<span style="background:{color};color:#fff;padding:2px 8px;'
             f'border-radius:3px;font-size:0.85em;">{status}</span>')
    if url:
        return f'<a href="{url}" style="text-decoration:none;">{badge}</a>'
    return badge


DAYS_OPTIONS = [1, 3, 7, 14, 30]


def _get_days(request):
    """Extract days parameter from request, default 7."""
    try:
        return int(request.GET.get('days', 7))
    except (ValueError, TypeError):
        return 7


def _days_context(days):
    """Build days selector context for templates."""
    return {
        'days': days,
        'days_options': [{'value': d, 'active': d == days} for d in DAYS_OPTIONS],
    }


# ── Activity overview ────────────────────────────────────────────────────────

@login_required
def panda_activity(request):
    days = _get_days(request)
    data = get_activity(days=days)
    if 'error' in data:
        ctx = {'error': data['error']}
        ctx.update(_days_context(days))
        return render(request, 'monitor_app/panda_activity.html', ctx)
    data.update(_days_context(days))
    return render(request, 'monitor_app/panda_activity.html', data)


# ── Job list ─────────────────────────────────────────────────────────────────

@login_required
def panda_jobs_list(request):
    days = _get_days(request)
    context = {
        'table_title': 'PanDA Jobs',
        'table_description': f'Production jobs from the last {days} days.',
        'ajax_url': reverse('monitor_app:panda_jobs_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:panda_jobs_filter_counts'),
        'columns': JOB_COLUMNS,
        'filter_fields': [
            {'name': 'status', 'label': 'Status', 'type': 'select'},
            {'name': 'username', 'label': 'User', 'type': 'select'},
            {'name': 'site', 'label': 'Site', 'type': 'select'},
        ],
        'selected_status': request.GET.get('status', ''),
        'selected_username': request.GET.get('username', ''),
        'selected_site': request.GET.get('site', ''),
        'selected_taskid': request.GET.get('taskid', ''),
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_jobs_list.html', context)


@login_required
def panda_jobs_datatable_ajax(request):
    dt = DataTablesProcessor(request, JOB_FIELD_NAMES,
                             default_order_column=0, default_order_direction='desc')
    days = _get_days(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    site = request.GET.get('site', '') or None
    taskid = request.GET.get('taskid', '') or None
    reqid = request.GET.get('reqid', '') or None

    order_col = JOB_ORDER_MAP.get(dt.order_column_idx, '"pandaid"')
    order_dir = 'ASC' if dt.order_direction == 'asc' else 'DESC'
    order_by = f'{order_col} {order_dir}'

    rows, total, filtered = list_jobs_dt(
        days=days, status=status, username=username, site=site,
        taskid=taskid, reqid=reqid,
        order_by=order_by, limit=dt.length, offset=dt.start,
        search=dt.search_value or None,
    )

    data = []
    for job in rows:
        job_url = reverse('monitor_app:panda_job_detail', args=[job['pandaid']])
        task_url = reverse('monitor_app:panda_task_detail', args=[job['jeditaskid']]) if job.get('jeditaskid') else None
        jobs_by_user_url = reverse('monitor_app:panda_jobs_list') + f'?days={days}&username={job["produsername"]}' if job.get('produsername') else None
        jobs_by_site_url = reverse('monitor_app:panda_jobs_list') + f'?days={days}&site={job["computingsite"]}' if job.get('computingsite') else None
        jobs_by_status_url = reverse('monitor_app:panda_jobs_list') + f'?days={days}&status={job["jobstatus"]}' if job.get('jobstatus') else None

        data.append([
            f'<a href="{job_url}">{job["pandaid"]}</a>',
            f'<a href="{task_url}">{job["jeditaskid"]}</a>' if task_url else str(job.get('jeditaskid', '')),
            f'<a href="{jobs_by_user_url}">{job["produsername"]}</a>' if jobs_by_user_url else '',
            _status_badge(job['jobstatus'], jobs_by_status_url) if job.get('jobstatus') else '',
            f'<a href="{jobs_by_site_url}">{job["computingsite"]}</a>' if jobs_by_site_url else '',
            job.get('transformation', '') or '',
            _fmt_dt(job.get('creationtime')),
            _fmt_dt(job.get('endtime')),
            str(job.get('corecount', '') or ''),
        ])

    return dt.create_response(data, total, filtered)


@login_required
def panda_jobs_filter_counts(request):
    days = _get_days(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    site = request.GET.get('site', '') or None
    taskid = request.GET.get('taskid', '') or None
    reqid = request.GET.get('reqid', '') or None

    counts = job_filter_counts(days=days, status=status, username=username,
                               site=site, taskid=taskid, reqid=reqid)
    return JsonResponse({'filter_counts': counts})


# ── Task list ────────────────────────────────────────────────────────────────

@login_required
def panda_tasks_list(request):
    days = _get_days(request)
    context = {
        'table_title': 'PanDA Tasks',
        'table_description': f'JEDI tasks from the last {days} days.',
        'ajax_url': reverse('monitor_app:panda_tasks_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:panda_tasks_filter_counts'),
        'columns': TASK_COLUMNS,
        'filter_fields': [
            {'name': 'status', 'label': 'Status', 'type': 'select'},
            {'name': 'username', 'label': 'User', 'type': 'select'},
            {'name': 'workinggroup', 'label': 'Working Group', 'type': 'select'},
        ],
        'selected_status': request.GET.get('status', ''),
        'selected_username': request.GET.get('username', ''),
        'selected_workinggroup': request.GET.get('workinggroup', ''),
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_tasks_list.html', context)


@login_required
def panda_tasks_datatable_ajax(request):
    dt = DataTablesProcessor(request, TASK_FIELD_NAMES,
                             default_order_column=0, default_order_direction='desc')
    days = _get_days(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    taskname = request.GET.get('taskname', '') or None
    workinggroup = request.GET.get('workinggroup', '') or None

    order_col = TASK_ORDER_MAP.get(dt.order_column_idx, '"jeditaskid"')
    order_dir = 'ASC' if dt.order_direction == 'asc' else 'DESC'
    order_by = f'{order_col} {order_dir}'

    rows, total, filtered = list_tasks_dt(
        days=days, status=status, username=username, taskname=taskname,
        workinggroup=workinggroup,
        order_by=order_by, limit=dt.length, offset=dt.start,
        search=dt.search_value or None,
    )

    data = []
    for task in rows:
        task_url = reverse('monitor_app:panda_task_detail', args=[task['jeditaskid']])
        tasks_by_user_url = reverse('monitor_app:panda_tasks_list') + f'?days={days}&username={task["username"]}' if task.get('username') else None
        tasks_by_status_url = reverse('monitor_app:panda_tasks_list') + f'?days={days}&status={task["status"]}' if task.get('status') else None
        tasks_by_wg_url = reverse('monitor_app:panda_tasks_list') + f'?days={days}&workinggroup={task["workinggroup"]}' if task.get('workinggroup') else None

        # Truncate taskname for display
        taskname_display = task.get('taskname', '') or ''
        if len(taskname_display) > 80:
            taskname_display = taskname_display[:77] + '...'

        progress = task.get('progress')
        progress_str = f'{progress}%' if progress is not None else ''

        failurerate = task.get('failurerate')
        failurerate_str = f'{failurerate}%' if failurerate is not None else ''

        data.append([
            f'<a href="{task_url}">{task["jeditaskid"]}</a>',
            f'<a href="{task_url}" title="{task.get("taskname", "")}">{taskname_display}</a>',
            _status_badge(task['status'], tasks_by_status_url) if task.get('status') else '',
            f'<a href="{tasks_by_user_url}">{task["username"]}</a>' if tasks_by_user_url else '',
            f'<a href="{tasks_by_wg_url}">{task["workinggroup"]}</a>' if tasks_by_wg_url else str(task.get('workinggroup', '') or ''),
            _fmt_dt(task.get('creationdate')),
            _fmt_dt(task.get('modificationtime')),
            progress_str,
            failurerate_str,
        ])

    return dt.create_response(data, total, filtered)


@login_required
def panda_tasks_filter_counts(request):
    days = _get_days(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    workinggroup = request.GET.get('workinggroup', '') or None

    counts = task_filter_counts(days=days, status=status,
                                username=username, workinggroup=workinggroup)
    return JsonResponse({'filter_counts': counts})


# ── Job detail ───────────────────────────────────────────────────────────────

@login_required
def panda_job_detail(request, pandaid):
    data = study_job(int(pandaid))
    if 'error' in data:
        return render(request, 'monitor_app/panda_job_detail.html',
                      {'error': data['error'], 'pandaid': pandaid})
    data['pandaid'] = pandaid
    return render(request, 'monitor_app/panda_job_detail.html', data)


# ── Task detail ──────────────────────────────────────────────────────────────

@login_required
def panda_task_detail(request, jeditaskid):
    task = get_task(int(jeditaskid))
    if isinstance(task, dict) and 'error' in task:
        return render(request, 'monitor_app/panda_task_detail.html',
                      {'error': task['error'], 'jeditaskid': jeditaskid})

    # Get jobs for this task
    jobs_data = list_jobs(taskid=int(jeditaskid), days=90, limit=200)
    jobs = jobs_data.get('jobs', []) if not jobs_data.get('error') else []
    summary = jobs_data.get('summary', {}) if not jobs_data.get('error') else {}

    return render(request, 'monitor_app/panda_task_detail.html', {
        'task': task,
        'jeditaskid': jeditaskid,
        'jobs': jobs,
        'job_summary': summary,
        'job_count': len(jobs),
    })


# ── Error summary ────────────────────────────────────────────────────────────

@login_required
def panda_errors_list(request):
    days = _get_days(request)
    context = {
        'table_title': 'PanDA Error Summary',
        'table_description': f'Top error patterns across failed jobs in the last {days} days.',
        'ajax_url': reverse('monitor_app:panda_errors_datatable_ajax'),
        'columns': ERROR_COLUMNS,
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_errors.html', context)


@login_required
def panda_errors_datatable_ajax(request):
    dt = DataTablesProcessor(request, [c['name'] for c in ERROR_COLUMNS],
                             default_order_column=3, default_order_direction='desc')
    days = _get_days(request)
    username = request.GET.get('username', '') or None
    site = request.GET.get('site', '') or None
    error_source = request.GET.get('error_source', '') or None

    result = error_summary(days=days, username=username, site=site,
                           error_source=error_source, limit=200)

    if 'error' in result:
        return dt.create_response([], 0, 0)

    errors = result.get('errors', [])
    total = len(errors)

    data = []
    for err in errors:
        diag_url = reverse('monitor_app:panda_diagnostics_list') + f'?days={days}&error_source={err["error_source"]}'
        users_str = ', '.join(err.get('users', [])[:5])
        if len(err.get('users', [])) > 5:
            users_str += f' (+{len(err["users"]) - 5})'
        sites_str = ', '.join(err.get('sites', [])[:3])
        if len(err.get('sites', [])) > 3:
            sites_str += f' (+{len(err["sites"]) - 3})'

        diag_text = err.get('error_diag', '') or ''
        if len(diag_text) > 120:
            diag_text = diag_text[:117] + '...'

        data.append([
            f'<a href="{diag_url}">{err["error_source"]}</a>',
            str(err.get('error_code', '')),
            f'<span title="{err.get("error_diag", "")}">{diag_text}</span>',
            str(err.get('count', 0)),
            str(err.get('task_count', 0)),
            users_str,
            sites_str,
        ])

    return dt.create_response(data, total, total)


# ── Diagnostics ──────────────────────────────────────────────────────────────

@login_required
def panda_diagnostics_list(request):
    days = _get_days(request)
    context = {
        'table_title': 'PanDA Job Diagnostics',
        'table_description': f'Failed jobs with error details from the last {days} days.',
        'ajax_url': reverse('monitor_app:panda_diagnostics_datatable_ajax'),
        'columns': DIAG_COLUMNS,
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_diagnostics.html', context)


@login_required
def panda_diagnostics_datatable_ajax(request):
    dt = DataTablesProcessor(request, [c['name'] for c in DIAG_COLUMNS],
                             default_order_column=0, default_order_direction='desc')
    days = _get_days(request)
    username = request.GET.get('username', '') or None
    site = request.GET.get('site', '') or None
    taskid = request.GET.get('taskid', '') or None
    error_source = request.GET.get('error_source', '') or None

    result = diagnose_jobs(days=days, username=username, site=site,
                           taskid=taskid, error_component=error_source,
                           limit=500)

    if 'error' in result:
        return dt.create_response([], 0, 0)

    jobs = result.get('jobs', [])
    total = len(jobs)

    # Apply pagination
    page_jobs = jobs[dt.start:dt.start + dt.length]

    data = []
    for job in page_jobs:
        job_url = reverse('monitor_app:panda_job_detail', args=[job['pandaid']])
        task_url = reverse('monitor_app:panda_task_detail', args=[job['jeditaskid']]) if job.get('jeditaskid') else None

        errors_html = []
        for err in job.get('errors', []):
            diag = err.get('diag', '')
            if len(diag) > 80:
                diag = diag[:77] + '...'
            errors_html.append(f'<strong>{err["component"]}</strong>:{err["code"]} {diag}')

        data.append([
            f'<a href="{job_url}">{job["pandaid"]}</a>',
            f'<a href="{task_url}">{job["jeditaskid"]}</a>' if task_url else str(job.get('jeditaskid', '')),
            job.get('produsername', ''),
            _status_badge(job['jobstatus']) if job.get('jobstatus') else '',
            job.get('computingsite', ''),
            '<br>'.join(errors_html) if errors_html else '',
            _fmt_dt(job.get('endtime')),
        ])

    return dt.create_response(data, total, total)
