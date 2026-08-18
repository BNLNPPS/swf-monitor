"""
PanDA Production Monitor views.

Web views for ePIC PanDA production monitoring — jobs, tasks, errors,
activity overview, and detail pages with rich cross-linking.
"""

from django.contrib.auth.decorators import login_required
from collections import Counter

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.conf import settings

import json
import logging
import os
import hashlib
import re
from html import escape
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from ..panda.operations import BULK_OPERATION_STATUSES

from ..utils import DataTablesProcessor
from ..panda import (
    get_activity, study_job, list_jobs,
    list_jobs_dt, build_tasks_window,
    job_filter_counts, task_filter_counts,
    get_task, error_summary, diagnose_jobs, job_completion_details,
    list_queues, get_queue, queue_last_use, resource_usage, job_outcomes,
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
    {'name': 'exec_time', 'title': 'Exec time', 'orderable': False},
    {'name': 'corecount', 'title': 'Cores', 'orderable': True},
]

JOB_FIELD_NAMES = [c['name'] for c in JOB_COLUMNS]

# Map DataTables column index to SQL ORDER BY expression
JOB_ORDER_MAP = {
    0: '"pandaid"', 1: '"jeditaskid"', 2: '"produsername"',
    3: '"jobstatus"', 4: '"computingsite"', 5: '"transformation"',
    6: '"creationtime"', 7: '"endtime"', 9: '"corecount"',
}

TASK_COLUMNS = [
    {'name': 'select', 'title': '', 'orderable': False},
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
    # new job record in the ePIC PanDA schema. The retry ceiling is the
    # file-level maxattempt in JEDI, set per task at submission.
    {'name': 'nretries', 'title': 'Retries', 'orderable': True},
    # Average retries per successful job — SUM(attemptnr-1) over finished
    # records / nfinished. 0 = every job passed first attempt. Efficiency
    # of the successes; complements Final Fail Rate (the permanent
    # failures). Requested by Rahman 2026-07-25.
    {'name': 'avg_retries_success', 'title': 'Avg Retries', 'orderable': True},
    # Derived from nfailed / (nfailed+nfinished). The native JEDI failurerate
    # column is always NULL in this deployment (post-processing that populates
    # it isn't running for ePIC task types), so this is the only signal shown.
    {'name': 'computed_failurerate', 'title': 'Fail Rate', 'orderable': True},
    # Final-failed: input files that exhausted the retry budget, from JEDI's
    # file-level accounting (jedi_datasets.nfilesfailed, master input rows).
    # The rate derived from these is what alarms trigger on — distinguishes
    # true failures from transient-fail-then-retry-succeeds. Job records
    # cannot express this: JEDI sets each record's maxattempt equal to its
    # own attemptnr.
    {'name': 'nfinalfailed', 'title': 'Final Failed Files', 'orderable': True},
    {'name': 'computed_finalfailurerate', 'title': 'Final Fail Rate', 'orderable': True},
]

TASK_FIELD_NAMES = [c['name'] for c in TASK_COLUMNS]

# The tasks list is served from a cached full-window product
# (docs/CACHED_PRODUCTS.md): one batch aggregation over the whole days
# window, built by queries.build_tasks_window, cached in swfdb and
# reused across users and draws. Sorting, filtering, and paging happen
# in-process over the product rows, so aggregate-column sorts cost
# nothing at the PanDA DB (the retired per-draw LATERAL cost ~2 s per
# aggregate sort). Each product row carries the rendered cells plus a
# raw record keyed by TASK_FIELD_NAMES for sort/filter/search.
TASKS_WINDOW_TTL_SECONDS = 120
TASKS_WINDOW_CAP = 5000
# Search parity with the retired SQL path (TASK_SEARCH_FIELDS).
TASKS_WINDOW_SEARCH_FIELDS = ('jeditaskid', 'taskname', 'status', 'username',
                              'processingtype', 'workinggroup', 'transpath')

ERROR_COLUMNS = [
    {'name': 'error_source', 'title': 'Component', 'orderable': False},
    {'name': 'error_code', 'title': 'Code', 'orderable': False},
    {'name': 'error_diag', 'title': 'Diagnostic', 'orderable': False},
    {'name': 'count', 'title': 'Count', 'orderable': False},
    {'name': 'task_count', 'title': 'Tasks', 'orderable': False},
    {'name': 'avg_time_to_error', 'title': 'Avg time to error',
     'orderable': False},
    {'name': 'users', 'title': 'Users', 'orderable': False},
    {'name': 'sites', 'title': 'Sites', 'orderable': False},
]


def _duration_text(seconds):
    """Compact human duration: 42s, 12.5m, 3.4h, 2.1d."""
    if seconds is None:
        return ''
    seconds = float(seconds)
    if seconds < 120:
        return f'{seconds:.0f}s'
    if seconds < 7200:
        return f'{seconds / 60:.1f}m'
    if seconds < 172800:
        return f'{seconds / 3600:.1f}h'
    return f'{seconds / 86400:.1f}d'

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


_NAIVE_ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$')


