"""Human-facing Snapper report and instrument views."""

import json

from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from snapper_ai.models import CaptureCursor, CurrentComponent, SystemSnap

from ..models import SysConfig, SystemStatus


SCOPES = ('epicprod', 'testbed')
RECENT_SNAP_LIMIT = 100


def _validated_scope(scope):
    if scope not in SCOPES:
        raise Http404(f'Unknown Snapper scope {scope!r}')
    return scope


def _scope_options(scope):
    return [
        {
            'name': name,
            'label': 'epicprod' if name == 'epicprod' else 'Testbed',
            'active': name == scope,
        }
        for name in SCOPES
    ]


def _dict(value):
    return value if isinstance(value, dict) else {}


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _value_at(data, path):
    value = data
    for part in str(path or '').split('.'):
        if not part or not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _quantity_values(registration, data):
    rows = []
    for name, definition in sorted(
            _dict(registration.get('quantities')).items()):
        definition = _dict(definition)
        value = _value_at(data, definition.get('path', name))
        is_complex = isinstance(value, (dict, list))
        if is_complex:
            display = json.dumps(value, indent=2, sort_keys=True)
        elif value is None:
            display = '—'
        else:
            display = str(value)
        rows.append({
            'name': name,
            'description': definition.get('description', ''),
            'value': display,
            'is_complex': is_complex,
        })
    return rows


def _age_text(delta_seconds):
    """Compact human age ('31s', '2m 31s', '3h 04m', '2d 5h'); None when
    under a second — the caller then falls back to the absolute time."""
    seconds = int(round(delta_seconds))
    if seconds < 1:
        return None
    if seconds < 60:
        return f'{seconds}s'
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m {seconds:02d}s' if seconds else f'{minutes}m'
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f'{hours}h {minutes:02d}m' if minutes else f'{hours}h'
    days, hours = divmod(hours, 24)
    return f'{days}d {hours}h' if hours else f'{days}d'


def _present_snap_component(name, payload):
    payload = _dict(payload)
    registration = _dict(payload.get('registration'))
    data = _dict(payload.get('data'))
    health = None
    if name == 'health':
        overall = _dict(data.get('overall'))
        checks = []
        for check_name, check in sorted(_dict(data.get('checks')).items()):
            check = _dict(check)
            checks.append({
                'name': check_name,
                'category': check.get('category', ''),
                'status': check.get('status', 'unknown'),
                'summary': check.get('summary', ''),
            })
        health = {
            'status': overall.get('status', 'unknown'),
            'reason': overall.get('reason', ''),
            'counts': _dict(overall.get('counts')),
            'checks': checks,
            'non_ok': [c for c in checks
                       if c['status'] not in ('ok', 'healthy')],
        }
    # Known components render as the same structured card the cut uses
    # (no deltas — there is no previous snap in this presentation); only
    # a component without a known rendering falls back to the quantity
    # table.
    card = {'kind': None}
    if name == 'datataking':
        card = {'kind': 'datataking', 'namespaces': _datataking_rows(data)}
    elif name == 'panda':
        card = {'kind': 'panda'}
        card.update(_cut_panda_card(data, {}))
    elif name == 'workflow':
        card = {'kind': 'workflow'}
        card.update(_cut_workflow_card(data, {}))
    return {
        'name': name,
        'title': registration.get('title') or name,
        'description': registration.get('description', ''),
        'revision': payload.get('revision'),
        'registration_version': payload.get('registration_version'),
        'assessed_at': payload.get('assessed_at'),
        'source_as_of': payload.get('source_as_of'),
        'accepted_at': payload.get('accepted_at'),
        'publisher_identity': payload.get('publisher_identity', ''),
        'health': health,
        'card': card,
        'quantities': _quantity_values(registration, data),
        'payload_json': _json(payload),
    }


