"""
PCS web UI views and DataTable AJAX endpoints.

Views are generic across tag types (p/e/s/r) where possible, parameterized by tag_type.
Tag list views use server-side DataTables via monitor_app._datatable_base.html.
Read operations are public; create/edit/lock require login.
"""
import json
import time
import hashlib
from functools import wraps
from urllib.request import urlopen
from urllib.parse import quote as urlquote
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.http import JsonResponse, Http404
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime


# ---------------------------------------------------------------------------
# Auth / method-guard decorators that flash instead of silently redirecting.
# Project-wide NO-SILENT-FAILURES rule: an action-button click that hits a
# guard must tell the user what happened, never just refresh the page.
# ---------------------------------------------------------------------------

def _login_required_flash(view):
    """Like @login_required but flashes an explicit error before the redirect."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, 'Sign in required for this action.')
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")
        return view(request, *args, **kwargs)
    return wrapped


def _post_only_redirect(request, fallback_url, action_label='This action'):
    """Helper used by POST-only views: flash a warning, redirect to fallback.

    Use at the top of any POST-only handler instead of a bare
    ``if request.method != 'POST': return redirect(...)`` block.
    """
    messages.warning(request, f'{action_label} only responds to POST submissions.')
    return redirect(fallback_url)

from monitor_app.utils import DataTablesProcessor, get_filter_params, format_datetime

from .models import (
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
    Dataset, ProdConfig, ProdTask,
    Campaign, Questionnaire, ProdRequest,
    PRODTASK_STATUS_CHOICES,
)
from .serializers import _redact_contact

CATALOG_TASK_LIST_CACHE_VERSION = 4
CATALOG_BUILD_TIMING_ENABLED = False

# Seed list of known requestor labels (PWGs + DSCs). Catalog pulldown
# surfaces these plus any distinct values already in the DB.
REQUESTOR_SEED_OPTIONS = (
    'DIS', 'SIDIS', 'EXCLUSIVE', 'JET', 'HF', 'EW', 'BSM',
    'TRACKING-DSC', 'CALORIMETRY-DSC', 'PID-DSC',
)


def _timing_ms(seconds):
    return round(seconds * 1000.0, 1)


def _timing_record(timings, label, start, *, detail=''):
    if timings is not None:
        ms = _timing_ms(time.perf_counter() - start)
        timings.append({
            'label': label,
            'ms': ms,
            'ms_display': f'{ms} ms',
            'detail': detail,
        })


def _timing_note(timings, label, *, detail=''):
    if timings is not None:
        timings.append({'label': label, 'ms': None, 'ms_display': '', 'detail': detail})


def _timed(timings, label, fn, *, detail_fn=None):
    if timings is None:
        return fn()
    start = time.perf_counter()
    result = fn()
    detail = detail_fn(result) if detail_fn else ''
    _timing_record(timings, label, start, detail=detail)
    return result


def _requestor_options():
    """Distinct existing requestors ∪ seed options, sorted."""
    cache_key = 'pcs:catalog:requestor-options:v1'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    seen = set(REQUESTOR_SEED_OPTIONS)
    seen.update(
        ProdRequest.objects.exclude(requestor='')
        .values_list('requestor', flat=True).distinct()
    )
    seen.update(
        ProdTask.objects.exclude(requestor='')
        .values_list('requestor', flat=True).distinct()
    )
    options = sorted(seen)
    cache.set(cache_key, options, 300)
    return options


def _parse_catalog_filters(request):
    """Parse catalog filter query params into a dict of clean values."""
    return {
        'q': (request.GET.get('q') or '').strip(),
        'status': (request.GET.get('status') or '').strip(),
        'requestor': (request.GET.get('requestor') or '').strip(),
        'submission_path': (request.GET.get('submission_path') or '').strip(),
        'pre_tdr_use': request.GET.get('pre_tdr_use') == '1',
        'early_science_use': request.GET.get('early_science_use') == '1',
        'other_use': request.GET.get('other_use') == '1',
    }


def _apply_catalog_filters(qs, filters):
    """Apply a parsed-filters dict to a ProdTask queryset."""
    if filters['q']:
        qs = qs.filter(Q(name__icontains=filters['q'])
                       | Q(description__icontains=filters['q']))
    if filters['status']:
        qs = qs.filter(status=filters['status'])
    if filters['requestor']:
        qs = qs.filter(requestor=filters['requestor'])
    if filters['submission_path']:
        # submission_path lives in ProdConfig.data JSON; default 'condor'
        if filters['submission_path'] == 'condor':
            # Match rows where data is null/missing the key (default) OR key='condor'
            qs = qs.filter(
                Q(prod_config__data__submission_path='condor')
                | Q(prod_config__data__submission_path__isnull=True)
                | Q(prod_config__data__isnull=True)
            )
        else:
            qs = qs.filter(prod_config__data__submission_path=filters['submission_path'])
    if filters['pre_tdr_use']:
        qs = qs.filter(pre_tdr_use=True)
    if filters['early_science_use']:
        qs = qs.filter(early_science_use=True)
    if filters['other_use']:
        qs = qs.filter(other_use=True)
    return qs


def _catalog_view_url(request, active_lifecycle, view_mode):
    q = request.GET.copy()
    q['lifecycle'] = active_lifecycle
    if view_mode == 'progress':
        q['view'] = 'progress'
        q['refresh'] = '1'
    else:
        q.pop('view', None)
        q.pop('refresh', None)
    encoded = q.urlencode()
    return '?' + encoded if encoded else '?'


def _annotate_task_progress(tasks, snapshot):
    rows = (snapshot or {}).get('rows') or {}
    empty = {'outputs': [], 'configured_jobs': None, 'has_processing': False}
    empty_processing = {
        'jeditaskid': '', 'status': '', 'total_jobs': '', 'nfailed': '',
        'nactive': '', 'nfinished': '', 'nfinalfailed': '',
        'processing_percent': None, 'final_failure_rate': None,
    }
    empty_output = {
        'completion_percent': None, 'expected_jobs': '', 'link': '',
        'processing': empty_processing,
    }
    for task in tasks:
        task.progress = rows.get(str(task.pk), empty)
        outputs = task.progress.get('outputs') or []
        if outputs:
            first = dict(empty_output)
            first.update(outputs[0])
            processing = dict(empty_processing)
            processing.update(first.get('processing') or {})
            first['processing'] = processing
            task.progress_first = first
        else:
            task.progress_first = empty_output
        linked = []
        completion_values = []
        job_values = []
        for output in outputs:
            if output.get('completion_percent') is not None:
                completion_values.append(output.get('completion_percent'))
            processing = output.get('processing') or {}
            total_jobs = processing.get('total_jobs') or output.get('expected_jobs')
            if total_jobs not in (None, ''):
                try:
                    job_values.append(int(total_jobs))
                except (TypeError, ValueError):
                    pass
            if processing.get('jeditaskid'):
                linked.append(output)
        failure_values = []
        for output in linked:
            try:
                failure_values.append(int((output.get('processing') or {}).get('nfailed') or 0))
            except (TypeError, ValueError):
                failure_values.append(0)
        task.progress_sort = {
            'completion': max(completion_values) if completion_values else -1,
            'jobs': max(job_values) if job_values else '',
            'processing': (
                '1:' + str((linked[0].get('processing') or {}).get('status') or '')
                if linked else '0:'
            ),
            'failures': (
                f'1:{max(failure_values):09d}' if failure_values else '0:'
            ),
            'link': '1:' + str(linked[0].get('link') or '') if linked else '0:',
        }
    return tasks


def _catalog_cache_dt(value):
    return value.isoformat() if value else ''


def _catalog_task_list_cache_signature(campaign, catalog_view, progress_snapshot):
    task_meta = ProdTask.objects.filter(campaign=campaign).aggregate(
        count=Count('id'), updated=Max('updated_at'))
    return {
        'version': CATALOG_TASK_LIST_CACHE_VERSION,
        'view': catalog_view,
        'campaign_id': campaign.pk,
        'campaign_name': campaign.name,
        'task_count': task_meta['count'] or 0,
        'task_updated_at': _catalog_cache_dt(task_meta['updated']),
        'progress_generated_at': (
            (progress_snapshot or {}).get('generated_at') or ''
            if catalog_view == 'progress' else ''
        ),
    }


def _catalog_table_cache_key(campaign_id, catalog_view, signature):
    payload = json.dumps(signature, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return f'pcs:catalog-table:{campaign_id}:{catalog_view}:{digest}'


def _catalog_table_latest_key(campaign_id, catalog_view):
    return f'pcs:catalog-table:latest:{campaign_id}:{catalog_view}'


def _campaign_data(campaign):
    if campaign is None:
        return {}
    return (
        Campaign.objects
        .filter(pk=campaign.pk)
        .values_list('data', flat=True)
        .first()
    ) or {}


def _current_catalog_tasks(campaign, catalog_view, progress_snapshot, timings=None):
    def load_tasks():
        return list(
            ProdTask.objects.select_related(
                'campaign', 'dataset', 'prod_config', 'request',
                'dataset__physics_tag', 'dataset__evgen_tag', 'dataset__simu_tag',
                'dataset__reco_tag', 'dataset__background_tag',
            ).filter(campaign=campaign).order_by('-updated_at')
        )
    tasks = _timed(
        timings,
        'task query',
        load_tasks,
        detail_fn=lambda rows: f'{len(rows)} rows',
    )
    tasks = _timed(
        timings,
        'questionnaire match cache hydrate',
        lambda: _annotate_task_questionnaire_matches(tasks),
        detail_fn=lambda rows: f'{len(rows)} task-local cached match lists',
    )
    if catalog_view == 'progress':
        tasks = _timed(
            timings,
            'progress row annotation',
            lambda: _annotate_task_progress(tasks, progress_snapshot),
            detail_fn=lambda rows: f'{len(rows)} rows',
        )
    return tasks


def _cached_current_task_list_html(campaign, catalog_view, context, progress_snapshot, timings=None):
    if campaign is None or catalog_view not in ('catalog', 'progress'):
        return None, False, {}
    signature = _timed(
        timings,
        'table cache signature',
        lambda: _catalog_task_list_cache_signature(campaign, catalog_view, progress_snapshot),
    )
    cache_key = _catalog_table_cache_key(campaign.pk, catalog_view, signature)
    latest_key = _catalog_table_latest_key(campaign.pk, catalog_view)
    cached = _timed(
        timings,
        'table cache lookup',
        lambda: cache.get(cache_key),
        detail_fn=lambda value: (
            f'cache hit, {len(value.get("html", ""))} html bytes'
            if value and value.get('html') else 'miss'
        ),
    )
    if cached and cached.get('html'):
        cache.set(latest_key, cache_key, None)
        _timing_note(
            timings,
            'table render',
            detail='Django cache hit',
        )
        return cached['html'], True, cached

    latest_cache_key = cache.get(latest_key)
    if latest_cache_key and latest_cache_key != cache_key:
        stale = _timed(
            timings,
            'table stale cache lookup',
            lambda: cache.get(latest_cache_key),
            detail_fn=lambda value: (
                f'stale cache hit, {len(value.get("html", ""))} html bytes'
                if value and value.get('html') else 'miss'
            ),
        )
        if stale and stale.get('html'):
            _timing_note(
                timings,
                'table render',
                detail='stale Django cache used; page-load rebuild suppressed',
            )
            return stale['html'], True, {**stale, 'stale': True}

    tasks = _timed(
        timings,
        'table cache miss task query',
        lambda: _current_catalog_tasks(campaign, catalog_view, progress_snapshot),
        detail_fn=lambda rows: f'{len(rows)} rows',
    )
    html = _timed(
        timings,
        'table cache miss render',
        lambda: render_to_string(
            'pcs/_task_list_filter.html',
            {
                'tasks': tasks,
                'catalog_view': catalog_view,
                'columns_mode': 'full',
                'status_choices': PRODTASK_STATUS_CHOICES,
            },
        ),
        detail_fn=lambda value: f'{len(value)} html bytes',
    )
    entry = {
        'signature': signature,
        'html': html,
        'rendered_at': timezone.now().isoformat(),
    }
    cache.set(cache_key, entry, None)
    cache.set(latest_key, cache_key, None)
    _timing_note(
        timings,
        'table render',
        detail='cache miss rebuilt and cached',
    )
    return html, False, entry


def rebuild_current_task_list_html_cache(campaign, catalog_view='catalog', progress_snapshot=None):
    """Rebuild the current-campaign table fragment outside the page GET path."""
    if campaign is None or catalog_view not in ('catalog', 'progress'):
        raise ValueError('campaign and catalog/progress view are required')
    signature = _catalog_task_list_cache_signature(campaign, catalog_view, progress_snapshot)
    tasks = _current_catalog_tasks(campaign, catalog_view, progress_snapshot)
    html = render_to_string(
        'pcs/_task_list_filter.html',
        {
            'tasks': tasks,
            'catalog_view': catalog_view,
            'columns_mode': 'full',
            'status_choices': PRODTASK_STATUS_CHOICES,
        },
    )
    entry = {
        'signature': signature,
        'html': html,
        'rendered_at': timezone.now().isoformat(),
    }
    cache_key = _catalog_table_cache_key(campaign.pk, catalog_view, signature)
    cache.set(cache_key, entry, None)
    cache.set(_catalog_table_latest_key(campaign.pk, catalog_view), cache_key, None)
    return {
        'campaign': campaign.name,
        'view': catalog_view,
        'tasks': len(tasks),
        'html_bytes': len(html),
        'rendered_at': entry['rendered_at'],
    }


from .schemas import TAG_SCHEMAS, get_tag_model, get_param_defs, save_param_defs
from .forms import PhysicsTagForm, SimpleTagForm, DatasetForm, PhysicsCategoryForm, ProdConfigForm


def pcs_hub_counts():
    """PCS entity counts — shared by PCS hub and production hub."""
    return {
        'categories_count': PhysicsCategory.objects.count(),
        'physics_tags_count': PhysicsTag.objects.count(),
        'evgen_tags_count': EvgenTag.objects.count(),
        'simu_tags_count': SimuTag.objects.count(),
        'reco_tags_count': RecoTag.objects.count(),
        'background_tags_count': BackgroundTag.objects.count(),
        'datasets_count': Dataset.objects.values('dataset_name').distinct().count(),
        'questionnaires_count': Questionnaire.objects.count(),
        'prod_configs_count': ProdConfig.objects.count(),
        'prod_tasks_count': ProdTask.objects.count(),
    }


def pcs_hub(request):
    return render(request, 'pcs/pcs_hub.html', pcs_hub_counts())


# ── Questionnaire intake ───────────────────────────────────────────

def _questionnaire_contact_display(questionnaire, *, authenticated):
    if not authenticated:
        return _redact_contact(questionnaire.contact)

    contacts = (questionnaire.data or {}).get('contacts') or []
    parts = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        name = (contact.get('name') or '').strip()
        emails = [
            str(email).strip()
            for email in (contact.get('emails') or [])
            if str(email).strip()
        ]
        if name and emails:
            parts.append(f"{name} ({', '.join(emails)})")
        elif name:
            parts.append(name)
        elif emails:
            parts.append(', '.join(emails))
    return ', '.join(parts) or questionnaire.contact


def _questionnaire_contacts(questionnaire):
    return [
        contact for contact in ((questionnaire.data or {}).get('contacts') or [])
        if isinstance(contact, dict)
    ]


def _questionnaire_contact_names(questionnaire):
    names = []
    seen = set()
    for contact in _questionnaire_contacts(questionnaire):
        name = (contact.get('name') or '').strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _questionnaire_has_email(questionnaire):
    return any(contact.get('emails') for contact in _questionnaire_contacts(questionnaire))


def _questionnaire_data_label(questionnaire, key):
    value = (questionnaire.data or {}).get(key) or {}
    return (value.get('label') or '').strip() if isinstance(value, dict) else ''


def _questionnaire_prod_matches(questionnaire, *, status=None):
    matches = []
    for match in (questionnaire.data or {}).get('prod_matches') or []:
        if not isinstance(match, dict):
            continue
        if status and (match.get('status') or 'accepted') != status:
            continue
        match = dict(match)
        matched_at = match.get('matched_at')
        if matched_at:
            dt = parse_datetime(str(matched_at))
            match['matched_at_display'] = format_datetime(dt) if dt else matched_at
        else:
            match['matched_at_display'] = ''
        matches.append(match)
    return matches


def _task_display_name(task):
    return task.composed_name or task.name


def _resolve_questionnaire_match_task(match):
    from .services import resolve_prodtask
    qs = ProdTask.objects.select_related('campaign', 'dataset', 'prod_config')
    for key in (match.get('task_name'), match.get('legacy_name'), match.get('task_id')):
        if not key:
            continue
        try:
            return resolve_prodtask(str(key), qs)
        except ProdTask.DoesNotExist:
            continue
    return None


def _annotate_questionnaire_matches(questionnaires):
    for questionnaire in questionnaires:
        matches = _questionnaire_prod_matches(questionnaire, status='accepted')
        questionnaire.prod_match_count = len(matches)
        questionnaire.prod_matches = matches


def _annotate_task_questionnaire_matches(tasks):
    tasks = list(tasks)
    qids = set()
    for task in tasks:
        task.questionnaire_matches = []
        for match in (task.overrides or {}).get('questionnaire_matches') or []:
            if not isinstance(match, dict):
                continue
            qid = match.get('questionnaire_id')
            if isinstance(qid, int) or str(qid).isdigit():
                qids.add(int(qid))
    questionnaires = {
        q.pk: q for q in Questionnaire.objects.filter(pk__in=qids)
    } if qids else {}
    for task in tasks:
        for match in (task.overrides or {}).get('questionnaire_matches') or []:
            if not isinstance(match, dict):
                continue
            qid = match.get('questionnaire_id')
            questionnaire = questionnaires.get(int(qid)) if str(qid).isdigit() else None
            if questionnaire is None:
                continue
            task.questionnaire_matches.append({
                'questionnaire': questionnaire,
                'confidence': match.get('confidence') or '',
                'reason': match.get('reason') or '',
            })
    return tasks


def questionnaires_list(request):
    rows = list(Questionnaire.objects.all())
    authenticated = request.user.is_authenticated
    for row in rows:
        row.contact_display = _questionnaire_contact_display(
            row, authenticated=authenticated)
        row.repository_display = _questionnaire_data_label(
            row, 'repository_curated')
        row.generator_display = _questionnaire_data_label(row, 'generator')
        row.generator_filter = row.generator_display or '__undefined__'
        row.has_contact = bool(_questionnaire_contacts(row))
        row.has_email = _questionnaire_has_email(row)
        row.contact_filter = '||'.join(_questionnaire_contact_names(row))
        row.search_text = ' '.join([
            row.description or '',
            row.repository or '',
            row.repository_display or '',
            row.generator_display or '',
            row.contact_display or '',
            row.nevents or '',
            row.benchmark or '',
            row.estimate or '',
        ]).lower()
    _annotate_questionnaire_matches(rows)
    return render(request, 'pcs/questionnaires_list.html', {
        'questionnaires': rows,
        'total_count': len(rows),
    })


def questionnaire_detail(request, pk):
    questionnaire = get_object_or_404(Questionnaire, pk=pk)
    authenticated = request.user.is_authenticated
    questionnaire.contact_display = _questionnaire_contact_display(
        questionnaire, authenticated=authenticated)
    questionnaire.repository_display = _questionnaire_data_label(
        questionnaire, 'repository_curated')
    questionnaire.generator_display = _questionnaire_data_label(
        questionnaire, 'generator')
    matches = []
    for match in _questionnaire_prod_matches(questionnaire):
        resolved = _resolve_questionnaire_match_task(match)
        matches.append({'match': match, 'task': resolved})
    return render(request, 'pcs/questionnaire_detail.html', {
        'questionnaire': questionnaire,
        'matches': matches,
        'confidence_choices': ('high', 'medium', 'low'),
    })


@_login_required_flash
def questionnaire_match_add(request, pk):
    questionnaire = get_object_or_404(Questionnaire, pk=pk)
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:questionnaire_detail', kwargs={'pk': pk}),
            action_label='Questionnaire match')
    task_key = (request.POST.get('task') or '').strip()
    confidence = (request.POST.get('confidence') or '').strip()
    reason = (request.POST.get('reason') or '').strip()
    if confidence not in {'high', 'medium', 'low'}:
        confidence = 'medium'
    if not task_key:
        messages.error(request, 'Provide a production task name.')
        return redirect('pcs:questionnaire_detail', pk=pk)
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(
            task_key, ProdTask.objects.select_related('dataset', 'campaign'))
    except ProdTask.DoesNotExist:
        messages.error(request, f'No production task matches {task_key!r}.')
        return redirect('pcs:questionnaire_detail', pk=pk)

    data = dict(questionnaire.data or {})
    matches = [
        match for match in (data.get('prod_matches') or [])
        if isinstance(match, dict) and match.get('task_id') != task.pk
    ]
    matches.append({
        'task_id': task.pk,
        'task_name': _task_display_name(task),
        'legacy_name': task.name,
        'confidence': confidence,
        'status': 'accepted',
        'reason': reason,
        'matched_by': getattr(request.user, 'username', '') or 'web',
        'matched_at': timezone.now().isoformat(),
    })
    data['prod_matches'] = matches
    questionnaire.data = data
    questionnaire.save(update_fields=['data', 'updated_at'])
    messages.success(request, f'Matched request #{pk} to {_task_display_name(task)}.')
    return redirect('pcs:questionnaire_detail', pk=pk)


@_login_required_flash
def questionnaire_match_remove(request, pk, task_id):
    questionnaire = get_object_or_404(Questionnaire, pk=pk)
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:questionnaire_detail', kwargs={'pk': pk}),
            action_label='Questionnaire match removal')
    data = dict(questionnaire.data or {})
    before = len(data.get('prod_matches') or [])
    data['prod_matches'] = [
        match for match in (data.get('prod_matches') or [])
        if not (isinstance(match, dict) and str(match.get('task_id')) == str(task_id))
    ]
    questionnaire.data = data
    questionnaire.save(update_fields=['data', 'updated_at'])
    removed = before - len(data['prod_matches'])
    if removed:
        messages.success(request, f'Removed {removed} production match.')
    else:
        messages.warning(request, 'No matching production task link was present.')
    return redirect('pcs:questionnaire_detail', pk=pk)


@_login_required_flash
def questionnaire_import(request):
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:questionnaires_list'),
            action_label='Questionnaire import')
    from .services import questionnaire_intake_csv, ServiceError
    csv_url = (request.POST.get('csv_url') or '').strip()
    if not csv_url:
        messages.error(request, 'Provide a questionnaire CSV import URL.')
        return redirect(reverse('pcs:questionnaires_list'))
    try:
        with urlopen(csv_url, timeout=30) as response:
            csv_text = response.read().decode('utf-8-sig')
    except Exception as e:
        messages.error(request, f'Questionnaire CSV fetch failed: {e}')
        return redirect(reverse('pcs:questionnaires_list'))
    if not csv_text.strip():
        messages.error(request, 'Questionnaire CSV import URL returned no CSV text.')
        return redirect(reverse('pcs:questionnaires_list'))
    try:
        summary = questionnaire_intake_csv(
            csv_text,
            source_url=csv_url,
            created_by=getattr(request.user, 'username', '') or 'questionnaire_import',
        )
    except ServiceError as e:
        messages.error(request, f'Questionnaire import failed: {e.detail}')
        return redirect(reverse('pcs:questionnaires_list'))
    messages.success(
        request,
        f'Questionnaire import: {summary["created"]} new, '
        f'{summary["updated"]} updated, {summary["unchanged"]} unchanged.'
    )
    return redirect(reverse('pcs:questionnaires_list'))


# ── Physics Categories ────────────────────────────────────────────

def physics_categories_list(request):
    categories = PhysicsCategory.objects.annotate(tag_count=Count('tags')).order_by('digit')
    return render(request, 'pcs/physics_categories_list.html', {'categories': categories})


@_login_required_flash
def physics_category_create(request):
    if request.method == 'POST':
        form = PhysicsCategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Category {form.instance.digit}: {form.instance.name} created.")
            return redirect('pcs:physics_categories_list')
    else:
        form = PhysicsCategoryForm()
    return render(request, 'pcs/physics_category_create.html', {'form': form})


# ── Tag list/detail/create (generic across p/e/s/r) ──────────────

TAG_MODELS = {
    'p': PhysicsTag,
    'e': EvgenTag,
    's': SimuTag,
    'r': RecoTag,
    'k': BackgroundTag,
}


def tags_list(request, tag_type):
    schema = TAG_SCHEMAS[tag_type]
    model = TAG_MODELS[tag_type]

    status_filter = request.GET.get('status', '')
    category_filter = request.GET.get('category', '')

    columns = [
        {'name': 'tag_label', 'title': 'Tag', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
        {'name': 'actions', 'title': '', 'orderable': False},
    ]
    if tag_type == 'p':
        columns.insert(1, {'name': 'category__name', 'title': 'Category', 'orderable': True})

    statuses = ['draft', 'locked']
    categories = list(PhysicsCategory.objects.values_list('name', flat=True)) if tag_type == 'p' else []

    context = {
        'table_title': f'{schema["label"]} Tags',
        'table_description': f'All {schema["label"].lower()} tags registered in PCS.',
        'ajax_url': reverse('pcs:tags_datatable_ajax', args=[tag_type]),
        'columns': columns,
        'tag_type': tag_type,
        'schema': schema,
        'statuses': statuses,
        'categories': categories,
        'selected_status': status_filter,
        'selected_category': category_filter,
    }
    return render(request, 'pcs/tag_list.html', context)


def tags_datatable_ajax(request, tag_type):
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        col_names = ['tag_label', 'category__name', 'description', 'status', 'created_by', 'created_at', 'actions']
    else:
        col_names = ['tag_label', 'description', 'status', 'created_by', 'created_at', 'actions']

    dt = DataTablesProcessor(request, col_names, default_order_column=0, default_order_direction='desc')

    qs = model.objects.all()
    if tag_type == 'p':
        qs = qs.select_related('category')

    filters = get_filter_params(request, ['status', 'category'])
    if filters['status']:
        qs = qs.filter(status=filters['status'])
    if tag_type == 'p' and filters.get('category'):
        qs = qs.filter(category__name=filters['category'])

    records_total = model.objects.count()
    search_fields = ['tag_label', 'description', 'created_by']
    if tag_type == 'p':
        search_fields.append('category__name')
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()

    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for tag in page:
        compose_url = reverse('pcs:tag_compose', args=[tag_type])
        tag_url = f'{compose_url}?selected={urlquote(tag.tag_label)}'
        tag_link = f'<a href="{tag_url}">{tag.tag_label}</a>'
        status_badge = (
            f'<span class="badge bg-secondary">{tag.status}</span>'
            if tag.status == 'draft'
            else f'<span class="badge bg-success">{tag.status}</span>'
        )
        row = [tag_link]
        if tag_type == 'p':
            row.append(tag.category.name)
        row += [
            tag.description[:80] + ('...' if len(tag.description) > 80 else ''),
            status_badge,
            tag.created_by,
            format_datetime(tag.created_at),
            f'<a href="{tag_url}">View</a>',
        ]
        data.append(row)

    return dt.create_response(data, records_total, records_filtered)


def tag_detail(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = TAG_SCHEMAS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)

    datasets = []
    if tag.status == 'locked':
        field_map = {'p': 'physics_tag', 'e': 'evgen_tag', 's': 'simu_tag', 'r': 'reco_tag', 'k': 'background_tag'}
        datasets = Dataset.objects.filter(**{field_map[tag_type]: tag}).order_by('-created_at')

    defs = get_param_defs(tag_type)
    context = {
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'datasets': datasets,
        'required_fields': [d['name'] for d in defs if d.get('required')],
        'optional_fields': [d['name'] for d in defs if not d.get('required')],
    }
    return render(request, 'pcs/tag_detail.html', context)


@_login_required_flash
def tag_create(request, tag_type):
    schema = TAG_SCHEMAS[tag_type]

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            model = TAG_MODELS[tag_type]
            params = form.get_parameters()

            if tag_type == 'p':
                category = form.cleaned_data['category']
                tag_number = PhysicsTag.allocate_next(category)
                tag = PhysicsTag(
                    tag_number=tag_number,
                    category=category,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            else:
                tag_number = model.allocate_next()
                tag = model(
                    tag_number=tag_number,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} created.")
            compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
            return redirect(f'{compose_url}?selected={urlquote(tag.tag_label)}')
    else:
        form = FormClass(**form_kwargs)

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
    }
    template = 'pcs/tag_create_physics.html' if tag_type == 'p' else 'pcs/tag_create.html'
    return render(request, template, context)


def tag_compose(request, tag_type):
    """Split-panel browse + compose UI for physics tags."""
    schema = TAG_SCHEMAS[tag_type]
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    selected_tag = None
    if request.method == 'POST':
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            params = form.get_parameters()
            if tag_type == 'p':
                category = form.cleaned_data['category']
                tag_number = PhysicsTag.allocate_next(category)
                tag = PhysicsTag(
                    tag_number=tag_number,
                    category=category,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            else:
                tag_number = model.allocate_next()
                tag = model(
                    tag_number=tag_number,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} created.")
            compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
            return redirect(f'{compose_url}?selected={urlquote(tag.tag_label)}')
    else:
        form = FormClass(**form_kwargs)
        selected_tag = request.GET.get('selected')

    qs = model.objects.order_by('-tag_number')
    if tag_type == 'p':
        qs = qs.select_related('category')
    tags_data = []
    for t in qs:
        entry = {
            'tag_number': t.tag_number,
            'tag_label': t.tag_label,
            'status': t.status,
            'description': t.description,
            'parameters': t.parameters,
            'created_by': t.created_by,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': t.updated_at.strftime('%Y-%m-%d %H:%M'),
        }
        if tag_type == 'p':
            entry['category_digit'] = t.category.digit
            entry['category_name'] = t.category.name
        tags_data.append(entry)

    param_defs = get_param_defs(tag_type)
    choices_from_defs = {d['name']: d['choices'] for d in param_defs if d.get('choices')}
    filter_fields = [d['name'] for d in param_defs
                     if d['name'] not in ('notes', 'description')]

    # Peek at next tag suffix from PersistentState (read-only, no increment)
    from monitor_app.models import PersistentState
    state_keys = {'p': 'pcs_next_physics', 'e': 'pcs_next_evgen',
                  's': 'pcs_next_simu', 'r': 'pcs_next_reco',
                  'k': 'pcs_next_background'}
    try:
        ps = PersistentState.objects.get(id=1)
        next_suffix = ps.state_data.get(state_keys[tag_type], 1)
    except PersistentState.DoesNotExist:
        next_suffix = 1

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
        'tags_json': json.dumps(tags_data, default=str),
        'choices_json': json.dumps(choices_from_defs),
        'filter_fields_json': json.dumps(filter_fields),
        'param_defs_json': json.dumps(param_defs),
        'next_suffix': next_suffix,
        'username': request.user.username if request.user.is_authenticated else '',
        'selected_tag_json': json.dumps(selected_tag),
    }
    return render(request, 'pcs/tag_compose.html', context)


def tag_datasets(request, tag_type, tag_number):
    """On-demand 'used by' for a tag: the datasets composed with it, each with a
    representative task so the tag detail can link into the compose page (the
    task anchors the campaign). GET JSON, read-only."""
    if tag_type not in TAG_SCHEMAS:
        return JsonResponse({'error': 'Invalid tag type'}, status=400)
    model = get_tag_model(tag_type)
    tag = get_object_or_404(model, tag_number=tag_number)
    datasets = tag.datasets.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
    ).prefetch_related('prod_tasks').order_by('-created_at')
    out = []
    for ds in datasets:
        tasks = list(ds.prod_tasks.all())
        # A live (non-archive) task is the better link target; fall back to any.
        rep = [t for t in tasks if t.status != 'past_output'] or tasks
        out.append({
            'composed_name': ds.build_dataset_name(),
            'dataset_id': ds.id,
            'task_name': rep[0].name if rep else '',
            'task_count': len(tasks),
        })
    return JsonResponse({'datasets': out})


def param_defs_api(request, tag_type):
    if tag_type not in TAG_SCHEMAS:
        return JsonResponse({'error': 'Invalid tag type'}, status=400)
    if request.method == 'GET':
        return JsonResponse({'defs': get_param_defs(tag_type)})
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Login required'}, status=403)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        defs = data.get('defs')
        if not isinstance(defs, list):
            return JsonResponse({'error': 'defs must be a list'}, status=400)
        names_seen = set()
        for i, d in enumerate(defs):
            if not isinstance(d, dict) or not d.get('name'):
                return JsonResponse({'error': f'Invalid param def at index {i}'}, status=400)
            name = d['name'].strip()
            if name in names_seen:
                return JsonResponse({'error': f'Duplicate param name: {name}'}, status=400)
            names_seen.add(name)
            d['name'] = name
            d.setdefault('type', 'string')
            d.setdefault('required', False)
            d.setdefault('choices', [])
            d.setdefault('allow_other', True)
            d['sort_order'] = i
        save_param_defs(tag_type, defs)
        return JsonResponse({'ok': True, 'defs': defs})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@_login_required_flash
def tag_delete(request, tag_type, tag_number):
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:tag_compose', kwargs={'tag_type': tag_type}),
            action_label='Tag delete')
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be deleted.")
        return redirect('pcs:tag_compose', tag_type=tag_type)
    if tag.created_by != request.user.username:
        messages.error(request, f"Only the creator ({tag.created_by}) can delete {tag.tag_label}.")
        return redirect('pcs:tag_compose', tag_type=tag_type)
    label = tag.tag_label
    tag.delete()
    messages.success(request, f"Tag {label} deleted.")
    return redirect('pcs:tag_compose', tag_type=tag_type)


@_login_required_flash
def tag_lock(request, tag_type, tag_number):
    compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
    selected_url = f'{compose_url}?selected={tag_number}'
    if request.method != 'POST':
        return _post_only_redirect(request, selected_url, action_label='Tag lock')
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.created_by != request.user.username:
        messages.error(request, f"Only the creator ({tag.created_by}) can lock this tag.")
    elif tag.status == 'locked':
        messages.warning(request, f"Tag {tag.tag_label} is already locked.")
    else:
        tag.status = 'locked'
        tag.save(update_fields=['status', 'updated_at'])
        messages.success(request, f"Tag {tag.tag_label} locked. It can now be used in datasets.")
    return redirect(selected_url)


@_login_required_flash
def tag_edit(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = TAG_SCHEMAS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)

    compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
    selected_url = f'{compose_url}?selected={tag_number}'
    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be edited.")
        return redirect(selected_url)

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            tag.description = form.cleaned_data['description']
            tag.parameters = form.get_parameters()
            if tag_type == 'p':
                tag.category = form.cleaned_data['category']
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} updated.")
            return redirect(selected_url)
    else:
        initial = {
            'description': tag.description,
            'created_by': tag.created_by,
        }
        if tag_type == 'p':
            initial['category'] = tag.category
        for k, v in tag.parameters.items():
            initial[f'param_{k}'] = v
        form = FormClass(initial=initial, **form_kwargs)

    context = {
        'form': form,
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'editing': True,
    }
    template = 'pcs/tag_create_physics.html' if tag_type == 'p' else 'pcs/tag_create.html'
    return render(request, template, context)


# ── Datasets ──────────────────────────────────────────────────────

def datasets_compose(request):
    """Two-pane browse/create UI for datasets."""
    if request.method == 'POST' and request.user.is_authenticated:
        form = DatasetForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            ds = Dataset(
                scope=cd['scope'],
                detector_version=cd['detector_version'],
                detector_config=cd['detector_config'],
                physics_tag=cd['physics_tag'],
                evgen_tag=cd['evgen_tag'],
                simu_tag=cd['simu_tag'],
                reco_tag=cd['reco_tag'],
                background_tag=cd.get('background_tag'),
                description=cd.get('description', ''),
                metadata=cd.get('metadata') or None,
                created_by=cd['created_by'],
            )
            ds.save()
            messages.success(request, f"Dataset created: {ds.did}")
            return redirect(f"{reverse('pcs:datasets_compose')}?selected={urlquote(ds.dataset_name)}")

    qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
    ).order_by('-created_at')
    datasets_data = []
    for ds in qs:
        datasets_data.append({
            'id': ds.id,
            'dataset_name': ds.dataset_name,
            'composed_name': ds.build_dataset_name(),
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'description': ds.description,
            'blocks': ds.blocks,
            'created_by': ds.created_by,
            'created_at': ds.created_at.strftime('%Y-%m-%d %H:%M'),
            'physics_tag': {'id': ds.physics_tag_id, 'label': ds.physics_tag.tag_label,
                            'description': ds.physics_tag.description, 'parameters': ds.physics_tag.parameters},
            'evgen_tag': {'id': ds.evgen_tag_id, 'label': ds.evgen_tag.tag_label,
                          'description': ds.evgen_tag.description, 'parameters': ds.evgen_tag.parameters},
            'simu_tag': {'id': ds.simu_tag_id, 'label': ds.simu_tag.tag_label,
                         'description': ds.simu_tag.description, 'parameters': ds.simu_tag.parameters},
            'reco_tag': {'id': ds.reco_tag_id, 'label': ds.reco_tag.tag_label,
                         'description': ds.reco_tag.description, 'parameters': ds.reco_tag.parameters},
            'background_tag': ({'id': ds.background_tag_id, 'label': ds.background_tag.tag_label,
                                'description': ds.background_tag.description, 'parameters': ds.background_tag.parameters}
                               if ds.background_tag_id else None),
        })

    # Full tag data for browsing and diffs
    tags_data = {}
    for ttype, model in TAG_MODELS_MAP.items():
        tag_list = []
        qs_tags = model.objects.order_by('tag_number')
        if ttype == 'p':
            qs_tags = qs_tags.select_related('category')
        for t in qs_tags:
            entry = {'id': t.id, 'tag_number': t.tag_number, 'label': t.tag_label,
                     'description': t.description, 'status': t.status,
                     'parameters': t.parameters, 'created_by': t.created_by,
                     'updated_at': t.updated_at.strftime('%Y-%m-%d %H:%M')}
            if ttype == 'p':
                entry['category_name'] = t.category.name
            tag_list.append(entry)
        tags_data[ttype] = tag_list

    context = {
        'datasets_json': json.dumps(datasets_data),
        'tags_json': json.dumps(tags_data),
        'selected_item_json': json.dumps(request.GET.get('selected') or None),
        'username': request.user.username if request.user.is_authenticated else '',
    }
    return render(request, 'pcs/dataset_compose.html', context)


def datasets_list(request):
    columns = [
        {'name': 'dataset_name', 'title': 'Dataset Name', 'orderable': True},
        {'name': 'physics_tag__tag_label', 'title': 'Physics', 'orderable': True},
        {'name': 'evgen_tag__tag_label', 'title': 'EvGen', 'orderable': True},
        {'name': 'simu_tag__tag_label', 'title': 'Simu', 'orderable': True},
        {'name': 'reco_tag__tag_label', 'title': 'Reco', 'orderable': True},
        {'name': 'background_tag__tag_label', 'title': 'Background', 'orderable': True},
        {'name': 'blocks', 'title': 'Blocks', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
    ]
    context = {
        'table_title': 'Datasets',
        'table_description': 'All datasets registered in PCS.',
        'ajax_url': reverse('pcs:datasets_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/datasets_list.html', context)


def datasets_datatable_ajax(request):
    col_names = [
        'dataset_name', 'physics_tag__tag_label', 'evgen_tag__tag_label',
        'simu_tag__tag_label', 'reco_tag__tag_label', 'background_tag__tag_label',
        'blocks', 'created_at',
    ]
    dt = DataTablesProcessor(request, col_names, default_order_column=7, default_order_direction='desc')

    # Only show block 1 rows (one row per logical dataset)
    qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    )

    records_total = Dataset.objects.filter(block_num=1).count()
    search_fields = ['dataset_name', 'physics_tag__tag_label', 'evgen_tag__tag_label',
                     'simu_tag__tag_label', 'reco_tag__tag_label', 'background_tag__tag_label']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for ds in page:
        detail_url = reverse('pcs:dataset_detail', args=[ds.id])
        p_url = f"{reverse('pcs:tag_compose', args=['p'])}?selected={ds.physics_tag.tag_number}"
        e_url = f"{reverse('pcs:tag_compose', args=['e'])}?selected={ds.evgen_tag.tag_number}"
        s_url = f"{reverse('pcs:tag_compose', args=['s'])}?selected={ds.simu_tag.tag_number}"
        r_url = f"{reverse('pcs:tag_compose', args=['r'])}?selected={ds.reco_tag.tag_number}"
        # Name: the tag-composed name (build_dataset_name); the internal
        # csv_import.<hash> dataset_name is plumbing and is never shown.
        if ds.background_tag_id:
            k_url = f"{reverse('pcs:tag_compose', args=['k'])}?selected={ds.background_tag.tag_number}"
            k_cell = f'<a href="{k_url}" title="{ds.background_tag.description}">{ds.background_tag.tag_label}</a>'
        else:
            k_cell = '-'
        data.append([
            f'<a href="{detail_url}">{ds.composed_name}</a>',
            f'<a href="{p_url}" title="{ds.physics_tag.description}">{ds.physics_tag.tag_label}</a>',
            f'<a href="{e_url}" title="{ds.evgen_tag.description}">{ds.evgen_tag.tag_label}</a>',
            f'<a href="{s_url}" title="{ds.simu_tag.description}">{ds.simu_tag.tag_label}</a>',
            f'<a href="{r_url}" title="{ds.reco_tag.description}">{ds.reco_tag.tag_label}</a>',
            k_cell,
            str(ds.blocks),
            format_datetime(ds.created_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def dataset_detail(request, pk):
    dataset = get_object_or_404(
        Dataset.objects.select_related('physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag'),
        pk=pk,
    )
    blocks = Dataset.objects.filter(dataset_name=dataset.dataset_name).order_by('block_num')

    # Reverse references — tasks that use this dataset and in what role.
    # Output: legacy FK or override list contains DID. Input: legacy single
    # override or list contains DID. Intermediate: list only.
    did = dataset.did
    output_tasks = (ProdTask.objects
                    .filter(Q(dataset=dataset)
                            | Q(overrides__output_dataset_dids__contains=[did]))
                    .distinct().order_by('name'))
    input_tasks = (ProdTask.objects
                   .filter(Q(overrides__input_dataset_did=did)
                           | Q(overrides__input_dataset_dids__contains=[did]))
                   .distinct().order_by('name'))
    intermediate_tasks = (ProdTask.objects
                          .filter(overrides__intermediate_dataset_dids__contains=[did])
                          .order_by('name'))

    context = {
        'dataset': dataset,
        'blocks': blocks,
        'output_tasks': output_tasks,
        'input_tasks': input_tasks,
        'intermediate_tasks': intermediate_tasks,
    }
    return render(request, 'pcs/dataset_detail.html', context)


@_login_required_flash
def dataset_create(request):
    if request.method == 'POST':
        form = DatasetForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            ds = Dataset(
                scope=cd['scope'],
                detector_version=cd['detector_version'],
                detector_config=cd['detector_config'],
                physics_tag=cd['physics_tag'],
                evgen_tag=cd['evgen_tag'],
                simu_tag=cd['simu_tag'],
                reco_tag=cd['reco_tag'],
                background_tag=cd.get('background_tag'),
                description=cd.get('description', ''),
                metadata=cd.get('metadata') or None,
                created_by=cd['created_by'],
            )
            ds.save()
            messages.success(request, f"Dataset created: {ds.did}")
            return redirect('pcs:dataset_detail', pk=ds.pk)
    else:
        form = DatasetForm()
    return render(request, 'pcs/dataset_create.html', {'form': form})


@_login_required_flash
def dataset_add_block(request, pk):
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:dataset_detail', kwargs={'pk': pk}),
            action_label='Add-block')
    dataset = get_object_or_404(Dataset, pk=pk)
    new_block_num = dataset.blocks + 1
    Dataset.objects.filter(dataset_name=dataset.dataset_name).update(blocks=new_block_num)
    new_block = Dataset.objects.create(
        dataset_name=dataset.dataset_name,
        scope=dataset.scope,
        detector_version=dataset.detector_version,
        detector_config=dataset.detector_config,
        physics_tag=dataset.physics_tag,
        evgen_tag=dataset.evgen_tag,
        simu_tag=dataset.simu_tag,
        reco_tag=dataset.reco_tag,
        background_tag=dataset.background_tag,
        block_num=new_block_num,
        blocks=new_block_num,
        did=f"{dataset.scope}:{dataset.dataset_name}.b{new_block_num}",
        description=dataset.description,
        metadata=dataset.metadata,
        created_by=request.user.username if request.user.is_authenticated else 'unknown',
    )
    messages.success(request, f"Block {new_block_num} added: {new_block.did}")
    return redirect('pcs:dataset_detail', pk=dataset.pk)


# ── Production Configs ────────────────────────────────────────────

def prod_configs_compose(request):
    """Two-pane browse/create/edit UI for production configs."""
    if request.method == 'POST' and request.user.is_authenticated:
        editing_pk = request.POST.get('editing_pk')
        if editing_pk:
            instance = get_object_or_404(ProdConfig, pk=editing_pk)
            form = ProdConfigForm(request.POST, instance=instance)
        else:
            form = ProdConfigForm(request.POST)
        if form.is_valid():
            pc = form.save()
            messages.success(request, f"Config '{pc.name}' {'updated' if editing_pk else 'created'}.")
            return redirect(f"{reverse('pcs:prod_configs_compose')}?selected={urlquote(pc.name)}")

    qs = ProdConfig.objects.order_by('-updated_at')
    configs_data = []
    for pc in qs:
        configs_data.append({
            'id': pc.id,
            'name': pc.name,
            'description': pc.description,
            'bg_mixing': pc.bg_mixing,
            'bg_cross_section': pc.bg_cross_section,
            'bg_evtgen_file': pc.bg_evtgen_file,
            'copy_reco': pc.copy_reco,
            'copy_full': pc.copy_full,
            'copy_log': pc.copy_log,
            'use_rucio': pc.use_rucio,
            'jug_xl_tag': pc.jug_xl_tag,
            'container_image': pc.container_image,
            'target_hours_per_job': str(pc.target_hours_per_job) if pc.target_hours_per_job else '',
            'events_per_task': pc.events_per_task,
            'panda_site': pc.panda_site,
            'panda_queue': pc.panda_queue,
            'panda_working_group': pc.panda_working_group,
            'panda_resource_type': pc.panda_resource_type,
            'rucio_rse': pc.rucio_rse,
            'rucio_replication_rules': pc.rucio_replication_rules,
            'condor_template': pc.condor_template,
            'data': pc.data or {},
            'created_by': pc.created_by,
            'created_at': pc.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': pc.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    context = {
        'configs_json': json.dumps(configs_data),
        'selected_item_json': json.dumps(request.GET.get('selected') or None),
        'username': request.user.username if request.user.is_authenticated else '',
    }
    return render(request, 'pcs/prod_config_compose.html', context)


def prod_configs_list(request):
    columns = [
        {'name': 'name', 'title': 'Name', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'jug_xl_tag', 'title': 'JUG_XL', 'orderable': True},
        {'name': 'target_hours_per_job', 'title': 'Hours/Job', 'orderable': True},
        {'name': 'events_per_task', 'title': 'Events/Task', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    context = {
        'table_title': 'Production Configs',
        'table_description': 'Reusable production configuration templates for job submission.',
        'ajax_url': reverse('pcs:prod_configs_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/prod_configs_list.html', context)


def prod_configs_datatable_ajax(request):
    col_names = ['name', 'description', 'jug_xl_tag', 'target_hours_per_job',
                 'events_per_task', 'created_by', 'updated_at']
    dt = DataTablesProcessor(request, col_names, default_order_column=6, default_order_direction='desc')

    qs = ProdConfig.objects.all()
    records_total = qs.count()
    search_fields = ['name', 'description', 'created_by', 'jug_xl_tag']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for pc in page:
        detail_url = reverse('pcs:prod_config_detail', args=[pc.pk])
        data.append([
            f'<a href="{detail_url}">{pc.name}</a>',
            pc.description[:80] + ('...' if len(pc.description) > 80 else ''),
            pc.jug_xl_tag or '-',
            str(pc.target_hours_per_job) if pc.target_hours_per_job else '-',
            str(pc.events_per_task) if pc.events_per_task else '-',
            pc.created_by,
            format_datetime(pc.updated_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def prod_config_detail(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    return render(request, 'pcs/prod_config_detail.html', {'config': config})


@_login_required_flash
def prod_config_create(request):
    if request.method == 'POST':
        form = ProdConfigForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{form.instance.name}' created.")
            return redirect('pcs:prod_config_detail', pk=form.instance.pk)
    else:
        form = ProdConfigForm()
    return render(request, 'pcs/prod_config_form.html', {'form': form})


@_login_required_flash
def prod_config_edit(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    if request.method == 'POST':
        form = ProdConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{config.name}' updated.")
            return redirect('pcs:prod_config_detail', pk=config.pk)
    else:
        form = ProdConfigForm(instance=config)
    return render(request, 'pcs/prod_config_form.html', {'form': form, 'editing': True, 'config': config})


# ── Production Tasks ─────────────────────────────────────────────

TAG_MODELS_MAP = {'p': PhysicsTag, 'e': EvgenTag, 's': SimuTag, 'r': RecoTag, 'k': BackgroundTag}


LIFECYCLE_KEYS = ('past', 'last', 'current', 'future')


@_login_required_flash
def pcs_catalog_csv_update(request):
    """POST handler for the 'Update from CSV' button on the catalog.

    Runs the default-datasets CSV import service and redirects back to
    the catalog with a flash summary. POST-only.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update from CSV')
    from .services import import_default_datasets_csv, ServiceError
    try:
        summary = import_default_datasets_csv(
            created_by=getattr(request.user, 'username', '') or 'csv_import',
        )
    except (ServiceError, FileNotFoundError, OSError) as e:
        messages.error(request, f'CSV import failed: {e}')
        return redirect(reverse('pcs:pcs_catalog'))
    msg = (f'CSV import: {summary["created"]} new, '
           f'{summary["updated"]} updated, '
           f'{len(summary["errors"])} errors '
           f'(of {summary["rows"]} rows)')
    if summary['errors']:
        messages.warning(request, msg)
    else:
        messages.success(request, msg)
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_set_current(request):
    """POST handler for the 'Make current' button.

    Renames the existing PCS lifecycle='current' Campaign to whatever
    target the operator selected on the banner. AI never auto-flips
    this — humans switch.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Make current')
    target = (request.POST.get('name') or '').strip()
    from .services import (rename_pcs_current_campaign,
                           import_jlab_rucio_current_snapshot, ServiceError)
    try:
        result = rename_pcs_current_campaign(
            target,
            created_by=getattr(request.user, 'username', '') or 'operator',
        )
    except ServiceError as e:
        messages.error(request, f'Switch failed: {e}')
        return redirect(reverse('pcs:pcs_catalog'))
    if not result.get('changed'):
        messages.info(request, f"PCS current campaign already {target}.")
        return redirect(reverse('pcs:pcs_catalog'))
    # Pull the snapshot for the new current as part of the same click —
    # operator already consented by clicking 'Make current'; no point
    # making them hunt for 'Update from Rucio' next.
    try:
        snap = import_jlab_rucio_current_snapshot(
            created_by=getattr(request.user, 'username', '') or 'operator',
        )
        counts = ', '.join(f'{k}={v}' for k, v in snap['paths'].items())
        messages.success(
            request,
            f"PCS current: {result['old_name']} -> {result['name']}. "
            f"Snapshot pulled: {counts}. {len(snap['errors'])} errors.")
    except (ServiceError, OSError) as e:
        messages.warning(
            request,
            f"PCS current renamed to {result['name']} but snapshot pull "
            f"failed: {e}. Click 'Update from Rucio' to retry.")
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_set_last(request):
    """POST handler for 'Make last' button (Last tab selector).

    Sets the PCS lifecycle='last' Campaign to the named release and
    pulls its Rucio snapshot in the same click.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog') + '?lifecycle=last',
            action_label='Make last')
    target = (request.POST.get('name') or '').strip()
    from .services import (set_pcs_campaign_lifecycle,
                           import_jlab_rucio_current_snapshot, ServiceError)
    try:
        result = set_pcs_campaign_lifecycle(
            target, 'last',
            created_by=getattr(request.user, 'username', '') or 'operator')
    except ServiceError as e:
        messages.error(request, f'Make last failed: {e}')
        return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=last')
    try:
        snap = import_jlab_rucio_current_snapshot(
            campaign_name=target,
            created_by=getattr(request.user, 'username', '') or 'operator')
        counts = ', '.join(f'{k}={v}' for k, v in snap['paths'].items())
        messages.success(
            request,
            f"PCS last set to {result['name']}. Snapshot: {counts}.")
    except (ServiceError, OSError) as e:
        messages.warning(
            request,
            f"PCS last set to {result['name']} but snapshot pull failed: {e}")
    return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=last')