def _tag_panda_utc(obj):
    """PanDA DB timestamps are naive UTC. Stamp the offset onto naive ISO
    strings, in place, so downstream formatters render true Eastern."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                if _NAIVE_ISO_RE.match(value):
                    obj[key] = value + '+00:00'
            elif isinstance(value, (dict, list)):
                _tag_panda_utc(value)
    elif isinstance(obj, list):
        for item in obj:
            _tag_panda_utc(item)


def _fmt_dt(val):
    """Format an ISO datetime string or datetime object for display.
    Naive values are PanDA DB timestamps, which are UTC."""
    if not val:
        return ''
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except (ValueError, TypeError):
            return val
    if val.tzinfo is None:
        val = val.replace(tzinfo=dt_timezone.utc)
    return val.astimezone(_EASTERN).strftime('%Y%m%d %H:%M:%S')


def _exec_duration(job):
    """Return execution seconds for a terminal job from raw SQL timestamps."""
    if job.get('jobstatus') not in {'finished', 'failed'}:
        return None
    start = job.get('starttime')
    end = job.get('endtime')
    try:
        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        if isinstance(end, str):
            end = datetime.fromisoformat(end)
        seconds = (end - start).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


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


def _get_ended_window(request):
    """Exact terminal-job interval carried by Snapper drill-down links."""
    from django.utils.dateparse import parse_datetime

    after = parse_datetime((request.GET.get('ended_after') or '').strip())
    before = parse_datetime((request.GET.get('ended_before') or '').strip())
    if (after is None or before is None
            or after.tzinfo is None or before.tzinfo is None
            or after >= before):
        return None, None
    return after, before


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

def _activity_product(days, refresh):
    """PanDA activity aggregates as a cached product
    (docs/CACHED_PRODUCTS.md): served from the store, rebuilt behind.
    A builder error raises so a transient failure is never cached."""
    from ..cached_product import get_product

    def build():
        data = get_activity(days=days)
        if 'error' in data:
            raise RuntimeError(data['error'])
        return data

    return get_product(f'panda_activity:{days}', build,
                       ttl_seconds=300, refresh=refresh)


def _snapper_embed_product(days, refresh):
    """Curves-only Snapper state-history plot over the page's window
    (snapper_ai.embed), served as a cached product. Windows beyond the
    embed's 30-day clamp share one key: their content is identical."""
    from django.utils import timezone as dj_timezone

    from snapper_ai.embed import MAX_EMBED_DAYS, embed_context
    from ..cached_product import get_product

    def build():
        now = dj_timezone.now()
        ctx = embed_context('epicprod', now - timedelta(days=days), now,
                            families=('In-flight jobs', 'Tasks'))
        if ctx.get('error'):
            raise RuntimeError(ctx['error'])
        return ctx

    key_days = min(days, MAX_EMBED_DAYS + 1)
    return get_product(f'snapper_embed:v3:epicprod:{key_days}', build,
                       ttl_seconds=300, refresh=refresh)


def panda_activity(request):
    days = max(1, min(_get_days(request), 365))
    if request.GET.get('chip') == '1':
        # Read-only freshness status for the chip's bounded poll while
        # "refreshing…" shows. Reads the store rows directly — never
        # triggers a build.
        from snapper_ai.embed import MAX_EMBED_DAYS

        from ..models import CachedProduct
        keys = [f'panda_activity:{days}',
                f'snapper_embed:v3:epicprod:'
                f'{min(days, MAX_EMBED_DAYS + 1)}']
        rows = list(CachedProduct.objects.filter(key__in=keys))
        built = [row.built_at for row in rows if row.built_at]
        return JsonResponse({
            'built_at': (min(built).isoformat()
                         if len(built) == len(keys) else None),
            'refreshing': any(row.building_since for row in rows),
        })
    refresh = request.GET.get('refresh') == '1'
    built_ats = []
    refreshing = False
    try:
        product = _activity_product(days, refresh)
        data = product['value'] or {
            'error': 'PanDA activity is building — reload shortly.'}
        if product['built_at']:
            built_ats.append(product['built_at'])
        refreshing = refreshing or product['refreshing']
    except Exception as e:
        logger.error('panda activity build failed: %s', e)
        data = {'error': str(e)}
    if 'error' in data:
        data = {'error': data['error']}
    data.update(_days_context(days))
    try:
        embed_product = _snapper_embed_product(days, refresh)
        data['snapper_embed'] = embed_product['value'] or {
            'scope': 'epicprod',
            'error': 'state history is building — reload shortly.'}
        if embed_product['built_at']:
            built_ats.append(embed_product['built_at'])
        refreshing = refreshing or embed_product['refreshing']
    except Exception as e:
        logger.error('snapper embed failed for panda activity: %s', e)
        data['snapper_embed'] = {'scope': 'epicprod', 'error': str(e)}
    if built_ats:
        data['product_built_at_text'] = (
            min(built_ats).astimezone(ZoneInfo(settings.TIME_ZONE))
            .strftime('%Y-%m-%d %H:%M ET'))
        data['product_built_at_iso'] = min(built_ats).isoformat()
        data['product_refreshing'] = refreshing
    return render(request, 'monitor_app/panda_activity.html', data)


