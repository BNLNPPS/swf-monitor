"""Host-side Snapper scope providers for the swf platform.

Everything experiment-specific about the Snapper surfaces lives here
and registers with the agnostic snapper_ai core (snapper_ai.registry):
the epicprod and testbed scopes' curve extraction, labels and families,
the panda / workflow / datataking component cards with their links into
monitor pages, the testbed run-arc activity lanes, reference
resolution, and the host service hooks (preferences, configuration,
scheduler status, health page). Registered from MonitorAppConfig.ready().
"""

from snapper_ai.registry import ScopeProvider, register, register_hooks
from snapper_ai.series import ET_ZONE, _component_data, _et_naive, _span_text
from snapper_ai.views import cut_chip, cut_delta

# The host template rendering the provider card kinds below.
CARD_TEMPLATE = 'monitor_app/_snapper_cards.html'


# ── Testbed run arcs: the single source behind lanes and the cut ─────────

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

    from .models import Run, RunState, SystemStateEvent
    from .workflow_models import Namespace, WorkflowExecution

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
    the same arcs the activity lanes draw. Returns namespace → {phase,
    run_number, workflow, execution_id, since} with phase one of
    'datataking', 'processing', 'idle'."""
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


# ── Curve extraction and vocabulary ──────────────────────────────────────

def _epicprod_curve_values(state):
    values = {}
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


def _testbed_curve_values(state):
    values = {}
    workflow = _component_data(state, 'workflow')
    executions = workflow.get('executions') or {}
    stf_tasks = workflow.get('stf_tasks') or {}
    if executions:
        values['wf_active'] = int(executions.get('active') or 0)
    if stf_tasks:
        values['stf_total'] = int(stf_tasks.get('in_flight_total') or 0)
        for key, count in (stf_tasks.get('by_site_status') or {}).items():
            site, _, status = str(key).partition('/')
            values[f'sts_{site}_{status}'] = int(count or 0)
    return values


def _epicprod_curve_label(curve_id):
    if curve_id == 'jobs_total':
        return 'jobs total'
    if curve_id == 'tasks_total':
        return 'tasks total'
    if curve_id == 'running_cores':
        return 'running cores'
    if curve_id.startswith('job_'):
        return f'jobs {curve_id[4:]}'
    if curve_id.startswith('task_'):
        return f'tasks {curve_id[5:]}'
    # Curves in the in-flight families are in flight by construction —
    # the family title says it once; per-curve qualifiers are noise.
    if curve_id.startswith('type_'):
        return curve_id[5:]
    if curve_id.startswith('ts_'):
        remainder = curve_id[3:]
        ptype, _, status = remainder.rpartition('_')
        return f'{ptype} · {status}' if ptype else remainder
    return None


def _testbed_curve_label(curve_id):
    if curve_id == 'wf_active':
        return 'workflow executions (running)'
    if curve_id == 'stf_total':
        return 'STF tasks total'
    if curve_id.startswith('sts_'):
        remainder = curve_id[4:]
        site, _, status = remainder.rpartition('_')
        return f'{site} · {status}' if site else remainder
    return None


EPICPROD_GROUPS = (
    {'name': 'In-flight jobs', 'prefixes': ['job_'],
     'ids': ['jobs_total', 'running_cores']},
    {'name': 'Tasks', 'prefixes': ['task_'], 'ids': ['tasks_total']},
    {'name': 'In-flight job types', 'prefixes': ['type_'], 'ids': []},
    {'name': 'Type × state', 'prefixes': ['ts_'], 'ids': []},
)

TESTBED_GROUPS = (
    {'name': 'Workflows', 'prefixes': ['wf_'], 'ids': []},
    {'name': 'STF tasks', 'prefixes': ['sts_'], 'ids': ['stf_total']},
)


# ── Component cards ──────────────────────────────────────────────────────

def _panda_card(data, previous_data, ctx):
    jobs_now = (data.get('jobs') or {}).get('in_flight_now') or {}
    prev_jobs = ((previous_data.get('jobs') or {})
                 .get('in_flight_now') or {})
    tasks_now = (data.get('tasks') or {}).get('in_flight_now') or {}
    prev_tasks = ((previous_data.get('tasks') or {})
                  .get('in_flight_now') or {})

    def stat(label, value, previous):
        return {'label': label,
                'value': value if value is not None else '—',
                'delta': cut_delta(value, previous)}

    headline = [
        stat('running jobs', jobs_now.get('running_jobs'),
             prev_jobs.get('running_jobs')),
        stat('running cores', jobs_now.get('running_cores'),
             prev_jobs.get('running_cores')),
        stat('in-flight jobs', jobs_now.get('total'),
             prev_jobs.get('total')),
        stat('queued (activated)',
             (jobs_now.get('by_status') or {}).get('activated'),
             (prev_jobs.get('by_status') or {}).get('activated')),
        stat('in-flight tasks', tasks_now.get('total'),
             prev_tasks.get('total')),
    ]
    types = sorted((jobs_now.get('by_type') or {}).items(),
                   key=lambda item: -item[1])
    type_states = []
    for ptype, states in sorted(
            (jobs_now.get('by_type_status') or {}).items()):
        for status, count in sorted((states or {}).items()):
            previous = ((prev_jobs.get('by_type_status') or {})
                        .get(ptype) or {}).get(status)
            type_states.append({
                'label': f'{ptype} · {status}', 'value': count,
                'delta': cut_delta(count, previous)})
    return {'kind': 'panda', 'template': CARD_TEMPLATE,
            'headline': headline, 'types': types,
            'type_states': type_states}


def _workflow_card(data, previous_data, ctx):
    from .snapper_workflow import STF_PROCESSING_TYPE

    executions = data.get('executions') or {}
    prev_exec = previous_data.get('executions') or {}
    stf = data.get('stf_tasks') or {}
    prev_stf = previous_data.get('stf_tasks') or {}

    def stat(label, value, previous):
        return {'label': label,
                'value': value if value is not None else '—',
                'delta': cut_delta(value, previous)}

    headline = [
        stat('executions running', executions.get('active'),
             prev_exec.get('active')),
        stat('executions started (24h)', executions.get('started_24h'),
             prev_exec.get('started_24h')),
        stat('STF tasks in flight', stf.get('in_flight_total'),
             prev_stf.get('in_flight_total')),
    ]
    by_workflow = sorted(
        (executions.get('by_workflow') or {}).items(),
        key=lambda item: -item[1])
    site_states = []
    for key, count in sorted((stf.get('by_site_status') or {}).items()):
        site, _, status = str(key).partition('/')
        previous = (prev_stf.get('by_site_status') or {}).get(key)
        site_states.append({'site': site, 'status': status, 'value': count,
                            'delta': cut_delta(count, previous)})
    return {'kind': 'workflow', 'template': CARD_TEMPLATE,
            'headline': headline, 'by_workflow': by_workflow,
            'site_states': site_states,
            'stf_processing_type': STF_PROCESSING_TYPE}


def _datataking_card(data, previous_data, ctx):
    # One truth: at a reference instant (the cut, or the snap's own
    # time) the rows come from the run record, like the lanes; the
    # snap's recorded entry stays in the card's audit document.
    requested_at = ctx.get('requested_at')
    if requested_at is not None:
        rows = [
            {'namespace': namespace,
             'chip': cut_chip(info['phase']),
             'run_number': info['run_number'],
             'phase': info['workflow'],
             'since': (info['since'].isoformat() if info['since'] else '')}
            for namespace, info in sorted(
                namespace_activity_at(requested_at).items())
        ]
    else:
        rows = [
            {'namespace': namespace,
             'chip': cut_chip(
                 f"{ns.get('state')}"
                 + (f"/{ns.get('substate')}" if ns.get('substate') else '')),
             'run_number': ns.get('run_number'),
             'phase': ns.get('phase'),
             'since': ns.get('last_transition_at')}
            for namespace, ns in sorted(
                (data.get('namespaces') or {}).items())
            for ns in [ns if isinstance(ns, dict) else {}]
        ]
    return {'kind': 'datataking', 'template': CARD_TEMPLATE,
            'namespaces': rows}


# ── Host service hooks ───────────────────────────────────────────────────

SNAPPER_PREFS_KEY = 'snapper'


def _prefs_get(username, scope):
    from .models import UserPreference

    row = UserPreference.objects.filter(username=username).first()
    section = (row.prefs or {}).get(SNAPPER_PREFS_KEY) if row else None
    per_scope = (section or {}).get(scope) if isinstance(section, dict) \
        else None
    return per_scope if isinstance(per_scope, dict) else {}


def _prefs_set(username, scope, values):
    from .models import UserPreference

    row, _ = UserPreference.objects.get_or_create(username=username)
    prefs = row.prefs if isinstance(row.prefs, dict) else {}
    section = prefs.get(SNAPPER_PREFS_KEY)
    if not isinstance(section, dict):
        section = {}
    per_scope = section.get(scope)
    if not isinstance(per_scope, dict):
        per_scope = {}
    per_scope.update(values)
    section[scope] = per_scope
    prefs[SNAPPER_PREFS_KEY] = section
    row.prefs = prefs
    row.save()


def _config_get(key, default=None):
    from .models import SysConfig

    return SysConfig.get_setting(key, default)


def _scheduler_status(scope):
    from .models import SystemStatus

    return SystemStatus.objects.filter(
        name=f'snapper-{scope}-scheduler').first()


def _health_url():
    from django.urls import reverse

    return reverse('monitor_app:system_status')


def register_snapper_providers():
    """Register the swf scopes and host hooks with the snapper core.

    Called from MonitorAppConfig.ready(); idempotent by construction
    (registration replaces by scope/hook name).
    """
    from .snapper_resolvers import annotate_references

    register(ScopeProvider(
        scope='epicprod',
        label='epicprod',
        curve_values=_epicprod_curve_values,
        curve_label=_epicprod_curve_label,
        curve_groups=EPICPROD_GROUPS,
        component_cards={'panda': _panda_card},
        annotate_references=annotate_references,
    ))
    register(ScopeProvider(
        scope='testbed',
        label='Testbed',
        curve_values=_testbed_curve_values,
        curve_label=_testbed_curve_label,
        curve_groups=TESTBED_GROUPS,
        episodic_lanes=_run_activity_lanes,
        activity_at=namespace_activity_at,
        component_cards={'workflow': _workflow_card,
                         'datataking': _datataking_card},
        annotate_references=annotate_references,
    ))
    register_hooks(
        prefs_get=_prefs_get,
        prefs_set=_prefs_set,
        config_get=_config_get,
        scheduler_status=_scheduler_status,
        health_url=_health_url,
    )