@_login_required_flash
def pcs_catalog_rucio_update(request):
    """No-JS POST fallback for the catalog 'Update from Rucio' button.

    The button's JavaScript posts to the /pcs/api/ endpoint (the external-safe
    trigger that survives the swf-remote proxy — see docs/EPICPROD_OPS_AGENT.md);
    this page-view handles the no-JavaScript case only and is reachable on the
    internal face. Both publish the same rucio_snapshot_update via
    services.rucio_snapshot_update_request. POST-only.
    See docs/EPICPROD_DATA_LINEAGE.md, docs/EPICPROD_OPS_AGENT.md.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update from Rucio')
    from .services import rucio_snapshot_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'rucio_snapshot'
    try:
        rucio_snapshot_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(reverse('pcs:pcs_catalog'))
    messages.success(request, 'Rucio update queued — refreshing in the background.')
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_evgen_update(request):
    """No-JS POST fallback for the catalog 'Update EVGEN from Rucio' button.

    The button's JavaScript posts to the /pcs/api/ endpoint (the external-safe
    trigger); this page-view handles the no-JavaScript case on the internal
    face. Both publish the same evgen_rucio_update via
    services.evgen_rucio_update_request. POST-only.
    See docs/EPICPROD_EVGEN_INPUTS.md, docs/EPICPROD_OPS_AGENT.md.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update EVGEN from Rucio')
    from .services import evgen_rucio_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'evgen_rucio'
    try:
        evgen_rucio_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(reverse('pcs:pcs_catalog'))
    messages.success(request, 'EVGEN update queued — refreshing in the background.')
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_questionnaire_match_update(request):
    """No-JS fallback for the catalog questionnaire-match cache button.

    The JavaScript path posts to /pcs/api/ and waits for the prod-ops
    questionnaire_match_ready event. This page-view only queues the same
    background agent work when JavaScript is unavailable.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update questionnaire matches')
    from .services import questionnaire_match_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'questionnaire_match'
    try:
        questionnaire_match_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(reverse('pcs:pcs_catalog'))
    messages.success(
        request,
        'Questionnaire match update queued — refreshing in the background.')
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_progress_refresh(request):
    """Refresh the cached current-campaign progress snapshot.

    This is intentionally a manual refresh path. The catalog page reads the
    cached snapshot from Campaign.data and does not query Rucio or scan PanDA on
    every page load.
    """
    target_url = reverse('pcs:pcs_catalog') + '?lifecycle=current&view=progress'
    if request.method != 'POST':
        return _post_only_redirect(
            request, target_url,
            action_label='Refresh progress')
    from .services import campaign_progress_refresh_request, ServiceError
    user = getattr(request.user, 'username', '') or 'progress_refresh'
    try:
        campaign_progress_refresh_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(target_url)
    messages.success(request, 'Progress refresh queued — updating in the background.')
    return redirect(target_url)


@_login_required_flash
def pcs_catalog_cache_refresh(request):
    """Drop cached current-campaign catalog/progress table HTML."""
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Refresh catalog table')
    view = (request.POST.get('view') or 'catalog').strip()
    if view not in ('catalog', 'progress'):
        view = 'catalog'
    target_url = reverse('pcs:pcs_catalog') + '?lifecycle=current'
    if view == 'progress':
        target_url += '&view=progress'
    messages.error(
        request,
        'Table rebuild is disabled in the web request path to protect the host. '
        'The page will continue to use cached table HTML.')
    return redirect(target_url)


def rucio_did_detail(request, scope, name):
    """Self-hosted Rucio DID detail — a live, read-only browser for any DID,
    since ePIC has no public Rucio webui. GET page-view → external-safe through
    the swf-remote proxy (no write, no redirect, no agent credential; reads use
    the public eicread userpass). Generic over DID type — input EVGEN and output
    RECO render identically; only the links into it differ. The file list loads
    on demand (rucio_did_files). A back-link to associated ProdTasks is a planned
    phase-1.5 add (reverse lookup over overrides['outputs'] / input DIDs).
    See docs/EPICPROD_DATA_LINEAGE.md."""
    from .services import fetch_jlab_rucio_did, ServiceError
    norm = '/' + name.lstrip('/')
    ctx = {'scope': scope, 'name': norm, 'name_url': norm.lstrip('/'),
           'did': f'{scope}:{norm}'}
    try:
        ctx['r'] = fetch_jlab_rucio_did(scope, norm)
    except ServiceError as e:
        ctx['error'] = e.detail
        return render(request, 'pcs/rucio_did_detail.html', ctx, status=e.status)
    return render(request, 'pcs/rucio_did_detail.html', ctx)


def rucio_did_files(request, scope, name):
    """On-demand JSON file list for the DID detail page (can be thousands)."""
    from .services import fetch_jlab_rucio_did_files, ServiceError
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Login required'}, status=403)
    try:
        files = fetch_jlab_rucio_did_files(scope, name)
    except ServiceError as e:
        return JsonResponse({'error': e.detail}, status=e.status)
    return JsonResponse({'files': files, 'count': len(files)})


@_login_required_flash
def pcs_catalog_past_update(request):
    """POST handler for the 'Update from epic-prod' button on the Past tab.

    Runs the past-campaign output ingest (FULL + RECO 2026 versions from
    the cloned epic-prod docs tree) and redirects back to the catalog
    Past view with a flash summary. POST-only.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request,
            reverse('pcs:pcs_catalog') + '?lifecycle=past',
            action_label='Update from epic-prod')
    from .services import import_epic_prod_past_campaigns, ServiceError
    try:
        summary = import_epic_prod_past_campaigns(
            created_by=getattr(request.user, 'username', '') or 'past_import',
        )
    except (ServiceError, FileNotFoundError, OSError) as e:
        messages.error(request, f'Past-campaign import failed: {e}')
        return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=past')
    msg = (f'epic-prod past import: {summary["created"]} new, '
           f'{summary["updated"]} updated, '
           f'across {summary["campaigns"]} campaigns, '
           f'{len(summary["errors"])} errors '
           f'(of {summary["rows"]} rows)')
    if summary['errors']:
        messages.warning(request, msg)
    else:
        messages.success(request, msg)
    return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=past')