def _compute_usage_dates(request):
    """Parse an inclusive Eastern calendar-date range from the request."""
    today = datetime.now(ZoneInfo(settings.TIME_ZONE)).date()
    try:
        start_date = date.fromisoformat(
            request.GET.get('start') or str(today - timedelta(days=29)))
        end_date = date.fromisoformat(request.GET.get('end') or str(today))
    except ValueError:
        return None, 'start and end must be dates in YYYY-MM-DD form'
    if start_date > end_date:
        return None, 'start must be on or before end'
    bucket = (request.GET.get('bucket') or 'day').strip().lower()
    if bucket not in {'day', 'week'}:
        return None, "bucket must be 'day' or 'week'"
    return (start_date, end_date, bucket), ''


def _query_compute_usage(start_date, end_date, bucket, site=None,
                         series_rollup=False):
    """Plot-ready resource data for an inclusive Eastern date range,
    served as a cached product (docs/CACHED_PRODUCTS.md): the PanDA-DB
    aggregation never builds in the request path once a key is
    filled."""
    from ..cached_product import get_product

    def build():
        tz = ZoneInfo(settings.TIME_ZONE)
        start_time = datetime.combine(start_date, time.min, tzinfo=tz)
        end_time = datetime.combine(
            end_date + timedelta(days=1), time.min, tzinfo=tz)
        usage = resource_usage(
            start_time=start_time,
            end_time=end_time,
            bucket=bucket,
            site=site,
            series_rollup=series_rollup,
        )
        if usage.get('error'):
            raise RuntimeError(usage['error'])
        usage['display_window'] = {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
            'timezone': settings.TIME_ZONE,
        }
        return usage

    key = (f'compute_usage:v2:{start_date}:{end_date}:{bucket}'
           f":{site or ''}:{int(series_rollup)}")
    try:
        product = get_product(key, build, ttl_seconds=300)
    except Exception as e:                                  # noqa: BLE001
        logger.error('compute usage build failed: %s', e)
        return None, str(e)
    usage = product['value']
    if not usage:
        return None, 'compute usage is building — reload shortly'
    return usage, ''


def _compute_usage(request):
    """Plot/API resource data for the request's exact date range."""
    selection, error = _compute_usage_dates(request)
    if error:
        return None, error
    start_date, end_date, bucket = selection
    return _query_compute_usage(
        start_date,
        end_date,
        bucket,
        site=(request.GET.get('site') or '').strip() or None,
    )


def compute_usage(request):
    """Site-level PanDA core-hour history and interactive production plot."""
    today = datetime.now(ZoneInfo(settings.TIME_ZONE)).date()
    selection, error = _compute_usage_dates(request)
    if selection:
        selected_start, selected_end, bucket = selection
        # Keep the standard six-month aggregate in this page. Preset range
        # changes are then pure client-side filtering, not new requests.
        loaded_start = min(selected_start, today - timedelta(days=179))
        loaded_end = max(selected_end, today)
        usage, error = _query_compute_usage(
            loaded_start,
            loaded_end,
            'day',
            series_rollup=True,
        )
        start = str(selected_start)
        end = str(selected_end)
    else:
        usage = None
        bucket = (request.GET.get('bucket') or 'day').strip().lower()
        start = request.GET.get('start') or str(today - timedelta(days=29))
        end = request.GET.get('end') or str(today)
    periods = []
    for days, label in (
        (7, 'Last week'),
        (14, '2 weeks'),
        (30, 'Month'),
        (90, '3 months'),
        (180, '6 months'),
    ):
        period_start = str(today - timedelta(days=days - 1))
        period_end = str(today)
        periods.append({
            'label': label,
            'start': period_start,
            'end': period_end,
            'active': start == period_start and end == period_end,
        })
    return render(request, 'monitor_app/compute_usage.html', {
        'error': error,
        'usage': usage or {},
        'start': start,
        'end': end,
        'bucket': bucket,
        'periods': periods,
    })


def compute_usage_data(request):
    """Plot-ready JSON peer of the compute-usage production page."""
    usage, error = _compute_usage(request)
    if error:
        return JsonResponse({'error': error}, status=400)
    return JsonResponse(usage)


# ── Job list ─────────────────────────────────────────────────────────────────

def _jobs_outcomes_product(days, site, refresh,
                           ended_after=None, ended_before=None):
    """Completed-job outcome series (finished/failed per bin with
    cumulative integrals) for the jobs page's graphical view, served
    as a cached product."""
    from ..cached_product import get_product

    def build():
        outcomes = job_outcomes(
            days=days, site=site or None,
            start_time=ended_after, end_time=ended_before)
        if outcomes.get('error'):
            raise RuntimeError(outcomes['error'])
        return outcomes

    exact_key = (
        f':{ended_after.isoformat()}:{ended_before.isoformat()}'
        if ended_after is not None and ended_before is not None else '')
    return get_product(
        f"jobs_outcomes:v2:{days}:{site or ''}{exact_key}", build,
        ttl_seconds=300, refresh=refresh)


