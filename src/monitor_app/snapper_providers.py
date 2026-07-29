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

# The categorization lenses of CAMPAIGN_DELIVERY.md: read-time
# projections of the PC leaves into group curves. 'seg' is the
# curve-id segment, 'value' the URL selector value. The requestors
# labeling holds PWG and DSC labels in one multi-membership list — the
# data does not distinguish them, so they are one lens.
DELIVERY_LENSES = (
    {'seg': 'cat', 'value': 'category', 'label': 'physics category'},
    {'seg': 'req', 'value': 'requestor', 'label': 'PWG/DSC'},
)

_PC_CACHE = {'at': None, 'requestors': {}, 'keys': {},
             'categories': {}, 'group_names': {}}


def _group_slug(name):
    import re
    return re.sub(r'[^a-z0-9]+', '_', str(name).lower()).strip('_')


def _pc_cache():
    """pc label -> requestor labels, physics category, and identity
    key, cached briefly: series assembly calls curve extraction once
    per snap, and lens membership is current-state (lenses apply
    retroactively). group_names maps curve-id slugs back to display
    names across every lens."""
    from django.utils import timezone

    from pcs.models import PhysicsConfig

    now = timezone.now()
    if (_PC_CACHE['at'] is None
            or (now - _PC_CACHE['at']).total_seconds() > 60):
        requestors, keys, categories = {}, {}, {}
        for label, groups, key, category in (
                PhysicsConfig.objects.values_list(
                    'label', 'requestors', 'config_key',
                    'physics_tag__category__name')):
            requestors[label] = list(groups or [])
            keys[label] = key
            categories[label] = category or 'Uncategorized'
        group_names = {}
        for name in set(categories.values()):
            group_names[_group_slug(name)] = name
        for groups in requestors.values():
            for name in groups:
                group_names[_group_slug(name)] = name
        for name in ('Unassigned', 'Uncategorized'):
            group_names[_group_slug(name)] = name
        _PC_CACHE.update({'requestors': requestors, 'keys': keys,
                          'categories': categories,
                          'group_names': group_names, 'at': now})
    return _PC_CACHE


def _lens_groups(pc, lens_seg, cache):
    """The lens groups one PC's leaf sums into (N-way for requestors;
    the empty labeling gets its stated bucket, never silence)."""
    if lens_seg == 'cat':
        return [cache['categories'].get(pc) or 'Uncategorized']
    return cache['requestors'].get(pc) or ['Unassigned']


def _delivery_curve_values(state):
    """Delivery curves from the DAILY arrivals record only (leaves
    carrying arrived_files): lens-group daily bumps (the quilt) and
    lens-group cumulative series with a total, per lens, both on the
    registered basis throughout. The live placed-basis component feeds
    cut cards, never curves, so the plotted series ends at the last
    complete day. Events emit in MILLIONS — the family titles carry
    (M); files stay raw counts. Unmeasured coverage is stated on the
    cut card, never silently mixed into the event sums."""
    values = {}
    delivery = component_data(state, 'delivery')
    cache = _pc_cache() if delivery.get('campaigns') else None
    for campaign, block in (delivery.get('campaigns') or {}).items():
        totals = block.get('totals') or {}
        if 'arrived_files' not in totals:
            continue  # the live placed-basis component: cards only
        tag = campaign.replace('.', '_')
        # The quilt stays PER-CONFIGURATION — one patch color per PC,
        # production bursts attributed — the lens only clusters the
        # tick boxes and the cut card. Cumulative draws at lens-group
        # level: a handful of lines, plus the total.
        for pc, leaf in (block.get('leaves') or {}).items():
            arrived_events = int(leaf.get('arrived_events') or 0)
            arrived_files = int(leaf.get('arrived_files') or 0)
            if arrived_events:
                values[f'dlvq_{tag}_{pc}'] = round(
                    arrived_events / 1e6, 2)
            if arrived_files:
                values[f'dlvqf_{tag}_{pc}'] = arrived_files
        for lens in DELIVERY_LENSES:
            seg = lens['seg']
            cum_e, cum_f = {}, {}
            for pc, leaf in (block.get('leaves') or {}).items():
                for group in _lens_groups(pc, seg, cache):
                    slug = _group_slug(group)
                    cum_e[slug] = (cum_e.get(slug, 0)
                                   + int(leaf.get('events') or 0))
                    cum_f[slug] = (cum_f.get(slug, 0)
                                   + int(leaf.get('cum_files') or 0))
            for slug, v in cum_e.items():
                if v:
                    values[f'dlvc_{seg}_{tag}_{slug}'] = round(v / 1e6, 2)
            for slug, v in cum_f.items():
                if v:
                    values[f'dlvcf_{seg}_{tag}_{slug}'] = v
            values[f'dlvc_{seg}_{tag}__total'] = round(
                int(totals.get('events') or 0) / 1e6, 2)
            values[f'dlvcf_{seg}_{tag}__total'] = int(
                totals.get('cum_files') or 0)
    return values


