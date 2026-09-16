"""The Node guard page (site-canary docs/NODE_GUARD.md): the node record
(black holes, half open, pinned) from the canary store with a person's
clear and pin; every node the guard's last cycle judged, with its
state, reason and evidence; the queues' calibration and any storm; the
switches. Reads the store and the cached product the cycle stored;
computes nothing and reaches neither PanDA nor Rucio.
"""
import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from monitor_app.panda.node_guard import DEFAULTS, STATE_KEY

logger = logging.getLogger(__name__)

STATE_LABELS = {'would_exclude': 'would exclude', 'excluded': 'excluded', 'clear': 'clear'}
RECORD_LABELS = {'black_hole': 'black hole', 'half_open': 'half open', 'pinned': 'pinned',
                 'clear': 'clear'}
REASON_LABELS = {
    'black_hole': 'black hole',
    'fixed_time': 'fixed-time kill',
    'below_floor': 'under the job floor',
    'failed_fraction': 'failed share under the threshold',
    'not_fast': 'failures not fast',
    'no_calibration': 'no calibration for the queue',
    'tasks_fail_everywhere': 'its tasks finish nowhere on the queue',
    'no_other_node': 'no other node on the queue',
    'queue_event': "the queue's event",
}


def _evidence_view(e):
    """The evidence dict as the templates read it."""
    e = e or {}
    return {
        'jobs': e.get('jobs'), 'failed': e.get('failed'), 'finished': e.get('finished'),
        'failed_pct': round(100 * (e.get('failed_fraction') or 0)),
        'fast_failed': e.get('fast_failed'), 'failed_no_duration': e.get('failed_no_duration'),
        'fast_pct': round(100 * (e.get('fast_fraction') or 0)),
        'tasks_failed': e.get('tasks_failed') or [],
        'tasks_finished_elsewhere': e.get('tasks_finished_elsewhere') or [],
        'other_nodes_finishing': e.get('other_nodes_finishing'),
        'first_end': e.get('first_end'), 'last_end': e.get('last_end'),
        'site': e.get('site') or '', 'sites': e.get('sites') or [],
        'durations': {k: (round(v / 60.0, 1) if v is not None else None)
                      for k, v in (e.get('failed_duration_s') or {}).items()},
        'error_codes': e.get('error_codes') or [],
        'sample_jobs': e.get('sample_jobs') or [],
        'duration_spread': e.get('duration_spread'), 'fixed_time': bool(e.get('fixed_time')),
        'elsewhere_hosts': sorted((e.get('tasks_finished_elsewhere_hosts') or {}).items()),
        'median_finished_min': (round(e['median_finished_s'] / 60.0)
                                if e.get('median_finished_s') else None),
        'fast_under_min': (round(e['fast_under_s'] / 60.0) if e.get('fast_under_s') else None),
    }


def _record_rows():
    """The node record as the page shows it: every node not clear."""
    from canary.store.models import NodeState
    rows = []
    for node in (NodeState.objects.exclude(status='clear')
                 .select_related('queue').order_by('queue__name', 'host')):
        lv = node.last_verdict or {}
        row = {'queue': node.queue.name, 'host': node.host, 'site': node.site,
               'status': node.status, 'status_label': RECORD_LABELS.get(node.status, node.status),
               'reason': node.reason, 'opened_at': node.opened_at, 'expires_at': node.expires_at,
               'reopened': node.reopened, 'trips': node.trips,
               'last_seen_at': node.last_seen_at,
               'last_verdict_at': lv.get('at'), 'last_tripped': lv.get('tripped'),
               'last_finished': lv.get('finished'), 'last_fast_failed': lv.get('fast_failed'),
               'changes': node.changes.count()}
        row.update(_evidence_view(node.evidence))
        rows.append(row)
    return rows


def panda_node_guard(request):
    from monitor_app.middleware import is_tunnel_request
    from monitor_app.authority import may_act
    from monitor_app.models import CachedProduct, SysConfig

    config = SysConfig.get_config()
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    state = (row.value if row else None) or {}
    settings = {k: config.get(f'node_guard.{k}', v) for k, v in DEFAULTS.items()}
    nodes = []
    for n in state.get('nodes') or []:
        row = {
            'queue': n.get('queue'), 'host': n.get('host'),
            'state': n.get('state') or 'clear',
            'state_label': STATE_LABELS.get(n.get('state'), n.get('state') or ''),
            'reason': n.get('reason') or '',
            'reason_label': REASON_LABELS.get(n.get('reason'), n.get('reason') or ''),
            'log_id': n.get('log_id'),
        }
        row.update(_evidence_view(n.get('evidence')))
        nodes.append(row)
    try:
        record = _record_rows()
    except Exception as exc:  # noqa: BLE001
        logger.exception('node guard: the record could not be read')
        record = []
        state = dict(state, errors=list(state.get('errors') or [])
                     + [f'the node record could not be read: {type(exc).__name__}: {exc}'])
    operable = (request.user.is_authenticated and may_act(request.user.username)
                and not is_tunnel_request(request))
    # tripped first, then by queue and host
    nodes.sort(key=lambda n: (n['state'] == 'clear', n['queue'] or '', n['host'] or ''))
    queues = []
    for name, q in sorted((state.get('queues') or {}).items()):
        med = q.get('median_finished_s')
        fast = q.get('fast_under_s')
        queues.append({
            'queue': name, 'jobs': q.get('jobs'), 'hosts': q.get('hosts'),
            'judged': q.get('judged'), 'tripped': q.get('tripped'),
            'queue_event': bool(q.get('queue_event')), 'storm_hosts': q.get('storm_hosts'),
            'storm_nodes': q.get('storm_nodes') or [],
            'median_finished_min': round(med / 60.0) if med else None,
            'fast_under_min': round(fast / 60.0) if fast else None,
            'not_nodes': sorted((q.get('not_nodes') or {}).items()),
        })
    context = {
        'active_nav': {'panda_node_guard': True, 'sites': True},
        'state': state, 'never_run': not state,
        'cycle_at': state.get('cycle_at'),
        'mode': state.get('mode') or settings['mode'],
        'enabled': bool(state.get('enabled', settings['enabled'])),
        'settings': settings, 'errors': state.get('errors') or [],
        'nodes': nodes, 'tripped': [n for n in nodes if n['state'] != 'clear'],
        'queues': queues, 'record': record, 'operable': operable,
    }
    return render(request, 'monitor_app/panda_node_guard.html', context)