def _jobs_site_graphics_product(days, site, refresh,
                                ended_after=None, ended_before=None):
    """Placeable Snapper site history and outcomes pie for this page."""
    from django.utils import timezone as dj_timezone

    from snapper_ai.embed import embed_context
    from ..cached_product import get_product
    from ..snapper_providers import panda_site_outcomes_pie

    def build():
        end = ended_before or dj_timezone.now()
        start = ended_after or (end - timedelta(days=days))
        ctx = embed_context(
            'epicprod', start, end,
            families=(f'Site jobs {site}',))
        if ctx.get('error'):
            raise RuntimeError(ctx['error'])
        # This embedded Site plot must open the same focused Site page,
        # not the generic epicprod report.
        ctx['report_focus_slug'] = 'site'
        ctx['report_query'] = (
            urlencode({'site': site}) + '&' + ctx['report_query'])
        return {
            'embed': ctx,
            'outcomes_pie': panda_site_outcomes_pie(
                site, start, end, size=270),
        }

    exact_key = (
        f':{ended_after.isoformat()}:{ended_before.isoformat()}'
        if ended_after is not None and ended_before is not None else '')
    return get_product(
        f'snapper_site_graphics:v4:epicprod:site:{site}:{days}{exact_key}',
        build,
        ttl_seconds=300, refresh=refresh)


def panda_jobs_list(request):
    days = _get_days(request)
    ended_after, ended_before = _get_ended_window(request)
    selected_site = request.GET.get('site', '')
    if ended_after is not None:
        et = ZoneInfo(settings.TIME_ZONE)
        ended_range_label = (
            f"{ended_after.astimezone(et).strftime('%m-%d %H:%M')}"
            f" – {ended_before.astimezone(et).strftime('%m-%d %H:%M')} ET")
        description = f'Production jobs ending in {ended_range_label}.'
        jobs_window_label = ended_range_label
    else:
        description = f'Production jobs from the last {days} days.'
        jobs_window_label = f'last {days} day' + ('' if days == 1 else 's')
    if selected_site:
        site_url = reverse('monitor_app:epic_queue_detail', args=[selected_site])
        description += f'<br><a href="{site_url}">Site info for <strong>{selected_site}</strong></a>'

    refresh = request.GET.get('refresh') == '1'
    try:
        outcomes_product = _jobs_outcomes_product(
            days, selected_site, refresh,
            ended_after=ended_after, ended_before=ended_before)
        job_outcomes_data = outcomes_product['value'] or {
            'error': 'Outcome history is building — reload shortly.'}
        if outcomes_product['value'] and outcomes_product['built_at']:
            job_outcomes_data = dict(job_outcomes_data)
            job_outcomes_data['built_at_text'] = (
                outcomes_product['built_at']
                .astimezone(ZoneInfo(settings.TIME_ZONE))
                .strftime('%H:%M ET'))
    except Exception as e:                                   # noqa: BLE001
        logger.error('jobs outcomes build failed: %s', e)
        job_outcomes_data = {'error': str(e)}
    snapper_embed = None
    job_outcomes_pie = None
    if selected_site:
        try:
            graphics_product = _jobs_site_graphics_product(
                days, selected_site, refresh,
                ended_after=ended_after, ended_before=ended_before)
            graphics = graphics_product['value'] or {}
            snapper_embed = graphics.get('embed') or {
                'scope': 'epicprod',
                'error': 'state history is building — reload shortly.'}
            job_outcomes_pie = graphics.get('outcomes_pie')
        except Exception as e:                               # noqa: BLE001
            logger.error('snapper site graphics failed for jobs list: %s', e)
            snapper_embed = {'scope': 'epicprod', 'error': str(e)}

    context = {
        'job_outcomes': job_outcomes_data,
        'job_outcomes_pie': job_outcomes_pie,
        'snapper_embed': snapper_embed,
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
        'ended_after': (ended_after.isoformat()
                        if ended_after is not None else ''),
        'ended_before': (ended_before.isoformat()
                         if ended_before is not None else ''),
        'jobs_window_label': jobs_window_label,
        # Nearest Snapper named window covering this page's day range,
        # for the Snapper view link on the time-window line.
        'snapper_window': ('24h' if days <= 1 else
                           '48h' if days <= 2 else
                           '7d' if days <= 7 else '30d'),
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_jobs_list.html', context)


def panda_jobs_datatable_ajax(request):
    dt = DataTablesProcessor(request, JOB_FIELD_NAMES,
                             default_order_column=0, default_order_direction='desc')
    days = _get_days(request)
    ended_after, ended_before = _get_ended_window(request)
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
        ended_after=ended_after, ended_before=ended_before,
    )

    window_params = {'days': days}
    if ended_after is not None:
        window_params.update({
            'ended_after': ended_after.isoformat(),
            'ended_before': ended_before.isoformat(),
        })
    data = []
    for job in rows:
        exec_duration = _exec_duration(job)
        job_url = reverse('monitor_app:panda_job_detail', args=[job['pandaid']])
        task_url = reverse('monitor_app:panda_task_detail', args=[job['jeditaskid']]) if job.get('jeditaskid') else None
        jobs_by_user_url = _url_with_query(
            'monitor_app:panda_jobs_list',
            **{**window_params, 'username': job['produsername']}
        ) if job.get('produsername') else None
        jobs_by_site_url = _url_with_query(
            'monitor_app:panda_jobs_list',
            **{**window_params, 'site': job['computingsite']}
        ) if job.get('computingsite') else None
        jobs_by_status_url = _url_with_query(
            'monitor_app:panda_jobs_list',
            **{**window_params, 'status': job['jobstatus']}
        ) if job.get('jobstatus') else None

        data.append([
            f'<a href="{job_url}">{job["pandaid"]}</a>',
            f'<a href="{task_url}">{job["jeditaskid"]}</a>' if task_url else str(job.get('jeditaskid', '')),
            f'<a href="{jobs_by_user_url}">{job["produsername"]}</a>' if jobs_by_user_url else '',
            _fill_cell(job['jobstatus'], job['jobstatus'], jobs_by_status_url) if job.get('jobstatus') else '',
            f'<a href="{jobs_by_site_url}">{job["computingsite"]}</a>' if jobs_by_site_url else '',
            _linkify(job.get('transformation', '') or ''),
            _fmt_dt(job.get('creationtime')),
            _fmt_dt(job.get('endtime')),
            _duration_text(exec_duration),
            str(job.get('corecount', '') or ''),
        ])

    return dt.create_response(data, total, filtered)