def _snap_row(snap):
    return {
        'id': snap.id,
        'snap_time': snap.snap_time,
        'observed_at': snap.observed_at,
        'reasons': ', '.join(snap.reasons or []) or '—',
        'changed_components': (
            ', '.join(snap.changed_components or []) or '—'),
        'capture_policy': snap.capture_policy,
        'encoding': snap.encoding,
        'component_count': len(snap.component_revisions or {}),
    }


def snapper_root(request):
    return redirect('monitor_app:snapper_report', scope='epicprod')


SNAPPER_PREFS_KEY = 'snapper'


def _snapper_prefs(request, scope):
    """The signed-in user's remembered UI state for one scope."""
    if not request.user.is_authenticated:
        return {}
    from ..models import UserPreference

    row = UserPreference.objects.filter(
        username=request.user.username).first()
    section = (row.prefs or {}).get(SNAPPER_PREFS_KEY) if row else None
    per_scope = (section or {}).get(scope) if isinstance(section, dict) else None
    return per_scope if isinstance(per_scope, dict) else {}


def snapper_prefs_save(request, scope):
    """POST endpoint remembering the observatory UI state per user."""
    import json as _json_module

    from django.http import JsonResponse

    from ..models import UserPreference

    scope = _validated_scope(scope)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'saved': False, 'reason': 'not signed in'})
    try:
        payload = _json_module.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'error': 'invalid JSON'}, status=400)
    allowed = {key: payload[key]
               for key in ('curves_off', 'window', 'lanes_open')
               if key in payload}
    row, _ = UserPreference.objects.get_or_create(
        username=request.user.username)
    prefs = row.prefs if isinstance(row.prefs, dict) else {}
    section = prefs.get(SNAPPER_PREFS_KEY)
    if not isinstance(section, dict):
        section = {}
    per_scope = section.get(scope)
    if not isinstance(per_scope, dict):
        per_scope = {}
    per_scope.update(allowed)
    section[scope] = per_scope
    prefs[SNAPPER_PREFS_KEY] = section
    row.prefs = prefs
    row.save()
    return JsonResponse({'saved': True})


def snapper_report(request, scope, snap_id=None):
    from django.utils import timezone

    from .snapper_series import (DEFAULT_WINDOW, WINDOW_HOURS,
                                 observatory_series, parse_window)

    scope = _validated_scope(scope)
    snaps = SystemSnap.objects.filter(scope=scope).order_by('-snap_time')
    latest_snap = snaps.first()

    user_prefs = _snapper_prefs(request, scope)
    window_start, window_end, window_key = parse_window(
        request, timezone.now(),
        default_window=str(user_prefs.get('window') or DEFAULT_WINDOW))
    observatory = observatory_series(scope, window_start, window_end)
    if snap_id is None:
        selected_snap = latest_snap
    else:
        selected_snap = get_object_or_404(snaps, id=snap_id)

    components = []
    observation_delay = None
    if selected_snap is not None:
        state = _dict(selected_snap.state)
        components = [
            _present_snap_component(name, payload)
            for name, payload in sorted(
                _dict(state.get('components')).items())
        ]
        observation_delay = (
            selected_snap.observed_at - selected_snap.snap_time
        ).total_seconds()

    from django.core.paginator import Paginator

    paginator = Paginator(snaps, RECENT_SNAP_LIMIT)
    try:
        snap_page_number = max(int(request.GET.get('snap_page') or 1), 1)
    except ValueError:
        snap_page_number = 1
    snap_page = paginator.get_page(snap_page_number)
    recent = list(snap_page.object_list)
    pager_params = request.GET.copy()
    pager_params.pop('snap_page', None)
    pager_query = pager_params.urlencode()
    return render(request, 'monitor_app/snapper.html', {
        'snap_page': snap_page,
        'snap_pager_query': f'{pager_query}&' if pager_query else '',
        'active_tab': 'report',
        'scope': scope,
        'scope_label': 'epicprod' if scope == 'epicprod' else 'Testbed',
        'scope_options': _scope_options(scope),
        'selected_snap': selected_snap,
        'latest_snap': latest_snap,
        'observation_delay': observation_delay,
        'components': components,
        'selected_snap_json': (
            _json(selected_snap.state) if selected_snap is not None else ''),
        'snap_rows': [_snap_row(snap) for snap in recent],
        'snap_count': snaps.count(),
        'recent_snap_limit': RECENT_SNAP_LIMIT,
        'observatory': observatory,
        'observatory_window': window_key,
        'observatory_windows': list(WINDOW_HOURS),
        'observatory_default_window': DEFAULT_WINDOW,
        'observatory_cut': (request.GET.get('cut') or '').strip(),
        'observatory_prefs': user_prefs,
    })


