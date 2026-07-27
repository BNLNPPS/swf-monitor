"""Host-side Snapper scope providers for the swf platform.

Everything experiment-specific about the Snapper surfaces lives here
and registers with the agnostic snapper_ai core (snapper_ai.registry):
the epicprod and testbed scopes' curve extraction, labels and families,
the panda / workflow / datataking component cards with their links into
monitor pages, the testbed run-arc activity lanes, reference
resolution, and the host service hooks (preferences, configuration,
scheduler status, health page). Registered from MonitorAppConfig.ready().
"""

from snapper_ai.presentation import (ET_ZONE, component_data, cut_chip,
                                     cut_delta, et_naive, span_text)
from snapper_ai.registry import ScopeProvider, register, register_hooks

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


def _arc_summary(exec_row):
    """One-line story for the numbered activity table, from the
    execution record: STF volume and the decision-box plan."""
    if not exec_row:
        return ''
    params, executed_by = exec_row
    sim = params.get('simulation') or {}
    pp = params.get('prompt_processing') or {}
    bits = []
    try:
        bits.append(f"{int(sim.get('stf_count')) * int(sim.get('physics_period_count'))} STF")
    except (TypeError, ValueError):
        pass
    if pp.get('decision_box_enabled'):
        sites = ', '.join(str(s) for s in (pp.get('decision_box_sites') or []))
        policy = str(pp.get('decision_box_policy') or '')
        bits.append(f'decision box ({policy}) → {sites}')
    if executed_by:
        bits.append(f'by {executed_by}')
    return ' · '.join(bits)


def _run_activity_lanes(start, end, dangle_seconds):
    """Activity lane segments rendered from the shared per-namespace run
    arcs: a solid datataking tile opening into a lighter processing
    tail, hatched when the run never recorded an end. Idle namespaces
    keep an empty lane (a grey track on the plot). Segments carry the
    run number as the activity key, with a flag and story summary on
    the leading tile for the numbered activity table."""
    from .workflow_models import WorkflowExecution
    arcs_by_namespace = _namespace_run_arcs(start, end, dangle_seconds)
    exec_ids = {arc['execution'] for runs in arcs_by_namespace.values()
                for arc in runs if arc['execution']}
    exec_rows = {
        row[0]: (row[1] or {}, row[2] or '')
        for row in WorkflowExecution.objects
        .filter(execution_id__in=exec_ids)
        .values_list('execution_id', 'parameter_values', 'executed_by')}
    lanes = {}
    for namespace, runs in arcs_by_namespace.items():
        segments = lanes.setdefault(namespace, [])
        for arc in runs:
            ident = (f"{arc['workflow']} · run {arc['run_number']}"
                     + (f" · {arc['execution']}" if arc['execution']
                        else ''))
            started = arc['first'].astimezone(ET_ZONE).strftime(
                '%m-%d %H:%M ET')
            flag = f"{arc['workflow']} · run {arc['run_number']}"
            key = str(arc['run_number'])
            summary = _arc_summary(exec_rows.get(arc['execution']))
            if arc['dangling']:
                segments.append({
                    't0': et_naive(arc['first']),
                    't1': et_naive(arc['last']), 'value': 'run',
                    'hover': (f'{ident} — started {started}, no '
                              'recorded end; last activity '
                              + arc['last'].astimezone(
                                  ET_ZONE).strftime('%m-%d %H:%M ET')),
                    'open_end': True, 'flag': flag, 'key': key,
                    'summary': summary})
                continue
            datataking_end = arc['end_run'] or arc['last']
            total = span_text(
                (arc['last'] - arc['first']).total_seconds())
            hover = f'{ident} — started {started}, active {total}'
            segments.append({
                't0': et_naive(arc['first']),
                't1': et_naive(datataking_end), 'value': 'run',
                'hover': f'{hover} · datataking window',
                'open_end': False, 'flag': flag, 'key': key,
                'summary': summary})
            if arc['last'] > datataking_end:
                segments.append({
                    't0': et_naive(datataking_end),
                    't1': et_naive(arc['last']), 'value': 'processing',
                    'hover': f'{hover} · processing tail',
                    'open_end': False, 'key': key})
    return lanes