def panda_jobs_filter_counts(request):
    days = _get_days(request)
    ended_after, ended_before = _get_ended_window(request)
    status = request.GET.get('status', '') or None
    username = request.GET.get('username', '') or None
    site = request.GET.get('site', '') or None
    taskid = request.GET.get('taskid', '') or None
    reqid = request.GET.get('reqid', '') or None

    counts = job_filter_counts(days=days, status=status, username=username,
                               site=site, taskid=taskid, reqid=reqid,
                               ended_after=ended_after,
                               ended_before=ended_before)
    return JsonResponse({'filter_counts': counts})


# ── Task list ────────────────────────────────────────────────────────────────

def panda_tasks_list(request):
    days = _get_days(request)
    from ..middleware import is_tunnel_request

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
        'bulk_controls_operable': (
            request.user.is_authenticated and not is_tunnel_request(request)),
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_tasks_list.html', context)


def _format_task_row(task, days, *, controls_operable):
    """Render one task row, including its bulk-action selection contract."""
    task_url = reverse('monitor_app:panda_task_detail', args=[task['jeditaskid']])
    tasks_by_user_url = _url_with_query('monitor_app:panda_tasks_list', days=days, username=task['username']) if task.get('username') else None
    tasks_by_status_url = _url_with_query('monitor_app:panda_tasks_list', days=days, status=task['status']) if task.get('status') else None

    # Truncate taskname for display; escape user-influenced values so a
    # name containing quotes or markup cannot break out of the cell.
    taskname_display = task.get('taskname', '') or ''
    if len(taskname_display) > 80:
        taskname_display = taskname_display[:77] + '...'
    taskname_display = escape(taskname_display)
    taskname_title = escape(task.get('taskname', '') or '')
    username_html = escape(task.get('username', '') or '')

    comp_pr = task.get('computed_progress')
    comp_pr_str = f'{comp_pr}%' if comp_pr is not None else ''

    comp_fr = task.get('computed_failurerate')
    comp_fr_str = f'{comp_fr * 100:.1f}%' if comp_fr is not None else ''

    comp_ffr = task.get('computed_finalfailurerate')
    comp_ffr_str = f'{comp_ffr * 100:.1f}%' if comp_ffr is not None else ''

    avg_rs = task.get('avg_retries_success')
    avg_rs_str = f'{avg_rs:.2f}' if avg_rs is not None else ''

    processingtype = task.get('processingtype') or ''
    processingtype_html = escape(processingtype)
    processingtype_display = (
        f'<span class="badge bg-warning text-dark">{processingtype_html}</span>'
        if 'test' in processingtype.lower()
        else processingtype_html
    )
    task_status = str(task.get('status') or '').lower()
    eligible = {
        op: task_status in statuses
        for op, statuses in BULK_OPERATION_STATUSES.items()
    }
    actionable = any(eligible.values())
    checkbox_disabled = not controls_operable or not actionable
    eligible_attrs = ' '.join(
        f'data-{op.replace("_", "-")}-eligible="{1 if ok else 0}"'
        for op, ok in eligible.items())
    checkbox = (
        f'<input type="checkbox" class="panda-task-select" '
        f'data-task-id="{int(task["jeditaskid"])}" '
        f'{eligible_attrs} '
        f'aria-label="Select PanDA task {int(task["jeditaskid"])}"'
        f'{" disabled" if checkbox_disabled else ""}>'
    )

    return [
        checkbox,
        f'<a href="{task_url}">{task["jeditaskid"]}</a>',
        f'<a href="{task_url}" title="{taskname_title}">{taskname_display}</a>',
        _fill_cell(task['status'], task['status'], tasks_by_status_url) if task.get('status') else '',
        processingtype_display,
        f'<a href="{tasks_by_user_url}">{username_html}</a>' if tasks_by_user_url else '',
        _fmt_dt(task.get('creationdate')),
        _fmt_dt(task.get('modificationtime')),
        comp_pr_str,
        _fill_cell(task.get('nactive', 0), 'running') if task.get('nactive', 0) else 0,
        _fill_cell(task.get('nfinished', 0), 'finished') if task.get('nfinished', 0) else 0,
        _fill_cell(task.get('nfailed', 0), 'failed') if task.get('nfailed', 0) else 0,
        _fill_cell(task.get('nrunning', 0), 'running') if task.get('nrunning', 0) else 0,
        task.get('nretries', 0),
        avg_rs_str,
        comp_fr_str,
        _fill_cell(task.get('nfinalfailed', 0), 'failed') if task.get('nfinalfailed', 0) else 0,
        comp_ffr_str,
    ]


