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


def _span_text(seconds):
    if seconds >= 5400:
        return f'{seconds / 3600:.1f} h'
    if seconds >= 90:
        return f'{seconds / 60:.0f} min'
    return f'{seconds:.0f} s'


def _namespace_run_arcs(start, end, dangle_seconds):
    """Per-namespace workflow-run arcs — THE single source behind both
    the activity lanes and the cut's instant lookup, so the two can
    never disagree. An arc is one run's full activity: first observed
    instant, the end of its datataking window, and its last recorded
    activity (the processing tail), with workflow identity resolved
    through the execution record. The state-event record supplies the
    full arc where it exists; the universal run record covers every
    other workflow. Returns namespace → [arc, ...] ordered by start;
    every registered namespace is present."""
    from django.db.models import Q
    from django.db.models.fields.json import KeyTextTransform

    from ..models import Run, RunState, SystemStateEvent
    from ..workflow_models import Namespace, WorkflowExecution

    events = list(
        SystemStateEvent.objects
        .filter(timestamp__gte=start, timestamp__lte=end)
        .values('run_number', 'timestamp', 'event_type', 'event_data'))

    runs = {}
    for event in events:
        run = runs.setdefault(event['run_number'], {
            'first': event['timestamp'], 'last': event['timestamp'],
            'end_run': None, 'execution': ''})
        run['first'] = min(run['first'], event['timestamp'])
        run['last'] = max(run['last'], event['timestamp'])
        if event['event_type'] == 'end_run':
            run['end_run'] = event['timestamp']
        if not run['execution']:
            data = event['event_data']
            if isinstance(data, dict) and data.get('execution_id'):
                run['execution'] = str(data['execution_id'])

    run_executions = dict(
        RunState.objects
        .annotate(execution_key=KeyTextTransform(
            'execution_id', 'metadata'))
        .exclude(execution_key__isnull=True)
        .values_list('run_number', 'execution_key'))
    for row in (Run.objects
                .filter(Q(end_time__gte=start) | Q(end_time__isnull=True),
                        start_time__lte=end)
                .values('run_number', 'start_time', 'end_time')):
        if row['run_number'] in runs:
            continue
        run = {'first': max(row['start_time'], start),
               'end_run': row['end_time'],
               'execution': run_executions.get(row['run_number'], '')}
        if row['end_time'] is not None:
            run['last'] = min(row['end_time'], end)
        elif ((end - row['start_time']).total_seconds()
                <= dangle_seconds):
            run['last'] = end
            run['end_run'] = None
        else:
            if row['start_time'] < start:
                continue
            run['last'] = run['first']
            run['end_run'] = None
        runs[row['run_number']] = run

    namespaces = dict(
        WorkflowExecution.objects
        .filter(execution_id__in={run['execution']
                                  for run in runs.values()
                                  if run['execution']})
        .values_list('execution_id', 'namespace'))

    arcs = {name: [] for name in
            Namespace.objects.values_list('name', flat=True)}
    for run_number, run in sorted(runs.items(),
                                  key=lambda item: item[1]['first']):
        execution = run['execution']
        parts = execution.rsplit('-', 2)
        namespace = namespaces.get(execution) or 'unknown'
        arcs.setdefault(namespace, []).append({
            'run_number': run_number,
            'workflow': parts[0] if len(parts) == 3 else 'workflow',
            'execution': execution,
            'first': run['first'],
            'end_run': run['end_run'],
            'last': run['last'],
            'dangling': (run['end_run'] is None
                         and (end - run['last']).total_seconds()
                         > dangle_seconds),
        })
    return arcs


def namespace_activity_at(instant, dangle_seconds=12 * 3600):
    """Per-namespace datataking truth at one instant, classified from
    the same arcs the activity lanes draw. The arc window extends past
    the instant so a processing tail observed later still counts as
    ongoing at the instant. Returns namespace → {phase, run_number,
    workflow, execution_id, since} with phase one of 'datataking',
    'processing', 'idle'."""
    from datetime import timedelta

    arcs = _namespace_run_arcs(instant - timedelta(days=30),
                               instant + timedelta(days=30),
                               dangle_seconds)
    out = {}
    for namespace, runs in arcs.items():
        entry = {'phase': 'idle', 'run_number': None, 'workflow': '',
                 'execution_id': '', 'since': None}
        current = None
        for arc in runs:
            if arc['first'] <= instant:
                current = arc
        if current is not None:
            entry.update({'run_number': current['run_number'],
                          'workflow': current['workflow'],
                          'execution_id': current['execution']})
            if (current['end_run'] is not None
                    and instant <= current['end_run']):
                entry.update({'phase': 'datataking',
                              'since': current['first']})
            elif instant <= current['last']:
                entry.update(
                    {'phase': ('processing'
                               if current['end_run'] is not None
                               else 'datataking'),
                     'since': current['end_run'] or current['first']})
            else:
                entry.update({'phase': 'idle', 'since': current['last']})
        out[namespace] = entry
    return out


def _run_activity_lanes(start, end, dangle_seconds):
    """Activity lane segments rendered from the shared per-namespace run
    arcs: a solid datataking tile opening into a lighter processing
    tail, hatched when the run never recorded an end. Idle namespaces
    keep an empty lane (a grey track on the plot)."""
    lanes = {}
    for namespace, runs in _namespace_run_arcs(
            start, end, dangle_seconds).items():
        segments = lanes.setdefault(namespace, [])
        for arc in runs:
            ident = (f"{arc['workflow']} · run {arc['run_number']}"
                     + (f" · {arc['execution']}" if arc['execution']
                        else ''))
            started = arc['first'].astimezone(ET_ZONE).strftime(
                '%m-%d %H:%M ET')
            if arc['dangling']:
                segments.append({
                    't0': _et_naive(arc['first']),
                    't1': _et_naive(arc['last']), 'value': 'run',
                    'hover': (f'{ident} — started {started}, no '
                              'recorded end; last activity '
                              + arc['last'].astimezone(
                                  ET_ZONE).strftime('%m-%d %H:%M ET')),
                    'open_end': True})
                continue
            datataking_end = arc['end_run'] or arc['last']
            total = _span_text(
                (arc['last'] - arc['first']).total_seconds())
            hover = f'{ident} — started {started}, active {total}'
            segments.append({
                't0': _et_naive(arc['first']),
                't1': _et_naive(datataking_end), 'value': 'run',
                'hover': f'{hover} · datataking window',
                'open_end': False})
            if arc['last'] > datataking_end:
                segments.append({
                    't0': _et_naive(datataking_end),
                    't1': _et_naive(arc['last']), 'value': 'processing',
                    'hover': f'{hover} · processing tail',
                    'open_end': False})
    return lanes


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

    if boundary:
        start_naive = _et_naive(start)
        for curve_id, value in _curve_values(
                scope, boundary['state']).items():
            add_curve_point(curve_id, start_naive, value)
        for lane_id, entry in _lane_entries(
                scope, boundary['state']).items():
            add_lane_point(lane_id, start_naive, entry)

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

    # Episodic namespace lanes from the canonical workflow-execution
    # record: discrete activity with workflow identity, over a grey
    # idle track.
    if scope == 'testbed':
        for namespace, segments in sorted(
                _run_activity_lanes(start, end, dangle_seconds).items()):
            lanes[f'ns:{namespace}'] = {'label': namespace,
                                        'segments': segments}

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