# ── Curve extraction and vocabulary ──────────────────────────────────────

_PC_CACHE = {'at': None, 'requestors': {}, 'keys': {}}


def _pc_cache():
    """pc label -> requestor labels and identity key, cached briefly:
    series assembly calls curve extraction once per snap, and lens
    membership is current-state (lenses apply retroactively)."""
    from django.utils import timezone

    from pcs.models import PhysicsConfig

    now = timezone.now()
    if (_PC_CACHE['at'] is None
            or (now - _PC_CACHE['at']).total_seconds() > 60):
        requestors, keys = {}, {}
        for label, groups, key in PhysicsConfig.objects.values_list(
                'label', 'requestors', 'config_key'):
            requestors[label] = list(groups or [])
            keys[label] = key
        _PC_CACHE.update({'requestors': requestors, 'keys': keys,
                          'at': now})
    return _PC_CACHE


def _requestor_map():
    return _pc_cache()['requestors']


def _delivery_curve_values(state):
    """Delivery curves from the DAILY arrivals record only (leaves
    carrying arrived_files): the per-PC daily bumps (the quilt) and
    the cumulative series, both on the registered basis throughout.
    The live placed-basis component feeds cut cards, never curves, so
    the plotted series ends at the last complete day."""
    values = {}
    delivery = component_data(state, 'delivery')
    requestors = _requestor_map() if delivery.get('campaigns') else {}
    for campaign, block in (delivery.get('campaigns') or {}).items():
        totals = block.get('totals') or {}
        if 'arrived_files' not in totals:
            continue  # the live placed-basis component: cards only
        tag = campaign.replace('.', '_')
        values[f'dlvc_{tag}__total'] = int(totals.get('cum_files') or 0)
        cum_by_group = {}
        for pc, leaf in (block.get('leaves') or {}).items():
            values[f'dlvq_{tag}_{pc}'] = int(
                leaf.get('arrived_files') or 0)
            cum = int(leaf.get('cum_files') or 0)
            if cum:
                for group in requestors.get(pc) or ['Unassigned']:
                    cum_by_group[group] = cum_by_group.get(group, 0) + cum
        for group, cum in cum_by_group.items():
            values[f'dlvc_{tag}_{group}'] = cum
    return values


def _epicprod_curve_values(state):
    values = {}
    panda = component_data(state, 'panda')
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
    values.update(_delivery_curve_values(state))
    return values


def _testbed_curve_values(state):
    values = {}
    workflow = component_data(state, 'workflow')
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


def _delivery_curve_parts(curve_id):
    """(campaign, remainder) from a dlvq_/dlvc_ curve id; campaign
    tags serialize dots as underscores (26_07)."""
    remainder = curve_id.split('_', 1)[1]
    tag, _, rest = remainder.partition('_')
    while rest and rest[0].isdigit():
        extra, _, rest = rest.partition('_')
        tag = f'{tag}_{extra}'
    return tag.replace('_', '.'), rest.strip('_')


def _epicprod_curve_label(curve_id):
    if curve_id.startswith('dlvq_'):
        campaign, pc = _delivery_curve_parts(curve_id)
        key = _pc_cache()['keys'].get(pc, '')
        return f'{pc} {key}' if key else pc
    if curve_id.startswith('dlvc_'):
        campaign, group = _delivery_curve_parts(curve_id)
        return (f'{campaign} cumulative files' if group in ('', 'total')
                else f'{campaign} {group} cumulative')
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


_CAMPAIGN_START_CACHE = {'at': None, 'starts': {}}