def panda_node_guard_json(request):
    """The node guard as JSON for scripts (prod-notify): the last cycle's
    tripped nodes (would_exclude in shadow mode, excluded live) and the
    node record's rows that are not clear. The same reads as the page,
    nothing computed."""
    from django.core.serializers.json import DjangoJSONEncoder
    from django.http import JsonResponse
    from monitor_app.models import CachedProduct, SysConfig

    config = SysConfig.get_config()
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    state = (row.value if row else None) or {}
    tripped = [
        {'queue': n.get('queue'), 'host': n.get('host'), 'state': n.get('state'),
         'reason': n.get('reason') or '', 'site': (n.get('evidence') or {}).get('site') or '',
         'log_id': n.get('log_id')}
        for n in state.get('nodes') or [] if (n.get('state') or 'clear') != 'clear']
    errors = list(state.get('errors') or [])
    try:
        record = [{k: r.get(k) for k in ('queue', 'host', 'site', 'status', 'reason',
                                          'opened_at', 'expires_at', 'trips', 'last_verdict_at')}
                  for r in _record_rows()]
    except Exception as exc:  # noqa: BLE001
        logger.exception('node guard json: the record could not be read')
        record = []
        errors.append(f'the node record could not be read: {type(exc).__name__}: {exc}')
    return JsonResponse({
        'cycle_at': state.get('cycle_at'),
        'mode': state.get('mode') or config.get('node_guard.mode', DEFAULTS['mode']),
        'enabled': bool(state.get('enabled', config.get('node_guard.enabled', DEFAULTS['enabled']))),
        'errors': errors, 'tripped': tripped, 'record': record,
    }, encoder=DjangoJSONEncoder)


def panda_node_guard_set(request):
    """A person's decision on one node of the record: clear, pin, unpin
    (back to clear, the guard may trip it again) or open a black hole by
    hand. POST-only; a write, so login-gated, authority-gated, and
    refused on the tunnel face like every rare operator action."""
    from monitor_app.middleware import is_tunnel_request
    from monitor_app.authority import may_act
    from monitor_app.epicprod_logging import log_epicprod_action
    from canary.store import nodes as node_record

    url = reverse('monitor_app:panda_node_guard')
    if request.method != 'POST':
        messages.warning(request, 'Node guard actions only respond to POST submissions.')
        return redirect(url)
    queue = (request.POST.get('queue') or '').strip()
    host = (request.POST.get('host') or '').strip()
    action = (request.POST.get('action') or '').strip()
    if is_tunnel_request(request):
        logger.warning('node guard %s refused on the tunnel face (%s/%s)', action, queue, host)
        return redirect(url)
    if not request.user.is_authenticated:
        messages.error(request, 'Login required to act on a node.')
        return redirect(url)
    if not may_act(request.user.username):
        messages.error(request, 'Your account has no authority to act on the system.')
        return redirect(url)
    status = {'clear': 'clear', 'unpin': 'clear', 'pin': 'pinned', 'open': 'black_hole'}.get(action)
    if not (queue and host and status):
        messages.error(request, 'A node (queue and host) and one of clear, pin, unpin, open are needed.')
        return redirect(url)
    note = (request.POST.get('note') or '').strip()
    try:
        node = node_record.set_status(queue, host, status, username=request.user.username,
                                      reason=f'{action}: {note}' if note else action)
    except Exception as exc:  # noqa: BLE001
        logger.exception('node guard %s failed (%s/%s)', action, queue, host)
        messages.error(request, f'The action failed: {type(exc).__name__}: {exc}')
        return redirect(url)
    log_epicprod_action(
        'web', 'node_guard_set', subject_type='panda_node', subject_key=f'{queue}/{host}',
        username=request.user.username, outcome=node.status, sublevel='normal', live_default=True,
        message=f'node guard {queue} {host}: {action} by {request.user.username}'
                + (f' ({note})' if note else '') + f'; status {node.status}',
        action_taken=action, note=note, status=node.status)
    messages.success(request, f'{host} on {queue}: {RECORD_LABELS.get(node.status, node.status)}.')
    return redirect(url)