def _site_curve_values(panda):
    """Per-site job lifecycle curves from the sites maps recorded in
    every snap: the in-flight population by status (submission through
    queueing to execution), running cores, and the trailing-24h
    finished/failed outcomes. Site names carry underscores, so the
    status is always the id's last segment."""
    values = {}
    for site, block in ((panda.get('jobs') or {}).get('sites')
                        or {}).items():
        for status, count in (block.get('by_status_now') or {}).items():
            values[f'sj_{site}_{status}'] = int(count or 0)
        if block.get('running_cores_now') is not None:
            values[f'sjc_{site}'] = int(
                block.get('running_cores_now') or 0)
        # Cumulative terminal counters (raw absolute values; the
        # window-relative families rebase them at render).
        cum = block.get('cum') or {}
        if 'finished' in cum:
            values[f'sjfw_{site}'] = int(cum.get('finished') or 0)
        if 'failed' in cum:
            values[f'sjxw_{site}'] = int(cum.get('failed') or 0)
        for cls, count in (block.get('cum_failed_by_class')
                           or {}).items():
            values[f'sjxc_{site}_{cls}'] = int(count or 0)
    for site, block in ((panda.get('tasks') or {}).get('sites')
                        or {}).items():
        for status, count in (block.get('by_status_now') or {}).items():
            values[f'stt_{site}_{status}'] = int(count or 0)
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
    values.update(_site_curve_values(panda))
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
    """(campaign, remainder) from a per-PC arrivals curve id
    (dlvq_26_07_pc12); campaign tags serialize dots as underscores."""
    remainder = curve_id.split('_', 1)[1]
    tag, _, rest = remainder.partition('_')
    while rest and rest[0].isdigit():
        extra, _, rest = rest.partition('_')
        tag = f'{tag}_{extra}'
    return tag.replace('_', '.'), rest.strip('_')


def _delivery_lens_parts(curve_id):
    """(lens_seg, campaign, group_slug) from a lens-group cumulative
    curve id (dlvc_cat_26_07_single_particle)."""
    remainder = curve_id.split('_', 1)[1]
    seg, _, rest = remainder.partition('_')
    tag, _, rest = rest.partition('_')
    while rest and rest[0].isdigit():
        extra, _, rest = rest.partition('_')
        tag = f'{tag}_{extra}'
    return seg, tag.replace('_', '.'), rest.strip('_')


def _epicprod_curve_color(curve_id):
    """House state colors for status-bearing curves — one state-color
    vocabulary on every surface, red only where failure lives. Curves
    without semantic color (cores, types, deliveries) take the
    palette deal. Type-by-state curves stay on the palette too:
    several types sharing one status must stay distinguishable."""
    from .panda.constants import JOB_STATE_COLORS, TASK_STATE_COLORS

    # Operator-set colors on the site jobs panel: finished takes the
    # activated green (the state map's finished is too dark beside the
    # staircase), and the running pair reads as blues — cores strong,
    # running jobs lighter — so the two connect at a glance.
    if curve_id.startswith('sjfw_'):
        return JOB_STATE_COLORS.get('activated')
    if curve_id.startswith('sjxw_'):
        return JOB_STATE_COLORS.get('failed')
    if curve_id.startswith('sjxc_'):
        return _FAILURE_CLASS_COLORS.get(
            curve_id.rsplit('_', 1)[1], '#424242')
    if curve_id.startswith('sjc_'):
        return '#1565c0'
    if curve_id.startswith('sj_'):
        status = curve_id.rsplit('_', 1)[1]
        if status == 'running':
            return '#64b5f6'
        if status == 'activated':
            # Grey: the queued pool is context, not the story — the
            # greens belong to completion and the blues to running.
            return '#8a8a8a'
        return JOB_STATE_COLORS.get(status)
    if curve_id.startswith('job_'):
        return JOB_STATE_COLORS.get(curve_id[4:])
    if curve_id.startswith('stt_'):
        status = curve_id.rsplit('_', 1)[1]
        if status == 'running':
            # 'Running' is light blue on the site plots — jobs and
            # tasks alike; green belongs to completion.
            return '#64b5f6'
        return TASK_STATE_COLORS.get(status)
    if curve_id.startswith('task_'):
        return TASK_STATE_COLORS.get(curve_id[5:])
    return None


