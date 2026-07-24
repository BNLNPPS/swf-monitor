"""Observatory series assembly for the Snapper page.

Host-side, component-aware extraction of plot series from recorded
snaps: numeric curves (the panda component's in-flight job and task
counts, running cores) and categorical state lanes (health, testbed
datataking namespaces), plus recovery-gap spans. The page renders these
directly; the generic evidence semantics hold — every point is a real
snap at its actual snap time, and known gaps are returned for display
rather than painted over.
"""

from zoneinfo import ZoneInfo

from django.utils.dateparse import parse_datetime

from snapper_ai.models import SystemSnap

# All plotted time strings are Eastern wall time: Plotly renders date
# strings literally, and the app presents time in ET everywhere. The
# series carries a true-UTC anchor (end_ms) so the page can convert a
# clicked plot position back to a real instant.
ET_ZONE = ZoneInfo('America/New_York')

# The observatory plot draws every snap point; windows are bounded so
# assembly stays a bounded read (12-13 snaps/hour at current cadence).
WINDOW_HOURS = {'6h': 6, '24h': 24, '48h': 48, '7d': 168, '30d': 720}
DEFAULT_WINDOW = '24h'


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z') if value else None


def _et_naive(value):
    """Eastern wall-time string for plotting (no offset suffix)."""
    if not value:
        return None
    return value.astimezone(ET_ZONE).strftime('%Y-%m-%dT%H:%M:%S')


def _component_data(state, name):
    components = state.get('components') if isinstance(state, dict) else None
    payload = components.get(name) if isinstance(components, dict) else None
    data = payload.get('data') if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _curve_values(scope, state):
    """Numeric curve values for one snap, keyed by curve id."""
    values = {}
    if scope == 'testbed':
        workflow = _component_data(state, 'workflow')
        executions = workflow.get('executions') or {}
        stf_tasks = workflow.get('stf_tasks') or {}
        if executions:
            values['wf_active'] = int(executions.get('active') or 0)
        if stf_tasks:
            values['stf_total'] = int(
                stf_tasks.get('in_flight_total') or 0)
            for key, count in (
                    stf_tasks.get('by_site_status') or {}).items():
                site, _, status = str(key).partition('/')
                values[f'sts_{site}_{status}'] = int(count or 0)
        return values
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
        for ptype, count in (jobs_now.get('by_type') or {}).items():
            values[f'type_{ptype}'] = int(count or 0)
        for ptype, states in (jobs_now.get('by_type_status') or {}).items():
            for status, count in (states or {}).items():
                values[f'ts_{ptype}_{status}'] = int(count or 0)
    if tasks_now:
        values['tasks_total'] = int(tasks_now.get('total') or 0)
        for status, count in (tasks_now.get('by_status') or {}).items():
            values[f'task_{status}'] = int(count or 0)
    return values


def _lane_entries(scope, state):
    """Categorical lane entries for one snap, keyed by lane id.

    Each entry carries the band value (drives color), a hover text with
    the detail behind the color, an ``active`` flag (an inactive lane
    segment renders hollow), and a dedup ``key`` — a namespace's key
    includes its run number, so a new run starts a new segment even when
    the state value repeats (daily runs stay visible).
    """
    entries = {}
    health = _component_data(state, 'health')
    overall = health.get('overall') or {}
    if overall:
        status = str(overall.get('status') or 'unknown')
        reason = str(overall.get('reason') or '').strip()
        counts = overall.get('counts') or {}
        hover = status if not reason else f'{status} — {reason}'
        count_bits = ', '.join(
            f'{k} {v}' for k, v in sorted(counts.items())
            if isinstance(v, int) and v and k != 'total')
        if count_bits:
            hover += f' ({count_bits})'
        entries['health'] = {'value': status, 'hover': hover,
                            'active': True, 'key': f'{status}|{reason}'}
    # Per-check sub-lanes behind the health lane's expandable header.
    # Every check is emitted here; the assembler drops the always-ok
    # ones so the expansion shows only checks with a story in the window.
    for check_name, check in sorted(
            (health.get('checks') or {}).items()):
        check = check if isinstance(check, dict) else {}
        status = str(check.get('status') or 'unknown')
        summary = str(check.get('summary') or '').strip()
        entries[f'check:{check_name}'] = {
            'value': status,
            'hover': f'{status} — {summary}' if summary else status,
            'active': True, 'key': f'{status}|{summary}',
            'parent': 'health'}
    if scope == 'testbed':
        datataking = _component_data(state, 'datataking')
        for namespace, ns_state in sorted(
                (datataking.get('namespaces') or {}).items()):
            ns_state = ns_state if isinstance(ns_state, dict) else {}
            value = str(ns_state.get('state') or 'unknown')
            substate = ns_state.get('substate')
            if substate:
                value = f'{value}/{substate}'
            run = ns_state.get('run_number')
            phase = str(ns_state.get('phase') or '')
            transition = str(ns_state.get('last_transition_at') or '')
            hover = f'run {run} — {phase}/{value}'
            if transition:
                parsed = parse_datetime(transition)
                hover += ' since ' + (
                    parsed.astimezone(ET_ZONE).strftime('%m-%d %H:%M ET')
                    if parsed else transition)
            entries[f'ns:{namespace}'] = {
                'value': value, 'hover': hover,
                # 'run' is the stamped E0-E1 state; 'running' is the
                # fast-processing agent's legacy value.
                'active': str(ns_state.get('state') or '') in ('run', 'running'),
                'key': f'{run}|{value}'}
    return entries