def pcs_catalog(request):
    """Production Task Catalog — lifecycle-grouped task listing.

    Authenticated-only: the page hosts action buttons (CSV refresh,
    bulk actions, future per-task actions) whose POST handlers require
    sign-in. Catching auth at the GET prevents the silent-fail trap
    where an anonymous user sees buttons that quietly do nothing.
    """
    build_start = time.perf_counter() if CATALOG_BUILD_TIMING_ENABLED else None
    timings = [] if CATALOG_BUILD_TIMING_ENABLED else None
    filters = _timed(timings, 'parse filters', lambda: _parse_catalog_filters(request))
    active_lifecycle = (request.GET.get('lifecycle') or '').strip()
    if active_lifecycle not in LIFECYCLE_KEYS:
        active_lifecycle = 'current'
    catalog_view = (request.GET.get('view') or 'catalog').strip()
    if active_lifecycle != 'current' or catalog_view not in ('catalog', 'progress'):
        catalog_view = 'catalog'

    campaigns_by_lifecycle = _timed(
        timings,
        'campaign lifecycle query',
        lambda: {
            k: list(
                Campaign.objects
                .filter(lifecycle=k)
                .only('id', 'name', 'lifecycle', 'start_date', 'created_at')
                .order_by('name')
            )
            for k in LIFECYCLE_KEYS
        },
        detail_fn=lambda value: f'{sum(len(v) for v in value.values())} campaigns',
    )
    def _tab_detail(key, camps):
        # Future campaigns are stage-prefixed (RECO/26.06.0); show the bare
        # version so the tab reads "Future · 26.06.0", mirroring Current.
        if key == 'past':
            return ''
        if key == 'future':
            return ', '.join(sorted(
                {c.name.split('/', 1)[1] for c in camps if '/' in c.name}))
        return ', '.join(c.name for c in camps)
    lifecycle_tabs = [
        {'key': 'past',    'label': 'Past',    'color': 'secondary',
         'campaigns': campaigns_by_lifecycle['past'],
         'detail': _tab_detail('past', campaigns_by_lifecycle['past'])},
        {'key': 'last',    'label': 'Last',    'color': 'last-green',
         'campaigns': campaigns_by_lifecycle['last'],
         'detail': _tab_detail('last', campaigns_by_lifecycle['last'])},
        {'key': 'current', 'label': 'Current', 'color': 'success',
         'campaigns': campaigns_by_lifecycle['current'],
         'detail': _tab_detail('current', campaigns_by_lifecycle['current'])},
        {'key': 'future',  'label': 'Future',  'color': 'primary',
         'campaigns': campaigns_by_lifecycle['future'],
         'detail': _tab_detail('future', campaigns_by_lifecycle['future'])},
    ]

    # Past lifecycle: per-release view of output datasets. Each release
    # is one SW version (e.g. 26.04.1) covering up to two stages
    # (FULL=Simu, RECO=Reco). ?release=<v> picks one release; default is
    # the most recent. ?release=all spans every past release; release=
    # all_2025 / all_2026 spans that year. ?stage=FULL|RECO filters
    # within the chosen release set.
    #
    # Last lifecycle reuses the same path with release pinned to the
    # Last campaign's name (e.g. 26.04.1) and adds the Rucio timeline
    # plot above the table — a hybrid of Past's row model and Current's
    # snapshot view.
    if active_lifecycle in ('past', 'last', 'future'):
        # Future and Past share the release-table view; each lists its own
        # lifecycle's produced releases (Last pins to a past version, below).
        past_campaigns = list(
            campaigns_by_lifecycle['future' if active_lifecycle == 'future' else 'past'])
        # Time flows left to right; releases ordered ASC.
        release_versions = sorted(
            {c.name.split('/', 1)[1] for c in past_campaigns if '/' in c.name}
        )
        def _version_year(v):
            head = v.split('.', 1)[0]
            return ('20' + head) if head.isdigit() and len(head) == 2 else ''
        # {'2025': [versions...], '2026': [...]} in ASC release order.
        releases_by_year = {}
        for v in release_versions:
            yr = _version_year(v)
            if yr:
                releases_by_year.setdefault(yr, []).append(v)
        # Year groups listed newest-first (2026 then 2025) per Torre's
        # preference; releases within each year stay ASC.
        years_sorted = sorted(releases_by_year.keys(), reverse=True)

        if active_lifecycle == 'last':
            # Pin release to the Last campaign's version; no nav. The
            # Last Campaign carries its version directly as its name
            # (e.g. '26.04.1'); past campaigns for the same version are
            # named 'FULL/26.04.1' and 'RECO/26.04.1'.
            last_camps = campaigns_by_lifecycle['last']
            active_release = last_camps[0].name if last_camps else ''
        else:
            requested_release = (request.GET.get('release') or '').strip()
            if requested_release == 'all':
                active_release = 'all'
            elif (requested_release.startswith('all_')
                  and requested_release[4:] in years_sorted):
                active_release = requested_release
            elif requested_release in release_versions:
                active_release = requested_release
            else:
                # Default landing = most recent release (last in ASC).
                active_release = release_versions[-1] if release_versions else ''

        requested_stage = (request.GET.get('stage') or '').strip().upper()
        active_stage = requested_stage if requested_stage in ('FULL', 'RECO') else ''

        def in_release(c):
            if active_release == 'all':
                return True
            if active_release.startswith('all_'):
                year = active_release[4:]
                versions = releases_by_year.get(year, [])
                return any(c.name.endswith('/' + v) for v in versions)
            return c.name.endswith('/' + active_release)
        release_campaigns = [c for c in past_campaigns if in_release(c)]

        def in_stage(c, s):
            return c.name.startswith(s + '/')
        selected_campaigns = [c for c in release_campaigns
                              if not active_stage or in_stage(c, active_stage)]

        # Stage-facet counts: number of past_output rows under each stage
        # in the active release.
        per_campaign_count = dict(
            ProdTask.objects
            .filter(campaign__in=release_campaigns, status='past_output')
            .values_list('campaign__name')
            .annotate(Count('id'))
        )
        def count_for(stage):
            return sum(n for name, n in per_campaign_count.items()
                       if name.startswith(stage + '/'))
        stage_counts = {
            'all':  sum(per_campaign_count.values()),
            'FULL': count_for('FULL'),
            'RECO': count_for('RECO'),
        }

        past_tasks = list(
            ProdTask.objects
            .select_related(
                'campaign', 'dataset', 'dataset__physics_tag',
                'dataset__evgen_tag', 'dataset__simu_tag',
                'dataset__reco_tag', 'dataset__background_tag',
            )
            .filter(campaign__in=selected_campaigns, status='past_output')
            .order_by('campaign__name', 'dataset__dataset_name')
        )
        past_tasks = _annotate_task_questionnaire_matches(past_tasks)
        selected_campaign_data = dict(
            Campaign.objects
            .filter(pk__in=[c.pk for c in selected_campaigns])
            .values_list('pk', 'data')
        )
        agg_files = sum((selected_campaign_data.get(c.pk) or {})
                        .get('past_summary', {}).get('file_count', 0)
                        for c in selected_campaigns)
        agg_size = sum((selected_campaign_data.get(c.pk) or {})
                       .get('past_summary', {}).get('data_size_bytes', 0)
                       for c in selected_campaigns)

        # Year groups for the template's per-year nav blocks.
        release_year_groups = [
            {'year': yr, 'versions': releases_by_year[yr],
             'all_key': f'all_{yr}'}
            for yr in years_sorted
        ]

        # Last lifecycle add-on: Rucio snapshot/timeline + Make-last
        # selector + unmatched details, layered on top of the
        # past-style table.
        rucio_timeline = None
        rucio_unmatched = []
        rucio_unmatched_campaign = ''
        rucio_detected = []
        rucio_current_name = ''
        if active_lifecycle == 'last':
            last_camps = campaigns_by_lifecycle['last']
            target = last_camps[0] if last_camps else None
            if target is not None:
                from .services import load_rucio_timeline
                rucio_timeline = load_rucio_timeline(target.name)
                target_data = _campaign_data(target)
                rucio_unmatched = target_data.get('rucio_unmatched', []) or []
                rucio_unmatched_campaign = target.name
                rucio_detected = target_data.get('detected_releases', []) or []
                rucio_current_name = target.name
            else:
                # No Last set yet — borrow detected releases from
                # current so the operator has options to pick from.
                cur = campaigns_by_lifecycle['current'][0] if campaigns_by_lifecycle['current'] else None
                rucio_detected = _campaign_data(cur).get('detected_releases', []) if cur else []
                rucio_current_name = cur.name if cur else ''

        return render(request, 'pcs/pcs_catalog_past.html', {
            'show_tabs': True,
            'active_lifecycle': active_lifecycle,
            'lifecycle_tabs': lifecycle_tabs,
            'release_versions': release_versions,
            'release_year_groups': release_year_groups,
            'active_release': active_release,
            'active_stage': active_stage,
            'stage_counts': stage_counts,
            'selected_campaign_count': len(selected_campaigns),
            'aggregate_file_count': agg_files,
            'aggregate_data_size': agg_size,
            'tasks': past_tasks,
            'rucio_timeline_json': json.dumps(rucio_timeline) if rucio_timeline else 'null',
            'rucio_unmatched': rucio_unmatched,
            'rucio_unmatched_campaign': rucio_unmatched_campaign,
            'rucio_detected': rucio_detected,
            'rucio_current_name': rucio_current_name,
        })

    # Rucio arrivals timeline for the current campaign (when a snapshot
    # exists). Surfaced at the top of the page as a Plotly chart.
    rucio_timeline = None
    rucio_unmatched = []
    rucio_unmatched_campaign = ''
    rucio_detected = []
    rucio_current_name = ''
    evgen_rucio_unmatched = []
    evgen_rucio_checked_at = ''
    if active_lifecycle == 'current':
        camp_list = campaigns_by_lifecycle['current']
        target = camp_list[0] if camp_list else None
        if target is not None:
            from .services import load_rucio_timeline
            rucio_timeline = _timed(
                timings,
                'Rucio timeline cached read',
                lambda: load_rucio_timeline(target.name),
                detail_fn=lambda value: (
                    f'{len(value.get("dates") or [])} bins'
                    if value else 'missing'
                ),
            )
            data_start = time.perf_counter()
            target_data = _campaign_data(target)
            rucio_unmatched = target_data.get('rucio_unmatched', []) or []
            rucio_unmatched_campaign = target.name
            rucio_detected = target_data.get('detected_releases', []) or []
            rucio_current_name = target.name
            evgen_rucio_unmatched = target_data.get('evgen_rucio_unmatched', []) or []
            evgen_rucio_checked_at = target_data.get('evgen_rucio_checked_at', '')
            _timing_record(
                timings,
                'Rucio cached metadata read',
                data_start,
                detail=f'{len(rucio_unmatched)} unmatched, {len(evgen_rucio_unmatched)} EVGEN unmatched',
            )

    progress_snapshot = None
    progress_refresh_requested = request.GET.get('refresh') == '1'
    progress_refreshed_for_request = False
    progress_refresh_error = ''
    progress_campaign = campaigns_by_lifecycle['current'][0] if campaigns_by_lifecycle['current'] else None
    if progress_campaign is not None:
        from .services import load_campaign_progress_snapshot
        if catalog_view == 'progress' and progress_refresh_requested:
            progress_refresh_error = 'refresh=1 requested; page-load refresh is disabled to avoid running the heavy progress rebuild inside GET.'
        progress_snapshot = _timed(
            timings,
            'progress snapshot cached read',
            lambda: load_campaign_progress_snapshot(progress_campaign),
            detail_fn=lambda value: (
                'generated_at=' + str((value or {}).get('generated_at') or '')
                if value else 'missing'
            ),
        )
    rucio_json = _timed(
        timings,
        'Rucio chart JSON encode',
        lambda: json.dumps(rucio_timeline) if rucio_timeline else 'null',
        detail_fn=lambda value: f'{len(value)} bytes',
    )
    requestor_options = _timed(
        timings,
        'requestor filter options',
        _requestor_options,
        detail_fn=lambda value: f'{len(value)} options',
    )
    context = {
        'tasks': [],
        'show_tabs': True,
        'columns_mode': 'full',
        'catalog_view': catalog_view,
        'catalog_view_urls': {
            'catalog': _catalog_view_url(request, active_lifecycle, 'catalog'),
            'progress': _catalog_view_url(request, active_lifecycle, 'progress'),
        },
        'active_lifecycle': active_lifecycle,
        'lifecycle_tabs': lifecycle_tabs,
        'active_campaigns': campaigns_by_lifecycle[active_lifecycle],
        'progress_campaign_name': progress_campaign.name if progress_campaign else '',
        'focused_campaign': None,
        'focused_task_id': None,
        'filters': filters,
        'requestor_options': requestor_options,
        'status_choices': PRODTASK_STATUS_CHOICES,
        'form_action': reverse('pcs:pcs_catalog'),
        'rucio_timeline_json': rucio_json,
        'rucio_unmatched': rucio_unmatched,
        'rucio_unmatched_campaign': rucio_unmatched_campaign,
        'rucio_detected': rucio_detected,
        'rucio_current_name': rucio_current_name,
        'evgen_rucio_unmatched': evgen_rucio_unmatched,
        'evgen_rucio_checked_at': evgen_rucio_checked_at,
        'progress_snapshot': progress_snapshot,
        'progress_errors': (progress_snapshot or {}).get('errors') or [],
        'progress_generated_at': (progress_snapshot or {}).get('generated_at') or '',
        'progress_generated_by': (progress_snapshot or {}).get('generated_by') or '',
        'progress_refresh_requested': progress_refresh_requested,
        'progress_refreshed_for_request': progress_refreshed_for_request,
        'progress_refresh_error': progress_refresh_error,
    }
    task_list_html, task_list_cache_hit, task_list_cache_meta = _cached_current_task_list_html(
        progress_campaign, catalog_view, context, progress_snapshot, timings=timings)
    context['task_list_html'] = task_list_html
    context['task_list_cache_hit'] = task_list_cache_hit
    context['task_list_cache_rendered_at'] = task_list_cache_meta.get('rendered_at') or ''
    context['task_list_cache_stale'] = bool(task_list_cache_meta.get('stale'))
    context['task_list_cache_miss_suppressed'] = bool(
        task_list_cache_meta.get('cache_miss_suppressed'))
    if CATALOG_BUILD_TIMING_ENABLED:
        context['catalog_timing_rows'] = timings
        context['catalog_timing_total_ms'] = _timing_ms(time.perf_counter() - build_start)
    return render(request, 'pcs/pcs_catalog.html', context)