def _epicprod_curve_label(curve_id):
    # Per-site curves: the family title names the site; the curve
    # label is the lifecycle stage alone. 'running' says 'running
    # jobs' — 'running cores' sits beside it and the bare word is
    # ambiguous.
    if curve_id.startswith('sjc_'):
        return 'running cores'
    if curve_id.startswith('sjfw_'):
        return 'finished'
    if curve_id.startswith('sjxw_'):
        return 'failed'
    if curve_id.startswith('sjxc_'):
        # Failure-class curves: the class is the last id segment.
        return curve_id.rsplit('_', 1)[1]
    if curve_id.startswith('sj_'):
        status = curve_id.rsplit('_', 1)[1]
        return 'running jobs' if status == 'running' else status
    if curve_id.startswith('stt_'):
        return curve_id.rsplit('_', 1)[1]
    if curve_id.startswith(('dlvq_', 'dlvqf_')):
        _campaign, pc = _delivery_curve_parts(curve_id)
        key = _pc_cache()['keys'].get(pc, '')
        return f'{pc} {key}' if key else pc
    if curve_id.startswith(('dlvc_', 'dlvcf_')):
        # The line states the group; the family header states
        # campaign, kind, and unit.
        _seg, _campaign, slug = _delivery_lens_parts(curve_id)
        if slug in ('', 'total'):
            return 'total'
        return _pc_cache()['group_names'].get(slug, slug)
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
        # Two selector axes: the plotted quantity (files is the
        # default while the measured event rates are under review;
        # events, in millions, stays one click away) and the grouping
        # lens the quilt factorizes by.
        'selectors': [
            {'param': 'quantity', 'label': 'Counting',
             'default': 'files',
             'choices': [{'value': 'files', 'label': 'files'},
                         {'value': 'events', 'label': 'events'}]},
            {'param': 'lens', 'label': 'Grouping',
             'default': 'category',
             'choices': [{'value': lens['value'],
                          'label': lens['label']}
                         for lens in DELIVERY_LENSES]},
        ],
        'options': [
            {'value': name, 'label': name,
             # The arrivals family is the same per-PC quilt under
             # every lens — the lens clusters its tick boxes;
             # cumulative swaps to the lens's group lines.
             'families_by': {
                 f'{quantity}|{lens["value"]}': [
                     f'Arrivals {name} {quantity}',
                     f'Cumulative {name} {quantity} {lens["value"]}',
                 ]
                 for quantity in ('files', 'events')
                 for lens in DELIVERY_LENSES},
             'component': 'delivery',
             'collapse_below': 0.01,
             'start': (starts[name] - timedelta(hours=12))
                      if name in starts else None}
            for name in campaigns],
    }


def _pc_tick_groupings():
    """pc label -> {lens value: [group display names]} for every PC —
    the client clusters the per-PC quilt's tick boxes by the active
    lens with this; a box toggles its PC set, the PC colors stand."""
    cache = _pc_cache()
    return {
        pc: {lens['value']: _lens_groups(pc, lens['seg'], cache)
             for lens in DELIVERY_LENSES}
        for pc in cache['keys']
    }


def _delivery_groups():
    """Curve families per target campaign: the PER-PC daily arrivals
    quilt per quantity (one patch color per configuration — the basis
    of the display; tick boxes cluster by the active lens via
    pc_groups) and the lens-group cumulative series per quantity ×
    lens (off by default — the quilt is the display). The unique
    registry name carries selector qualifiers; the display title does
    not. A resolution failure yields no delivery families rather than
    blocking registration."""
    try:
        from swf_epicprod.analytics.rollup import resolve_target_campaigns
        campaigns = resolve_target_campaigns()
    except Exception:                                       # noqa: BLE001
        return ()
    pc_groups = _pc_tick_groupings()
    groups = []
    for name in campaigns:
        tag = name.replace('.', '_')
        groups.append({
            'name': f'Arrivals {name} files',
            'title': f'Arrivals {name}',
            'prefixes': [f'dlvqf_{tag}_'], 'ids': [],
            'stacked': True, 'pc_groups': pc_groups,
            'units': 'files'})
        groups.append({
            'name': f'Arrivals {name} events',
            'title': f'Arrivals {name}',
            'prefixes': [f'dlvq_{tag}_'], 'ids': [],
            'stacked': True, 'pc_groups': pc_groups,
            'default_off': True, 'units': 'events (M)'})
        for lens in DELIVERY_LENSES:
            seg, lens_value = lens['seg'], lens['value']
            groups.append({
                'name': f'Cumulative {name} files {lens_value}',
                'title': f'Cumulative {name}',
                'prefixes': [f'dlvcf_{seg}_{tag}_'], 'ids': [],
                'default_off': True, 'units': 'files'})
            groups.append({
                'name': f'Cumulative {name} events {lens_value}',
                'title': f'Cumulative {name}',
                'prefixes': [f'dlvc_{seg}_{tag}_'], 'ids': [],
                'default_off': True, 'units': 'events (M)'})
    return tuple(groups)

_SITE_CACHE = {'at': None, 'sites': ()}


