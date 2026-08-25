"""Host-side Snapper scope providers for the swf platform.

Everything experiment-specific about the Snapper surfaces lives here
and registers with the agnostic snapper_ai core (snapper_ai.registry):
the epicprod and testbed scopes' curve extraction, labels and families,
the panda / workflow / datataking component cards with their links into
monitor pages, the testbed run-arc activity lanes, reference
resolution, and the host service hooks (preferences, configuration,
scheduler status, health page). Registered from MonitorAppConfig.ready().
"""

import logging

from snapper_ai.presentation import (ET_ZONE, component_data, cut_chip,
                                     cut_delta, et_naive, span_text)
from snapper_ai.registry import ScopeProvider, register, register_hooks

logger = logging.getLogger(__name__)

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
             'categories': {}, 'processes': {}, 'species': {},
             'beam_energies': {}, 'q2_ranges': {}, 'samples': {},
             'group_names': {}}


def _group_slug(name):
    import re
    return re.sub(r'[^a-z0-9]+', '_', str(name).lower()).strip('_')


def _species_slug(name):
    # Preserve charge in curve identities: e+ and e- must not both
    # collapse to the same generic ``e`` slug.
    charged = str(name).replace('+', '_plus').replace('-', '_minus')
    return _group_slug(charged) or 'unspecified'


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
        processes, species = {}, {}
        beam_energies, q2_ranges, samples = {}, {}, {}
        for label, groups, key, category, parameters, sample in (
                PhysicsConfig.objects.values_list(
                    'label', 'requestors', 'config_key',
                    'physics_tag__category__name',
                    'physics_tag__parameters', 'sample_name')):
            requestors[label] = list(groups or [])
            keys[label] = key
            categories[label] = category or 'Uncategorized'
            parameters = parameters or {}
            processes[label] = str(parameters.get('process') or '')
            # Collision species for ordinary samples; generated particle
            # species for particle-gun samples, whose beam species is
            # intentionally empty.
            species[label] = str(parameters.get('beam_species')
                                 or parameters.get('particle') or '')
            electron = parameters.get('beam_energy_electron')
            hadron = parameters.get('beam_energy_hadron')
            energies = [str(value) for value in (electron, hadron)
                        if value not in (None, '')]
            beam_energies[label] = (
                f"{' × '.join(energies)} GeV" if energies else '')
            q2_range = str(parameters.get('q2_range') or '')
            if q2_range.startswith('q2_'):
                q2_range = q2_range[3:]
            q2_ranges[label] = q2_range.replace('to', '–')
            samples[label] = str(sample or '')
        group_names = {}
        for name in set(categories.values()):
            group_names[_group_slug(name)] = name
        for groups in requestors.values():
            for name in groups:
                group_names[_group_slug(name)] = name
        for name in ('Unassigned', 'Uncategorized'):
            group_names[_group_slug(name)] = name
        species_names = {
            _species_slug(name or 'Unspecified'): name or 'Unspecified'
            for name in species.values()
        }
        _PC_CACHE.update({'requestors': requestors, 'keys': keys,
                          'categories': categories,
                          'processes': processes, 'species': species,
                          'beam_energies': beam_energies,
                          'q2_ranges': q2_ranges, 'samples': samples,
                          'species_names': species_names,
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
        species_cum_e, species_cum_f = {}, {}
        for pc, leaf in (block.get('leaves') or {}).items():
            arrived_events = int(leaf.get('arrived_events') or 0)
            arrived_files = int(leaf.get('arrived_files') or 0)
            # Zeros emit: a quiet day must stamp the quilt's time base,
            # or the next arrival day's column widens back over the
            # gap. The renderer stacks absent-at-stamp members as zero,
            # so the stamps are what carry the day.
            values[f'dlvq_{tag}_{pc}'] = round(arrived_events / 1e6, 2)
            values[f'dlvqf_{tag}_{pc}'] = arrived_files
            # The same daily record carries each configuration's
            # cumulative standing.  Keep these as separate curves from
            # the daily-arrival quilt: the campaign view reuses them in
            # one stacked panel per top-level physics category.
            cumulative_events = int(leaf.get('events') or 0)
            cumulative_files = int(leaf.get('cum_files') or 0)
            if cumulative_events:
                values[f'dlvpc_{tag}_{pc}'] = round(
                    cumulative_events / 1e6, 2)
            if cumulative_files:
                values[f'dlvpcf_{tag}_{pc}'] = cumulative_files
            if cache['categories'].get(pc) == 'Single Particle':
                species = (cache.get('species') or {}).get(pc) \
                    or 'Unspecified'
                slug = _species_slug(species)
                species_cum_e[slug] = (species_cum_e.get(slug, 0)
                                       + cumulative_events)
                species_cum_f[slug] = (species_cum_f.get(slug, 0)
                                       + cumulative_files)
        for slug, value in species_cum_e.items():
            if value:
                values[f'dlvsp_{tag}_{slug}'] = round(value / 1e6, 2)
        for slug, value in species_cum_f.items():
            if value:
                values[f'dlvspf_{tag}_{slug}'] = value
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


_QUEUE_STACK_CACHE = {'members': (
    'NERSC_Perlmutter_epic',
    'BNL_OSG_EPIC_PROD_1',
    'UM_GREX_PanDA_1',
    'BNL_ePIC_GOOGLE',
    'BNL_NPPS_GPU',
    'BNL_PanDA_1',
)}

# Named bands in the seven-day cores-by-queue stack; every other queue
# collapses into 'other'.
QUEUE_STACK_MAX = 6

# The categorical palette used by the Site compute usage plots. Queue
# colors are assigned by time-weighted stack rank; 'other' takes the next color.
_QUEUE_BAND_COLORS = (
    '#636efa', '#ef553b', '#00cc96', '#ab63fa', '#ffa15a', '#19d3f3',
    '#ff6692', '#b6e880', '#ff97ff', '#fecb52', '#2f4b7c', '#a05195')

# Keep these states in the captured PanDA record and detail tables, but do not
# turn them into plot curves.  In particular, ``sent`` is normally a very
# short dispatch transition; sampling it at five-minute cadence creates tall,
# one-snap needles without useful operational structure.
_EPICPROD_DISABLED_PLOT_JOB_STATES = frozenset(('sent',))
_EPICPROD_TYPE_EXCLUDED_JOB_STATES = frozenset(
    ('activated', 'starting')) | _EPICPROD_DISABLED_PLOT_JOB_STATES


def _queue_stack_members():
    """Members derived from the report series already in hand."""
    return _QUEUE_STACK_CACHE['members']


def _site_curve_values(panda):
    """Per-site job lifecycle curves from the sites maps recorded in
    every snap: the in-flight population by status (submission through
    queueing to execution), running cores, and the trailing-24h
    finished/failed outcomes. Site names carry underscores, so the
    status is always the id's last segment."""
    values = {}
    jobs = panda.get('jobs') or {}
    job_sites = jobs.get('sites') or {}
    # The scope view's cores-by-queue stack shares this walk: a handful
    # of named queues and one 'other' band carrying every remaining
    # queue, so the stack always sums to the whole running-core count.
    for site, block in job_sites.items():
        for status, count in (block.get('by_status_now') or {}).items():
            if (status == 'starting'
                    or status in _EPICPROD_DISABLED_PLOT_JOB_STATES):
                continue
            values[f'sj_{site}_{status}'] = int(count or 0)
        if block.get('running_cores_now') is not None:
            cores = int(block.get('running_cores_now') or 0)
            values[f'sjc_{site}'] = cores
            values[f'qc_{site}'] = cores
        # Cumulative terminal counters (raw absolute values; the
        # window-relative families rebase them at render).
        cum = block.get('cum') or {}
        # The same counters twice under distinct ids: one curve id
        # carries one render projection, and these counters feed two —
        # the integrated outcomes (window-relative) and the completions
        # flow (counter_flow per-interval deltas).
        if 'finished' in cum:
            values[f'sjfw_{site}'] = int(cum.get('finished') or 0)
            values[f'sjfin_{site}'] = int(cum.get('finished') or 0)
        if 'failed' in cum:
            values[f'sjxw_{site}'] = int(cum.get('failed') or 0)
            values[f'sjfail_{site}'] = int(cum.get('failed') or 0)
        for cls, count in (block.get('cum_failed_by_class')
                           or {}).items():
            values[f'sjxc_{site}_{cls}'] = int(count or 0)
    for site, block in ((panda.get('tasks') or {}).get('sites')
                        or {}).items():
        for status, count in (block.get('by_status_now') or {}).items():
            values[f'stt_{site}_{status}'] = int(count or 0)
    return values


def _epicprod_series_transform(series):
    """Rank and fold queue curves using the series already assembled."""
    from datetime import datetime

    curves = series.get('curves') or {}
    queue_curves = {
        curve_id: curve for curve_id, curve in curves.items()
        if curve_id.startswith('qc_') and curve_id != 'qc_other'
    }
    running_points = (curves.get('running_cores') or {}).get('points') or []
    if not queue_curves or not running_points:
        return series

    stamps = [point[0] for point in running_points]
    running = {point[0]: int(point[1] or 0) for point in running_points}
    values = {
        curve_id: {point[0]: int(point[1] or 0)
                   for point in curve.get('points') or []}
        for curve_id, curve in queue_curves.items()
    }
    end = datetime.fromisoformat(series['end'])
    durations = {}
    for index, stamp in enumerate(stamps):
        until = (datetime.fromisoformat(stamps[index + 1])
                 if index + 1 < len(stamps) else end)
        durations[stamp] = max(
            0, (until - datetime.fromisoformat(stamp)).total_seconds())
    ranked = sorted(
        queue_curves,
        key=lambda curve_id: (
            -sum(values[curve_id].get(stamp, 0) * durations[stamp]
                 for stamp in stamps),
            curve_id))
    selected = [curve_id for curve_id in ranked[:QUEUE_STACK_MAX]
                if any(values[curve_id].values())]
    members = tuple(curve_id[3:] for curve_id in selected)
    _QUEUE_STACK_CACHE['members'] = members

    folded = {curve_id: curve for curve_id, curve in curves.items()
              if not curve_id.startswith('qc_')}
    for curve_id in selected:
        folded[curve_id] = {
            'label': curve_id[3:],
            'points': [[stamp, values[curve_id].get(stamp, 0)]
                       for stamp in stamps],
        }
    folded['qc_other'] = {
        'label': 'other',
        'points': [
            [stamp, max(0, running[stamp] - sum(
                values[curve_id].get(stamp, 0)
                for curve_id in selected))]
            for stamp in stamps
        ],
    }
    series['curves'] = folded
    series['queue_members'] = list(members)
    return series


def _epicprod_curve_values(state):
    values = {}
    panda = component_data(state, 'panda')
    jobs = panda.get('jobs') or {}
    jobs_now = jobs.get('in_flight_now') or {}
    tasks_now = (panda.get('tasks') or {}).get('in_flight_now') or {}
    if jobs_now:
        values['running_cores'] = int(jobs_now.get('running_cores') or 0)
        for status, count in (jobs_now.get('by_status') or {}).items():
            if (status == 'starting'
                    or status in _EPICPROD_DISABLED_PLOT_JOB_STATES):
                continue
            values[f'job_{status}'] = int(count or 0)
        type_states = jobs_now.get('by_type_status') or {}
        for ptype, count in (jobs_now.get('by_type') or {}).items():
            states = type_states.get(ptype) or {}
            waiting = sum(int(states.get(status) or 0)
                          for status in
                          _EPICPROD_TYPE_EXCLUDED_JOB_STATES)
            values[f'type_{ptype}'] = max(0, int(count or 0) - waiting)
        for ptype, states in type_states.items():
            for status, count in (states or {}).items():
                if (status in ('activated', 'starting')
                        or status in _EPICPROD_DISABLED_PLOT_JOB_STATES):
                    continue
                values[f'ts_{ptype}_{status}'] = int(count or 0)
    for status in ('finished', 'failed'):
        if status in (jobs.get('cum') or {}):
            values[f'outcome_{status}'] = int(
                jobs['cum'].get(status) or 0)
    if tasks_now:
        for status, count in (tasks_now.get('by_status') or {}).items():
            if status in ('defined', 'ready'):
                continue
            values[f'task_{status}'] = int(count or 0)
    values.update(_site_curve_values(panda))
    values.update(_delivery_curve_values(state))
    values.update(_platform_curve_values(state))
    return values


# Monitor-host volume paths as curve-id segments.
_PLATFORM_VOLUME_SLUGS = {'/': 'root'}


def _platform_volume_slug(path):
    return _PLATFORM_VOLUME_SLUGS.get(path) or _group_slug(path) or 'root'


def _platform_curve_values(state):
    """Curves from the platform-health component (docs/SNAPPER_PLATFORM.md).
    Heartbeat-age tiers are recorded nested (older than 30, 60, 120
    minutes) and plotted as exclusive bands so the stack sums to the
    jobs older than 30 minutes; the per-site curves carry the oldest
    tier. A group that recorded a read failure emits no curves — the
    failure is stated on the card, never drawn as zero."""
    values = {}
    plat = component_data(state, 'platform')
    if not plat:
        return values
    hb = plat.get('heartbeats') or {}
    if hb and 'error' not in hb:
        values['plhb_received'] = int(hb.get('received') or 0)
        values['plhb_started'] = int(hb.get('started') or 0)
        if hb.get('yield') is not None:
            values['plhy_yield'] = float(hb['yield'])
        s30 = int(hb.get('stale_30') or 0)
        s60 = int(hb.get('stale_60') or 0)
        s120 = int(hb.get('stale_120') or 0)
        values['plst_30'] = max(0, s30 - s60)
        values['plst_60'] = max(0, s60 - s120)
        values['plst_120'] = s120
        for site, entry in (hb.get('sites') or {}).items():
            values[f'plss_{site}'] = int((entry or {}).get('stale_120') or 0)
    db = plat.get('database') or {}
    if db and 'error' not in db:
        for key in ('active', 'idle', 'waiting'):
            values[f'pldb_{key}'] = int(db.get(key) or 0)
    server = plat.get('server') or {}
    if server.get('latency_ms') is not None:
        values['plsv_latency'] = float(server['latency_ms'])
    host = plat.get('monitor_host') or {}
    load = host.get('load') or {}
    for key in ('1m', '5m', '15m'):
        if load.get(key) is not None:
            values[f'plml_{key}'] = float(load[key])
    memory = host.get('memory') or {}
    if memory.get('used_percent') is not None:
        values['plmm_used_pct'] = float(memory['used_percent'])
    for path, entry in (host.get('volumes') or {}).items():
        used = (entry or {}).get('used_percent')
        if used is not None:
            values[f'plmv_{_platform_volume_slug(path)}'] = float(used)
    for key in ('httpd', 'wsgi', 'asgi', 'ops_agent'):
        rss = (host.get(key) or {}).get('rss_mb')
        if rss is not None:
            values[f'plmp_{key}'] = float(rss)
    return values


def _epicprod_event_values(state):
    """Discrete events recorded in one epicprod snap, for event_flow
    families (snapper_ai series). Errors is the first event source."""
    return _errors_event_values(state)


def _errors_event_values(state):
    """Error events from the error-state component's interval record
    (docs/SNAPPER_ERRORS.md): one stamp per failed job at its end
    time, keyed into the category (perr_), component (perrc_), and
    per-task (terr_, terrc_) curves. Each stamp carries the job's
    terminal state as its event qualifier (series bins per-qualifier
    breakdowns for the view's terminal-state filter); rows recorded
    before the status column report 'unrecorded'. Overflow rows —
    storm intervals beyond the entry bound — carry no individual
    stamps; their exact counts land at the interval end, scope curves
    only, keyed 'category@status' since v3."""
    events = {}
    errors = component_data(state, 'errors')
    entries = errors.get('entries')
    if entries is None:
        return events
    for row in entries:
        try:
            taskid = int(row[1] or 0)
            comp, _, code = str(row[2]).partition(':')
            stamp = str(row[3])
        except (IndexError, TypeError, ValueError):
            continue
        status = (str(row[4]) if len(row) > 4 and row[4]
                  else 'unrecorded')
        event = [stamp, status]
        events.setdefault(f'perr_{comp}_{code}', []).append(event)
        events.setdefault(f'perrc_{comp}', []).append(event)
        if taskid:
            events.setdefault(
                f'terr_{taskid}_{comp}_{code}', []).append(event)
            events.setdefault(f'terrc_{taskid}_{comp}', []).append(event)
    overflow = errors.get('overflow') or {}
    interval_end = str((errors.get('interval') or {}).get('end') or '')
    if interval_end:
        for fold_key, count in (overflow.get('by_category') or {}).items():
            category, at, status = str(fold_key).partition('@')
            comp, _, code = str(category).partition(':')
            event = [interval_end, status if at and status else 'unrecorded']
            stamps = [event] * int(count or 0)
            events.setdefault(f'perr_{comp}_{code}', []).extend(stamps)
            events.setdefault(f'perrc_{comp}', []).extend(stamps)
    return events


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
    """(campaign, remainder) from a per-PC delivery curve id
    (dlvq_26_07_pc12 or dlvpc_26_07_pc12); campaign tags serialize
    dots as underscores."""
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

    # Operator-set colors on every jobs panel: the running pair reads
    # as blues — cores dark, running jobs lighter — and activated is
    # grey context rather than health/completion green.
    if curve_id == 'running_cores':
        return '#0d47a1'
    if curve_id.startswith('sjfw_'):
        return JOB_STATE_COLORS.get('activated')
    if curve_id.startswith('sjxw_'):
        return JOB_STATE_COLORS.get('failed')
    if curve_id.startswith('sjfin_'):
        return JOB_STATE_COLORS.get('finished')
    if curve_id.startswith('sjfail_'):
        return JOB_STATE_COLORS.get('failed')
    if curve_id.startswith('sjxc_'):
        return _FAILURE_CLASS_COLORS.get(
            curve_id.rsplit('_', 1)[1], '#424242')
    if curve_id.startswith('perrc_'):
        return _FAILURE_CLASS_COLORS.get(curve_id[6:], '#424242')
    if curve_id.startswith('terrc_'):
        return _FAILURE_CLASS_COLORS.get(
            curve_id[6:].partition('_')[2], '#424242')
    # Platform view: heartbeat-age bands climb warning → failure; the
    # connection limit and the yield line wear the operator dark blue;
    # waiting connections are the warning color.
    if curve_id == 'plst_30':
        return '#f9a825'
    if curve_id == 'plst_60':
        return '#ef6c00'
    if curve_id == 'plst_120':
        return '#c62828'
    if curve_id == 'plhy_yield':
        return '#0d47a1'
    if curve_id == 'pldb_waiting':
        return '#ef6c00'
    if curve_id == 'pldb_active':
        return '#1565c0'
    if curve_id == 'pldb_idle':
        return '#90caf9'
    if curve_id.startswith('qc_'):
        queue = curve_id[3:]
        members = _queue_stack_members()
        if queue == 'other':
            return _QUEUE_BAND_COLORS[
                len(members) % len(_QUEUE_BAND_COLORS)]
        if queue in members:
            return _QUEUE_BAND_COLORS[
                members.index(queue) % len(_QUEUE_BAND_COLORS)]
        return _QUEUE_BAND_COLORS[-1]
    if curve_id.startswith('sjc_'):
        return '#0d47a1'
    if curve_id.startswith('sj_'):
        status = curve_id.rsplit('_', 1)[1]
        if status == 'running':
            return '#64b5f6'
        if status == 'sent':
            return '#6a1b9a'
        if status == 'activated':
            # Grey: the queued pool is context, not the story — the
            # greens belong to completion and the blues to running.
            return '#8a8a8a'
        return JOB_STATE_COLORS.get(status)
    if curve_id.startswith('job_'):
        status = curve_id[4:]
        if status == 'running':
            return '#64b5f6'
        if status == 'sent':
            return '#6a1b9a'
        if status == 'activated':
            return '#8a8a8a'
        return JOB_STATE_COLORS.get(status)
    if curve_id.startswith('outcome_'):
        return JOB_STATE_COLORS.get(curve_id[8:])
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


_PLATFORM_LABELS = {
    'plhb_received': 'heartbeats received',
    'plhb_started': 'jobs started',
    'plhy_yield': 'yield',
    'pldb_active': 'active',
    'pldb_idle': 'idle',
    'pldb_waiting': 'waiting',
    'plsv_latency': 'is_alive latency',
    'plst_30': '30–60 min silent',
    'plst_60': '60–120 min silent',
    'plst_120': 'over 120 min silent',
    'plml_1m': '1 min',
    'plml_5m': '5 min',
    'plml_15m': '15 min',
    'plmm_used_pct': 'memory used',
    'plmv_root': '/',
    'plmp_httpd': 'httpd (all)',
    'plmp_wsgi': 'WSGI daemon',
    'plmp_asgi': 'ASGI (MCP)',
    'plmp_ops_agent': 'prod-ops agent',
}


def _epicprod_curve_label(curve_id):
    # Per-site curves: the family title names the site; the curve
    # label is the lifecycle stage alone. 'running' says 'running
    # jobs' — 'running cores' sits beside it and the bare word is
    # ambiguous.
    if curve_id in _PLATFORM_LABELS:
        return _PLATFORM_LABELS[curve_id]
    if curve_id.startswith('plss_'):
        return curve_id[5:]
    if curve_id.startswith('plmv_'):
        return '/' + curve_id[5:]
    if curve_id.startswith('perrc_'):
        return curve_id[6:]
    if curve_id.startswith('perr_'):
        comp, _, code = curve_id[5:].rpartition('_')
        return f'{comp} {code}'
    if curve_id.startswith('terrc_'):
        return curve_id[6:].partition('_')[2]
    if curve_id.startswith('terr_'):
        rest = curve_id[5:].partition('_')[2]
        comp, _, code = rest.rpartition('_')
        return f'{comp} {code}'
    if curve_id.startswith('qc_'):
        # The cores-by-queue stack: the member is the queue itself.
        return curve_id[3:]
    if curve_id.startswith('sjc_'):
        return 'running cores'
    if curve_id.startswith(('sjfw_', 'sjfin_')):
        return 'finished'
    if curve_id.startswith(('sjxw_', 'sjfail_')):
        return 'failed'
    if curve_id.startswith('sjxc_'):
        # Failure-class curves: the class is the last id segment.
        return curve_id.rsplit('_', 1)[1]
    if curve_id.startswith('sj_'):
        status = curve_id.rsplit('_', 1)[1]
        return 'running jobs' if status == 'running' else status
    if curve_id.startswith('stt_'):
        return curve_id.rsplit('_', 1)[1]
    if curve_id.startswith(('dlvq_', 'dlvqf_', 'dlvpc_', 'dlvpcf_')):
        _campaign, pc = _delivery_curve_parts(curve_id)
        key = _pc_cache()['keys'].get(pc, '')
        return f'{pc} {key}' if key else pc
    if curve_id.startswith(('dlvsp_', 'dlvspf_')):
        _campaign, slug = _delivery_curve_parts(curve_id)
        return (_pc_cache().get('species_names') or {}).get(slug, slug)
    if curve_id.startswith(('dlvc_', 'dlvcf_')):
        # The line states the group; the family header states
        # campaign, kind, and unit.
        _seg, _campaign, slug = _delivery_lens_parts(curve_id)
        if slug in ('', 'total'):
            return 'total'
        return _pc_cache()['group_names'].get(slug, slug)
    if curve_id == 'running_cores':
        return 'running cores'
    if curve_id.startswith('job_'):
        return f'jobs {curve_id[4:]}'
    if curve_id.startswith('outcome_'):
        return curve_id[8:]
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
    {'name': 'Running cores by queue', 'title': 'Running cores · by queue',
     'prefixes': ['qc_'], 'ids': [], 'stacked': True, 'panel_px': 150,
     'units': 'cores'},
    {'name': 'Job outcomes', 'prefixes': ['outcome_'], 'ids': [],
     'stacked': True, 'panel_px': 150,
     'order': ['outcome_finished', 'outcome_failed'],
     'window_relative': True},
    {'name': 'In-flight job types', 'title': 'Job types',
     'prefixes': ['type_'], 'ids': [], 'stacked': True, 'panel_px': 150,
     'units': 'jobs'},
    {'name': 'In-flight jobs', 'title': 'Job states', 'prefixes': ['job_'],
     'ids': [], 'default_off_ids': ['job_activated'],
     'stacked': True, 'panel_px': 150, 'units': 'jobs'},
    {'name': 'Type × state', 'prefixes': ['ts_'], 'ids': [],
     'stacked': True, 'panel_px': 150, 'units': 'jobs'},
    {'name': 'Tasks', 'prefixes': ['task_'], 'ids': [],
     'stacked': True, 'panel_px': 150, 'units': 'tasks'},
)


def _epicprod_scope_groups():
    """The compact scope families with the queue stack in rank order."""
    groups = [dict(group) for group in EPICPROD_GROUPS
              if group['name'] not in ('In-flight jobs', 'Type × state')]
    queue_order = [f'qc_{site}' for site in _queue_stack_members()]
    queue_order.append('qc_other')
    for group in groups:
        if group['name'] == 'Running cores by queue':
            group['order'] = queue_order
            break
    return tuple(groups)


_CAMPAIGN_START_CACHE = {'at': None, 'starts': {}}


def _campaign_delivery_starts():
    """Campaign name -> first recorded delivery activity, from the
    daily delivery snaps (small, bounded read), cached for an hour.
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
            .filter(scope='epicprod',
                    capture_policy__in=('backfill-v1', 'delivery-daily-v1'))
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


def _delivery_categories():
    """The stable top-level physics-category vocabulary used to split
    the campaign cumulative PC plots."""
    return tuple(sorted(set(_pc_cache()['categories'].values())
                        or {'Uncategorized'}))


def _delivery_pc_family_name(campaign, quantity, category):
    if category == 'Single Particle':
        return f'Cumulative {campaign} {quantity} species {category}'
    return f'Cumulative {campaign} {quantity} PCs {category}'


def _delivery_detail_key(kind, campaign):
    return f'delivery-{kind}-{_group_slug(campaign)}'


def _delivery_pc_detail_key(campaign, category):
    return (f'delivery-pcs-{_group_slug(campaign)}-'
            f'{_group_slug(category)}')


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
        'note': ("Click on a bin to see a breakdown of data arrivals "
                 "below. Daily bins are constructed overnight based on the "
                 "day's arrivals, hence no today bin."),
        # The natural campaign span carries thousands of unrelated
        # scope snaps and curves. Persist the selected campaign
        # families as their own small product for immediate display,
        # built from the delivery component's snaps alone — the scope's
        # frequent panda/health snaps carry nothing for this record.
        'cache_series': True,
        'components': ('delivery',),
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
                 ] + [
                     _delivery_pc_family_name(name, quantity, category)
                     for category in _delivery_categories()
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
    cache = _pc_cache()
    pc_groups = _pc_tick_groupings()
    categories = _delivery_categories()
    groups = []
    for name in campaigns:
        tag = name.replace('.', '_')
        groups.append({
            'name': f'Arrivals {name} files',
            'title': f'Arrivals {name}',
            'prefixes': [f'dlvqf_{tag}_'], 'ids': [],
            'stacked': True, 'end_stamped': True,
            'detail_key': _delivery_detail_key('arrivals', name),
            'pc_groups': pc_groups,
            'units': 'files'})
        groups.append({
            'name': f'Arrivals {name} events',
            'title': f'Arrivals {name}',
            'prefixes': [f'dlvq_{tag}_'], 'ids': [],
            'stacked': True, 'end_stamped': True,
            'detail_key': _delivery_detail_key('arrivals', name),
            'pc_groups': pc_groups,
            'default_off': True, 'units': 'events (M)'})
        for lens in DELIVERY_LENSES:
            seg, lens_value = lens['seg'], lens['value']
            category_stack = lens_value == 'category'
            category_names = categories if category_stack else ()
            category_file_ids = [
                f'dlvcf_{seg}_{tag}_{_group_slug(category)}'
                for category in category_names]
            category_event_ids = [
                f'dlvc_{seg}_{tag}_{_group_slug(category)}'
                for category in category_names]
            groups.append({
                'name': f'Cumulative {name} files {lens_value}',
                'title': f'Cumulative {name}',
                # The former wireframe carried an explicit total line.
                # A stack is the total of its category bands; including
                # that old line as another band would double the height.
                'prefixes': ([] if category_stack
                             else [f'dlvcf_{seg}_{tag}_']),
                'ids': category_file_ids,
                'order': category_file_ids,
                'stacked': category_stack,
                'cumulative_stack': category_stack,
                'panel_px': 300 if category_stack else 0,
                'detail_key': (_delivery_detail_key('categories', name)
                               if category_stack else ''),
                'focus_closed': category_stack,
                'default_off': True, 'units': 'files'})
            groups.append({
                'name': f'Cumulative {name} events {lens_value}',
                'title': f'Cumulative {name}',
                'prefixes': ([] if category_stack
                             else [f'dlvc_{seg}_{tag}_']),
                'ids': category_event_ids,
                'order': category_event_ids,
                'stacked': category_stack,
                'cumulative_stack': category_stack,
                'panel_px': 300 if category_stack else 0,
                'detail_key': (_delivery_detail_key('categories', name)
                               if category_stack else ''),
                'focus_closed': category_stack,
                'default_off': True, 'units': 'events (M)'})
        for category in categories:
            pcs = sorted(pc for pc, pc_category
                         in cache['categories'].items()
                         if pc_category == category)
            by_species = category == 'Single Particle'
            species_names = sorted({
                (cache.get('species') or {}).get(pc) or 'Unspecified'
                for pc in pcs
            }) if by_species else []
            file_ids = ([f'dlvspf_{tag}_{_species_slug(species)}'
                         for species in species_names]
                        if by_species
                        else [f'dlvpcf_{tag}_{pc}' for pc in pcs])
            event_ids = ([f'dlvsp_{tag}_{_species_slug(species)}'
                          for species in species_names]
                         if by_species
                         else [f'dlvpc_{tag}_{pc}' for pc in pcs])
            title = (f'Cumulative {name} · {category} by species'
                     if by_species else f'Cumulative {name} · {category}')
            groups.append({
                'name': _delivery_pc_family_name(name, 'files', category),
                'title': title,
                'prefixes': [],
                'ids': file_ids, 'order': file_ids,
                'stacked': True, 'cumulative_stack': True,
                'compact': not by_species,
                'detail_key': _delivery_pc_detail_key(name, category),
                'panel_px': 300, 'units': 'files'})
            groups.append({
                'name': _delivery_pc_family_name(name, 'events', category),
                'title': title,
                'prefixes': [],
                'ids': event_ids, 'order': event_ids,
                'stacked': True, 'cumulative_stack': True,
                'compact': not by_species,
                'detail_key': _delivery_pc_detail_key(name, category),
                'panel_px': 300, 'units': 'events (M)'})
    return tuple(groups)

_SITE_CACHE = {'at': None, 'sites': ()}


def _panda_sites():
    """Queue names from current PanDA activity plus Canary's queue list.

    Canary queues whose names contain ``test`` are deliberately excluded.
    Current in-flight jobs still determine the display order. The union is
    cached briefly and drives the per-queue families and Site-page queue
    options.
    """
    from django.utils import timezone

    from canary.store.models import Queue as CanaryQueue
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
    canary_sites = set(
        CanaryQueue.objects
        .exclude(name__icontains='test')
        .values_list('name', flat=True))
    sites = tuple(sorted(
        set(job_sites) | set(task_sites) | canary_sites,
        key=lambda site: (-int((job_sites.get(site) or {})
                               .get('in_flight_jobs_now') or 0), site)))
    _SITE_CACHE.update({'sites': sites, 'at': now})
    return sites


# Display order of the site jobs stack, bottom to top. Running cores is
# deliberately last: it is complementary capacity information in different
# units, so its band may move the outer boundary but never re-seat or distort
# any of the job-state boundaries below it.
_JOB_LIFECYCLE_EARLY = ('defined', 'waiting', 'assigned', 'activated',
                        'sent')
_JOB_LIFECYCLE_LATE = ('holding', 'transferring', 'merging')


def _site_groups():
    """Per-queue curve families on the Site page: in-flight jobs with cores,
    window-relative terminal outcomes, and tasks. Off by default on the
    scope view — the Site focus page is their home."""
    groups = []
    sites = list(_panda_sites())
    # A queue name extending another (SITE vs SITE_test) collides under
    # prefix matching: the longer sibling's curves are excluded from the
    # shorter name's families. Siblings are scanned over the full known
    # queue inventory, not the displayed site list — test-named queues
    # are excluded from display yet their curves exist in snap history.
    known = set(sites)
    try:
        from .models import PandaQueue
        known.update(
            PandaQueue.objects.values_list('queue_name', flat=True))
        from canary.store.models import Queue as CanaryQueue
        known.update(CanaryQueue.objects.values_list('name', flat=True))
    except Exception as e:                                   # noqa: BLE001
        logger.error('site sibling inventory failed: %s', e)
    for site in sites:
        longer = [s for s in known
                  if s != site and s.startswith(site + '_')]
        # Keep instantaneous job populations and terminal flow on
        # separate scales. Outcome staircases rise from zero at the
        # window's left edge; the displayed window is their integration
        # range.
        order = ([f'sj_{site}_{s}' for s in _JOB_LIFECYCLE_EARLY]
                 + [f'sj_{site}_{s}' for s in _JOB_LIFECYCLE_LATE]
                 + [f'sj_{site}_running', f'sjc_{site}'])
        groups.append({
            'name': f'Site jobs {site}',
            'title': f'Jobs · {site}',
            'prefixes': [f'sj_{site}_'],
            'exclude_prefixes': [f'sj_{s}_' for s in longer],
            'ids': [f'sjc_{site}'],
            'order': order,
            'default_off_ids': [f'sj_{site}_activated'],
            # Cores are a distinct quantity, not a job population: they
            # ride the jobs stack as a foreground line, never summed in.
            'overlay_ids': [f'sjc_{site}'],
            'stacked': True, 'panel_px': 300,
            'default_off': True})
        groups.append({
            'name': f'Site completions {site}',
            'title': f'Job completions · {site}',
            'prefixes': [],
            'ids': [f'sjfin_{site}', f'sjfail_{site}'],
            'order': [f'sjfin_{site}', f'sjfail_{site}'],
            'counter_flow': True, 'end_stamped': True,
            'stacked': True,
            'panel_px': 150, 'units': 'jobs',
            'default_off': True})
        groups.append({
            'name': f'Site outcomes {site}',
            'title': f'Job outcomes · {site}',
            'detail_key': site,
            'prefixes': [],
            'ids': [f'sjfw_{site}', f'sjxw_{site}'],
            'order': [f'sjfw_{site}', f'sjxw_{site}'],
            'window_relative': True, 'stacked': True,
            'panel_px': 150, 'units': 'jobs',
            'default_off': True})
        groups.append({
            'name': f'Site failures {site}',
            'title': f'Failures by class · {site}',
            'prefixes': [f'sjxc_{site}_'], 'ids': [],
            'exclude_prefixes': [f'sjxc_{s}_' for s in longer],
            'window_relative': True, 'stacked': True,
            'panel_px': 150, 'units': 'jobs',
            'focus_closed': True,
            'default_off': True})
        groups.append({
            'name': f'Site tasks {site}',
            'title': f'Tasks · {site}',
            'prefixes': [f'stt_{site}_'], 'ids': [],
            'exclude_prefixes': [f'stt_{s}_' for s in longer],
            'stacked': True, 'panel_px': 150, 'units': 'tasks',
            'focus_closed': True,
            'default_off': True})
    return tuple(groups)


def _epicprod_groups():
    """The epicprod curve families, resolved per render (the seam's
    callable form) so new campaigns and sites appear without an app
    restart."""
    return (EPICPROD_GROUPS + _delivery_groups() + _site_groups()
            + _errors_groups() + _platform_groups())


# The Platform view's families in panel order — load, platform,
# consequences (docs/SNAPPER_PLATFORM.md). The load and consequence
# families re-project curves the scope families already carry (in-flight
# jobs, outcomes, error events) under platform-view names so the page
# orders its panels itself; nothing is recorded twice.
_PLATFORM_FAMILIES_COMMON_HEAD = (
    'Platform heartbeats', 'Platform heartbeat yield')
_PLATFORM_FAMILIES_COMMON_TAIL = (
    'Platform DB connections', 'Platform server latency',
    'Platform monitor load', 'Platform monitor memory',
    'Platform monitor storage', 'Platform monitor processes',
    'Platform jobs', 'Platform kills', 'Platform outcomes')
PLATFORM_FAMILIES_BY_LENS = {
    'tiers': (_PLATFORM_FAMILIES_COMMON_HEAD
              + ('Platform staleness',) + _PLATFORM_FAMILIES_COMMON_TAIL),
    'sites': (_PLATFORM_FAMILIES_COMMON_HEAD
              + ('Platform stale by site',) + _PLATFORM_FAMILIES_COMMON_TAIL),
}


def _platform_groups():
    qualifier = {'qualifier_label': 'Terminal state',
                 'qualifier_param': 'states',
                 'qualifiers_off': ['closed']}
    lifecycle = ([f'job_{s}' for s in _JOB_LIFECYCLE_EARLY]
                 + [f'job_{s}' for s in _JOB_LIFECYCLE_LATE]
                 + ['job_running', 'running_cores'])
    # Panel order: the platform's own quantities first, then the load
    # and consequence panels beneath them for correlation by eye.
    return (
        {'name': 'Platform heartbeats', 'title': 'Heartbeats',
         'prefixes': [], 'ids': ['plhb_received', 'plhb_started'],
         'order': ['plhb_received', 'plhb_started'],
         'panel_px': 150, 'units': 'per interval'},
        {'name': 'Platform heartbeat yield', 'title': 'Heartbeat yield',
         'prefixes': [], 'ids': ['plhy_yield'],
         'panel_px': 110, 'units': 'received / expected'},
        {'name': 'Platform staleness', 'title': 'Heartbeat staleness',
         'prefixes': [], 'ids': ['plst_30', 'plst_60', 'plst_120'],
         'order': ['plst_30', 'plst_60', 'plst_120'],
         'stacked': True, 'panel_px': 150, 'units': 'running jobs'},
        {'name': 'Platform stale by site', 'title': 'Silent over 120 min · by site',
         'prefixes': ['plss_'], 'ids': [],
         'stacked': True, 'panel_px': 150, 'units': 'running jobs'},
        # The connection limit is stated on the card and in the summary
        # ('of N'); drawn on the plot it dwarfs the stack into a sliver.
        {'name': 'Platform DB connections', 'title': 'DB connections',
         'prefixes': [], 'ids': ['pldb_idle', 'pldb_active', 'pldb_waiting'],
         'order': ['pldb_idle', 'pldb_active', 'pldb_waiting'],
         'stacked': True, 'panel_px': 150, 'units': 'connections'},
        {'name': 'Platform server latency', 'title': 'Server latency',
         'prefixes': [], 'ids': ['plsv_latency'],
         'panel_px': 110, 'units': 'ms'},
        {'name': 'Platform monitor load', 'title': 'Monitor host load',
         'prefixes': ['plml_'], 'ids': [],
         'order': ['plml_1m', 'plml_5m', 'plml_15m'],
         'panel_px': 110, 'units': 'load average'},
        {'name': 'Platform monitor memory', 'title': 'Monitor host memory',
         'prefixes': [], 'ids': ['plmm_used_pct'],
         'panel_px': 110, 'units': '% used'},
        {'name': 'Platform monitor storage', 'title': 'Monitor host storage',
         'prefixes': ['plmv_'], 'ids': [],
         'panel_px': 110, 'units': '% used'},
        {'name': 'Platform monitor processes', 'title': 'Monitor host processes',
         'prefixes': ['plmp_'], 'ids': [],
         'order': ['plmp_httpd', 'plmp_wsgi', 'plmp_asgi', 'plmp_ops_agent'],
         'panel_px': 110, 'units': 'MB resident'},
        {'name': 'Platform jobs', 'title': 'Jobs in flight',
         'prefixes': ['job_'], 'ids': ['running_cores'],
         'order': lifecycle, 'default_off_ids': ['job_activated'],
         'overlay_ids': ['running_cores'],
         'stacked': True, 'panel_px': 220, 'units': 'jobs'},
        {'name': 'Platform kills', 'title': 'Faulty job events by component',
         'prefixes': ['perrc_'], 'ids': [],
         'event_flow': True, 'end_stamped': True, 'stacked': True,
         'member_ticks': False,
         'panel_px': 200, 'units': 'errors', **qualifier},
        {'name': 'Platform outcomes', 'title': 'Job outcomes',
         'prefixes': ['outcome_'], 'ids': [],
         'order': ['outcome_finished', 'outcome_failed'],
         'window_relative': True, 'stacked': True,
         'panel_px': 150, 'units': 'jobs'},
    )


def _platform_focus_view():
    """The Platform focus tab (docs/SNAPPER_PLATFORM.md): load,
    platform state, and consequences on one axis, the cut narrowed to
    the platform component's card with the summary across every
    metric. One option — the whole platform — with a staleness lens
    switching the heartbeat-age panel between age tiers and sites."""
    return {
        'param': 'platform',
        'label': 'Platform',
        'selector_label': 'Platform',
        'cache_series': True,
        'components': ('platform', 'panda', 'errors'),
        'prewarm_series': False,
        'note': ('Load above, platform state in the middle, consequences '
                 'below, on one time axis. Heartbeats and starts count '
                 'the 5-minute publication interval ending at each stamp; '
                 'yield is heartbeats received over the count expected '
                 'from the running population. Click the plot for the '
                 'platform state at that instant and the summary across '
                 'every metric.'),
        'default': 'overall',
        'selectors': [
            {'param': 'lens', 'label': 'Staleness',
             'default': 'tiers',
             'choices': [{'value': 'tiers', 'label': 'by age'},
                         {'value': 'sites', 'label': 'by site'}]},
        ],
        'options': [{'value': 'overall', 'label': 'Overall',
                     'families_by': {
                         lens: list(families)
                         for lens, families in PLATFORM_FAMILIES_BY_LENS.items()},
                     'component': 'platform'}],
    }


def _errors_groups():
    """Error-flood families (docs/SNAPPER_ERRORS.md): recorded error
    events by category or component, event-flow binned at render by
    each job's end time. Scope families only — a ?task= request
    synthesizes its per-task families through the focus view's
    open_option hook. Absent from the compact scope families, so they
    render only on the Errors focus page and in embeds that name
    them. Member ticks stay off: identification lives in hover and
    the breakdown below."""
    # The terminal-state chip declarations feed the observatory's
    # event-qualifier filter: chips self-discover from the recorded
    # statuses with counts over the visible range; closed is off by
    # default — the server disposed of those jobs for workflow
    # reasons, by design not actual errors (docs/SNAPPER_ERRORS.md).
    qualifier = {'qualifier_label': 'Terminal state',
                 'qualifier_param': 'states',
                 'qualifiers_off': ['closed']}
    return (
        {'name': 'Errors by category', 'title': 'Errors by category',
         'prefixes': ['perr_'], 'ids': [],
         'event_flow': True, 'end_stamped': True, 'stacked': True,
         'member_ticks': False,
         'panel_px': 300, 'units': 'errors', 'default_off': True,
         **qualifier},
        {'name': 'Errors by component', 'title': 'Errors by component',
         'prefixes': ['perrc_'], 'ids': [],
         'event_flow': True, 'end_stamped': True, 'stacked': True,
         'member_ticks': False,
         'panel_px': 300, 'units': 'errors', 'default_off': True,
         **qualifier},
        # Umbrella over every per-task error curve: the series build
        # resolves event_flow membership from registered families, and
        # per-task families are synthesized per request. Panels never
        # name this family — the synthesized per-task groups do that.
        {'name': 'Task errors', 'title': 'Task errors',
         'prefixes': ['terr_', 'terrc_'], 'ids': [],
         'event_flow': True, 'end_stamped': True, 'stacked': True,
         'member_ticks': False,
         'panel_px': 300, 'units': 'errors', 'default_off': True,
         **qualifier},
    )


def _errors_task_groups(taskid):
    """The synthesized per-task error families for one requested task."""
    qualifier = {'qualifier_label': 'Terminal state',
                 'qualifier_param': 'states',
                 'qualifiers_off': ['closed']}
    return (
        {'name': f'Task errors {taskid} category',
         'title': f'Errors · task {taskid}',
         'prefixes': [f'terr_{taskid}_'], 'ids': [],
         'event_flow': True, 'end_stamped': True, 'stacked': True,
         'member_ticks': False,
         'panel_px': 300, 'units': 'errors', 'default_off': True,
         **qualifier},
        {'name': f'Task errors {taskid} component',
         'title': f'Errors · task {taskid}',
         'prefixes': [f'terrc_{taskid}_'], 'ids': [],
         'event_flow': True, 'end_stamped': True, 'stacked': True,
         'member_ticks': False,
         'panel_px': 300, 'units': 'errors', 'default_off': True,
         **qualifier},
    )


def _errors_open_task(value):
    """open_option hook of the Errors focus view: any task id reached
    by link (the task page's Error History) is a valid filter; its
    families are synthesized for the request."""
    try:
        taskid = int(value)
    except (TypeError, ValueError):
        return None
    if taskid <= 0:
        return None
    return {
        'option': {
            'value': str(taskid),
            'label': f'Task {taskid}',
            'families_by': {
                'category': [f'Task errors {taskid} category'],
                'component': [f'Task errors {taskid} component']},
            'component': 'errors'},
        'groups': _errors_task_groups(taskid),
    }


def _errors_focus_view():
    """The Errors focus tab: recorded error events by category or
    component. The per-task reading is the overall view filtered
    (?task=, an open parameter — the PanDA task page links here); no
    task list is offered on the view itself."""
    return {
        'param': 'task',
        'label': 'Errors',
        'selector_label': 'Task',
        'cache_series': True,
        'components': ('errors',),
        'prewarm_series': False,
        'note': ('Each bin counts the jobs that ended with an error '
                 'in that interval; zooming in refines the bins down '
                 'to the recorded 5-minute quantum. The breakdown '
                 'below the plot reads at the clicked moment.'),
        'default': 'overall',
        'open_option': _errors_open_task,
        'selectors': [
            {'param': 'lens', 'label': 'Grouping',
             'default': 'category',
             'choices': [{'value': 'category', 'label': 'error category'},
                         {'value': 'component', 'label': 'error component'}]},
        ],
        'options': [{'value': 'overall', 'label': 'Overall',
                     'families_by': {
                         'category': ['Errors by category'],
                         'component': ['Errors by component']},
                     'component': 'errors'}],
    }


def _site_focus_view():
    """The Site focus tab: one queue's job lifecycle — submission
    through queueing to execution to the trailing finished/failed
    outcomes — with its tasks panel, and the cut narrowed to the panda
    component's queue detail."""
    sites = _panda_sites()
    if not sites:
        return None
    return {
        'param': 'site',
        'label': 'Site',
        'selector_label': 'Queue',
        # A Site view needs only the selected queue's PanDA curves. Sharing
        # the all-scope product makes a cold 30-day request assemble every
        # campaign and every queue before discarding nearly all of it.
        'cache_series': True,
        'components': ('panda',),
        # The delivery rebuild prewarms campaign products. It must not turn
        # that one job into a 30-day build for every PanDA queue.
        'prewarm_series': False,
        'note': ('In-flight counts are the recorded queue state through '
                 'time; finished and failed accumulate from the left '
                 'edge of the shown window — the window is the '
                 'integration range, and zooming re-bases it. Click '
                 'the plot for the full picture at that instant.'),
        'default': sites[0],
        'options': [
            {'value': site, 'label': site,
             'families': [f'Site jobs {site}',
                          f'Site completions {site}',
                          f'Site outcomes {site}',
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
    jobs = data.get('jobs') or {}
    prev_job_data = previous_data.get('jobs') or {}
    jobs_now = (data.get('jobs') or {}).get('in_flight_now') or {}
    prev_jobs = ((previous_data.get('jobs') or {})
                 .get('in_flight_now') or {})
    tasks_now = (data.get('tasks') or {}).get('in_flight_now') or {}
    prev_tasks = ((previous_data.get('tasks') or {})
                  .get('in_flight_now') or {})
    params = (ctx or {}).get('params') or {}

    def plotted_type_counts(block):
        by_type_status = block.get('by_type_status') or {}
        if not by_type_status:
            return {str(name): int(count or 0)
                    for name, count in (block.get('by_type') or {}).items()}
        return {
            str(ptype): sum(
                int(count or 0) for status, count in (states or {}).items()
                if status not in _EPICPROD_TYPE_EXCLUDED_JOB_STATES)
            for ptype, states in by_type_status.items()
        }

    type_counts = plotted_type_counts(jobs_now)
    prev_type_counts = plotted_type_counts(prev_jobs)
    types = [
        {'label': ptype, 'curve': f'type_{ptype}', 'value': value,
         'delta': cut_delta(value, prev_type_counts.get(ptype))}
        for ptype, value in sorted(
            type_counts.items(), key=lambda item: (-item[1], item[0]))
        if value or prev_type_counts.get(ptype)
    ]

    job_statuses = jobs_now.get('by_status') or {}
    prev_job_statuses = prev_jobs.get('by_status') or {}
    states = [
        {'label': status, 'curve': f'job_{status}',
         'value': int(count or 0),
         'delta': cut_delta(count, prev_job_statuses.get(status))}
        for status, count in sorted(job_statuses.items())
        if status not in _EPICPROD_TYPE_EXCLUDED_JOB_STATES
    ]

    task_statuses = tasks_now.get('by_status') or {}
    prev_task_statuses = prev_tasks.get('by_status') or {}
    tasks = [
        {'label': status, 'curve': f'task_{status}',
         'value': int(count or 0),
         'delta': cut_delta(count, prev_task_statuses.get(status))}
        for status, count in sorted(task_statuses.items())
        if status not in ('defined', 'ready')
    ]

    cut_cum = jobs.get('cum') or {}
    prev_cum = prev_job_data.get('cum') or {}
    since_jobs = (((ctx or {}).get('since_data') or {}).get('jobs') or {})
    basis_cum = since_jobs.get('cum') or {}
    have_basis = (ctx or {}).get('since') is not None
    outcomes = []
    for status in ('finished', 'failed'):
        current = int(cut_cum.get(status) or 0)
        basis = int(basis_cum.get(status) or 0)
        outcomes.append({
            'label': status, 'curve': f'outcome_{status}',
            'value': max(0, current - basis) if have_basis else current,
            'delta': cut_delta(current, prev_cum.get(status)),
        })
    # The Site focus narrows the card to the selected sites' detail:
    # the germane facts of the slice, color-coded as in the plot —
    # window outcomes first (differenced against the ?since= basis,
    # the view's left edge), then the in-flight standing in lifecycle
    # order, then tasks. One swatch per fact, no repetition.
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
    # Cores by queue: the scope view's stacked panel read as numbers,
    # folding the tail into 'other' exactly as the curves do.
    requested_queues = ((ctx.get('params') or {}).get('queues') or '')
    tracked_order = tuple(
        site for site in requested_queues.split(',') if site
    )[:QUEUE_STACK_MAX] or _queue_stack_members()
    tracked = set(tracked_order)
    site_blocks = (data.get('jobs') or {}).get('sites') or {}
    prev_blocks = (previous_data.get('jobs') or {}).get('sites') or {}
    queue_cores = []
    for site in tracked_order:
        cores = int((site_blocks.get(site) or {})
                    .get('running_cores_now') or 0)
        was = int((prev_blocks.get(site) or {})
                  .get('running_cores_now') or 0)
        queue_cores.append({'site': site, 'curve': f'qc_{site}',
                            'value': cores,
                            'delta': cut_delta(cores, was)})
    other_now = other_prev = 0
    for site in set(site_blocks) | set(prev_blocks):
        cores = int((site_blocks.get(site) or {})
                    .get('running_cores_now') or 0)
        was = int((prev_blocks.get(site) or {})
                  .get('running_cores_now') or 0)
        if site not in tracked:
            other_now += cores
            other_prev += was
    queue_cores.append({'site': 'other', 'curve': 'qc_other',
                        'value': other_now,
                        'delta': cut_delta(other_now, other_prev)})

    site_only = bool(sites) and compact
    return {'kind': 'panda', 'types': types, 'states': states,
            'tasks': tasks, 'outcomes': outcomes,
            'outcomes_basis': ('Since window start'
                               if have_basis else 'Cumulative'),
            'sites': sites, 'queue_cores': queue_cores,
            'site_only': site_only, 'split_panels': not site_only}


def _errors_card(data, previous_data, ctx):
    """The error-state cut card: the breakdown over the detail window
    around the cut — the clicked display bin when it is an hour or
    wider, else the hour around the cut (docs/SNAPPER_ERRORS.md).
    Category counts and the component-share donut aggregate the
    recorded interval entries over the window; the diagnostic
    patterns aggregate live from the job records over the same
    bounds. A task selection (?task=) narrows everything to that
    task's events."""
    import math
    from datetime import datetime, timedelta
    from datetime import timezone as dt_timezone
    from urllib.parse import quote

    from django.urls import reverse
    from zoneinfo import ZoneInfo

    from snapper_ai.models import SystemSnap

    from .panda.error_labels import category_label
    from .snapper_errors import (
        MAX_PATTERN_SITES,
        MAX_PATTERN_TASKS,
        MAX_PATTERNS,
        error_axes,
        error_patterns,
    )

    if data.get('entries') is None:
        # A counter-era snap holds no interval record to read.
        return None
    params = ctx.get('params') or {}
    selected = [v for v in (params.get('task') or '').split(',')
                if v and v != 'overall']
    single_task = selected[0] if len(selected) == 1 else None
    # The terminal-state chip selection (?states=): entries filter on
    # the recorded status, live job-record queries restrict to the
    # real statuses among it. Absent means unrestricted.
    state_filter = [v for v in (params.get('states') or '').split(',')
                    if v]

    def _parse(iso):
        try:
            parsed = datetime.fromisoformat(
                str(iso or '').replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        return parsed

    window_from = _parse(params.get('from'))
    window_to = _parse(params.get('to'))
    if (window_from is None or window_to is None
            or not window_from < window_to):
        # No client bounds: the hour ending at the cut snap's interval.
        window_to = _parse((data.get('interval') or {}).get('end'))
        if window_to is None:
            return None
        window_from = window_to - timedelta(hours=1)

    # Entries over the window from the recorded snaps. Snap intervals
    # tile, so snaps stamped in (from, to + one capture interval]
    # cover every event in bounds; an unchanged component can repeat
    # across snaps, so intervals dedup by their end.
    states = (SystemSnap.objects
              .filter(scope='epicprod',
                      snap_time__gt=window_from,
                      snap_time__lte=window_to + timedelta(minutes=10),
                      state__components__errors__data__has_key='entries')
              .order_by('snap_time')
              .values_list('state', flat=True))
    cat_counts = {}
    total = 0
    seen_intervals = set()
    for state in states.iterator():
        errors = (((state.get('components') or {}).get('errors')
                   or {}).get('data')) or {}
        interval = errors.get('interval') or {}
        interval_key = str(interval.get('end') or '')
        if not interval_key or interval_key in seen_intervals:
            continue
        seen_intervals.add(interval_key)
        for row in errors.get('entries') or []:
            try:
                taskid = str(int(row[1] or 0))
                category = str(row[2])
                when = _parse(row[3])
            except (IndexError, TypeError, ValueError):
                continue
            if when is None or when <= window_from or when > window_to:
                continue
            if selected and taskid not in selected:
                continue
            status = (str(row[4]) if len(row) > 4 and row[4]
                      else 'unrecorded')
            if state_filter and status not in state_filter:
                continue
            cat_counts[category] = (cat_counts.get(category) or 0) + 1
            total += 1
        if not selected:
            interval_end = _parse(interval.get('end'))
            if (interval_end is not None
                    and window_from < interval_end <= window_to):
                # Overflow folds key 'component:code@status' since v3;
                # earlier keys carry no status and read 'unrecorded'.
                for fold_key, count in ((errors.get('overflow') or {})
                                        .get('by_category') or {}).items():
                    category, at, status = str(fold_key).partition('@')
                    if not (at and status):
                        status = 'unrecorded'
                    if state_filter and status not in state_filter:
                        continue
                    cat_counts[category] = (
                        cat_counts.get(category) or 0) + int(count or 0)
                    total += int(count or 0)

    eastern = ZoneInfo('America/New_York')
    from_et = window_from.astimezone(eastern)
    to_et = window_to.astimezone(eastern)
    basis_text = (from_et.strftime('%m-%d %H:%M') + ' – '
                  + to_et.strftime(
                      '%H:%M ET' if to_et.date() == from_et.date()
                      else '%m-%d %H:%M ET'))

    errors_base = reverse('monitor_app:panda_errors_list')
    jobs_base = reverse('monitor_app:panda_jobs_list')
    window_days = max(1, math.ceil(
        (window_to - window_from).total_seconds() / 86400))
    window_q = (f'&days={window_days}&ended_after='
                + quote(window_from.isoformat())
                + '&ended_before=' + quote(window_to.isoformat()))

    def _errors_url(comp):
        # No single-status pin: the record counts every faulty status
        # (failed, cancelled, closed), and a status=failed link on a
        # kill storm — closed jobs — lands on an empty page. The
        # active terminal-state selection carries over instead, so
        # the pattern page shows the same population as the card.
        query = []
        if comp and comp != 'other':
            query.append(f'classified=1&error_source={quote(comp)}')
        if single_task:
            query.append(f'taskid={quote(single_task)}')
        real_states = [s for s in state_filter if s != 'unrecorded']
        if real_states:
            query.append('status=' + quote(','.join(real_states)))
        joined = '&'.join(query)
        return (errors_base + '?'
                + (joined + window_q if joined else window_q.lstrip('&')))

    rows = []
    comp_counts = {}
    for key in sorted(cat_counts, key=lambda k: (-cat_counts[k], k)):
        comp, _, code = str(key).partition(':')
        count = cat_counts[key]
        comp_counts[comp] = comp_counts.get(comp, 0) + count
        curve = (f'terr_{single_task}_{comp}_{code}' if single_task
                 else f'perr_{comp}_{code}')
        rows.append({
            'label': category_label(comp, code),
            'curve': curve,
            'url': _errors_url(comp),
            'window': str(count),
            'delta': '',
        })
    rows = rows[:24]

    # The donut follows the active lens and wears the plot's own
    # colors — the same data-curve painting as the table swatches, so
    # plot, rows, and donut tell one color story. Small shares fold
    # into a grey remainder slice.
    lens = str(params.get('lens') or 'category')
    if lens == 'component':
        shares = comp_counts

        def _slice_curve(key):
            return (f'terrc_{single_task}_{key}' if single_task
                    else f'perrc_{key}')

        def _slice_label(key):
            return key

        def _slice_url(key):
            return _errors_url(key)
    else:
        shares = cat_counts

        def _slice_curve(key):
            comp, _, code = key.partition(':')
            return (f'terr_{single_task}_{comp}_{code}' if single_task
                    else f'perr_{comp}_{code}')

        def _slice_label(key):
            comp, _, code = key.partition(':')
            return category_label(comp, code)

        def _slice_url(key):
            return _errors_url(key.partition(':')[0])

    pie = []
    if total:
        max_slices = 12
        ranked = sorted(shares.items(), key=lambda kv: (-kv[1], kv[0]))
        slices = [
            {'count': count, 'curve': _slice_curve(key),
             'color': None, 'url': _slice_url(key),
             'label': _slice_label(key)}
            for key, count in ranked[:max_slices]]
        folded = sum(count for _, count in ranked[max_slices:])
        if folded:
            slices.append({
                'count': folded, 'curve': '', 'color': '#9e9e9e',
                'url': _errors_url(''),
                'label': f'{len(ranked) - max_slices} more'})
        tau = 2 * math.pi
        angle = 0.0
        for entry in slices:
            span = tau * entry['count'] / total
            pie.append({
                'path': _pie_segment(60, 60, 26, 58, angle, angle + span),
                'curve': entry['curve'],
                'color': entry['color'],
                'url': entry['url'],
                'title': (f"{entry['label']} · {entry['count']:,} "
                          f"({entry['count'] / total:.0%})")})
            angle += span

    # Diagnostic patterns aggregate live from the job records over the
    # same bounds; a single-task filter restricts the query itself,
    # a multi-task selection filters the aggregated rows.
    patterns = []
    window_patterns = 0
    for (comp, code, _pattern, diag, count, rep, taskids,
         pattern_sites) in error_patterns(
            window_from, window_to,
            taskid=int(single_task) if single_task else None,
            statuses=state_filter or None):
        task_list = sorted({int(t) for t in (taskids or []) if t})
        if (selected and not single_task
                and not any(str(t) in selected for t in task_list)):
            continue
        window_patterns += 1
        if len(patterns) < MAX_PATTERNS:
            rep = int(rep or 0)
            site_list = sorted({str(s) for s in (pattern_sites or []) if s})
            patterns.append({
                'category': category_label(comp, code),
                'curve': f'perr_{comp}_{code}',
                'diag': str(diag or ''),
                'count': int(count or 0),
                'rep_pandaid': rep,
                'rep_url': (reverse('monitor_app:panda_job_detail',
                                    args=[rep]) if rep else ''),
                'tasks': task_list[:MAX_PATTERN_TASKS],
                'sites': site_list[:MAX_PATTERN_SITES],
                'site_overflow': max(0, len(site_list) - MAX_PATTERN_SITES),
            })

    # The attribution reading: where the window's errors concentrate,
    # axis by axis — category, task, site — with spread itself a
    # conclusion. Deterministic shares from one live scan
    # (error_axes); the task axis is omitted when the view is already
    # filtered to tasks.
    axes = error_axes(window_from, window_to, taskids=selected or None,
                      statuses=state_filter or None)

    def _axis_item(values, singular, plural, label_of, url_of):
        axis_total = sum(values.values())
        if not axis_total:
            return None
        ranked = sorted(values.items(),
                        key=lambda kv: (-kv[1], str(kv[0])))
        top_key, top_count = ranked[0]
        share = top_count / axis_total
        n = len(ranked)
        if n == 1:
            pre, post = f'single {singular}: ', ' (100%)'
        elif share >= 0.9:
            pre, post = (f'essentially one {singular}: ',
                         f' ({share:.0%} of {n})')
        elif share >= 0.55:
            pre, post = (f'{singular}-dominated: ',
                         f' ({share:.0%} of {n})')
        elif share >= 0.3:
            pre, post = ('led by ', f' ({share:.0%} of {n} {plural})')
        else:
            pre, post = (f'spread over {n} {plural} — largest ',
                         f' ({share:.0%})')
        return {'pre': pre, 'label': label_of(top_key),
                'post': post, 'url': url_of(top_key)}

    reading = []
    item = _axis_item(
        axes['categories'], 'category', 'categories',
        lambda key: category_label(*str(key).partition(':')[::2]),
        lambda key: _errors_url(str(key).partition(':')[0]))
    if item:
        reading.append(item)
    if not selected:
        item = _axis_item(
            axes['tasks'], 'task', 'tasks',
            lambda t: f'task {t}',
            lambda t: reverse('monitor_app:panda_task_detail',
                              kwargs={'jeditaskid': t}))
        if item:
            reading.append(item)
    item = _axis_item(
        axes['sites'], 'site', 'sites',
        lambda s: str(s),
        lambda s: f'{jobs_base}?site={quote(str(s))}' + window_q)
    if item:
        reading.append(item)

    return {
        'kind': 'errors',
        'task_selection': ', '.join(selected),
        'basis': basis_text,
        'reading': reading,
        'rows': rows,
        'row_overflow': max(0, len(cat_counts) - len(rows)),
        'row_overflow_note': 'more categories',
        'pie': pie,
        'pie_label': ('Component shares' if lens == 'component'
                      else 'Category shares'),
        'pie_size': min(360, max(200, 30 * (len(rows) + 1))),
        'window_total': total,
        'detail_window_minutes': int(
            (window_to - window_from).total_seconds() // 60),
        'window_errors': total,
        'window_patterns': window_patterns,
        'patterns': patterns,
        'errors_url': _errors_url(''),
        'jobs_url': (f'{jobs_base}?status=failed'
                     + (f'&taskid={quote(single_task)}'
                        if single_task else '') + window_q),
    }


# Summary rows of the Platform view's cut: label, curve id for the
# swatch ('' when the row is a total no single curve draws), unit, and
# the extractor over (platform data, panda data). Order = panel order.
_PLATFORM_SUMMARY_SPECS = (
    ('heartbeats received', 'plhb_received', 'per interval',
     lambda p, j: (p.get('heartbeats') or {}).get('received')),
    ('jobs started', 'plhb_started', 'per interval',
     lambda p, j: (p.get('heartbeats') or {}).get('started')),
    ('heartbeat yield', 'plhy_yield', '',
     lambda p, j: (p.get('heartbeats') or {}).get('yield')),
    ('silent 30–60 min', 'plst_30', 'jobs',
     lambda p, j: _platform_band(p, 30, 60)),
    ('silent 60–120 min', 'plst_60', 'jobs',
     lambda p, j: _platform_band(p, 60, 120)),
    ('silent over 120 min', 'plst_120', 'jobs',
     lambda p, j: (p.get('heartbeats') or {}).get('stale_120')),
    ('DB connections', '', 'of limit',
     lambda p, j: (p.get('database') or {}).get('connections')),
    ('DB active', 'pldb_active', 'connections',
     lambda p, j: (p.get('database') or {}).get('active')),
    ('DB waiting', 'pldb_waiting', 'connections',
     lambda p, j: (p.get('database') or {}).get('waiting')),
    ('longest transaction', '', 's',
     lambda p, j: (p.get('database') or {}).get('longest_transaction_s')),
    ('server latency', 'plsv_latency', 'ms',
     lambda p, j: (p.get('server') or {}).get('latency_ms')),
    ('monitor load (1 min)', 'plml_1m', '',
     lambda p, j: ((p.get('monitor_host') or {}).get('load') or {}).get('1m')),
    ('monitor memory used', 'plmm_used_pct', '%',
     lambda p, j: ((p.get('monitor_host') or {}).get('memory') or {}).get('used_percent')),
    ('monitor WSGI resident', 'plmp_wsgi', 'MB',
     lambda p, j: ((p.get('monitor_host') or {}).get('wsgi') or {}).get('rss_mb')),
    ('jobs in flight', '', 'jobs',
     lambda p, j: ((j.get('jobs') or {}).get('in_flight_now') or {}).get('total')),
    ('running jobs', 'job_running', 'jobs',
     lambda p, j: ((j.get('jobs') or {}).get('in_flight_now') or {}).get('running_jobs')),
    ('running cores', 'running_cores', 'cores',
     lambda p, j: ((j.get('jobs') or {}).get('in_flight_now') or {}).get('running_cores')),
)


def _platform_band(plat, lower, upper):
    hb = plat.get('heartbeats') or {}
    if 'error' in hb or f'stale_{lower}' not in hb:
        return None
    return max(0, int(hb.get(f'stale_{lower}') or 0)
               - int(hb.get(f'stale_{upper}') or 0))


def _platform_summary_walk(scope, since, until, limit=3000):
    """(snap_time, platform data, panda data) rows over (since, until],
    newest first, from the snaps carrying the platform component —
    only the two components' payloads leave the database."""
    from django.db.models.fields.json import KeyTransform

    from snapper_ai.models import SystemSnap

    query = (SystemSnap.objects
             .filter(scope=scope, snap_time__lte=until,
                     state__components__has_key='platform'))
    if since is not None:
        query = query.filter(snap_time__gt=since)
    rows = (query.order_by('-snap_time')
            .annotate(plat=KeyTransform('platform',
                                        KeyTransform('components', 'state')),
                      pand=KeyTransform('panda',
                                        KeyTransform('components', 'state')))
            .values_list('snap_time', 'plat', 'pand')[:limit + 1])
    out = []
    for snap_time, plat, pand in rows:
        out.append((snap_time,
                    (plat or {}).get('data') or {},
                    (pand or {}).get('data') or {}))
    return out


def _faulty_events_in_window(scope, window_from, window_to):
    """Total faulty job events and per-component counts over the
    window, from the error-state component's interval entries (the
    same aggregation the errors card performs)."""
    from datetime import datetime, timedelta
    from datetime import timezone as dt_timezone

    from snapper_ai.models import SystemSnap

    def _parse(iso):
        try:
            parsed = datetime.fromisoformat(str(iso or '').replace('Z', '+00:00'))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)

    states = (SystemSnap.objects
              .filter(scope=scope, snap_time__gt=window_from,
                      snap_time__lte=window_to + timedelta(minutes=10),
                      state__components__errors__data__has_key='entries')
              .order_by('snap_time')
              .values_list('state', flat=True))
    total = 0
    by_component = {}
    seen = set()
    for state in states.iterator():
        errors = (((state.get('components') or {}).get('errors')
                   or {}).get('data')) or {}
        interval = errors.get('interval') or {}
        key = str(interval.get('end') or '')
        if not key or key in seen:
            continue
        seen.add(key)
        for row in errors.get('entries') or []:
            try:
                comp = str(row[2]).partition(':')[0]
                when = _parse(row[3])
            except (IndexError, TypeError):
                continue
            if when is None or when <= window_from or when > window_to:
                continue
            total += 1
            by_component[comp] = by_component.get(comp, 0) + 1
        interval_end = _parse(interval.get('end'))
        if interval_end is not None and window_from < interval_end <= window_to:
            for fold_key, count in ((errors.get('overflow') or {})
                                    .get('by_category') or {}).items():
                comp = str(fold_key).partition(':')[0]
                total += int(count or 0)
                by_component[comp] = by_component.get(comp, 0) + int(count or 0)
    return total, by_component


def _scope_cum_at(scope, instant):
    """The panda component's scope-level cumulative outcome counters at
    the nearest counter-bearing snap at or before the instant."""
    from snapper_ai.models import SystemSnap

    if instant is None:
        return {}
    row = (SystemSnap.objects
           .filter(scope=scope, snap_time__lte=instant,
                   state__components__panda__data__jobs__has_key='cum')
           .order_by('-snap_time')
           .values('state').first())
    if not row:
        return {}
    jobs = (((((row['state'] or {}).get('components') or {})
              .get('panda') or {}).get('data') or {}).get('jobs') or {})
    return jobs.get('cum') or {}


def _platform_card(data, previous_data, ctx):
    """The platform cut card (docs/SNAPPER_PLATFORM.md): the assessment
    against thresholds, the database, heartbeat, server, and
    monitor-host detail at the instant, and — last — the summary
    across every metric the view plots: value at the cut, change
    against the previous snap, and min/mean/max over the shown range,
    read from the coherent snaps carrying the platform component."""
    from datetime import datetime, timedelta
    from datetime import timezone as dt_timezone
    from urllib.parse import quote

    from django.urls import reverse

    if not data or 'heartbeats' not in data:
        return None
    params = (ctx or {}).get('params') or {}
    scope = (ctx or {}).get('scope') or 'epicprod'
    requested_at = (ctx or {}).get('requested_at')
    since = (ctx or {}).get('since')
    hb = data.get('heartbeats') or {}
    db = data.get('database') or {}
    server = data.get('server') or {}
    host = data.get('monitor_host') or {}
    assessment = data.get('assessment') or {}
    thresholds = assessment.get('thresholds') or {}

    threshold_text = {
        'heartbeat_yield': f"warn below {thresholds.get('platform_yield_warn_below')}",
        'heartbeat_staleness': (
            f"warn when over {thresholds.get('platform_stale_warn_fraction')} "
            f"of running jobs are silent {thresholds.get('platform_stale_warn_tier_minutes')}+ min"),
        'db_connections': (
            f"warn above {thresholds.get('platform_connections_warn_fraction')} of the limit"),
        'server_latency': f"warn above {thresholds.get('platform_latency_warn_ms')} ms or not ok",
        'monitor_volumes': f"warn above {thresholds.get('platform_volume_warn_percent')}% used",
        'monitor_services': 'warn when the ASGI or prod-ops service is not active',
        'reporter': f"stale after {thresholds.get('platform_reporter_stale_seconds')} s",
    }
    verdicts = [
        {'name': name.replace('_', ' '), 'chip': cut_chip(
            'ok' if verdict == 'ok' else 'warning' if verdict == 'warning'
            else 'unknown'),
         'threshold': threshold_text.get(name, '')}
        for name, verdict in (assessment.get('verdicts') or {}).items()]

    # Detail tables.
    running = int(hb.get('running') or 0)
    site_rows = []
    site_url = reverse('snapper_ai:snapper_focus',
                       kwargs={'scope': scope, 'focus_slug': 'site'})
    cut_q = (f'&cut={quote(requested_at.isoformat())}'
             if requested_at is not None else '')
    for site, entry in sorted((hb.get('sites') or {}).items(),
                              key=lambda kv: -int((kv[1] or {}).get('running') or 0)):
        entry = entry or {}
        if not int(entry.get('running') or 0):
            continue
        site_rows.append({
            'site': site,
            'url': f'{site_url}?site={quote(site)}{cut_q}',
            'running': int(entry.get('running') or 0),
            'received': int(entry.get('received') or 0),
            'stale_30': int(entry.get('stale_30') or 0),
            'stale_60': int(entry.get('stale_60') or 0),
            'stale_120': int(entry.get('stale_120') or 0),
        })
    by_app = sorted((db.get('by_app') or {}).items(), key=lambda kv: -kv[1])
    j4 = db.get('jobsactive4') or {}
    volumes = [{'path': path, **(entry or {})}
               for path, entry in sorted((host.get('volumes') or {}).items())]
    processes = [
        {'label': label, **(host.get(key) or {})}
        for key, label in (('httpd', 'httpd (all processes)'),
                           ('wsgi', 'WSGI daemon'),
                           ('asgi', 'ASGI service (MCP)'),
                           ('ops_agent', 'prod-ops agent'))]

    # Faulty events over the detail window (the errors card's window
    # convention: the client's from/to, else the hour ending at the
    # cut interval).
    def _parse(iso):
        try:
            parsed = datetime.fromisoformat(str(iso or '').replace('Z', '+00:00'))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)

    window_from = _parse(params.get('from'))
    window_to = _parse(params.get('to'))
    if window_from is None or window_to is None or not window_from < window_to:
        window_to = _parse((data.get('interval') or {}).get('end')) or requested_at
        window_from = (window_to - timedelta(hours=1)) if window_to else None
    kills_total, kills_by_comp = (0, {})
    kills_error = ''
    if window_from and window_to:
        try:
            kills_total, kills_by_comp = _faulty_events_in_window(
                scope, window_from, window_to)
        except Exception as e:                               # noqa: BLE001
            logger.error('platform card: faulty-event window failed: %s', e)
            kills_error = f'faulty-event count failed: {e}'

    # The summary walk over the shown range.
    walk = []
    walk_error = ''
    if requested_at is not None:
        try:
            walk = _platform_summary_walk(scope, since, requested_at)
        except Exception as e:                               # noqa: BLE001
            logger.error('platform card: summary walk failed: %s', e)
            walk_error = f'range statistics failed: {e}'
    capped = len(walk) > 3000
    walk = walk[:3000]
    at = walk[0] if walk else (requested_at, data, {})
    prev = walk[1] if len(walk) > 1 else None
    warn_rows = set()
    verdict_map = assessment.get('verdicts') or {}
    if verdict_map.get('heartbeat_yield') == 'warning':
        warn_rows.add('heartbeat yield')
    if verdict_map.get('heartbeat_staleness') == 'warning':
        warn_rows.update({'silent 60–120 min', 'silent over 120 min'})
    if verdict_map.get('db_connections') == 'warning':
        warn_rows.add('DB connections')
    if verdict_map.get('server_latency') == 'warning':
        warn_rows.add('server latency')
    if verdict_map.get('monitor_volumes') == 'warning':
        warn_rows.add('monitor storage')
    summary = []
    for label, curve, unit, extract in _PLATFORM_SUMMARY_SPECS:
        value = extract(at[1], at[2])
        previous = extract(prev[1], prev[2]) if prev else None
        samples = [v for v in (extract(p, j) for _, p, j in walk)
                   if isinstance(v, (int, float)) and not isinstance(v, bool)]
        stats = None
        if samples:
            lo, hi = min(samples), max(samples)
            mean = sum(samples) / len(samples)
            position = (None if hi == lo or not isinstance(value, (int, float))
                        else round(100 * (value - lo) / (hi - lo)))
            stats = {'min': _fmt_num(lo), 'mean': _fmt_num(mean),
                     'max': _fmt_num(hi), 'position': position}
        summary.append({
            'label': label, 'curve': curve, 'unit': unit,
            'value': _fmt_num(value), 'delta': cut_delta(
                round(value) if isinstance(value, float) else value,
                round(previous) if isinstance(previous, float) else previous)
            if isinstance(value, (int, float)) and isinstance(previous, (int, float))
            and label != 'heartbeat yield' else (
                _fmt_signed(value - previous)
                if label == 'heartbeat yield'
                and isinstance(value, (int, float))
                and isinstance(previous, (int, float)) else None),
            'stats': stats, 'warn': label in warn_rows,
        })
    if db.get('max_connections'):
        for row in summary:
            if row['label'] == 'DB connections':
                row['unit'] = f"of {db['max_connections']}"
    # Consequence rows: faulty events over the detail window, outcomes
    # since the shown range's start (the counters' window basis).
    summary.append({
        'label': 'faulty job events', 'curve': '',
        'unit': 'in the detail window', 'value': _fmt_num(kills_total),
        'delta': None, 'stats': None, 'warn': False,
        'detail': ', '.join(f'{c} {n}' for c, n in sorted(
            kills_by_comp.items(), key=lambda kv: -kv[1])[:6]),
    })
    cum_at = _scope_cum_at(scope, requested_at)
    cum_since = _scope_cum_at(scope, since) if since is not None else {}
    for status in ('finished', 'failed'):
        current = int(cum_at.get(status) or 0)
        basis = int(cum_since.get(status) or 0)
        summary.append({
            'label': f'{status} since range start', 'curve': f'outcome_{status}',
            'unit': 'jobs', 'value': _fmt_num(max(0, current - basis))
            if cum_at else '—', 'delta': None, 'stats': None, 'warn': False,
        })

    range_text = ''
    if walk:
        first, last = walk[-1][0], walk[0][0]
        range_text = (f"{first.astimezone(ET_ZONE).strftime('%m-%d %H:%M')} – "
                      f"{last.astimezone(ET_ZONE).strftime('%m-%d %H:%M ET')}, "
                      f"{len(walk)} snaps"
                      + (' (the newest 3000)' if capped else ''))
    window_text = ''
    if window_from and window_to:
        window_text = (f"{window_from.astimezone(ET_ZONE).strftime('%m-%d %H:%M')} – "
                       f"{window_to.astimezone(ET_ZONE).strftime('%H:%M ET')}")
    errors_url = (reverse('snapper_ai:snapper_focus',
                          kwargs={'scope': scope, 'focus_slug': 'errors'})
                  + (f'?cut={quote(requested_at.isoformat())}'
                     if requested_at is not None else ''))
    return {
        'kind': 'platform',
        'overall': assessment.get('overall') or 'unknown',
        'overall_chip': cut_chip(
            'ok' if assessment.get('overall') == 'ok'
            else 'warning' if assessment.get('overall') == 'warning'
            else 'unknown'),
        'verdicts': verdicts,
        'interval': data.get('interval') or {},
        'reporter_status': data.get('reporter_status') or 'absent',
        'heartbeats': {
            'running': running,
            'received': int(hb.get('received') or 0),
            'expected': int(hb.get('expected') or 0),
            'yield': hb.get('yield'),
            'started': int(hb.get('started') or 0),
            'period_minutes': round(int(hb.get('period_seconds') or 1800) / 60),
            'stale_30': int(hb.get('stale_30') or 0),
            'stale_60': int(hb.get('stale_60') or 0),
            'stale_120': int(hb.get('stale_120') or 0),
            'error': hb.get('error') or '',
        },
        'site_rows': site_rows,
        'database': {
            'connections': db.get('connections'),
            'max_connections': db.get('max_connections'),
            'active': db.get('active'), 'idle': db.get('idle'),
            'waiting': db.get('waiting'),
            'longest_transaction_s': db.get('longest_transaction_s'),
            'live_tuples': j4.get('live_tuples'),
            'dead_tuples': j4.get('dead_tuples'),
            'minutes_since_autovacuum': j4.get('minutes_since_autovacuum'),
            'by_app': by_app, 'error': db.get('error') or '',
        },
        'server': server,
        'host': {
            'load': host.get('load') or {}, 'cpus': host.get('cpus'),
            'memory': host.get('memory') or {},
            'volumes': volumes, 'processes': processes,
            'db_connections': host.get('db_connections'),
            'errors': [v for k, v in host.items() if k.endswith('_error')],
        },
        'summary': summary,
        'summary_range': range_text,
        'summary_window': window_text,
        'summary_errors': [e for e in (walk_error, kills_error) if e],
        'errors_url': errors_url,
    }


def _fmt_num(value):
    if value is None:
        return '—'
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if abs(value) < 10:
            return f'{value:.3g}'
        return f'{value:,.0f}' if abs(value) >= 1000 else f'{value:.1f}'
    return f'{value:,}'


def _fmt_signed(value):
    if value == 0:
        return None
    return f'{value:+.3f}'


def _delivery_card(data, previous_data, ctx):
    """The delivery cut card. On a daily-record snap (the quilt), the
    breakdown of that day: what arrived, per configuration, with
    cumulative standing. On a live placed-basis snap, the placement
    totals with deltas and the top configurations. Full lists live on
    the campaign plan page."""
    # Delivery detail belongs to the Campaign focus, whose selected PCs
    # and daily bins provide its visual context. The scope report has no
    # campaign or PC family and therefore carries no delivery card.
    params = (ctx or {}).get('params') or {}
    selected = {value for value in
                (params.get('campaign') or '').split(',') if value}
    if not selected:
        return None
    from django.urls import reverse

    cache = _pc_cache()
    requestors = cache['requestors']
    keys = cache['keys']
    processes = cache.get('processes') or {}
    species = cache.get('species') or {}
    beam_energies = cache.get('beam_energies') or {}
    q2_ranges = cache.get('q2_ranges') or {}
    samples = cache.get('samples') or {}
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
                cum_events = int(leaf.get('events') or 0)
                expected = leaf.get('expected')
                row = {
                    'label': pc,
                    # The quilt curve this row is a patch of, in
                    # either plotted quantity: the swatch painter
                    # takes the first candidate the plot carries.
                    'curve': (f'dlvq_{tag}_{pc} dlvqf_{tag}_{pc}'),
                    'identity': keys.get(pc, ''),
                    'process': processes.get(pc, ''),
                    'species': species.get(pc, ''),
                    'url': reverse('pcs:pcs_config_detail', args=[pc]),
                    'cumulative_anchor': f'delivery-pc-{tag}-{pc}',
                    'groups': ', '.join(requestors.get(pc)
                                        or ['Unassigned']),
                    'arrived_events': int(
                        leaf.get('arrived_events') or 0),
                    'cum_events': cum_events,
                    'arrived': arrived,
                    'cum': int(leaf.get('cum_files') or 0),
                    'expected': expected,
                    'tier': leaf.get('tier') or '',
                    'completion': (round(100 * cum_events / expected, 1)
                                   if expected else None),
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
            # The second campaign panel is the additive cumulative
            # category stack.  Its cut table uses the same category
            # partition and the same cumulative values as the curves.
            category_totals = {}
            category_pc_rows = {}
            for pc, leaf in sorted(leaves.items()):
                category = cache['categories'].get(pc) or 'Uncategorized'
                slot = category_totals.setdefault(category, {
                    'name': category, 'configurations': 0,
                    'events': 0, 'today_events': 0,
                    'files': 0, 'today_files': 0})
                slot['configurations'] += 1
                slot['events'] += int(leaf.get('events') or 0)
                slot['today_events'] += int(
                    leaf.get('arrived_events') or 0)
                slot['files'] += int(leaf.get('cum_files') or 0)
                slot['today_files'] += int(
                    leaf.get('arrived_files') or 0)
                cumulative_events = int(leaf.get('events') or 0)
                expected = leaf.get('expected')
                row_species = species.get(pc, '') or 'Unspecified'
                if category == 'Single Particle':
                    species_slug = _species_slug(row_species)
                    row_curve = (f'dlvsp_{tag}_{species_slug} '
                                 f'dlvspf_{tag}_{species_slug}')
                else:
                    row_curve = (f'dlvpc_{tag}_{pc} '
                                 f'dlvpcf_{tag}_{pc}')
                category_pc_rows.setdefault(category, []).append({
                    'label': pc,
                    'identity': keys.get(pc, ''),
                    'process': processes.get(pc, ''),
                    'species': row_species,
                    'beam_energy': beam_energies.get(pc, ''),
                    'q2_range': q2_ranges.get(pc, ''),
                    'sample': samples.get(pc, ''),
                    'url': reverse('pcs:pcs_config_detail', args=[pc]),
                    'cumulative_anchor': f'delivery-pc-{tag}-{pc}',
                    'groups': ', '.join(requestors.get(pc)
                                        or ['Unassigned']),
                    'events': cumulative_events,
                    'today_events': int(
                        leaf.get('arrived_events') or 0),
                    'files': int(leaf.get('cum_files') or 0),
                    'today_files': int(
                        leaf.get('arrived_files') or 0),
                    'expected': expected,
                    'tier': leaf.get('tier') or '',
                    'completion': (
                        round(100 * cumulative_events / expected, 1)
                        if expected else None),
                    'curve': row_curve,
                })
            for rows in category_pc_rows.values():
                rows.sort(key=lambda row: (
                    row['species'], row['process'], row['label']))
            category_rows = []
            for category in sorted(category_totals):
                row = category_totals[category]
                slug = _group_slug(category)
                row['curve'] = (f'dlvc_cat_{tag}_{slug} '
                                f'dlvcf_cat_{tag}_{slug}')
                row['plot_anchor'] = (
                    'snapper-plot-'
                    + _delivery_pc_detail_key(name, category))
                category_rows.append(row)
            category_pc_groups = []
            for category in _delivery_categories():
                rows = category_pc_rows.get(category, [])
                species_groups = []
                if category == 'Single Particle':
                    by_species = {}
                    for row in rows:
                        by_species.setdefault(row['species'], []).append(row)
                    for species_name in sorted(by_species):
                        species_rows = by_species[species_name]
                        targets = [row['expected'] for row in species_rows
                                   if row['expected'] is not None]
                        target = sum(targets) if targets else None
                        events = sum(row['events'] for row in species_rows)
                        species_groups.append({
                            'name': species_name,
                            'curve': species_rows[0]['curve'],
                            'rows': species_rows,
                            'configurations': len(species_rows),
                            'events': events,
                            'today_events': sum(
                                row['today_events']
                                for row in species_rows),
                            'files': sum(row['files']
                                         for row in species_rows),
                            'today_files': sum(
                                row['today_files']
                                for row in species_rows),
                            'target': target,
                            'target_partial': (
                                bool(targets)
                                and len(targets) != len(species_rows)),
                            'completion': (
                                round(100 * events / target, 1)
                                if target and len(targets) == len(species_rows)
                                else None),
                        })
                category_pc_groups.append({
                    'name': category,
                    'detail_key': _delivery_pc_detail_key(name, category),
                    'rows': rows,
                    'species_groups': species_groups,
                })
            requested_at = (ctx or {}).get('requested_at')
            unmeasured = int(totals.get('unmeasured_files') or 0)
            campaigns.append({
                'name': name,
                'daily': True,
                'arrivals_detail_key': _delivery_detail_key(
                    'arrivals', name),
                'categories_detail_key': _delivery_detail_key(
                    'categories', name),
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
                'category_rows': category_rows,
                'category_pc_groups': category_pc_groups,
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
                episode_id = ''
                if arc['execution']:
                    # A live episode links like a finished one: watching
                    # a workflow unfold is the view's purpose. Integrity
                    # comes from the builder never orphaning a record,
                    # not from hiding in-progress ones.
                    from snapper_ai.models import Episode
                    if Episode.objects.filter(
                            scope='testbed',
                            episode_id=arc['execution']).exists():
                        episode_id = arc['execution']
                return {'kind': 'run_story',
                        'episode_id': episode_id,
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


def _series_cache(key, builder, refresh=False):
    """Snapper series as a cached product (docs/CACHED_PRODUCTS.md):
    served stored, rebuilt behind responses on staleness. Refresh state
    is returned with the value so the page can fetch the newly built
    product promptly; a concurrent first fill never duplicates work.

    Focus products (day-granular records such as the campaign quilt,
    rebuilt by the nightly prewarm when their record changes) carry an
    hours-scale TTL as a backstop; the scope report's live curves keep
    the short one. ``refresh=True`` is the prewarm path: rebuild
    synchronously."""
    from .cached_product import get_product

    # The Errors focus (param 'task') plots a component that advances
    # every refresh cycle; its products take the live TTL, not the
    # day-granular focus backstop.
    # The Platform focus (param 'platform') likewise plots components
    # that advance every refresh cycle.
    if ':focus:task:' in key or ':focus:platform:' in key:
        ttl_seconds = 90
    else:
        ttl_seconds = 6 * 3600 if ':focus:' in key else 90
    product = get_product(key, builder, ttl_seconds=ttl_seconds,
                          refresh=refresh,
                          async_first_fill=':focus:site:' in key)
    value = product.get('value')
    if isinstance(value, dict) and value.get('queue_members'):
        _QUEUE_STACK_CACHE['members'] = tuple(value['queue_members'])
    return {
        'value': value,
        'refreshing': product['refreshing'],
        'built_at': product['built_at'],
        'age_seconds': product['age_seconds'],
    }


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
        event_values=_epicprod_event_values,
        series_transform=_epicprod_series_transform,
        curve_label=_epicprod_curve_label,
        curve_color=_epicprod_curve_color,
        curve_groups=_epicprod_groups,
        scope_curve_groups=_epicprod_scope_groups,
        focus_view=(_delivery_focus_view, _site_focus_view,
                    _errors_focus_view, _platform_focus_view),
        component_cards={'panda': _panda_card,
                         'delivery': _delivery_card,
                         'errors': _errors_card,
                         'platform': _platform_card},
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