def _curve_label(curve_id):
    if curve_id == 'wf_active':
        return 'workflow executions (running)'
    if curve_id == 'stf_total':
        return 'STF tasks in flight (total)'
    if curve_id.startswith('sts_'):
        remainder = curve_id[4:]
        site, _, status = remainder.rpartition('_')
        return f'{site} · {status}' if site else remainder
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
    if curve_id.startswith('type_'):
        return f'{curve_id[5:]} (in flight)'
    if curve_id.startswith('ts_'):
        remainder = curve_id[3:]
        ptype, _, status = remainder.rpartition('_')
        return f'{ptype} · {status}' if ptype else remainder
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

    def add_lane_point(lane_id, time_iso, entry):
        if lane_id.startswith('ns:'):
            label = lane_id[3:]
        elif lane_id.startswith('check:'):
            label = lane_id[6:]
        else:
            label = lane_id
        lane = lanes.setdefault(lane_id, {'label': label, 'points': []})
        if entry.get('parent'):
            lane['parent'] = entry['parent']
        points = lane['points']
        if points and points[-1].get('key') == entry['key']:
            return
        points.append({'t': time_iso, 'value': entry['value'],
                       'hover': entry['hover'], 'active': entry['active'],
                       'key': entry['key']})

    if boundary:
        boundary_naive = _et_naive(start)
        for curve_id, value in _curve_values(
                scope, boundary['state']).items():
            add_curve_point(curve_id, boundary_naive, value)
        for lane_id, entry in _lane_entries(
                scope, boundary['state']).items():
            add_lane_point(lane_id, boundary_naive, entry)

    for row in rows:
        time_naive = _et_naive(row['snap_time'])
        for curve_id, value in _curve_values(scope, row['state']).items():
            add_curve_point(curve_id, time_naive, value)
        for lane_id, entry in _lane_entries(scope, row['state']).items():
            add_lane_point(lane_id, time_naive, entry)
        if row['recovered_gap_started_at'] is not None:
            gaps.append([_et_naive(row['recovered_gap_started_at']),
                         time_naive, 'gap'])
        elif row['recovered_gap_start_unknown']:
            gaps.append([None, time_naive, 'unknown start'])

    # A check sub-lane earns its place only with a non-ok story in the
    # window; an always-ok check would add a row and say nothing.
    for lane_id in [key for key, lane in lanes.items()
                    if lane.get('parent')
                    and all(point['value'] in ('ok', 'healthy')
                            for point in lane['points'])]:
        del lanes[lane_id]

    return {
        'scope': scope,
        # Plotted strings are Eastern wall time; end_ms is the true-UTC
        # anchor for converting a clicked plot position to an instant.
        'start': _et_naive(start),
        'end': _et_naive(end),
        'end_ms': int(end.timestamp() * 1000),
        'timezone': 'ET',
        'snap_count': len(rows),
        'curves': curves,
        'lanes': lanes,
        'gaps': gaps,
    }


def parse_window(request, now, default_window=DEFAULT_WINDOW):
    """(start, end, window_key) from ?window= or ?start=&end=.

    ``default_window`` lets a signed-in user's remembered preference
    stand in when the URL carries no window.
    """
    from datetime import timedelta

    raw_start = request.GET.get('start')
    raw_end = request.GET.get('end')
    if raw_start and raw_end:
        start = parse_datetime(raw_start)
        end = parse_datetime(raw_end)
        if start and end and start.tzinfo and end.tzinfo and start < end:
            return start, end, 'custom'
    if default_window not in WINDOW_HOURS:
        default_window = DEFAULT_WINDOW
    window = request.GET.get('window') or default_window
    hours = WINDOW_HOURS.get(window)
    if hours is None:
        window, hours = default_window, WINDOW_HOURS[default_window]
    return now - timedelta(hours=hours), now, window