def _panda_sites():
    """Sites in the latest snap's PanDA component (union of the job
    and task site maps, current in-flight jobs first), cached briefly.
    The list drives the per-site families and the Site focus options;
    a site drops out when it leaves the component's bounded ranked
    maps."""
    from django.utils import timezone

    from snapper_ai.models import SystemSnap

    now = timezone.now()
    if (_SITE_CACHE['at'] is not None
            and (now - _SITE_CACHE['at']).total_seconds() < 300):
        return _SITE_CACHE['sites']
    state = (SystemSnap.objects.filter(scope='epicprod')
             .order_by('-snap_time').values_list('state', flat=True)
             .first())
    panda = ((((state or {}).get('components') or {})
              .get('panda') or {}).get('data') or {})
    job_sites = (panda.get('jobs') or {}).get('sites') or {}
    task_sites = (panda.get('tasks') or {}).get('sites') or {}
    sites = tuple(sorted(
        set(job_sites) | set(task_sites),
        key=lambda site: (-int((job_sites.get(site) or {})
                               .get('in_flight_jobs_now') or 0), site)))
    _SITE_CACHE.update({'sites': sites, 'at': now})
    return sites


# Display order of the site jobs family: submission through queueing
# to execution (cores beside running jobs) to the trailing outcomes.
_JOB_LIFECYCLE_EARLY = ('defined', 'waiting', 'assigned', 'activated',
                        'sent', 'starting')
_JOB_LIFECYCLE_LATE = ('holding', 'transferring', 'merging')


def _site_groups():
    """Per-site curve families: one jobs panel following the lifecycle
    (queued states through running to the trailing finished/failed
    outcomes, with cores) and one tasks panel. Off by default on the
    scope view — the Site focus is their home."""
    groups = []
    for site in _panda_sites():
        # One plot tells the site story: the in-flight lifecycle with
        # the terminal-outcome staircases at its end. The staircases
        # are window-relative cumulative counters — they rise from
        # zero at the window's left edge, and the displayed window is
        # the integration range.
        order = ([f'sj_{site}_{s}' for s in _JOB_LIFECYCLE_EARLY]
                 + [f'sj_{site}_running', f'sjc_{site}']
                 + [f'sj_{site}_{s}' for s in _JOB_LIFECYCLE_LATE]
                 + [f'sjfw_{site}', f'sjxw_{site}'])
        groups.append({
            'name': f'Site jobs {site}',
            'title': f'Jobs · {site}',
            'prefixes': [f'sj_{site}_'],
            'ids': [f'sjc_{site}', f'sjfw_{site}', f'sjxw_{site}'],
            'order': order,
            'window_relative': [f'sjfw_{site}', f'sjxw_{site}'],
            'tall': True,
            'default_off': True})
        groups.append({
            'name': f'Site failures {site}',
            'title': f'Failures by class · {site}',
            'prefixes': [f'sjxc_{site}_'], 'ids': [],
            'window_relative': True,
            'focus_closed': True,
            'default_off': True})
        groups.append({
            'name': f'Site tasks {site}',
            'title': f'Tasks · {site}',
            'prefixes': [f'stt_{site}_'], 'ids': [],
            'focus_closed': True,
            'default_off': True})
    return tuple(groups)


def _epicprod_groups():
    """The epicprod curve families, resolved per render (the seam's
    callable form) so new campaigns and sites appear without an app
    restart."""
    return EPICPROD_GROUPS + _delivery_groups() + _site_groups()


def _site_focus_view():
    """The Site focus tab: one site's job lifecycle — submission
    through queueing to execution to the trailing finished/failed
    outcomes — with its tasks panel, and the cut narrowed to the panda
    component's site detail."""
    sites = _panda_sites()
    if not sites:
        return None
    return {
        'param': 'site',
        'label': 'Site',
        'note': ('In-flight counts are the recorded site state through '
                 'time; finished and failed accumulate from the left '
                 'edge of the shown window — the window is the '
                 'integration range, and zooming re-bases it. Click '
                 'the plot for the full picture at that instant.'),
        'default': sites[0],
        'options': [
            {'value': site, 'label': site,
             'families': [f'Site jobs {site}',
                          f'Site failures {site}',
                          f'Site tasks {site}'],
             'component': 'panda'}
            for site in sites],
    }


TESTBED_GROUPS = (
    {'name': 'Workflows', 'prefixes': ['wf_'], 'ids': []},
    {'name': 'STF tasks', 'prefixes': ['sts_'], 'ids': ['stf_total']},
)


# ── Component cards ──────────────────────────────────────────────────────

_FAILURE_CLASS_COLORS = {
    'brokerage': '#8d6e63',
    'ddm': '#0277bd',
    'executor': '#c2185b',
    'dispatcher': '#00838f',
    'pilot': '#ef6c00',
    'supervisor': '#6a1b9a',
    'taskbuffer': '#455a64',
    'other': '#757575',
}


