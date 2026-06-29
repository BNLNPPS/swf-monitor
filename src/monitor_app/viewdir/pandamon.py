"""
PanDA Production Monitor views.

Web views for ePIC PanDA production monitoring — jobs, tasks, errors,
activity overview, and detail pages with rich cross-linking.
"""

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.conf import settings

import json
import logging
import os
import hashlib
from html import escape
from datetime import datetime
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from ..utils import DataTablesProcessor
from ..panda import (
    get_activity, study_job, list_jobs,
    list_jobs_dt, list_tasks_dt,
    job_filter_counts, task_filter_counts,
    get_task, error_summary, diagnose_jobs, job_completion_details,
    list_queues, get_queue,
)
from ..panda.constants import (
    LIST_FIELDS, TASK_LIST_FIELDS,
    TASK_STATE_COLORS, JOB_STATE_COLORS,
)
from ..cell_fmt import fill_cell
from ..activemq_connection import ActiveMQConnectionManager
from ..epicprod_inventory import (
    cached_payload_log_parts,
    diagnosis_for_study_data,
    inventory_for_job_context,
)

logger = logging.getLogger(__name__)


def _pcs_task_for_jeditaskid(jeditaskid):
    try:
        from pcs.models import PandaTasks, ProdTask
        row = (PandaTasks.objects
               .select_related('prod_task', 'prod_task__dataset')
               .filter(jedi_task_id=int(jeditaskid)).first())
        if row:
            return row.prod_task
        return (ProdTask.objects.select_related('dataset')
                .filter(panda_task_id=int(jeditaskid)).first())
    except Exception:
        logger.exception("PCS lookup failed for PanDA task %s", jeditaskid)
        return None


def _panda_tasks_row_for_jeditaskid(jeditaskid):
    try:
        from pcs.models import PandaTasks
        return (PandaTasks.objects
                .select_related('prod_task')
                .filter(jedi_task_id=int(jeditaskid)).first())
    except Exception:
        logger.exception("PandaTasks lookup failed for PanDA task %s", jeditaskid)
        return None


def _pcs_task_for_panda_task(task):
    pcs_task = _pcs_task_for_jeditaskid(task.get('jeditaskid'))
    if pcs_task:
        return pcs_task
    try:
        from pcs.services import reconcile_panda_task_association
        pcs_task, _row, reason = reconcile_panda_task_association(task)
        if pcs_task:
            logger.info(
                "PCS dynamic PanDA association: jediTaskID=%s task=%s reason=%s",
                task.get('jeditaskid'), pcs_task.composed_name, reason)
        return pcs_task
    except Exception:
        logger.exception("PCS dynamic association failed for PanDA task %s",
                         task.get('jeditaskid'))
        return None


def _unit_value(value, unit, *, default_unit=''):
    if value in (None, ''):
        return None
    unit = unit or default_unit
    return f'{value} {unit}'.strip()


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
    {'name': 'processingtype', 'title': 'Processing type', 'orderable': True},
    {'name': 'username', 'title': 'User', 'orderable': True},
    {'name': 'creationdate', 'title': 'Created', 'orderable': True},
    {'name': 'modificationtime', 'title': 'Modified', 'orderable': True},
    # Progress column shows the computed (job-based) progress since native JEDI
    # progress is always NULL in this deployment. Same rationale as Fail Rate.
    {'name': 'computed_progress', 'title': 'Progress', 'orderable': True},
    # Per-task job-count aggregates + derived failure rate are SELECT aliases
    # on build_task_query_dt's enriched query, so they're SQL-sortable.
    {'name': 'nactive', 'title': 'Active', 'orderable': True},
    {'name': 'nfinished', 'title': 'Finished', 'orderable': True},
    {'name': 'nfailed', 'title': 'Failed', 'orderable': True},
    # Running is a subset of Active (jobstatus='running').
    {'name': 'nrunning', 'title': 'Running', 'orderable': True},
    # Retries: count of job records with attemptnr > 1. Every retry creates a
    # new job record in the ePIC PanDA schema. Retry limit is 3.
    {'name': 'nretries', 'title': 'Retries', 'orderable': True},
    # Derived from nfailed / (nfailed+nfinished). The native JEDI failurerate
    # column is always NULL in this deployment (post-processing that populates
    # it isn't running for ePIC task types), so this is the only signal shown.
    {'name': 'computed_failurerate', 'title': 'Fail Rate', 'orderable': True},
    # Final-failed: jobs that failed AND exhausted the retry budget
    # (attemptnr >= maxattempt). Subset of Failed. The rate derived from these is
    # what alarms trigger on — distinguishes true failures from
    # transient-fail-then-retry-succeeds.
    {'name': 'nfinalfailed', 'title': 'Final Failed', 'orderable': True},
    {'name': 'computed_finalfailurerate', 'title': 'Final Fail Rate', 'orderable': True},
]

