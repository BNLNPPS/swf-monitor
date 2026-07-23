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
        }
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
    allowed = {key: payload[key] for key in ('curves_off', 'window')
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

    recent = list(snaps[:RECENT_SNAP_LIMIT])
    return render(request, 'monitor_app/snapper.html', {
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