def _pie_segment(cx, cy, r_in, r_out, a0, a1):
    """SVG path of an annular sector; angles in radians clockwise from
    12 o'clock. A full circle is clamped a hair short so the arc pair
    stays well-formed."""
    import math

    a1 = min(a1, a0 + 2 * math.pi - 0.001)
    large = 1 if (a1 - a0) > math.pi else 0
    x0o, y0o = cx + r_out * math.sin(a0), cy - r_out * math.cos(a0)
    x1o, y1o = cx + r_out * math.sin(a1), cy - r_out * math.cos(a1)
    x1i, y1i = cx + r_in * math.sin(a1), cy - r_in * math.cos(a1)
    x0i, y0i = cx + r_in * math.sin(a0), cy - r_in * math.cos(a0)
    return (f'M {x0o:.2f} {y0o:.2f} '
            f'A {r_out} {r_out} 0 {large} 1 {x1o:.2f} {y1o:.2f} '
            f'L {x1i:.2f} {y1i:.2f} '
            f'A {r_in} {r_in} 0 {large} 0 {x0i:.2f} {y0i:.2f} Z')


def _site_outcomes_pie(site, window_finished, window_failed,
                       class_windows, class_names, jobs_url, errors_url):
    """Reusable context for Snapper's site-outcomes pie."""
    import math

    pie = []
    total = window_finished + window_failed
    if not total:
        return pie
    class_colors = {
        name: _FAILURE_CLASS_COLORS.get(name, '#424242')
        for name in class_names
    }
    tau = 2 * math.pi
    split = tau * window_finished / total
    if window_finished:
        curve = f'sjfw_{site}'
        pie.append({
            'path': _pie_segment(60, 60, 22, 40, 0, split),
            'curve': curve,
            'color': _epicprod_curve_color(curve),
            'url': jobs_url(site, 'finished'),
            'title': (f'finished · {window_finished:,} '
                      f'({window_finished / total:.0%})')})
    if window_failed:
        curve = f'sjxw_{site}'
        pie.append({
            'path': _pie_segment(60, 60, 22, 40, split, tau),
            'curve': curve,
            'color': _epicprod_curve_color(curve),
            'url': jobs_url(site, 'failed'),
            'title': (f'failed · {window_failed:,} '
                      f'({window_failed / total:.0%})')})
        angle = split
        for cls, in_window, _count in class_windows:
            span = (tau - split) * in_window / window_failed
            pie.append({
                'path': _pie_segment(60, 60, 42, 58,
                                     angle, angle + span),
                'curve': f'sjxc_{site}_{cls}',
                'color': class_colors.get(cls),
                'url': errors_url(site, cls),
                'title': f'{cls} · {in_window:,}'})
            angle += span
    return pie


def _avg_exec_times(site, since, until):
    """Average execution wall time (endtime − starttime) of the site's
    finished and failed jobs with end times in (since, until] — the
    same jobs the slice's window outcomes count. Never-started jobs
    carry no execution time and are excluded."""
    from django.db import connections

    from .panda.constants import PANDA_SCHEMA

    where = ('"computingsite" = %s AND "jobstatus" IN '
             "('finished', 'failed') AND \"endtime\" > %s "
             'AND "endtime" <= %s AND "starttime" IS NOT NULL')
    sql = f"""
        SELECT "jobstatus", AVG("endtime" - "starttime")
        FROM (
            SELECT "pandaid", "jobstatus", "starttime", "endtime"
            FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {where}
            UNION
            SELECT "pandaid", "jobstatus", "starttime", "endtime"
            FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {where}
        ) completed
        GROUP BY "jobstatus"
    """
    params = [site, since, until]
    out = {}
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, params + params)
        for status, average in cursor.fetchall():
            if average is not None:
                out[status] = average.total_seconds()
    return out


def _counter_site_blocks(scope, instant):
    """The per-site counter blocks of the nearest counter-bearing panda
    state at or before ``instant``: (sites map, snap time). The
    cumulative terminal counters ride two interleaved snap chains — the
    live v5 publications and the hourly backfill reconstruction — and a
    snap resolved from the pre-counter era carries none, so outcome
    differencing must find the counter-bearing chain itself."""
    from snapper_ai.models import SystemSnap

    if instant is None:
        return {}, None
    row = (SystemSnap.objects
           .filter(scope=scope, snap_time__lte=instant,
                   state__components__panda__data__jobs__has_key='cum')
           .order_by('-snap_time')
           .values('snap_time', 'state').first())
    if not row:
        return {}, None
    jobs = (((((row['state'] or {}).get('components') or {})
              .get('panda') or {}).get('data') or {}).get('jobs') or {})
    return (jobs.get('sites') or {}), row['snap_time']