def _positive_int(config, key, default, minimum):
    raw = config.get(key, default)
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError
    except (TypeError, ValueError):
        return raw, None
    return raw, value


def _duration(seconds):
    if seconds is None:
        return 'unavailable'
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f'{minutes}m {remainder}s'
    if minutes:
        return f'{minutes}m'
    return f'{remainder}s'


def _component_registration(component):
    registration = _dict(component.registration)
    quantities = []
    for name, definition in sorted(
            _dict(registration.get('quantities')).items()):
        definition = _dict(definition)
        limits = []
        for key, label in (
                ('max_items', 'max items'),
                ('max_length', 'max length'),
                ('minimum', 'minimum'),
                ('maximum', 'maximum')):
            if key in definition:
                limits.append(f'{label}: {definition[key]}')
        if definition.get('enum'):
            limits.append('values: ' + ', '.join(
                str(value) for value in definition['enum']))
        quantities.append({
            'name': name,
            'path': definition.get('path', name),
            'kind': definition.get('kind', ''),
            'type': definition.get('type', ''),
            'required': bool(definition.get('required')),
            'limits': '; '.join(limits) or '—',
            'description': definition.get('description', ''),
        })
    event_sources = []
    for source in registration.get('event_sources') or []:
        source = _dict(source)
        event_sources.append({
            'name': source.get('name', ''),
            'kind': source.get('event_kind', ''),
            'resolver': source.get('resolver', ''),
            'owner': source.get('owner', ''),
            'visibility': source.get('visibility', ''),
        })
    return {
        'record': component,
        'title': registration.get('title') or component.name,
        'description': registration.get('description', ''),
        'owning_subsystem': registration.get('owning_subsystem', ''),
        'visibility': registration.get('visibility', ''),
        'assessment_policy': registration.get('assessment_policy', ''),
        'max_serialized_bytes': registration.get('max_serialized_bytes'),
        'quantities': quantities,
        'event_sources': event_sources,
        'registration_json': _json(registration),
    }