def _campaign_delivery_starts():
    """Campaign name -> first recorded delivery activity, from the
    daily backfill snaps (small, bounded read), cached for an hour.
    The campaign focus view clamps its window here: the day count runs
    from when the campaign began delivering, never into the void
    before it."""
    from django.utils import timezone

    from snapper_ai.models import SystemSnap

    now = timezone.now()
    if (_CAMPAIGN_START_CACHE['at'] is not None
            and (now - _CAMPAIGN_START_CACHE['at']).total_seconds() < 3600):
        return _CAMPAIGN_START_CACHE['starts']
    starts = {}
    rows = (SystemSnap.objects
            .filter(scope='epicprod', capture_policy='backfill-v1')
            .order_by('snap_time')
            .values_list('snap_time', 'state'))
    for snap_time, state in rows:
        campaigns = (((state or {}).get('components') or {})
                     .get('delivery') or {}).get('data') or {}
        for name, block in (campaigns.get('campaigns') or {}).items():
            if name in starts:
                continue
            totals = block.get('totals') or {}
            if int(totals.get('cum_files')
                   or totals.get('files') or 0) > 0:
                starts[name] = snap_time
    _CAMPAIGN_START_CACHE['starts'] = starts
    _CAMPAIGN_START_CACHE['at'] = now
    return starts


def _delivery_focus_view():
    """The Campaign focus tab: the report narrowed to one campaign's
    delivery — its family only, the delivery card in the cut, the
    window floored at the campaign's first delivery."""
    from datetime import timedelta

    try:
        from swf_epicprod.analytics.rollup import resolve_target_campaigns
        campaigns = sorted(resolve_target_campaigns(), reverse=True)
    except Exception:                                       # noqa: BLE001
        return None
    if not campaigns:
        return None
    starts = _campaign_delivery_starts()
    return {
        'param': 'campaign',
        'label': 'Campaign',
        'default': campaigns[0],
        'options': [
            {'value': name, 'label': name,
             'families': [f'Arrivals {name}', f'Cumulative {name}'],
             'component': 'delivery',
             'collapse_below': 0.01,
             'start': (starts[name] - timedelta(hours=12))
                      if name in starts else None}
            for name in campaigns],
    }


def _delivery_groups():
    """Two curve families per target campaign, resolved at
    registration: the per-PC daily arrivals quilt (stacked areas, one
    color per configuration, no per-curve tick boxes) and the
    cumulative series (off by default — the quilt is the display).
    A resolution failure yields no delivery families rather than
    blocking registration."""
    try:
        from swf_epicprod.analytics.rollup import resolve_target_campaigns
        campaigns = resolve_target_campaigns()
    except Exception:                                       # noqa: BLE001
        return ()
    groups = []
    for name in campaigns:
        tag = name.replace('.', '_')
        groups.append({'name': f'Arrivals {name}',
                       'prefixes': [f'dlvq_{tag}_'], 'ids': [],
                       'stacked': True, 'compact': True})
        groups.append({'name': f'Cumulative {name}',
                       'prefixes': [f'dlvc_{tag}_'], 'ids': [],
                       'default_off': True})
    return tuple(groups)

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
    return {'kind': 'panda', 'headline': headline, 'types': types,
            'type_states': type_states}