def panda_site_outcomes_pie(site, since, until, size=270):
    """Placeable context for the Site page's final-job-state pie."""
    import math
    from urllib.parse import quote

    from django.urls import reverse

    cut_sites, _ = _counter_site_blocks('epicprod', until)
    basis_sites, _ = _counter_site_blocks('epicprod', since)
    cut = cut_sites.get(site) or {}
    basis = basis_sites.get(site) or {}
    cut_cum = cut.get('cum') or {}
    basis_cum = basis.get('cum') or {}
    cut_classes = cut.get('cum_failed_by_class') or {}
    basis_classes = basis.get('cum_failed_by_class') or {}

    def window_count(key):
        return max(0, int(cut_cum.get(key) or 0)
                   - int(basis_cum.get(key) or 0))

    class_names = set(cut_classes) | set(basis_classes)
    class_windows = []
    for cls in class_names:
        count = max(0, int(cut_classes.get(cls) or 0)
                    - int(basis_classes.get(cls) or 0))
        if count:
            class_windows.append(
                (cls, count, int(cut_classes.get(cls) or 0)))
    class_windows.sort(key=lambda item: (-item[1], item[0]))

    jobs_base = reverse('monitor_app:panda_jobs_list')
    errors_base = reverse('monitor_app:panda_errors_list')
    days = max(1, math.ceil((until - since).total_seconds() / 86400))
    window_q = (
        f'&days={days}&ended_after=' + quote(since.isoformat())
        + '&ended_before=' + quote(until.isoformat()))

    def jobs_url(site_name, status=None):
        return (f'{jobs_base}?site={quote(site_name)}'
                + (f'&status={quote(status)}' if status else '')
                + window_q)

    def errors_url(site_name, cls=None):
        return (f'{errors_base}?site={quote(site_name)}'
                + '&status=failed'
                + ('&classified=1' if cls and cls != 'other' else '')
                + (f'&error_source={quote(cls)}'
                   if cls and cls != 'other' else '')
                + window_q)

    finished = window_count('finished')
    failed = window_count('failed')
    return {
        'site': site,
        'pie': _site_outcomes_pie(
            site, finished, failed, class_windows, class_names,
            jobs_url, errors_url),
        'pie_size': int(size),
    }


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
    # The Site focus narrows the card to the selected sites' detail:
    # the germane facts of the slice, color-coded as in the plot —
    # window outcomes first (differenced against the ?since= basis,
    # the view's left edge), then the in-flight standing in lifecycle
    # order, then tasks. One swatch per fact, no repetition.
    params = (ctx or {}).get('params') or {}
    selected = [value for value in
                (params.get('site') or '').split(',') if value]
    compact = str(params.get('compact') or '') == '1'
    since_sites = ((((ctx or {}).get('since_data') or {})
                    .get('jobs') or {}).get('sites') or {})
    since_stamp = (ctx or {}).get('since')
    basis_text = ''
    if since_stamp is not None:
        from zoneinfo import ZoneInfo
        basis_text = (since_stamp
                      .astimezone(ZoneInfo('America/New_York'))
                      .strftime('%m-%d %H:%M ET'))
    lifecycle = (list(_JOB_LIFECYCLE_EARLY) + ['running']
                 + list(_JOB_LIFECYCLE_LATE))
    # Counter sourcing: the resolved snap and the ?since= basis snap
    # may come from the pre-counter era (no 'cum'); the outcomes rows
    # then difference the nearest counter-bearing snaps instead — the
    # finished/failed story must never vanish from the slice.
    scope = (ctx or {}).get('scope') or 'epicprod'
    requested_at = (ctx or {}).get('requested_at')
    counter_cut, counter_cut_time = ({}, None)
    counter_since = {}
    if selected:
        counter_cut, counter_cut_time = _counter_site_blocks(
            scope, requested_at)
        counter_since, _ = _counter_site_blocks(scope, since_stamp)
    # Every fact in the slice is a drill-down: rows and pie slices link
    # to the jobs list (site + status) or the error summary (site +
    # class), scoped to the window via the days filter.
    import math
    from urllib.parse import quote

    from django.urls import reverse
    from django.utils import timezone as _timezone

    jobs_base = reverse('monitor_app:panda_jobs_list')
    errors_base = reverse('monitor_app:panda_errors_list')
    window_q = ''
    if since_stamp is not None and requested_at is not None:
        window_days = max(1, math.ceil(
            (requested_at - since_stamp).total_seconds() / 86400))
        window_q = (
            f'&days={window_days}&ended_after='
            + quote(since_stamp.isoformat())
            + '&ended_before=' + quote(requested_at.isoformat()))
    elif since_stamp is not None:
        window_q = '&days=' + str(max(1, math.ceil(
            (_timezone.now() - since_stamp).total_seconds() / 86400)))

    def _jobs_url(site, status=None):
        return (f'{jobs_base}?site={quote(site)}'
                + (f'&status={quote(status)}' if status else '') + window_q)

    def _errors_url(site, cls=None):
        return (f'{errors_base}?site={quote(site)}'
                + '&status=failed'
                + ('&classified=1' if cls and cls != 'other' else '')
                + (f'&error_source={quote(cls)}'
                   if cls and cls != 'other' else '') + window_q)
    sites = []
    for site in selected:
        block = ((data.get('jobs') or {}).get('sites')
                 or {}).get(site) or {}
        prev_block = ((previous_data.get('jobs') or {}).get('sites')
                      or {}).get(site) or {}
        task_block = ((data.get('tasks') or {}).get('sites')
                      or {}).get(site) or {}
        base = since_sites.get(site) or {}
        counter_base = counter_since.get(site) or {}
        base_cum = base.get('cum') or counter_base.get('cum') or {}
        base_classes = (base.get('cum_failed_by_class')
                        or counter_base.get('cum_failed_by_class') or {})
        counter_block = counter_cut.get(site) or {}
        own_cum = bool(block.get('cum'))
        cum = block.get('cum') or counter_block.get('cum') or {}
        classes = (block.get('cum_failed_by_class')
                   or counter_block.get('cum_failed_by_class') or {})

        def _window(key, _cum=cum, _base=base_cum):
            return max(0, int(_cum.get(key) or 0)
                       - int(_base.get(key) or 0))

        # One table row per curve, in panel order — swatch-correlated
        # to the plot, with the in-window integral where the curve is
        # cumulative and the failure classes nested under failed.
        statuses = block.get('by_status_now') or {}
        prev_statuses = prev_block.get('by_status_now') or {}
        prev_cum = prev_block.get('cum') or {}
        prev_classes = prev_block.get('cum_failed_by_class') or {}
        ordered = ([s for s in lifecycle if s in statuses]
                   + sorted(s for s in statuses if s not in lifecycle))
        rows = [
            {'label': ('running jobs' if status == 'running'
                       else status),
             'curve': f'sj_{site}_{status}',
             'url': '',
             'at_cut': str(int(statuses.get(status) or 0)),
             'delta': cut_delta(statuses.get(status),
                                prev_statuses.get(status)) or '',
             'window': '—', 'indent': False}
            for status in ordered]
        if block.get('running_cores_now') is not None:
            position = next(
                (i + 1 for i, entry in enumerate(rows)
                 if entry['label'] == 'running jobs'), len(rows))
            rows.insert(position, {
                'label': 'running cores', 'curve': f'sjc_{site}',
                'url': '',
                'at_cut': str(int(block.get('running_cores_now')
                                  or 0)),
                'delta': cut_delta(block.get('running_cores_now'),
                                   prev_block.get('running_cores_now'))
                or '',
                'window': '—', 'indent': False})
        # The outcomes story renders whenever a counter chain exists —
        # zero finished and zero failed at a quiet instant are facts,
        # never omissions.
        have_counters = bool(cum or base_cum or counter_cut
                             or counter_since)
        window_finished = _window('finished')
        window_failed = _window('failed')
        avg_exec = {}
        avg_note = ''
        if since_stamp is not None and requested_at is not None:
            try:
                avg_exec = _avg_exec_times(site, since_stamp,
                                           requested_at)
            except Exception as e:                           # noqa: BLE001
                import logging
                logging.getLogger(__name__).error(
                    'average exec time lookup failed for %s: %s',
                    site, e)
                avg_note = 'average execution time lookup failed'
        class_windows = []
        for cls, count in sorted(classes.items(),
                                 key=lambda item: -int(item[1] or 0)):
            in_window = max(0, int(count or 0)
                            - int(base_classes.get(cls) or 0))
            if in_window:
                class_windows.append((cls, in_window, count))
        if have_counters:
            rows.append({
                'label': 'finished', 'curve': f'sjfw_{site}',
                'url': _jobs_url(site, 'finished'),
                'at_cut': '—',
                'delta': cut_delta(cum.get('finished'),
                                   prev_cum.get('finished')) or '',
                'window': str(window_finished),
                'avg': (span_text(avg_exec['finished'])
                        if 'finished' in avg_exec else ''),
                'indent': False})
            rows.append({
                'label': 'failed', 'curve': f'sjxw_{site}',
                'url': _jobs_url(site, 'failed'),
                'at_cut': '—',
                'delta': cut_delta(cum.get('failed'),
                                   prev_cum.get('failed')) or '',
                'window': str(window_failed),
                'avg': (span_text(avg_exec['failed'])
                        if 'failed' in avg_exec else ''),
                'indent': False})
            for cls, in_window, count in class_windows:
                rows.append({
                    'label': cls, 'curve': f'sjxc_{site}_{cls}',
                    'url': _errors_url(site, cls),
                    'at_cut': '—',
                    'delta': cut_delta(count,
                                       prev_classes.get(cls)) or '',
                    'window': str(in_window), 'indent': True})
        # The outcomes pie: inner ring finished|failed over the window,
        # outer ring the failure classes over the failed arc — every
        # slice the same drill-down as its table row, colored by
        # data-curve exactly as the plot.
        pie = _site_outcomes_pie(
            site, window_finished, window_failed, class_windows,
            classes, _jobs_url, _errors_url)
        counter_note = ''
        if have_counters and not own_cum and counter_cut_time:
            counter_note = ('outcomes from the counter record at '
                            + counter_cut_time.astimezone(ET_ZONE)
                            .strftime('%m-%d %H:%M ET'))
        sites.append({
            'site': site,
            'url': _jobs_url(site),
            'found': bool(block or task_block or have_counters),
            'quiet': not ordered,
            'counter_note': counter_note,
            'avg_note': avg_note,
            'basis': basis_text if have_counters else '',
            'rows': rows,
            'pie': pie,
            # The pie fills the table's height: sized to the row count.
            'pie_size': min(400, max(220, 34 * (len(rows) + 1))),
        })
    return {'kind': 'panda', 'headline': headline, 'types': types,
            'type_states': type_states, 'sites': sites,
            'site_only': bool(sites) and compact}


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
    # The campaign view's selection narrows the card: only the ticked
    # campaigns' sections render. Without the parameter (a scope-view
    # cut) every campaign in the snap renders.
    params = (ctx or {}).get('params') or {}
    selected = {value for value in
                (params.get('campaign') or '').split(',') if value}
    campaigns = []
    for name, block in sorted((data.get('campaigns') or {}).items()):
        if selected and name not in selected:
            continue
        totals = block.get('totals') or {}
        previous_totals = (((previous_data.get('campaigns') or {})
                            .get(name) or {}).get('totals') or {})
        if 'arrived_files' in totals:
            leaves = block.get('leaves') or {}
            # The day's arrivals grouped by the ACTIVE lens — the same
            # projection the quilt draws, so each section's swatch is
            # the patch color above it. The drilldown inside a group
            # stays in physics-configuration terms.
            lens_value = str(params.get('lens') or 'category').strip()
            lens = next((entry for entry in DELIVERY_LENSES
                         if entry['value'] == lens_value),
                        DELIVERY_LENSES[0])
            seg = lens['seg']
            tag = name.replace('.', '_')
            by_group = {}
            delivering = 0
            for pc, leaf in leaves.items():
                arrived = int(leaf.get('arrived_files') or 0)
                if not arrived:
                    continue
                delivering += 1
                row = {
                    'label': pc,
                    # The quilt curve this row is a patch of, in
                    # either plotted quantity: the swatch painter
                    # takes the first candidate the plot carries.
                    'curve': (f'dlvq_{tag}_{pc} dlvqf_{tag}_{pc}'),
                    'identity': keys.get(pc, ''),
                    'url': reverse('pcs:pcs_config_detail', args=[pc]),
                    'groups': ', '.join(requestors.get(pc)
                                        or ['Unassigned']),
                    'arrived_events': int(
                        leaf.get('arrived_events') or 0),
                    'cum_events': int(leaf.get('events') or 0),
                    'arrived': arrived,
                    'cum': int(leaf.get('cum_files') or 0),
                    'expected': leaf.get('expected'),
                    'tier': leaf.get('tier') or '',
                }
                for group in _lens_groups(pc, seg, cache):
                    slot = by_group.setdefault(group, {
                        'name': group,
                        'rows': [], 'arrived_events': 0,
                        'arrived_files': 0})
                    slot['rows'].append(row)
                    slot['arrived_events'] += row['arrived_events']
                    slot['arrived_files'] += arrived
            day_groups = sorted(
                by_group.values(),
                key=lambda g: (-g['arrived_events'],
                               -g['arrived_files']))
            for group in day_groups:
                group['rows'].sort(
                    key=lambda r: (-r['arrived_events'], -r['arrived']))
            requested_at = (ctx or {}).get('requested_at')
            unmeasured = int(totals.get('unmeasured_files') or 0)
            campaigns.append({
                'name': name,
                # The compact card names its own day — no surrounding
                # chrome does it anymore.
                'day': (requested_at.astimezone(ET_ZONE)
                        .strftime('%b %-d')
                        if requested_at is not None else ''),
                'day_groups': day_groups,
                'headline': [
                    {'label': 'events arrived this day',
                     'value': totals.get('arrived_events'),
                     'delta': None},
                    {'label': 'cumulative events',
                     'value': totals.get('events'),
                     'delta': cut_delta(totals.get('events'),
                                        previous_totals.get('events'))},
                    {'label': 'files arrived this day',
                     'value': totals.get('arrived_files'), 'delta': None},
                    {'label': 'cumulative TB',
                     'value': round(
                         (totals.get('cum_bytes') or 0) / 1e12, 1),
                     'delta': None},
                    {'label': 'configurations delivering',
                     'value': delivering, 'delta': None},
                ],
                'unmeasured_files': unmeasured,
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


def _series_cache(key, builder):
    """Snapper series as a cached product (docs/CACHED_PRODUCTS.md):
    served stored, rebuilt behind responses on staleness. A first fill
    that finds another worker's build lock has nothing to serve —
    build inline rather than serve nothing."""
    from .cached_product import get_product

    value = get_product(key, builder, ttl_seconds=90)['value']
    return value if value is not None else builder()


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
        curve_color=_epicprod_curve_color,
        curve_groups=_epicprod_groups,
        focus_view=(_delivery_focus_view, _site_focus_view),
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
        series_cache=_series_cache,
    )