def snapper_system(request, scope):
    scope = _validated_scope(scope)
    config = SysConfig.get_config()
    opportunity_key = f'snapper_opportunity_seconds_{scope}'
    baseline_key = f'snapper_baseline_every_{scope}'
    policy_key = f'snapper_capture_policy_{scope}'
    opportunity_raw, opportunity = _positive_int(
        config, opportunity_key, 10, 10)
    baseline_raw, baseline = _positive_int(config, baseline_key, 10, 1)
    lock_raw, lock_timeout = _positive_int(
        config, 'snapper_lock_timeout_ms', 5000, 1)
    max_quiet = (
        opportunity * baseline
        if opportunity is not None and baseline is not None else None)

    cursor = (
        CaptureCursor.objects.select_related('latest_snap')
        .filter(scope=scope).first()
    )
    scheduler_status = SystemStatus.objects.filter(
        name=f'snapper-{scope}-scheduler').first()
    components = [
        _component_registration(component)
        for component in CurrentComponent.objects.filter(scope=scope)
        .order_by('-active', 'name')
    ]
    policy_rows = [
        {
            'setting': 'Snap opportunity',
            'value': f'{opportunity}s' if opportunity is not None else opportunity_raw,
            'key': opportunity_key,
            'valid': opportunity is not None,
        },
        {
            'setting': 'Periodic baseline',
            'value': (
                f'every {baseline} opportunities'
                if baseline is not None else baseline_raw),
            'key': baseline_key,
            'valid': baseline is not None,
        },
        {
            'setting': 'Maximum quiet interval',
            'value': _duration(max_quiet),
            'key': 'derived from opportunity × baseline',
            'valid': max_quiet is not None,
        },
        {
            'setting': 'Capture policy',
            'value': config.get(policy_key, f'{scope}-v1'),
            'key': policy_key,
            'valid': bool(config.get(policy_key, f'{scope}-v1')),
        },
        {
            'setting': 'Database lock timeout',
            'value': (
                f'{lock_timeout} ms' if lock_timeout is not None else lock_raw),
            'key': 'snapper_lock_timeout_ms',
            'valid': lock_timeout is not None,
        },
    ]
    return render(request, 'monitor_app/snapper.html', {
        'active_tab': 'system',
        'scope': scope,
        'scope_label': 'epicprod' if scope == 'epicprod' else 'Testbed',
        'scope_options': _scope_options(scope),
        'cursor': cursor,
        'scheduler_status': scheduler_status,
        'policy_rows': policy_rows,
        'components': components,
        'baseline_every': baseline,
    })


# ── The cut: structured state at an instant ──────────────────────────────

CUT_STATE_COLORS = {
    # One state-color vocabulary, mirrored by the Time history plot
    # (_snapper_observatory.html STATE_COLORS). Keep the two in step.
    'ok': '#2e7d32', 'healthy': '#2e7d32', 'running': '#2e7d32',
    'warning': '#f9a825', 'error': '#c62828', 'ended': '#78909c',
    'unknown': '#9e9e9e',
    # Datataking activity phases, matching the lane tile colors.
    'datataking': '#2e7d32', 'processing': '#81c784', 'idle': '#90a4ae',
}
CUT_FALLBACK_COLOR = '#1565c0'


def _cut_chip(value):
    base = str(value or 'unknown').split('/')[0].lower()
    return {'value': str(value or 'unknown'),
            'color': CUT_STATE_COLORS.get(base, CUT_FALLBACK_COLOR)}


def _cut_delta(current, previous):
    if current is None or previous is None:
        return None
    difference = int(current) - int(previous)
    if difference == 0:
        return None
    return f'+{difference}' if difference > 0 else str(difference)


def _cut_panda_card(data, previous_data):
    jobs_now = _dict(_dict(data.get('jobs')).get('in_flight_now'))
    prev_jobs = _dict(_dict(previous_data.get('jobs')).get('in_flight_now'))
    tasks_now = _dict(_dict(data.get('tasks')).get('in_flight_now'))
    prev_tasks = _dict(_dict(previous_data.get('tasks')).get('in_flight_now'))

    def stat(label, value, previous):
        return {'label': label,
                'value': value if value is not None else '—',
                'delta': _cut_delta(value, previous)}

    headline = [
        stat('running jobs', jobs_now.get('running_jobs'),
             prev_jobs.get('running_jobs')),
        stat('running cores', jobs_now.get('running_cores'),
             prev_jobs.get('running_cores')),
        stat('in-flight jobs', jobs_now.get('total'),
             prev_jobs.get('total')),
        stat('queued (activated)',
             _dict(jobs_now.get('by_status')).get('activated'),
             _dict(prev_jobs.get('by_status')).get('activated')),
        stat('in-flight tasks', tasks_now.get('total'),
             prev_tasks.get('total')),
    ]
    types = sorted(_dict(jobs_now.get('by_type')).items(),
                   key=lambda item: -item[1])
    type_states = []
    for ptype, states in sorted(_dict(jobs_now.get('by_type_status')).items()):
        for status, count in sorted(_dict(states).items()):
            previous = _dict(_dict(prev_jobs.get('by_type_status'))
                             .get(ptype)).get(status)
            type_states.append({
                'label': f'{ptype} · {status}', 'value': count,
                'delta': _cut_delta(count, previous)})
    return {'headline': headline, 'types': types,
            'type_states': type_states}