def _delivery_card(data, previous_data, ctx):
    """The delivery cut card. On a daily-record snap (the quilt), the
    breakdown of that day: what arrived, per configuration, with
    cumulative standing. On a live placed-basis snap, the placement
    totals with deltas and the top configurations. Full lists live on
    the campaign plan page."""
    from django.urls import reverse

    cache = _pc_cache()
    requestors = cache['requestors']
    keys = cache['keys']
    campaigns = []
    for name, block in sorted((data.get('campaigns') or {}).items()):
        totals = block.get('totals') or {}
        previous_totals = (((previous_data.get('campaigns') or {})
                            .get(name) or {}).get('totals') or {})
        if 'arrived_files' in totals:
            leaves = block.get('leaves') or {}
            day_pcs = []
            for pc, leaf in sorted(
                    leaves.items(),
                    key=lambda kv: -int(kv[1].get('arrived_files') or 0)):
                arrived = int(leaf.get('arrived_files') or 0)
                if not arrived:
                    continue
                day_pcs.append({
                    'label': pc,
                    'identity': keys.get(pc, ''),
                    'url': reverse('pcs:pcs_config_detail', args=[pc]),
                    'groups': ', '.join(requestors.get(pc)
                                        or ['Unassigned']),
                    'arrived': arrived,
                    'cum': int(leaf.get('cum_files') or 0),
                    'expected': leaf.get('expected'),
                    'tier': leaf.get('tier') or '',
                })
            campaigns.append({
                'name': name,
                'day_pcs': day_pcs,
                'headline': [
                    {'label': 'files arrived this day',
                     'value': totals.get('arrived_files'), 'delta': None},
                    {'label': 'cumulative files',
                     'value': totals.get('cum_files'),
                     'delta': cut_delta(totals.get('cum_files'),
                                        previous_totals.get('cum_files'))},
                    {'label': 'cumulative TB',
                     'value': round(
                         (totals.get('cum_bytes') or 0) / 1e12, 1),
                     'delta': None},
                    {'label': 'configurations delivering',
                     'value': len(day_pcs), 'delta': None},
                ],
                'plan_url': (reverse('pcs:pcs_campaign_plan')
                             + f'?campaign={name}'),
            })
            continue
        by_group = {}
        for pc, leaf in (block.get('leaves') or {}).items():
            files = int(leaf.get('files') or 0)
            if not files:
                continue
            for group in requestors.get(pc) or ['Unassigned']:
                by_group[group] = by_group.get(group, 0) + files
        # The PC drill: the campaign's configurations by files placed,
        # in physics-configuration terms — pc label linked to its page,
        # per-group attribution, target beside delivery. Bounded to the
        # top rows; the plan page is the full list.
        pcs = []
        for pc, leaf in sorted(
                (block.get('leaves') or {}).items(),
                key=lambda kv: -int(kv[1].get('files') or 0))[:10]:
            if not int(leaf.get('files') or 0):
                continue
            pcs.append({
                'label': pc,
                'url': reverse('pcs:pcs_config_detail', args=[pc]),
                'groups': ', '.join(requestors.get(pc)
                                    or ['Unassigned']),
                'files': int(leaf.get('files') or 0),
                'expected': leaf.get('expected'),
                'tier': leaf.get('tier') or '',
            })
        campaigns.append({
            'name': name,
            'pcs': pcs,
            'headline': [
                {'label': 'configurations',
                 'value': totals.get('configs'),
                 'delta': cut_delta(totals.get('configs'),
                                    previous_totals.get('configs'))},
                {'label': 'with targets',
                 'value': totals.get('with_target'),
                 'delta': cut_delta(totals.get('with_target'),
                                    previous_totals.get('with_target'))},
                {'label': 'files placed',
                 'value': totals.get('files'),
                 'delta': cut_delta(totals.get('files'),
                                    previous_totals.get('files'))},
                {'label': 'TB placed',
                 'value': round((totals.get('bytes') or 0) / 1e12, 1),
                 'delta': None},
            ],
            'groups': sorted(by_group.items(), key=lambda kv: -kv[1]),
            'plan_url': (reverse('pcs:pcs_campaign_plan')
                         + f'?campaign={name}'),
        })
    return {'kind': 'delivery', 'campaigns': campaigns}


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
    return {'kind': 'workflow',
            'headline': headline, 'by_workflow': by_workflow,
            'site_states': site_states,
            'stf_processing_type': STF_PROCESSING_TYPE}


def _stf_tasks_for_run(run_number):
    """The run's STF prompt-processing tasks with file progress, from
    the PanDA mirror (jedi_tasks joined to input-dataset file counts)."""
    from django.db import connections
    from django.urls import reverse

    from .panda.constants import PANDA_SCHEMA
    sql = f"""
        SELECT t."jeditaskid", COALESCE(t."site", ''),
               COALESCE(t."status", ''),
               COALESCE(d."nfiles", 0), COALESCE(d."nfilesfinished", 0),
               COALESCE(d."nfilesfailed", 0)
        FROM "{PANDA_SCHEMA}"."jedi_tasks" t
        LEFT JOIN "{PANDA_SCHEMA}"."jedi_datasets" d
            ON d."jeditaskid" = t."jeditaskid" AND d."type" = 'input'
        WHERE t."processingtype" = 'stfprocessing'
          AND t."taskname" LIKE %s
        ORDER BY t."jeditaskid"
    """
    rows = []
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, [f'%swf.{int(run_number)}.%'])
        for taskid, site, status, nfiles, nfinished, nfailed in cursor.fetchall():
            rows.append({
                'jeditaskid': taskid,
                'site': site or 'unknown',
                'status': status or 'unknown',
                'files_total': int(nfiles or 0),
                'files_finished': int(nfinished or 0),
                'files_failed': int(nfailed or 0),
                'url': reverse('monitor_app:panda_task_detail',
                               args=[taskid]),
            })
    return rows