TASK_FIELD_NAMES = [c['name'] for c in TASK_COLUMNS]

TASK_ORDER_MAP = {
    0: '"jeditaskid"', 1: '"taskname"', 2: '"status"',
    3: '"processingtype"', 4: '"username"', 5: '"creationdate"', 6: '"modificationtime"',
    # Aggregates surface as SELECT aliases from build_task_query_dt.
    # Wrapped expressions in ORDER BY can't reference SELECT aliases in PG,
    # so these stay as bare alias names; the view's order_by construction
    # appends 'NULLS LAST' for computed_failurerate and computed_progress so
    # tasks with no terminal jobs (rate/progress=NULL) don't surface at the
    # top of a DESC ranking.
    7: 'computed_progress',
    8: 'nactive',
    9: 'nfinished',
    10: 'nfailed',
    11: 'nrunning',
    12: 'nretries',
    13: 'computed_failurerate',
    14: 'nfinalfailed',
    15: 'computed_finalfailurerate',
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


def _panda_view_text_url(url):
    return reverse('monitor_app:panda_view_text') + '?' + urlencode({'url': url})


def _linkify(text):
    """Wrap text in an <a> tag if it looks like a URL.

    TRF links (pandaserver-doma.cern.ch/trf/) are routed through our
    view-text endpoint which extracts readable content from self-extracting zips.
    """
    if text and text.startswith(('http://', 'https://')):
        href = text
        if 'pandaserver-doma.cern.ch/trf/' in text:
            href = _panda_view_text_url(text)
        return f'<a href="{href}" target="_blank" rel="noopener">{text}</a>'
    return text


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


_fill_cell = fill_cell  # backwards-compat alias within this module


DAYS_OPTIONS = [
    (1, '1d'),
    (3, '3d'),
    (7, '7d'),
    (14, '14d'),
    (30, '30d'),
    (90, '3mo'),
    (180, '6mo'),
    (365, '1yr'),
]


def _url_with_query(view_name, **params):
    return reverse(view_name) + '?' + urlencode(params)


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
        'days_options': [
            {'value': value, 'label': label, 'active': value == days}
            for value, label in DAYS_OPTIONS
        ],
    }


# ── Activity overview ────────────────────────────────────────────────────────

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

