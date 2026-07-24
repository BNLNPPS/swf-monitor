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
    """Continuous lane entries for one snap (health and its checks),
    keyed by lane id: the band value (drives color), hover text, and a
    dedup ``key``. Datataking namespaces are episodic, not continuous —
    they are assembled into discrete run periods by the series builder.
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
    return entries


# Terminal datataking states end a run period; the terminal value itself
# is never drawn — the bar simply stops.
_TERMINAL_RUN_STATES = ('ended', 'expired', 'abandoned')


def _datataking_observations(state):
    """Per-namespace run observation for one snap."""
    out = {}
    datataking = _component_data(state, 'datataking')
    for namespace, ns_state in sorted(
            (datataking.get('namespaces') or {}).items()):
        ns = ns_state if isinstance(ns_state, dict) else {}
        value = str(ns.get('state') or 'unknown')
        substate = ns.get('substate')
        if substate:
            value = f'{value}/{substate}'
        transition = str(ns.get('last_transition_at') or '')
        hover_since = ''
        if transition:
            parsed = parse_datetime(transition)
            hover_since = ' since ' + (
                parsed.astimezone(ET_ZONE).strftime('%m-%d %H:%M ET')
                if parsed else transition)
        out[namespace] = {
            'value': value,
            'run': ns.get('run_number'),
            'hover': (f"run {ns.get('run_number')} — "
                      f"{ns.get('phase') or ''}/{value}{hover_since}"),
            'terminal': str(ns.get('state') or '') in _TERMINAL_RUN_STATES,
        }
    return out


class _RunEpisodes:
    """Assemble one namespace lane as discrete run periods.

    A bar spans a run from its first observation to its terminal
    transition; idle time between runs is blank, never painted. A run
    that dangles — no terminal state and no transition for longer than
    the threshold — is truncated at its last observed change and marked
    open-ended, so stale bookkeeping cannot paint hours of false state.
    """

    def __init__(self, label, dangle_seconds):
        self.label = label
        self.dangle_seconds = dangle_seconds
        self.segments = []
        self.open = None    # {'run', 'value', 'hover', 'seg_start',
                            #  'last_change'} — times as (dt, naive) pairs

    def _flush_segment(self, end_pair, open_end=False):
        hover = self.open['hover']
        if open_end:
            hover += ' · dangling: no further transitions recorded'
        self.segments.append({
            't0': self.open['seg_start'][1], 't1': end_pair[1],
            'value': self.open['value'], 'hover': hover,
            'open_end': open_end})

    def _close(self, at_pair, dangling):
        """End the open episode: at its last change when it dangled, at
        the given time otherwise."""
        if dangling:
            self._flush_segment(self.open['last_change'], open_end=True)
        else:
            self._flush_segment(at_pair)
        self.open = None

    def observe(self, time_pair, obs):
        if self.open is not None and obs['run'] != self.open['run']:
            # A new run appeared while the old one never terminated.
            self._close(time_pair, dangling=True)
        if obs['terminal']:
            if self.open is not None:
                dangling = ((time_pair[0] - self.open['last_change'][0])
                            .total_seconds() > self.dangle_seconds)
                self._close(time_pair, dangling)
            return
        if self.open is None:
            self.open = {'run': obs['run'], 'value': obs['value'],
                         'hover': obs['hover'], 'seg_start': time_pair,
                         'last_change': time_pair}
            return
        if obs['value'] != self.open['value']:
            self._flush_segment(time_pair)
            self.open.update({'value': obs['value'], 'hover': obs['hover'],
                              'seg_start': time_pair,
                              'last_change': time_pair})

    def finish(self, end_pair):
        if self.open is not None:
            dangling = ((end_pair[0] - self.open['last_change'][0])
                        .total_seconds() > self.dangle_seconds)
            self._close(end_pair, dangling)
        return self.segments


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
    from ..models import SysConfig

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

    dangle_seconds = 3600.0 * float(
        SysConfig.get_setting('snapper_lane_dangle_hours', 12))
    curves = {}
    lanes = {}
    episodes = {}
    gaps = []

    def add_curve_point(curve_id, time_iso, value):
        curve = curves.setdefault(
            curve_id, {'label': _curve_label(curve_id), 'points': []})
        curve['points'].append([time_iso, value])

    def add_lane_point(lane_id, time_iso, entry):
        label = lane_id[6:] if lane_id.startswith('check:') else lane_id
        lane = lanes.setdefault(lane_id, {'label': label, 'points': []})
        if entry.get('parent'):
            lane['parent'] = entry['parent']
        points = lane['points']
        if points and points[-1].get('key') == entry['key']:
            return
        points.append({'t': time_iso, 'value': entry['value'],
                       'hover': entry['hover'], 'active': entry['active'],
                       'key': entry['key']})

    def observe_datataking(time_pair, state):
        if scope != 'testbed':
            return
        for namespace, obs in _datataking_observations(state).items():
            lane = episodes.setdefault(
                namespace, _RunEpisodes(namespace, dangle_seconds))
            lane.observe(time_pair, obs)

    if boundary:
        start_pair = (start, _et_naive(start))
        for curve_id, value in _curve_values(
                scope, boundary['state']).items():
            add_curve_point(curve_id, start_pair[1], value)
        for lane_id, entry in _lane_entries(
                scope, boundary['state']).items():
            add_lane_point(lane_id, start_pair[1], entry)
        observe_datataking(start_pair, boundary['state'])

    for row in rows:
        time_pair = (row['snap_time'], _et_naive(row['snap_time']))
        for curve_id, value in _curve_values(scope, row['state']).items():
            add_curve_point(curve_id, time_pair[1], value)
        for lane_id, entry in _lane_entries(scope, row['state']).items():
            add_lane_point(lane_id, time_pair[1], entry)
        observe_datataking(time_pair, row['state'])
        if row['recovered_gap_started_at'] is not None:
            gaps.append([_et_naive(row['recovered_gap_started_at']),
                         time_pair[1], 'gap'])
        elif row['recovered_gap_start_unknown']:
            gaps.append([None, time_pair[1], 'unknown start'])

    # A check sub-lane earns its place only with a non-ok story in the
    # window; an always-ok check would add a row and say nothing.
    for lane_id in [key for key, lane in lanes.items()
                    if lane.get('parent')
                    and all(point['value'] in ('ok', 'healthy')
                            for point in lane['points'])]:
        del lanes[lane_id]

    # Episodic namespace lanes: discrete run periods, blank when idle.
    # A namespace with no activity in the window keeps its lane — a
    # blank grey track says "present and inactive", absence says nothing.
    end_pair = (end, _et_naive(end))
    for namespace, builder in sorted(episodes.items()):
        lanes[f'ns:{namespace}'] = {'label': namespace,
                                    'segments': builder.finish(end_pair)}

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
