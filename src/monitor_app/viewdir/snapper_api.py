"""Snapper temporal-query REST adapters (snapper-ai PLAN.md Phase 5).

Thin transports over ``snapper_ai.queries``: each endpoint parses and
validates its parameters, calls the generic query, and returns the typed
evidence envelope's serialization unchanged — actual snap times,
schema/policy versions, provenance, hashes, and observer coverage are
the contract, and no adapter may present inferred continuity as fact.

Read-open like the rest of the monitor's read surfaces; errors are
explicit JSON, never empty results.
"""

from django.http import JsonResponse
from django.utils.dateparse import parse_datetime

from snapper_ai.queries import (InvalidQuery, SnapNotFound, SnapperError,
                                changes_between, component_history,
                                context_around, latest, state_at)

from ..snapper_resolvers import annotate_references


def _parse_time(raw, label):
    value = parse_datetime(str(raw or '').strip())
    if value is None:
        raise InvalidQuery(
            f'{label} must be an ISO 8601 datetime, e.g. 2026-07-23T04:00:00Z')
    if value.tzinfo is None:
        raise InvalidQuery(f'{label} must carry an explicit timezone offset')
    return value


def _run(query):
    try:
        result = query()
    except InvalidQuery as e:
        return JsonResponse({'error': str(e)}, status=400)
    except SnapNotFound as e:
        return JsonResponse({'error': str(e)}, status=404)
    except SnapperError as e:
        return JsonResponse({'error': str(e)}, status=500)
    payload = result.as_dict()
    return JsonResponse(payload, json_dumps_params={'default': str})


def snapper_latest(request, scope):
    """GET /api/snapper/<scope>/latest/"""
    return _run(lambda: latest(scope))


def snapper_state_at(request, scope):
    """GET /api/snapper/<scope>/state-at/?time=<ISO 8601>"""
    return _run(lambda: state_at(scope, _parse_time(
        request.GET.get('time'), 'time')))


def snapper_component_history(request, scope):
    """GET /api/snapper/<scope>/history/?component=&start=&end=
    [&include_unchanged=1]"""
    def query():
        component = str(request.GET.get('component') or '').strip()
        if not component:
            raise InvalidQuery('component is required')
        return component_history(
            scope, component,
            _parse_time(request.GET.get('start'), 'start'),
            _parse_time(request.GET.get('end'), 'end'),
            suppress_unchanged_baselines=(
                request.GET.get('include_unchanged') != '1'),
        )
    return _run(query)


def snapper_changes_between(request, scope):
    """GET /api/snapper/<scope>/changes/?start=&end="""
    return _run(lambda: changes_between(
        scope,
        _parse_time(request.GET.get('start'), 'start'),
        _parse_time(request.GET.get('end'), 'end')))


def system_status_history(request):
    """GET /api/system-status/history/?name=&start=&end=&limit=

    Read surface for the append-only health observations — the
    authoritative event stream behind the assessed health component
    (resolver swf-system-status-history).
    """
    from ..models import SystemStatusHistory

    rows = SystemStatusHistory.objects.order_by('-checked_at')
    name = (request.GET.get('name') or '').strip()
    if name:
        rows = rows.filter(name=name)
    raw_start = request.GET.get('start')
    raw_end = request.GET.get('end')
    try:
        if raw_start:
            rows = rows.filter(checked_at__gte=_parse_time(raw_start, 'start'))
        if raw_end:
            rows = rows.filter(checked_at__lt=_parse_time(raw_end, 'end'))
    except InvalidQuery as e:
        return JsonResponse({'error': str(e)}, status=400)
    try:
        limit = min(int(request.GET.get('limit') or 500), 2000)
    except ValueError:
        return JsonResponse({'error': 'limit must be an integer'}, status=400)
    observations = list(rows.values(
        'name', 'category', 'status', 'summary', 'checked_at')[:limit])
    return JsonResponse({'count': len(observations),
                         'observations': observations},
                        json_dumps_params={'default': str})


def snapper_context(request, scope):
    """GET /api/snapper/<scope>/context/?time=<ISO>[&window=seconds]

    State at the instant, changes in the window around it, and event
    references with their SWF resolver transports attached.
    """
    try:
        window = float(request.GET.get('window') or 3600)
        result = context_around(
            scope, _parse_time(request.GET.get('time'), 'time'), window)
    except InvalidQuery as e:
        return JsonResponse({'error': str(e)}, status=400)
    except SnapNotFound as e:
        return JsonResponse({'error': str(e)}, status=404)
    except (SnapperError, ValueError) as e:
        return JsonResponse({'error': str(e)}, status=500)
    payload = result.as_dict()
    payload['references'] = annotate_references(payload['references'])
    return JsonResponse(payload, json_dumps_params={'default': str})