def _build_tasks_window_product(days):
    """Builder for the cached tasks-window product: the raw task record
    per row. Cells render at SERVE time in request context — a builder
    can run in a background thread with no script prefix, and rendered
    reverse() links stored from there are dead (CLAUDE.md)."""
    window = build_tasks_window(days=days, cap=TASKS_WINDOW_CAP)
    rows = [{'raw': task} for task in window['tasks']]
    return {'rows': rows, 'count': window['count'],
            'truncated': window['truncated'], 'days': days}


def _window_sort_key(value):
    """Normalize a raw value for in-process column sorting. Dates are ISO
    strings (lexical == chronological); strings compare case-insensitively."""
    if isinstance(value, str):
        return value.lower()
    return value


def panda_tasks_datatable_ajax(request):
    from ..cached_product import get_product
    from ..middleware import is_tunnel_request

    dt = DataTablesProcessor(request, TASK_FIELD_NAMES,
                             default_order_column=0, default_order_direction='desc')
    days = _get_days(request)

    product = get_product(
        f'panda_tasks_window:v2:{days}',
        lambda: _build_tasks_window_product(days),
        ttl_seconds=TASKS_WINDOW_TTL_SECONDS,
        refresh=request.GET.get('refresh') == '1',
    )
    value = product['value'] or {}
    rows = value.get('rows') or []
    total = len(rows)
    product_extra = {
        'product_built_at': (product['built_at'].isoformat()
                             if product['built_at'] else None),
        'product_age_seconds': product['age_seconds'],
        'product_refreshing': product['refreshing'],
    }

    # Equality filters over the raw record; '__blank__' selects NULL/empty.
    for key in ('status', 'username', 'processingtype'):
        wanted = request.GET.get(key, '') or None
        if wanted is None:
            continue
        if wanted == '__blank__':
            rows = [r for r in rows if not (r['raw'].get(key) or '')]
        else:
            rows = [r for r in rows if (r['raw'].get(key) or '') == wanted]

    search = (dt.search_value or '').strip().lower()
    if search:
        rows = [r for r in rows
                if any(search in str(r['raw'].get(f) or '').lower()
                       for f in TASKS_WINDOW_SEARCH_FIELDS)]
    filtered = len(rows)

    # Sort on the raw column value; rows missing the value go last in
    # either direction (parity with the retired SQL NULLS LAST).
    idx = dt.order_column_idx
    col = TASK_FIELD_NAMES[idx] if 0 <= idx < len(TASK_FIELD_NAMES) else 'jeditaskid'
    reverse = dt.order_direction != 'asc'
    present = [r for r in rows if r['raw'].get(col) not in (None, '')]
    missing = [r for r in rows if r['raw'].get(col) in (None, '')]
    present.sort(key=lambda r: _window_sort_key(r['raw'].get(col)), reverse=reverse)
    rows = present + missing

    if dt.length and dt.length > 0:
        page = rows[dt.start:dt.start + dt.length]
    else:
        page = rows[dt.start:] if dt.start else rows

    controls_operable = (
        request.user.is_authenticated and not is_tunnel_request(request))
    data = [
        _format_task_row(r['raw'], days, controls_operable=controls_operable)
        for r in page
    ]
    return dt.create_response(data, total, filtered, extra=product_extra)


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
    _tag_panda_utc(data)
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
           'jeditaskid': str(jeditaskid), 'pandaid': str(pandaid),
           'requested_by': (request.user.username
                            if request.user.is_authenticated else '')}
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
    _tag_panda_utc(task)
    pcs_task = _pcs_task_for_panda_task(task)
    panda_tasks_row = _panda_tasks_row_for_jeditaskid(jeditaskid)

    def _task_transformation_url(value):
        transpath = (value or '').strip()
        if not transpath:
            return ''
        if transpath.startswith(('http://', 'https://')):
            return (
                _panda_view_text_url(transpath)
                if 'pandaserver-doma.cern.ch/trf/' in transpath else transpath
            )
        return _panda_view_text_url(
            'https://pandaserver-doma.cern.ch/trf/user/'
            + quote(transpath.strip('/'), safe='')
        )

    transpath = task.get('transpath') or ''
    if transpath:
        task['transformation_view_url'] = _task_transformation_url(transpath)

    # Get jobs for this task
    jobs_data = list_jobs(taskid=int(jeditaskid), days=90, limit=200)
    jobs = jobs_data.get('jobs', []) if not jobs_data.get('error') else []
    summary = jobs_data.get('summary', {}) if not jobs_data.get('error') else {}
    completion_details = job_completion_details([job.get('pandaid') for job in jobs])
    from ..models import EpicProdJob, PandaTaskOperation
    from ..middleware import is_tunnel_request
    from ..panda.operations import operation_controls, serialize_operation
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
    _tag_panda_utc(jobs)
    task_record = task.get('task_record') or {}
    task_record_items = [
        {
            'name': key,
            'value': '' if value is None else value,
            'href': _task_transformation_url(value) if key == 'transpath' else '',
        }
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
    # PanDA attributes every command to the DN of the issuing credential,
    # so agent-executed operations read as the credential holder in
    # errordialog. When the durable operation record identifies the real
    # requester, render the system as the actor and the requester by name.
    errordialog_display = str(task.get('errordialog') or '')
    dialog_match = re.match(
        r'^(pause|resume|retry|incexec|finish|kill)\s+by\s+', errordialog_display)
    if dialog_match:
        verb_operations = {
            'pause': 'pause', 'resume': 'resume', 'finish': 'finish',
            'kill': 'finish', 'retry': 'retry_failures',
            'incexec': 'retry_failures',
        }
        attributed_op = (PandaTaskOperation.objects
                         .filter(jedi_task_id=int(jeditaskid),
                                 operation=verb_operations[dialog_match.group(1)])
                         .exclude(status__in=('failed', 'timeout'))
                         .order_by('-requested_at')
                         .first())
        if attributed_op and attributed_op.requested_by:
            errordialog_display = (
                f'{dialog_match.group(1)} by epicprod. '
                f'Requested by {attributed_op.requested_by}')

    pending_operation = (PandaTaskOperation.objects
                         .filter(
                             jedi_task_id=int(jeditaskid),
                             status__in=PandaTaskOperation.PENDING_STATUSES)
                         .first())
    task_operation_controls = operation_controls(
        task,
        authenticated=request.user.is_authenticated,
        internal_monitor=not is_tunnel_request(request),
        pending=pending_operation,
    )

    return render(request, 'monitor_app/panda_task_detail.html', {
        'task': task,
        'errordialog_display': errordialog_display,
        'jeditaskid': jeditaskid,
        'pcs_task': pcs_task,
        'panda_tasks_metadata': (panda_tasks_row.metadata if panda_tasks_row else {}),
        'jobs': jobs,
        'job_summary': summary,
        'job_count': len(jobs),
        'requested_resource_items': requested_resource_items,
        'task_record_items': task_record_items,
        'task_operation_controls': task_operation_controls,
        'pending_task_operation': (
            serialize_operation(pending_operation)
            if pending_operation else None),
    })


# ── Error summary ────────────────────────────────────────────────────────────

def panda_errors_list(request):
    days = _get_days(request)
    ended_after, ended_before = _get_ended_window(request)
    if ended_after is not None:
        et = ZoneInfo(settings.TIME_ZONE)
        range_label = (
            f"{ended_after.astimezone(et).strftime('%m-%d %H:%M')}"
            f" – {ended_before.astimezone(et).strftime('%m-%d %H:%M')} ET")
        description = f'Top error patterns across jobs ending in {range_label}.'
    else:
        description = (
            f'Top error patterns across failed jobs in the last {days} days.')
    context = {
        'table_title': 'PanDA Error Summary',
        'table_description': description,
        'ajax_url': reverse('monitor_app:panda_errors_datatable_ajax'),
        'columns': ERROR_COLUMNS,
        'selected_site': request.GET.get('site', ''),
        'selected_error_source': request.GET.get('error_source', ''),
        'selected_status': request.GET.get('status', ''),
        'classified': request.GET.get('classified', ''),
        'ended_after': (ended_after.isoformat()
                        if ended_after is not None else ''),
        'ended_before': (ended_before.isoformat()
                         if ended_before is not None else ''),
    }
    context.update(_days_context(days))
    return render(request, 'monitor_app/panda_errors.html', context)


def panda_errors_datatable_ajax(request):
    from ..cached_product import get_product

    dt = DataTablesProcessor(request, [c['name'] for c in ERROR_COLUMNS],
                             default_order_column=3, default_order_direction='desc')
    days = _get_days(request)
    ended_after, ended_before = _get_ended_window(request)
    username = request.GET.get('username', '') or None
    site = request.GET.get('site', '') or None
    error_source = request.GET.get('error_source', '') or None
    status = request.GET.get('status', '') or None
    classified = request.GET.get('classified') == '1'

    # Served as a cached product: the error aggregation scans the window's
    # full faulty-job population (multi-second under failure churn), so
    # requests serve the stored summary and rebuilds run behind them.
    product = get_product(
        f'panda_errors:v2:{days}:{username or ""}:{site or ""}'
        f':{error_source or ""}:{status or ""}:{int(classified)}:'
        f'{ended_after.isoformat() if ended_after else ""}:'
        f'{ended_before.isoformat() if ended_before else ""}',
        lambda: error_summary(days=days, username=username, site=site,
                              error_source=error_source, limit=200,
                              ended_after=ended_after,
                              ended_before=ended_before, status=status,
                              classified=classified),
        ttl_seconds=300,
        refresh=request.GET.get('refresh') == '1',
    )
    result = product['value'] or {}
    product_extra = {
        'product_built_at': (product['built_at'].isoformat()
                             if product['built_at'] else None),
        'product_age_seconds': product['age_seconds'],
        'product_refreshing': product['refreshing'],
    }

    if 'error' in result:
        return dt.create_response([], 0, 0, extra=product_extra)

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

        # Plain escaped text: the cell's CSS ellipsis truncates visually
        # and the base template titles the td with the full value, which
        # Bootstrap shows as the one immediate tooltip. An inner
        # title= span would add a second, delayed native floater.
        diag_text = escape(err.get('error_diag', '') or '')

        # Average run time before this error ended the job; patterns
        # whose jobs never started (pre-run failures) say so instead of
        # averaging in zeros.
        avg_text = _duration_text(err.get('avg_seconds_to_error'))
        never_started = err.get('never_started_count') or 0
        if not avg_text and never_started:
            avg_text = 'never started'
        elif avg_text and never_started:
            avg_text += f' ({never_started} never started)'

        data.append([
            f'<a href="{diag_url}">{err["error_source"]}</a>',
            str(err.get('error_code', '')),
            diag_text,
            str(err.get('count', 0)),
            str(err.get('task_count', 0)),
            avg_text,
            users_str,
            sites_str,
        ])

    return dt.create_response(data, total, total, extra=product_extra)


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
    # Operator-written description and tier live in the local model's
    # metadata, which the CRIC sync never touches. One query, attached by
    # name. The tier held there overrides schedconfig, whose value reflects
    # the parent site rather than the facility actually providing the cycles.
    from monitor_app.models import PandaQueue
    local = {
        row.queue_name: (row.metadata or {})
        for row in PandaQueue.objects.only('queue_name', 'metadata')
    }
    # Canary's curated per-queue health, the failure percentage its policy
    # judges, and the last job activity from the PanDA jobs tables, all
    # joined by queue name.
    from canary.store.models import PassiveSample, Queue as CanaryQueue
    canary_queues = list(CanaryQueue.objects.all())
    canary_health = {q.name: q.status for q in canary_queues}
    canary_names = {q.id: q.name for q in canary_queues}
    canary_pct = {}
    seen_samples = set()
    for sample in PassiveSample.objects.order_by('queue_id', '-window_end'):
        if sample.queue_id in seen_samples:
            continue
        seen_samples.add(sample.queue_id)
        name = canary_names.get(sample.queue_id)
        if name and sample.failure_rate is not None:
            canary_pct[name] = f'{sample.failure_rate * 100:.0f}%'
    last_use = queue_last_use()
    for queue in queues:
        name = queue.get('panda_queue')
        meta = local.get(name, {})
        queue['description'] = meta.get('description', '')
        queue['tier'] = meta.get('tier') or queue.get('tier') or ''
        queue['canary'] = canary_health.get(name, 'unknown')
        queue['canary_pct'] = canary_pct.get(name, '')
        queue['last_use'] = last_use.get(name)
        # Schedconfig mixes caps in resource_type (GRID vs cloud/gpu);
        # display lowercase throughout.
        if queue.get('resource_type'):
            queue['resource_type'] = queue['resource_type'].lower()

    filter_fields = [
        ('status', 'Status'),
        ('canary', 'Canary'),
        ('resource_type', 'Resource Type'),
        ('type', 'Queue Type'),
        ('country', 'Region'),
        ('tier', 'Tier'),
    ]
    # Options come from the full set, so a chosen filter does not empty the
    # other filter bars.
    # Tier options rank T1, T2, ... then Opp, not alphabetically.
    def _tier_rank(v):
        if v.startswith('T') and v[1:].isdigit():
            return (0, int(v[1:]), v)
        if v == 'Opp':
            return (1, 0, v)
        return (2, 0, v)

    filters = []
    selected = {}
    for key, label in filter_fields:
        value = (request.GET.get(key) or '').strip()
        selected[key] = value
        counts = Counter((q.get(key) or '') for q in queues if q.get(key))
        ordered = sorted(counts, key=_tier_rank) if key == 'tier' else sorted(counts)
        filters.append({
            'key': key, 'label': label, 'selected': value,
            'options': [{'value': v, 'count': counts[v]} for v in ordered],
        })
    for key, value in selected.items():
        if value:
            queues = [q for q in queues if (q.get(key) or '') == value]

    return render(request, 'monitor_app/epic_queues_list.html', {
        'queues': queues,
        'filters': filters,
        'active_filters': [
            {'label': f['label'], 'value': f['selected']}
            for f in filters if f['selected']
        ],
        'clear_all_url': reverse('monitor_app:epic_queues_list'),
        'any_filter': any(selected.values()),
        'total_count': result.get('count', 0),
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
            'description': ((panda_queue.metadata if panda_queue else {}) or {}).get('description', ''),
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
        'description': ((panda_queue.metadata if panda_queue else {}) or {}).get('description', ''),
        'sections': sections,
        'other': other,
        'config_json': json_mod.dumps(config, indent=2, default=str),
    })


@login_required
def epic_queue_description_update(request, queue_name):
    """Save the operator-written description for a queue.

    The description is held in the local model's ``metadata`` JSON rather than
    in the mirrored schedconfig: the sync writers replace ``config_data`` and
    leave ``metadata`` alone, so the text survives every refresh. The local
    row is created on demand, since most queues are only known from
    schedconfig until somebody annotates them.
    """
    from monitor_app.models import PandaQueue

    if request.method != 'POST':
        return redirect('monitor_app:epic_queue_detail', queue_name=queue_name)

    queue, _created = PandaQueue.objects.get_or_create(
        queue_name=queue_name,
        defaults={'config_data': {}, 'metadata': {}},
    )
    metadata = dict(queue.metadata or {})
    metadata['description'] = (request.POST.get('description') or '').strip()
    metadata['description_updated_by'] = request.user.get_username()
    metadata['description_updated_at'] = timezone.now().isoformat()
    queue.metadata = metadata
    queue.save(update_fields=['metadata', 'updated_at'])
    return redirect('monitor_app:epic_queue_detail', queue_name=queue_name)