def panda_jobs_list(request):
    days = _get_days(request)
    selected_site = request.GET.get('site', '')
    description = f'Production jobs from the last {days} days.'
    if selected_site:
        site_url = reverse('monitor_app:epic_queue_detail', args=[selected_site])
        description += f'<br><a href="{site_url}">Site info for <strong>{selected_site}</strong></a>'

    context = {
        'table_title': 'PanDA Jobs',
        'table_description': description,
        'ajax_url': reverse('monitor_app:panda_jobs_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:panda_jobs_filter_counts'),
        'columns': JOB_COLUMNS,
        'show_query_count': True,
        'query_count_label': 'jobs',
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
        jobs_by_user_url = _url_with_query('monitor_app:panda_jobs_list', days=days, username=job['produsername']) if job.get('produsername') else None
        jobs_by_site_url = _url_with_query('monitor_app:panda_jobs_list', days=days, site=job['computingsite']) if job.get('computingsite') else None
        jobs_by_status_url = _url_with_query('monitor_app:panda_jobs_list', days=days, status=job['jobstatus']) if job.get('jobstatus') else None

        data.append([
            f'<a href="{job_url}">{job["pandaid"]}</a>',
            f'<a href="{task_url}">{job["jeditaskid"]}</a>' if task_url else str(job.get('jeditaskid', '')),
            f'<a href="{jobs_by_user_url}">{job["produsername"]}</a>' if jobs_by_user_url else '',
            _fill_cell(job['jobstatus'], job['jobstatus'], jobs_by_status_url) if job.get('jobstatus') else '',
            f'<a href="{jobs_by_site_url}">{job["computingsite"]}</a>' if jobs_by_site_url else '',
            _linkify(job.get('transformation', '') or ''),
            _fmt_dt(job.get('creationtime')),
            _fmt_dt(job.get('endtime')),
            str(job.get('corecount', '') or ''),
        ])

    return dt.create_response(data, total, filtered)


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

def panda_tasks_list(request):
    days = _get_days(request)
    context = {
        'table_title': 'PanDA Tasks',
        'table_description': f'JEDI tasks from the last {days} days.',
        'ajax_url': reverse('monitor_app:panda_tasks_datatable_ajax'),
        'filter_counts_url': reverse('monitor_app:panda_tasks_filter_counts'),
        'columns': TASK_COLUMNS,
        'show_query_count': True,
        'query_count_label': 'tasks',
        'filter_fields': [
            {'name': 'status', 'label': 'Status', 'type': 'select'},
            {'name': 'username', 'label': 'User', 'type': 'select'},
            {'name': 'processingtype', 'label': 'Processing type', 'type': 'select'},
        ],
        'selected_status': request.GET.get('status', ''),
        'selected_username': request.GET.get('username', ''),
        'selected_processingtype': request.GET.get('processingtype', ''),
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_tasks_list.html', context)


def panda_tasks_datatable_ajax(request):
    dt = DataTablesProcessor(request, TASK_FIELD_NAMES,
                             default_order_column=0, default_order_direction='desc')
    days = _get_days(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    taskname = request.GET.get('taskname', '') or None
    processingtype = request.GET.get('processingtype', '') or None

    order_col = TASK_ORDER_MAP.get(dt.order_column_idx, '"jeditaskid"')
    order_dir = 'ASC' if dt.order_direction == 'asc' else 'DESC'
    # NULL failurerate/progress = no jobs reported yet; push those to the
    # bottom of any ranking so they don't surface as the extremes view.
    null_suffix = ' NULLS LAST' if order_col in ('computed_failurerate', 'computed_progress') else ''
    order_by = f'{order_col} {order_dir}{null_suffix}'

    # workinggroup no longer exposed in the table view (dropped as low-signal in
    # this deployment — always EIC or NULL). Still on the task detail page and
    # in list_tasks_dt's filter contract for backward compat with direct callers.
    rows, total, filtered = list_tasks_dt(
        days=days, status=status, username=username, taskname=taskname,
        processingtype=processingtype,
        order_by=order_by, limit=dt.length, offset=dt.start,
        search=dt.search_value or None,
    )

    data = []
    for task in rows:
        task_url = reverse('monitor_app:panda_task_detail', args=[task['jeditaskid']])
        tasks_by_user_url = _url_with_query('monitor_app:panda_tasks_list', days=days, username=task['username']) if task.get('username') else None
        tasks_by_status_url = _url_with_query('monitor_app:panda_tasks_list', days=days, status=task['status']) if task.get('status') else None

        # Truncate taskname for display
        taskname_display = task.get('taskname', '') or ''
        if len(taskname_display) > 80:
            taskname_display = taskname_display[:77] + '...'

        comp_pr = task.get('computed_progress')
        comp_pr_str = f'{comp_pr}%' if comp_pr is not None else ''

        comp_fr = task.get('computed_failurerate')
        comp_fr_str = f'{comp_fr * 100:.1f}%' if comp_fr is not None else ''

        comp_ffr = task.get('computed_finalfailurerate')
        comp_ffr_str = f'{comp_ffr * 100:.1f}%' if comp_ffr is not None else ''

        processingtype = task.get('processingtype') or ''
        processingtype_html = escape(processingtype)
        processingtype_display = (
            f'<span class="badge bg-warning text-dark">{processingtype_html}</span>'
            if 'test' in processingtype.lower()
            else processingtype_html
        )

        data.append([
            f'<a href="{task_url}">{task["jeditaskid"]}</a>',
            f'<a href="{task_url}" title="{task.get("taskname", "")}">{taskname_display}</a>',
            _fill_cell(task['status'], task['status'], tasks_by_status_url) if task.get('status') else '',
            processingtype_display,
            f'<a href="{tasks_by_user_url}">{task["username"]}</a>' if tasks_by_user_url else '',
            _fmt_dt(task.get('creationdate')),
            _fmt_dt(task.get('modificationtime')),
            comp_pr_str,
            _fill_cell(task.get('nactive', 0), 'running') if task.get('nactive', 0) else 0,
            _fill_cell(task.get('nfinished', 0), 'finished') if task.get('nfinished', 0) else 0,
            _fill_cell(task.get('nfailed', 0), 'failed') if task.get('nfailed', 0) else 0,
            _fill_cell(task.get('nrunning', 0), 'running') if task.get('nrunning', 0) else 0,
            task.get('nretries', 0),
            comp_fr_str,
            _fill_cell(task.get('nfinalfailed', 0), 'failed') if task.get('nfinalfailed', 0) else 0,
            comp_ffr_str,
        ])

    return dt.create_response(data, total, filtered)


def panda_tasks_filter_counts(request):
    days = _get_days(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    processingtype = request.GET.get('processingtype', '') or None
    workinggroup = request.GET.get('workinggroup', '') or None

    counts = task_filter_counts(days=days, status=status,
                                username=username,
                                processingtype=processingtype,
                                workinggroup=workinggroup)
    return JsonResponse({'filter_counts': counts})


# ── Job detail ───────────────────────────────────────────────────────────────

def panda_job_detail(request, pandaid):
    data = study_job(int(pandaid))
    if 'error' in data:
        return render(request, 'monitor_app/panda_job_detail.html',
                      {'error': data['error'], 'pandaid': pandaid})
    data['pandaid'] = pandaid
    job = data.get('job') or {}
    job['transformation_is_url'] = (job.get('transformation') or '').startswith(('http://', 'https://'))
    trf = job.get('transformation') or ''
    if 'pandaserver-doma.cern.ch/trf/' in trf:
        job['transformation_view_url'] = _panda_view_text_url(trf)
    if job.get('jeditaskid'):
        data['pcs_task'] = _pcs_task_for_panda_task(data.get('task') or job)
    data['job_record_items'] = [
        {'name': key, 'value': '' if value is None else value}
        for key, value in sorted((data.get('job_record') or {}).items())
    ]
    data['job_parameter_items'] = [
        {'label': label, 'value': job.get(key)}
        for label, key in (
            ('Special handling', 'specialhandling'),
            ('Attempt number', 'attemptnr'),
            ('CPU consumption time (s)', 'cpuconsumptiontime'),
            ('Job metrics', 'jobmetrics'),
            ('Job parameters', 'jobparameters'),
            ('Pilot ID', 'pilotid'),
            ('Batch ID', 'batchid'),
        )
        if job.get(key) not in (None, '')
    ]
    data.update(inventory_for_job_context(data))
    data['epicprod_diagnosis'] = diagnosis_for_study_data(
        data, epicprod_job=data.get('epicprod_job'))
    return render(request, 'monitor_app/panda_job_detail.html', data)


def epicprod_job_refresh(request, pandaid):
    if request.method != 'POST':
        return redirect('monitor_app:panda_job_detail', pandaid=pandaid)
    msg = {
        'msg_type': 'sync_epicprod_inventory',
        'namespace': 'prodops',
        'pandaid': str(pandaid),
    }
    triggered = False
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', json.dumps(msg))
    except Exception as e:
        logger.error("epicprod inventory refresh trigger failed for job %s: %s", pandaid, e)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        status = 202 if triggered else 502
        return JsonResponse({'ok': triggered, 'queued': triggered, 'pandaid': pandaid},
                            status=status)
    return redirect('monitor_app:panda_job_detail', pandaid=pandaid)


# ── View text (transformation script viewer) ────────────────────────────────

def _is_panda_trf_url(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == 'https'
        and parsed.netloc.lower() == 'pandaserver-doma.cern.ch'
        and parsed.path.startswith('/trf/')
    )


def _trf_cache_paths(url):
    cache_root = getattr(settings, 'SWF_TMP_DIR', '/data/swf-tmp')
    cache_dir = os.path.join(cache_root, 'panda-trf')
    key = hashlib.sha256(url.encode('utf-8')).hexdigest()
    return {
        'dir': cache_dir,
        'raw': os.path.join(cache_dir, f'{key}.bin'),
        'text': os.path.join(cache_dir, f'{key}.txt'),
        'url': os.path.join(cache_dir, f'{key}.url'),
    }


def _write_file_atomic(path, mode, data):
    tmp_path = f'{path}.tmp'
    with open(tmp_path, mode) as handle:
        handle.write(data)
    os.replace(tmp_path, path)


def _transformation_filename(url):
    parsed = urlparse(url)
    name = os.path.basename(parsed.path.rstrip('/')) or 'transformation'
    return ''.join(ch if ch.isalnum() or ch in ('-', '_', '.') else '_' for ch in name)


def _extract_trf_text(data):
    import io
    import zipfile

    parts = []
    lines = []
    for line in data.split(b'\n'):
        try:
            lines.append(line.decode('utf-8'))
        except UnicodeDecodeError:
            break
    if lines:
        parts.append(f'=== Shell header ({len(lines)} lines) ===\n')
        parts.append('\n'.join(lines))

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                try:
                    content = zf.read(name).decode('utf-8')
                    parts.append(f'\n\n=== {name} ===\n')
                    parts.append(content)
                except UnicodeDecodeError:
                    parts.append(f'\n\n=== {name} (binary, skipped) ===\n')
                except KeyError as e:
                    parts.append(f'\n\n=== {name} (missing: {e}) ===\n')
    except zipfile.BadZipFile:
        if not parts:
            parts.append(data.decode('utf-8', errors='replace'))

    return ''.join(parts)


def _transformation_text_response(text, url, cache_status):
    title = _transformation_filename(url)
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
  <style>
    body {{
      margin: 0;
      background: #fff;
      color: #111;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 16px;
    }}
    header {{
      padding: 0.75rem 1rem;
      border-bottom: 1px solid #d0d7de;
      background: #f6f8fa;
    }}
    h1 {{
      margin: 0 0 0.35rem 0;
      font-size: 1.25rem;
      font-weight: 600;
    }}
    a {{
      color: #005ea8;
    }}
    pre {{
      margin: 0;
      padding: 1rem;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 14px;
      line-height: 1.35;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)}</h1>
    <a href="{escape(url)}">{escape(url)}</a>
  </header>
  <pre>{escape(text)}</pre>
</body>
</html>
"""
    response = HttpResponse(html, content_type='text/html; charset=utf-8')
    response['X-PanDA-TRF-Cache'] = cache_status
    return response


def panda_view_text(request):
    """Fetch a PanDA transformation URL — self-extracting zip with embedded scripts.

    Extracts the bash header and all text files from the zip, presents them
    as readable plain text.
    """
    import httpx

    url = request.GET.get('url', '')
    if not url or not _is_panda_trf_url(url):
        return HttpResponse('Missing or invalid url parameter', status=400,
                            content_type='text/plain')

    paths = _trf_cache_paths(url)
    try:
        if os.path.exists(paths['text']):
            with open(paths['text'], 'r', encoding='utf-8') as handle:
                return _transformation_text_response(handle.read(), url, 'HIT')
    except OSError as e:
        logger.error("failed reading cached transformation text for %s: %s", url, e)
        return HttpResponse(f'Failed to read cached transformation text: {e}', status=500,
                            content_type='text/plain')

    try:
        if os.path.exists(paths['raw']):
            with open(paths['raw'], 'rb') as handle:
                data = handle.read()
        else:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content
            os.makedirs(paths['dir'], exist_ok=True)
            _write_file_atomic(paths['raw'], 'wb', data)
            _write_file_atomic(paths['url'], 'w', url)
    except Exception as e:
        logger.error("failed fetching transformation %s: %s", url, e)
        return HttpResponse(f'Failed to fetch: {e}', status=502,
                            content_type='text/plain')

    try:
        text = _extract_trf_text(data)
        os.makedirs(paths['dir'], exist_ok=True)
        _write_file_atomic(paths['text'], 'w', text)
    except Exception as e:
        logger.error("failed extracting transformation %s: %s", url, e)
        return HttpResponse(f'Failed to extract transformation text: {e}', status=500,
                            content_type='text/plain')

    return _transformation_text_response(text, url, 'MISS')


# ── Payload log (clean, from the Rucio log tarball via the prod-ops agent) ────

def _payload_log_pending_page(message, pandaid, script_name):
    """202 page for a payload log still being fetched by the prod-ops agent.

    Holds an EventSource on the SSE relay (payload_log_ready) and, when the agent
    signals this job's log is ready, fetches and shows it in place — no manual
    refresh and no reload loop. An immediate check catches an event that fired
    before the stream connected; one slow check is the backstop. The stream URL
    carries the app's SCRIPT_NAME so swf-remote's body rewrite re-points it to
    /prod/ for the external face. See docs/SSE_PUSH.md.
    """
    from django.utils.html import escape
    stream = f"{script_name}/api/messages/stream/?msg_types=payload_log_ready"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Retrieving payload log…</title>
<style>body{{font-family:system-ui;font-size:15px;background:#1e1e1e;color:#ddd;padding:1.5rem}}
pre{{white-space:pre-wrap;font-size:15px}} .note{{color:#8ab4f8}}</style></head>
<body>
<pre id="plog-msg">{escape(message)}</pre>
<p class="note" id="plog-status">Retrieving from Rucio — the log will appear here automatically.</p>
<script>
const PANDAID="{escape(str(pandaid))}";
const STREAM="{escape(stream)}";
const SELF=window.location.href;
let done=false, es=null;
async function check(){{
  if(done) return;
  try{{
    const r=await fetch(SELF, {{headers:{{'Accept':'text/plain'}}}});
    if(r.status!==202){{                         // 200 log, or a terminal error page
      done=true;
      document.getElementById('plog-msg').textContent=await r.text();
      document.getElementById('plog-status').textContent='';
      if(es) es.close();
    }}
  }}catch(e){{}}
}}
es=new EventSource(STREAM);
es.addEventListener('payload_log_ready', (ev)=>{{
  try{{ const d=JSON.parse(ev.data); if(String(d.pandaid)===PANDAID) check(); }}catch(e){{}}
}});
check();                      // immediate: catch an event that fired before connect
setTimeout(check, 25000);     // backstop: one slow check if the event is missed
</script>
</body></html>"""
    return HttpResponse(html, status=202, content_type='text/html; charset=utf-8')


def panda_payload_log(request, pandaid):
    """Serve a job's clean payload log from the prod-ops cache.

    On a cache hit, return the extracted log members as text. On a miss, publish
    a fetch request to the production-operations agent (/queue/epicprod.ops) —
    which holds the Rucio proxy and does the xrootd pull — and ask the user to
    refresh. The web tier never touches the proxy or xrootd; it only reads the
    world-readable cache. See docs/EPICPROD_OPS.md.
    """
    data = study_job(int(pandaid))
    if 'error' in data:
        return HttpResponse(f"job {pandaid}: {data['error']}\n",
                            status=404, content_type='text/plain; charset=utf-8')
    job = data.get('job') or {}
    log_file = data.get('log_file') or {}
    jeditaskid = job.get('jeditaskid')
    scope = log_file.get('scope')
    lfn = log_file.get('lfn')
    if not (jeditaskid and scope and lfn):
        return HttpResponse(
            f"job {pandaid}: no Rucio log dataset registered (job may not be complete yet).\n",
            status=404, content_type='text/plain; charset=utf-8')

    cache_root = getattr(settings, 'SWF_TMP_DIR', '/data/swf-tmp')
    jobdir = os.path.join(cache_root, 'panda-logs', str(jeditaskid), str(pandaid))
    force = bool(request.GET.get('force'))
    max_attempts = getattr(settings, 'EPICPROD_MAX_FETCH_ATTEMPTS', 3)

    # Cache hit: the doer writes '.done' last, so this is only true once the dir
    # is fully populated — never keyed on a single member (a log may lack stdout).
    if not force and os.path.isfile(os.path.join(jobdir, '.done')):
        parts = []
        for part in cached_payload_log_parts(jeditaskid, pandaid):
            parts.append(f"===== {part['name']} =====\n{part['text']}\n")
        return HttpResponse(''.join(parts) or '(log cached but empty)\n',
                            content_type='text/plain; charset=utf-8')

    # Prior-failure marker the agent wrote, if any.
    err = None
    try:
        with open(os.path.join(jobdir, '.error')) as f:
            err = json.load(f)
    except (OSError, ValueError):
        err = None

    # Past the retry cap: surface the failure and stop auto-retrying. ?force=1 overrides.
    if err and not force and err.get('attempts', 0) >= max_attempts:
        return HttpResponse(
            f"Payload log retrieval for job {pandaid} failed {err.get('attempts')} times "
            f"(cap {max_attempts}).\n"
            f"Last error: {err.get('last_error', 'unknown')}\n"
            f"Append ?force=1 to retry, or check the agent / monitor logs.\n",
            status=502, content_type='text/plain; charset=utf-8')

    # Miss (or forced / under-cap retry): ask the prod-ops agent to fetch it.
    # The agent runs under the 'prodops' namespace and filters on it, so every
    # caller must address it explicitly.
    msg = {'msg_type': 'fetch_payload_log', 'namespace': 'prodops',
           'scope': scope, 'lfn': lfn,
           'jeditaskid': str(jeditaskid), 'pandaid': str(pandaid)}
    if force:
        msg['force'] = True
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', json.dumps(msg))
    except Exception as e:
        logger.error(f"payload-log fetch trigger failed for job {pandaid}: {e}")
        triggered = False

    if not triggered:
        return HttpResponse(
            f"Payload log for job {pandaid} is not cached, and the ops-agent queue "
            f"could not be reached to request it (see monitor logs).\n",
            status=502, content_type='text/plain; charset=utf-8')

    script_name = getattr(settings, 'FORCE_SCRIPT_NAME', '') or request.META.get('SCRIPT_NAME', '')
    if err:
        return _payload_log_pending_page(
            f"Payload log for job {pandaid}: previous attempt failed "
            f"({err.get('last_error', 'unknown')}). "
            f"Retrying (attempt {err.get('attempts', 0) + 1} of {max_attempts})…",
            pandaid, script_name)
    return _payload_log_pending_page(
        f"Payload log for job {pandaid} is not cached yet. "
        f"Requested retrieval from Rucio.",
        pandaid, script_name)


# ── Task detail ──────────────────────────────────────────────────────────────

def panda_task_detail(request, jeditaskid):
    task = get_task(int(jeditaskid))
    if isinstance(task, dict) and 'error' in task:
        return render(request, 'monitor_app/panda_task_detail.html',
                      {'error': task['error'], 'jeditaskid': jeditaskid})
    pcs_task = _pcs_task_for_panda_task(task)
    panda_tasks_row = _panda_tasks_row_for_jeditaskid(jeditaskid)
    transpath = task.get('transpath') or ''
    if transpath:
        if transpath.startswith(('http://', 'https://')):
            task['transformation_view_url'] = (
                _panda_view_text_url(transpath)
                if 'pandaserver-doma.cern.ch/trf/' in transpath else transpath
            )
        else:
            task['transformation_view_url'] = _panda_view_text_url(
                'https://pandaserver-doma.cern.ch/trf/user/'
                + quote(transpath.strip('/'), safe='')
            )

    # Get jobs for this task
    jobs_data = list_jobs(taskid=int(jeditaskid), days=90, limit=200)
    jobs = jobs_data.get('jobs', []) if not jobs_data.get('error') else []
    summary = jobs_data.get('summary', {}) if not jobs_data.get('error') else {}
    completion_details = job_completion_details([job.get('pandaid') for job in jobs])
    from ..models import EpicProdJob
    epicprod_jobs = {
        row.pandaid: row
        for row in EpicProdJob.objects.filter(
            pandaid__in=[job.get('pandaid') for job in jobs if job.get('pandaid')]
        )
    }
    for job in jobs:
        job.update(completion_details.get(job.get('pandaid'), {}))
        epicprod_job = epicprod_jobs.get(job.get('pandaid'))
        if epicprod_job and epicprod_job.failure_summary:
            job['epicprod_phase'] = epicprod_job.phase
            job['epicprod_failure_summary'] = epicprod_job.failure_summary
    task_record = task.get('task_record') or {}
    task_record_items = [
        {'name': key, 'value': '' if value is None else value}
        for key, value in sorted(task_record.items())
    ]
    requested_resource_items = [
        {'label': label, 'value': value}
        for label, value in (
            ('Container', task_record.get('container_name')),
            ('Cores', task_record.get('corecount') or task.get('corecount')),
            ('RAM', _unit_value(task_record.get('ramcount'), task_record.get('ramunit'))),
            ('Walltime', _unit_value(task_record.get('walltime'), task_record.get('walltimeunit'), default_unit='s')),
            ('Work disk', _unit_value(task_record.get('workdiskcount'), task_record.get('workdiskunit'))),
            ('Resource type', task_record.get('resource_type') or task_record.get('resourcetype')),
            ('Site', task_record.get('site') or task.get('site')),
        )
        if value not in (None, '')
    ]

    return render(request, 'monitor_app/panda_task_detail.html', {
        'task': task,
        'jeditaskid': jeditaskid,
        'pcs_task': pcs_task,
        'panda_tasks_metadata': (panda_tasks_row.metadata if panda_tasks_row else {}),
        'jobs': jobs,
        'job_summary': summary,
        'job_count': len(jobs),
        'requested_resource_items': requested_resource_items,
        'task_record_items': task_record_items,
    })


# ── Error summary ────────────────────────────────────────────────────────────

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
            _fill_cell(job['jobstatus'], job['jobstatus']) if job.get('jobstatus') else '',
            job.get('computingsite', ''),
            '<br>'.join(errors_html) if errors_html else '',
            _fmt_dt(job.get('endtime')),
        ])

    return dt.create_response(data, total, total)


# ── ePIC Queue views ────────────────────────────────────────────────────────

def epic_queues_list(request):
    """ePIC compute queues from live PanDA schedconfig."""
    result = list_queues(vo='eic')
    queues = result.get('queues', [])
    return render(request, 'monitor_app/epic_queues_list.html', {
        'queues': queues,
    })


def epic_queue_detail(request, queue_name):
    """Full schedconfig for a single ePIC queue."""
    import json as json_mod
    try:
        from monitor_app.models import PandaQueue
        panda_queue = PandaQueue.objects.filter(queue_name=queue_name).first()
    except Exception:
        logger.exception("PandaQueue lookup failed for %s", queue_name)
        panda_queue = None
    result = get_queue(queue_name)
    if 'error' in result:
        return render(request, 'monitor_app/epic_queue_detail.html', {
            'error': result['error'],
            'queue_name': queue_name,
            'panda_queue_metadata': (panda_queue.metadata if panda_queue else {}),
        })
    config = result['queue']

    # Separate into sections for readability
    identity_keys = [
        'panda_queue', 'name', 'nickname', 'siteid', 'site', 'panda_site',
        'atlas_site', 'gocname', 'id',
    ]
    status_keys = [
        'status', 'state', 'rc_site_state', 'state_comment', 'state_update',
        'last_modified', 'last_update',
    ]
    resource_keys = [
        'resource_type', 'type', 'capability', 'corecount', 'corepower',
        'maxrss', 'meanrss', 'minrss', 'maxtime', 'mintime', 'maxwdir',
        'maxinputsize', 'timefloor', 'vo_name',
    ]
    location_keys = [
        'region', 'country', 'cloud', 'tier', 'tier_level', 'rc', 'rc_site',
        'rc_country',
    ]
    container_keys = [
        'container_type', 'container_options', 'is_cvmfs',
    ]
    pilot_keys = [
        'pilot_version', 'pilot_manager', 'python_version', 'jobseed',
    ]

    def _section(keys):
        return {k: config[k] for k in keys if k in config}

    sections = [
        ('Identity', _section(identity_keys)),
        ('Status', _section(status_keys)),
        ('Resources', _section(resource_keys)),
        ('Location', _section(location_keys)),
        ('Container', _section(container_keys)),
        ('Pilot', _section(pilot_keys)),
    ]

    # Everything else goes in "Other"
    shown = set()
    for _, s in sections:
        shown.update(s.keys())
    other = {k: v for k, v in config.items() if k not in shown}

    return render(request, 'monitor_app/epic_queue_detail.html', {
        'queue_name': queue_name,
        'panda_queue_metadata': (panda_queue.metadata if panda_queue else {}),
        'sections': sections,
        'other': other,
        'config_json': json_mod.dumps(config, indent=2, default=str),
    })