def _run_story(info):
    """The activity's story for the cut card, at the level an operator
    would report it: what the execution set out to do (STF volume, the
    decision box and its target sites) and what its STF tasks did."""
    import logging

    from .workflow_models import WorkflowExecution
    story = {}
    execution_id = info.get('execution_id') or ''
    if execution_id:
        ex = WorkflowExecution.objects.filter(
            execution_id=execution_id).first()
        params = (ex.parameter_values or {}) if ex else {}
        sim = params.get('simulation') or {}
        pp = params.get('prompt_processing') or {}
        try:
            stf_total = (int(sim.get('stf_count'))
                         * int(sim.get('physics_period_count')))
        except (TypeError, ValueError):
            stf_total = None
        story.update({
            'executed_by': getattr(ex, 'executed_by', '') or '',
            'stf_total': stf_total,
            'decision_box': bool(pp.get('decision_box_enabled')),
            'policy': str(pp.get('decision_box_policy') or ''),
            'sites': [str(s) for s in (pp.get('decision_box_sites') or [])],
        })
    if info.get('run_number'):
        try:
            story['tasks'] = _stf_tasks_for_run(info['run_number'])
        except Exception as e:
            logging.getLogger(__name__).error(
                'STF task story query failed for run %s: %s',
                info.get('run_number'), e)
            story['tasks'] = []
            story['tasks_error'] = str(e)
    return story


def _activity_card(key):
    """Detail card for one numbered activity: the run's story at the
    level an operator would report it. The key is the run number."""
    from datetime import timedelta

    from django.utils import timezone
    now = timezone.now()
    arcs = _namespace_run_arcs(now - timedelta(days=90), now, 12 * 3600)
    for namespace, runs in arcs.items():
        for arc in runs:
            if str(arc['run_number']) == str(key):
                info = {'run_number': arc['run_number'],
                        'workflow': arc['workflow'],
                        'execution_id': arc['execution']}
                return {'kind': 'run_story',
                        'namespace': namespace,
                        'workflow': arc['workflow'],
                        'run_number': arc['run_number'],
                        'execution_id': arc['execution'],
                        'started': arc['first'].isoformat(),
                        'ended': (arc['last'].isoformat()
                                  if not arc['dangling'] else ''),
                        'story': _run_story(info)}
    return None


def _datataking_card(data, previous_data, ctx):
    # One truth: at a reference instant (the cut, or the snap's own
    # time) the rows come from the run record, like the lanes; the
    # snap's recorded entry stays in the card's audit document.
    # Lean by design: the run's story lives in the Time history's
    # numbered activity table (the activity card); the cut states each
    # namespace's phase without repeating it.
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
    return {'kind': 'datataking', 'namespaces': rows}


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
        curve_groups=EPICPROD_GROUPS + _delivery_groups(),
        focus_view=_delivery_focus_view,
        component_cards={'panda': _panda_card,
                         'delivery': _delivery_card},
        card_template=CARD_TEMPLATE,
        annotate_references=annotate_references,
    ))
    # Testbed curves retired 2026-07-26 (operator decision): on the
    # broad window the spike-train curves said nothing the numbered
    # activity bars don't say better. The extraction code stays
    # (_testbed_curve_values and friends) for the day testbed load
    # becomes continuous; re-registering the three curve fields
    # restores the panels.
    register(ScopeProvider(
        scope='testbed',
        label='Testbed',
        episodic_lanes=_run_activity_lanes,
        activity_at=namespace_activity_at,
        activity_card=_activity_card,
        component_cards={'workflow': _workflow_card,
                         'datataking': _datataking_card},
        card_template=CARD_TEMPLATE,
        annotate_references=annotate_references,
    ))
    register_hooks(
        prefs_get=_prefs_get,
        prefs_set=_prefs_set,
        config_get=_config_get,
        scheduler_status=_scheduler_status,
        health_url=_health_url,
    )