def prod_tasks_list(request):
    columns = [
        {'name': 'name', 'title': 'Name', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'dataset__dataset_name', 'title': 'Dataset', 'orderable': True},
        {'name': 'prod_config__name', 'title': 'Config', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    context = {
        'table_title': 'Production Tasks',
        'table_description': 'Production task compositions (Dataset + Config).',
        'ajax_url': reverse('pcs:prod_tasks_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/prod_tasks_list.html', context)


def prod_tasks_datatable_ajax(request):
    col_names = ['name', 'status', 'dataset__dataset_name', 'prod_config__name',
                 'created_by', 'updated_at']
    dt = DataTablesProcessor(request, col_names, default_order_column=5, default_order_direction='desc')

    qs = ProdTask.objects.select_related('dataset', 'prod_config')
    records_total = qs.count()
    search_fields = ['name', 'description', 'dataset__composed_name', 'dataset__dataset_name', 'prod_config__name', 'created_by']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    status_colors = {'draft': 'secondary', 'ready': 'primary', 'submitted': 'info',
                     'completed': 'success', 'failed': 'danger'}
    data = []
    for t in page:
        detail_url = reverse('pcs:prod_task_detail', args=[t.composed_name])
        color = status_colors.get(t.status, 'secondary')
        data.append([
            f'<a href="{detail_url}">{t.composed_name}</a>',
            f'<span class="badge bg-{color}">{t.status}</span>',
            t.dataset.dataset_name,
            t.prod_config.name,
            t.created_by,
            format_datetime(t.updated_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def prod_task_detail(request, name):
    from .commands import build_evgen_task_params
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    # Canonical task URL is the composed name; 301 a legacy/raw-name or stale
    # /tasks/<pk>/ inbound to it so a pk is never a resting URL.
    if name != task.composed_name:
        return redirect('pcs:prod_task_detail', name=task.composed_name, permanent=True)
    try:
        task_params = build_evgen_task_params(task)
        task_params_json = json.dumps(task_params, indent=2, sort_keys=False, default=str)
        task_params_error = None
    except Exception as e:
        task_params_json = None
        task_params_error = str(e)
    can_operate = request.user.is_authenticated
    return render(request, 'pcs/prod_task_detail.html', {
        'task': task,
        'task_params_json': task_params_json,
        'task_params_error': task_params_error,
        'can_operate': can_operate,
        'can_submit': can_operate and task.panda_task_id is None and task.status in ('draft', 'ready'),
        'can_reset_submission': can_operate and task.panda_task_id is not None,
    })


def prod_task_compose(request):
    """Two-pane compose UI for building production tasks.

    The page is scoped to ONE campaign — the current campaign by default, or the
    campaign of the ?selected=<name> task. Only that campaign's tasks, and the
    datasets they use, are shipped inline; cross-campaign and historical browsing
    is the full catalog's job (linked from the page caption). Per-item heavy
    detail (tag parameters, EVGEN submission spec, cached commands) is still omitted and
    hydrated on open (prod_task_compose_dataset_detail / _task_detail).
    """
    # Resolve the campaign first — it scopes the whole page. Default = the
    # current campaign; a ?selected=<name> task in another campaign follows that
    # task's campaign.
    selected_name = request.GET.get('selected') or None
    focused_task = None
    if selected_name:
        from .services import resolve_prodtask
        try:
            focused_task = resolve_prodtask(
                selected_name, ProdTask.objects.select_related('campaign', 'dataset'))
        except ProdTask.DoesNotExist:
            focused_task = None
    # Hand the JS the canonical composed name as the selection key (it resolves
    # composed-name-or-legacy), so a legacy-name or pk ?selected still focuses.
    if focused_task is not None:
        selected_name = focused_task.composed_name
    campaign = focused_task.campaign if (focused_task and focused_task.campaign) else None
    if campaign is None:
        campaign = Campaign.objects.filter(lifecycle='current').order_by('name').first()

    # Campaign-scoped task set — the single inline JSON source. Shipping every
    # campaign's tasks (and the ~4900 past_output archive rows) was what made
    # this page multi-MB and prone to proxy read timeouts.
    tasks_list = []
    if campaign is not None:
        tasks_list = list(
            ProdTask.objects.select_related(
                'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
                'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
            ).filter(campaign=campaign).order_by('-updated_at')
        )
    # Light task entries: EVGEN submission spec + cached commands omitted, hydrated on
    # open (prod_task_compose_task_detail). Readiness (cheap) is included so the
    # detail panel can show submit-readiness without a round trip.
    from .services import prodtask_readiness_problems
    tasks_list = _annotate_task_questionnaire_matches(tasks_list)
    tasks_data = []
    for t in tasks_list:
        tasks_data.append({
            'id': t.id,
            'name': t.name,
            # Canonical identity (stored dataset.composed_name); the JS keys and
            # links tasks on this, never on the pk or the legacy slash name.
            'composed_name': t.composed_name,
            'status': t.status,
            # The recorded submission — the JS reads `submitted = !!t.panda_task_id`
            # to show the PanDA-task link + the operator Reset control. Omitting it
            # left every submitted task with only the Copy button on page load.
            'panda_task_id': t.panda_task_id,
            'dataset_id': t.dataset_id,
            'dataset_name': t.dataset.dataset_name,
            'prod_config_id': t.prod_config_id,
            'prod_config_name': t.prod_config.name,
            'csv_file': t.csv_file,
            'overrides': t.overrides or {},
            'description': t.description,
            'created_by': t.created_by,
            'readiness': prodtask_readiness_problems(t),
            'updated_at': format_datetime(t.updated_at),
            'questionnaire_matches': [
                {
                    'id': item['questionnaire'].pk,
                    'confidence': item.get('confidence') or '',
                    'reason': item.get('reason') or '',
                }
                for item in getattr(t, 'questionnaire_matches', [])
            ],
        })

    # Datasets: only those used by the in-scope tasks — campaign-coherent, and
    # keeps the past_output archive datasets off the page.
    dataset_ids = {t.dataset_id for t in tasks_list}
    datasets_qs = Dataset.objects.filter(id__in=dataset_ids).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
    ).order_by('-created_at')
    datasets_data = []
    for ds in datasets_qs:
        datasets_data.append({
            'id': ds.id,
            'dataset_name': ds.dataset_name,
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'stage': ds.stage,
            'external': ds.is_external,
            'source_kind': ds.source_kind,
            'source_location': ds.source_location,
            'validation_status': ds.validation_status,
            # tag .parameters and .metadata omitted from the light payload;
            # hydrated on open (prod_task_compose_dataset_detail). Labels +
            # descriptions stay for the list, search, and the diff.
            'physics_tag': {'label': ds.physics_tag.tag_label, 'description': ds.physics_tag.description},
            'evgen_tag': {'label': ds.evgen_tag.tag_label, 'description': ds.evgen_tag.description},
            'simu_tag': {'label': ds.simu_tag.tag_label, 'description': ds.simu_tag.description},
            'reco_tag': {'label': ds.reco_tag.tag_label, 'description': ds.reco_tag.description},
            # Background (k) is optional; null when the dataset carries no
            # standalone-background tag.
            'background_tag': ({'label': ds.background_tag.tag_label,
                                'description': ds.background_tag.description}
                               if ds.background_tag_id else None),
            # The dataset's name in the tag-based system. The human-facing
            # identity on the page — the internal csv_import.<hash> dataset_name
            # and its synthetic DID are plumbing and are not shown.
            'composed_name': ds.build_dataset_name(),
            'created_by': ds.created_by,
            'created_at': ds.created_at.strftime('%Y-%m-%d %H:%M'),
        })

    configs_qs = ProdConfig.objects.order_by('-updated_at')
    configs_data = []
    for pc in configs_qs:
        configs_data.append({
            'id': pc.id,
            'name': pc.name,
            'description': pc.description,
            'jug_xl_tag': pc.jug_xl_tag,
            'container_image': pc.container_image,
            'bg_mixing': pc.bg_mixing,
            'bg_cross_section': pc.bg_cross_section,
            'bg_evtgen_file': pc.bg_evtgen_file,
            'copy_reco': pc.copy_reco,
            'copy_full': pc.copy_full,
            'copy_log': pc.copy_log,
            'use_rucio': pc.use_rucio,
            'target_hours_per_job': str(pc.target_hours_per_job) if pc.target_hours_per_job else '',
            'events_per_task': pc.events_per_task,
            'panda_site': pc.panda_site,
            'panda_queue': pc.panda_queue,
            'panda_working_group': pc.panda_working_group,
            'panda_resource_type': pc.panda_resource_type,
            'rucio_rse': pc.rucio_rse,
            'data': pc.data or {},
            'created_by': pc.created_by,
            'updated_at': pc.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    # Left-panel task list: same campaign scope, dataset-name order, the
    # partial-friendly shape the catalog uses.
    campaign_tasks = []
    if campaign is not None:
        campaign_tasks = list(
            ProdTask.objects
            .select_related(
                'campaign', 'dataset', 'prod_config', 'request',
                # The compose list falls back to dataset.composed_name (5 tag
                # FKs) when a row has no source path; prefetch so native-dataset
                # campaigns don't hit the same 1 + 5N as the catalog (a9a93ae).
                'dataset__physics_tag', 'dataset__evgen_tag',
                'dataset__simu_tag', 'dataset__reco_tag',
                'dataset__background_tag',
            )
            .filter(campaign=campaign)
            .order_by('dataset__dataset_name')
        )
        campaign_tasks = _annotate_task_questionnaire_matches(campaign_tasks)

    context = {
        'datasets_json': json.dumps(datasets_data),
        'configs_json': json.dumps(configs_data),
        'tasks_json': json.dumps(tasks_data),
        'selected_item_json': json.dumps(selected_name),
        'username': request.user.username if request.user.is_authenticated else '',
        # Left-panel task-list context (consumed by the list partial):
        'tasks': campaign_tasks,
        'focused_task_id': focused_task.id if focused_task else None,
        'focused_campaign': campaign,
        'filters': {},
    }
    return render(request, 'pcs/prod_task_compose.html', context)


@_login_required_flash
def prod_task_delete(request, name):
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related('dataset'))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:prod_task_detail', kwargs={'name': task.composed_name}),
            action_label='Task delete')
    if task.status != 'draft':
        messages.error(request, "Only draft tasks can be deleted.")
        return redirect('pcs:prod_task_detail', name=task.composed_name)
    task.delete()
    messages.success(request, f"Task '{task.composed_name}' deleted.")
    return redirect('pcs:prod_tasks_list')


def prod_task_generate_commands(request, name):
    """JSON endpoint: regenerate and return commands for a ProdTask."""
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    task.generate_commands()
    task.save(update_fields=['condor_command', 'panda_command', 'updated_at'])
    return JsonResponse({
        'condor_command': task.condor_command,
        'panda_command': task.panda_command,
    })


def prod_task_compose_dataset_detail(request, pk):
    """On-demand hydration for the compose view: a dataset's tag parameters and
    metadata, which the light initial payload omits. The compose JS merges this
    into the dataset entry the first time it is opened (never clobbering). GET
    JSON; read-only."""
    ds = get_object_or_404(Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'), pk=pk)
    payload = {
        'physics_tag': {'parameters': ds.physics_tag.parameters},
        'evgen_tag': {'parameters': ds.evgen_tag.parameters},
        'simu_tag': {'parameters': ds.simu_tag.parameters},
        'reco_tag': {'parameters': ds.reco_tag.parameters},
        'metadata': ds.metadata or {},
    }
    if ds.background_tag_id:
        payload['background_tag'] = {'parameters': ds.background_tag.parameters}
    return JsonResponse(payload)


def prod_task_compose_task_detail(request, name):
    """On-demand hydration for the compose view: a task's live EVGEN submission
    spec and cached condor/panda commands, which the light initial payload omits.
    The compose JS merges this into the task entry the first time it is opened
    (never clobbering). GET JSON; read-only — does not regenerate/save commands."""
    from .commands import build_evgen_task_params
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config'))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    try:
        task_params_json = json.dumps(build_evgen_task_params(task), indent=2, default=str)
        task_params_error = ''
    except Exception as e:                                       # noqa: BLE001
        task_params_json = ''
        task_params_error = str(e)
    return JsonResponse({
        'task_params_json': task_params_json,
        'task_params_error': task_params_error,
        'condor_command': task.condor_command,
        'panda_command': task.panda_command,
    })