def _datataking_rows(data):
    """Namespace rows shared by the cut card and the recorded-state card."""
    return [
        {'namespace': namespace,
         'chip': _cut_chip(
             f"{ns.get('state')}"
             + (f"/{ns.get('substate')}" if ns.get('substate') else '')),
         'run_number': ns.get('run_number'),
         'phase': ns.get('phase'),
         'since': ns.get('last_transition_at')}
        for namespace, ns in sorted(_dict(data.get('namespaces')).items())
        for ns in [_dict(ns)]
    ]


def _cut_workflow_card(data, previous_data):
    """The workflow component as usable detail: execution and STF-task
    activity with links into the workflow and PanDA surfaces — never a
    bare document."""
    from ..snapper_workflow import STF_PROCESSING_TYPE

    executions = _dict(data.get('executions'))
    prev_exec = _dict(previous_data.get('executions'))
    stf = _dict(data.get('stf_tasks'))
    prev_stf = _dict(previous_data.get('stf_tasks'))

    def stat(label, value, previous):
        return {'label': label,
                'value': value if value is not None else '—',
                'delta': _cut_delta(value, previous)}

    headline = [
        stat('executions running', executions.get('active'),
             prev_exec.get('active')),
        stat('executions started (24h)', executions.get('started_24h'),
             prev_exec.get('started_24h')),
        stat('STF tasks in flight', stf.get('in_flight_total'),
             prev_stf.get('in_flight_total')),
    ]
    by_workflow = sorted(
        _dict(executions.get('by_workflow')).items(),
        key=lambda item: -item[1])
    site_states = []
    for key, count in sorted(_dict(stf.get('by_site_status')).items()):
        site, _, status = str(key).partition('/')
        previous = _dict(prev_stf.get('by_site_status')).get(key)
        site_states.append({'site': site, 'status': status, 'value': count,
                            'delta': _cut_delta(count, previous)})
    return {'headline': headline, 'by_workflow': by_workflow,
            'site_states': site_states,
            'stf_processing_type': STF_PROCESSING_TYPE}


def _cut_components(snap, previous_snap, scope, requested_at=None):
    state = _dict(snap.state)
    previous_state = _dict(previous_snap.state) if previous_snap else {}
    cards = []
    for name, payload in sorted(_dict(state.get('components')).items()):
        payload = _dict(payload)
        data = _dict(payload.get('data'))
        previous_payload = _dict(
            _dict(previous_state.get('components')).get(name))
        previous_data = _dict(previous_payload.get('data'))
        card = {
            'name': name,
            'assessed_at': payload.get('assessed_at'),
            'changed': (payload.get('revision')
                        != previous_payload.get('revision')),
            'payload_json': _json(payload),
        }
        if name == 'health':
            overall = _dict(data.get('overall'))
            card['kind'] = 'health'
            card['chip'] = _cut_chip(overall.get('status'))
            card['reason'] = overall.get('reason', '')
            card['counts'] = _dict(overall.get('counts'))
            card['non_ok_checks'] = [
                {'name': check_name, 'chip': _cut_chip(check.get('status')),
                 'summary': check.get('summary', ''),
                 'category': check.get('category', '')}
                for check_name, check in sorted(
                    _dict(data.get('checks')).items())
                if str(check.get('status')) not in ('ok', 'healthy')
                for check in [_dict(check)]
            ]
        elif name == 'datataking':
            # The cut and the lanes must tell one story: the namespace
            # rows come from the run record at the cut instant, the same
            # source the activity lanes draw. The snap's recorded entry
            # stays in the card's audit document.
            card['kind'] = 'datataking'
            if requested_at is not None:
                from .snapper_series import namespace_activity_at
                card['namespaces'] = [
                    {'namespace': namespace,
                     'chip': _cut_chip(info['phase']),
                     'run_number': info['run_number'],
                     'phase': info['workflow'],
                     'since': (info['since'].isoformat()
                               if info['since'] else '')}
                    for namespace, info in sorted(
                        namespace_activity_at(requested_at).items())
                ]
            else:
                card['namespaces'] = _datataking_rows(data)
        elif name == 'panda':
            card['kind'] = 'panda'
            card.update(_cut_panda_card(data, previous_data))
        elif name == 'workflow':
            card['kind'] = 'workflow'
            card.update(_cut_workflow_card(data, previous_data))
        else:
            card['kind'] = 'generic'
        cards.append(card)
    return cards


