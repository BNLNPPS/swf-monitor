"""The Node guard page (site-canary docs/NODE_GUARD.md): every node the
guard's last cycle judged, with its state, reason and evidence; the
queues' calibration and any storm; the switches. Reads the cached
product the cycle stored and the settings; computes nothing and reaches
neither PanDA nor Rucio.
"""
from django.shortcuts import render

from monitor_app.panda.node_guard import DEFAULTS, STATE_KEY

STATE_LABELS = {'would_exclude': 'would exclude', 'excluded': 'excluded', 'clear': 'clear'}
REASON_LABELS = {
    'black_hole': 'black hole',
    'failed_fraction': 'failed share under the threshold',
    'not_fast': 'failures not fast',
    'no_calibration': 'no calibration for the queue',
    'tasks_fail_everywhere': 'its tasks finish nowhere on the queue',
    'no_other_node': 'no other node on the queue',
    'queue_event': "the queue's event",
}


def panda_node_guard(request):
    from monitor_app.models import CachedProduct, SysConfig

    config = SysConfig.get_config()
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    state = (row.value if row else None) or {}
    settings = {k: config.get(f'node_guard.{k}', v) for k, v in DEFAULTS.items()}
    nodes = []
    for n in state.get('nodes') or []:
        e = n.get('evidence') or {}
        nodes.append({
            'queue': n.get('queue'), 'host': n.get('host'),
            'state': n.get('state') or 'clear',
            'state_label': STATE_LABELS.get(n.get('state'), n.get('state') or ''),
            'reason': n.get('reason') or '',
            'reason_label': REASON_LABELS.get(n.get('reason'), n.get('reason') or ''),
            'log_id': n.get('log_id'),
            'jobs': e.get('jobs'), 'failed': e.get('failed'), 'finished': e.get('finished'),
            'failed_pct': round(100 * (e.get('failed_fraction') or 0)),
            'fast_failed': e.get('fast_failed'), 'failed_no_duration': e.get('failed_no_duration'),
            'fast_pct': round(100 * (e.get('fast_fraction') or 0)),
            'tasks_failed': e.get('tasks_failed') or [],
            'tasks_finished_elsewhere': e.get('tasks_finished_elsewhere') or [],
            'other_nodes_finishing': e.get('other_nodes_finishing'),
            'first_end': e.get('first_end'), 'last_end': e.get('last_end'),
        })
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
        'active_nav': {'panda_node_guard': True},
        'state': state, 'never_run': not state,
        'cycle_at': state.get('cycle_at'),
        'mode': state.get('mode') or settings['mode'],
        'enabled': bool(state.get('enabled', settings['enabled'])),
        'settings': settings, 'errors': state.get('errors') or [],
        'nodes': nodes, 'tripped': [n for n in nodes if n['state'] != 'clear'],
        'queues': queues,
    }
    return render(request, 'monitor_app/panda_node_guard.html', context)
