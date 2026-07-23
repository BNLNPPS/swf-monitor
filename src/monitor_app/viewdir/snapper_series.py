"""Observatory series assembly for the Snapper page.

Host-side, component-aware extraction of plot series from recorded
snaps: numeric curves (the panda component's in-flight job and task
counts, running cores) and categorical state lanes (health, testbed
datataking namespaces), plus recovery-gap spans. The page renders these
directly; the generic evidence semantics hold — every point is a real
snap at its actual snap time, and known gaps are returned for display
rather than painted over.
"""

from django.utils.dateparse import parse_datetime

from snapper_ai.models import SystemSnap

# The observatory plot draws every snap point; windows are bounded so
# assembly stays a bounded read (12-13 snaps/hour at current cadence).
WINDOW_HOURS = {'6h': 6, '24h': 24, '48h': 48, '7d': 168}
DEFAULT_WINDOW = '24h'


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z') if value else None


def _component_data(state, name):
    components = state.get('components') if isinstance(state, dict) else None
    payload = components.get(name) if isinstance(components, dict) else None
    data = payload.get('data') if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _curve_values(scope, state):
    """Numeric curve values for one snap, keyed by curve id."""
    values = {}
    if scope != 'epicprod':
        return values
    panda = _component_data(state, 'panda')
    jobs_now = (panda.get('jobs') or {}).get('in_flight_now') or {}
    tasks_now = (panda.get('tasks') or {}).get('in_flight_now') or {}
    if jobs_now:
        values['jobs_total'] = int(jobs_now.get('total') or 0)
        values['running_cores'] = int(jobs_now.get('running_cores') or 0)
        for status, count in (jobs_now.get('by_status') or {}).items():
            values[f'job_{status}'] = int(count or 0)
    if tasks_now:
        values['tasks_total'] = int(tasks_now.get('total') or 0)
        for status, count in (tasks_now.get('by_status') or {}).items():
            values[f'task_{status}'] = int(count or 0)
    return values


def _lane_values(scope, state):
    """Categorical lane values for one snap, keyed by lane id."""
    lanes = {}
    health = _component_data(state, 'health')
    overall = health.get('overall') or {}
    if overall:
        lanes['health'] = str(overall.get('status') or 'unknown')
    if scope == 'testbed':
        datataking = _component_data(state, 'datataking')
        for namespace, ns_state in sorted(
                (datataking.get('namespaces') or {}).items()):
            ns_state = ns_state if isinstance(ns_state, dict) else {}
            value = str(ns_state.get('state') or 'unknown')
            substate = ns_state.get('substate')
            if substate:
                value = f'{value}/{substate}'
            lanes[f'ns:{namespace}'] = value
    return lanes


def _curve_label(curve_id):
    if curve_id == 'jobs_total':
        return 'in-flight jobs (total)'
    if curve_id == 'tasks_total':
        return 'in-flight tasks (total)'
    if curve_id == 'running_cores':
        return 'running cores'
    if curve_id.startswith('job_'):
        return f'jobs {curve_id[4:]}'
    if curve_id.startswith('task_'):
        return f'tasks {curve_id[5:]}'
    return curve_id


def observatory_series(scope, start, end):
    """Curves, lanes, and gap spans for one scope and window."""
    rows = list(
        SystemSnap.objects
        .filter(scope=scope, snap_time__gte=start, snap_time__lte=end)
        .order_by('snap_time')
        .values('snap_time', 'state', 'recovered_gap_started_at',
                'recovered_gap_start_unknown', 'reasons'))
    boundary = (
        SystemSnap.objects
        .filter(scope=scope, snap_time__lt=start)
        .order_by('-snap_time')
        .values('snap_time', 'state')
        .first())

    curves = {}
    lanes = {}
    gaps = []

    def add_curve_point(curve_id, time_iso, value):
        curve = curves.setdefault(
            curve_id, {'label': _curve_label(curve_id), 'points': []})
        curve['points'].append([time_iso, value])

    def add_lane_point(lane_id, time_iso, value):
        label = lane_id[3:] if lane_id.startswith('ns:') else lane_id
        lane = lanes.setdefault(lane_id, {'label': label, 'points': []})
        points = lane['points']
        if points and points[-1][1] == value:
            return
        points.append([time_iso, value])

    if boundary:
        boundary_iso = _iso(start)
        for curve_id, value in _curve_values(
                scope, boundary['state']).items():
            add_curve_point(curve_id, boundary_iso, value)
        for lane_id, value in _lane_values(scope, boundary['state']).items():
            add_lane_point(lane_id, boundary_iso, value)

    for row in rows:
        time_iso = _iso(row['snap_time'])
        for curve_id, value in _curve_values(scope, row['state']).items():
            add_curve_point(curve_id, time_iso, value)
        for lane_id, value in _lane_values(scope, row['state']).items():
            add_lane_point(lane_id, time_iso, value)
        if row['recovered_gap_started_at'] is not None:
            gaps.append([_iso(row['recovered_gap_started_at']), time_iso,
                         'gap'])
        elif row['recovered_gap_start_unknown']:
            gaps.append([None, time_iso, 'unknown start'])

    return {
        'scope': scope,
        'start': _iso(start),
        'end': _iso(end),
        'snap_count': len(rows),
        'curves': curves,
        'lanes': lanes,
        'gaps': gaps,
    }


def parse_window(request, now):
    """(start, end, window_key) from ?window= or ?start=&end=."""
    from datetime import timedelta

    raw_start = request.GET.get('start')
    raw_end = request.GET.get('end')
    if raw_start and raw_end:
        start = parse_datetime(raw_start)
        end = parse_datetime(raw_end)
        if start and end and start.tzinfo and end.tzinfo and start < end:
            return start, end, 'custom'
    window = request.GET.get('window') or DEFAULT_WINDOW
    hours = WINDOW_HOURS.get(window)
    if hours is None:
        window, hours = DEFAULT_WINDOW, WINDOW_HOURS[DEFAULT_WINDOW]
    return now - timedelta(hours=hours), now, window