def snapper_cut(request, scope):
    """Server-rendered state cut: structured component cards at an
    instant, with deltas against the previous snap, exact-event context
    links, and the raw document one click behind (the Time history's
    selection panel; also the deep-link target for external dashboards)."""
    from django.utils.dateparse import parse_datetime

    from snapper_ai.queries import SnapNotFound, state_at

    from ..snapper_resolvers import annotate_references

    scope = _validated_scope(scope)
    requested = parse_datetime((request.GET.get('time') or '').strip())
    if requested is None or requested.tzinfo is None:
        return render(request, 'monitor_app/_snapper_cut.html',
                      {'error': 'time must be ISO 8601 with timezone'})
    try:
        result = state_at(scope, requested)
    except SnapNotFound as e:
        return render(request, 'monitor_app/_snapper_cut.html',
                      {'error': str(e)})
    snap = SystemSnap.objects.filter(id=result.snap_id).first()
    previous_snap = (SystemSnap.objects
                     .filter(scope=scope, snap_time__lt=snap.snap_time)
                     .order_by('-snap_time').first()) if snap else None

    references = []
    try:
        from snapper_ai.queries import context_around
        context = context_around(scope, requested, 3600).as_dict()
        references = annotate_references(context['references'])
    except Exception:                                        # noqa: BLE001
        pass  # references are enrichment; the cut renders without them

    # Attention economy: one absolute time, everything else relative to
    # it; coverage is mentioned only when it is NOT clean, in plain words.
    coverage = result.coverage.as_dict()
    coverage_notice = None
    if coverage.get('status') == 'gap':
        coverage_notice = {
            'chip': _cut_chip('error'), 'label': 'recording gap',
            'detail': 'Capture was down at this instant — showing the last '
                      'state recorded before the outage; the state may have '
                      'changed unseen.'}
    elif coverage.get('status') != 'covered':
        coverage_notice = {
            'chip': _cut_chip('warning'), 'label': 'coverage unknown',
            'detail': 'Whether capture was observing at this instant cannot '
                      'be established — showing the last recorded state.'}

    cards = (_cut_components(snap, previous_snap, scope,
                             requested_at=result.requested_at)
             if snap else [])
    for card in cards:
        assessed = parse_datetime(str(card.get('assessed_at') or ''))
        card['assessed_age_text'] = (
            _age_text((result.requested_at - assessed).total_seconds())
            if assessed and assessed.tzinfo else None)

    return render(request, 'monitor_app/_snapper_cut.html', {
        'scope': scope,
        'requested_at': result.requested_at,
        'actual_snap_time': result.snap_time,
        'snap_age_text': _age_text(
            (result.requested_at - result.snap_time).total_seconds()),
        'coverage': coverage,
        'coverage_notice': coverage_notice,
        'previous_age_text': (
            _age_text((snap.snap_time
                       - previous_snap.snap_time).total_seconds())
            if snap and previous_snap else None),
        'cards': cards,
        'references': references,
    })
