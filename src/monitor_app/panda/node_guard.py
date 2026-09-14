"""The node guard's cycle (site-canary docs/NODE_GUARD.md): every five
minutes, the terminal production jobs of the window per queue and host
go to the canary's decision (``canary.guard.decide_nodes``), and what
comes back is recorded on the action stream and stored as the cached
product ``node_guard_state``, which the Node guard page reads.

The cycle runs as the production-operations agent's ``node_guard_cycle``
doer (``scripts/node-guard-cycle.py``), by cron enqueue. It holds no
credential and acts on nothing: in shadow mode a tripped node reads
``would_exclude``; live mode (the published exclusion, the wrapper and
landing checks, the OSG clause) is a later build and is refused until
then, decided as shadow with the refusal recorded.

Settings live in SysConfig under ``node_guard.*`` and are seeded at
their defaults on first read, so every knob is visible on the System
page (the table in NODE_GUARD.md, Modes and settings).

Decision records: one ``node_guard_decision`` per node whose verdict
changed since the last record for it (tripped, or cleared after a
trip), and hourly as a heartbeat while tripped; one ``node_guard_cycle``
per cycle. Every read is fenced: a failed window read or calibration is
recorded as the cycle's error and the cycle goes on with what it has.
"""
import logging

from django.db import connections
from django.utils import timezone

from monitor_app.panda.constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

STATE_KEY = 'node_guard_state'
STATE_TTL_S = 24 * 3600
MODES = ('shadow',)          # 'live' once the actuation is built
HEARTBEAT_S = 3600
DEFAULTS = {
    'enabled': False,
    'mode': 'shadow',
    'queues': [],
    'window_h': 4,
    'attribution_h': 24,
    'min_jobs': 8,
    'failed_fraction': 0.8,
    'fast_fraction': 0.5,
    'fast_ratio': 0.5,
    'storm_nodes': 10,
    'not_nodes': ['pandaharvester01.sdcc.bnl.gov', 'osgsub01.sdcc.bnl.gov'],
    'expiry_h': 24,
}
DECISION_KEYS = ('min_jobs', 'failed_fraction', 'fast_fraction', 'fast_ratio',
                 'storm_nodes', 'not_nodes')
TRIPPED_STATES = {'shadow': 'would_exclude', 'live': 'excluded'}


def _safe(label, fn, fallback):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.exception('node guard: %s failed', label)
        return fallback(exc) if callable(fallback) else fallback


def setting(key, default):
    from monitor_app.models import SysConfig
    return SysConfig.get_setting(key, default)


def settings():
    out = {}
    for key, default in DEFAULTS.items():
        out[key] = setting(f'node_guard.{key}', default)
    queues = out['queues']
    if not (isinstance(queues, (list, tuple)) and all(isinstance(q, str) for q in queues)):
        logger.error('node_guard.queues is not a list of queue names: %r; judging every queue', queues)
        out['queues'] = []
    out['queues'] = [q for q in out['queues'] if q]
    not_nodes = out['not_nodes']
    if not (isinstance(not_nodes, (list, tuple)) and all(isinstance(h, str) for h in not_nodes)):
        logger.error('node_guard.not_nodes is not a list of host names: %r; using the defaults', not_nodes)
        out['not_nodes'] = list(DEFAULTS['not_nodes'])
    out['window_h'] = max(1, int(out['window_h'] or 1))
    out['attribution_h'] = max(out['window_h'], int(out['attribution_h'] or 1))
    return out


def window_rows(window_h, queues=()):
    """The terminal production jobs of the last ``window_h`` hours, one
    dict per job in the shape ``canary.guard.decide_nodes`` reads."""
    sql = f"""
        SELECT "computingsite", "modificationhost", "jobstatus", "jeditaskid",
               EXTRACT(EPOCH FROM ("endtime" - "starttime")), "endtime", "pandaid"
        FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "processingtype" = 'epicproduction'
          AND "endtime" > NOW() - INTERVAL %s
          AND "jobstatus" IN ('finished', 'failed')
    """
    params = [f'{window_h} hours']
    if queues:
        sql += ' AND "computingsite" = ANY(%s)'
        params.append(list(queues))
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    out = []
    for queue, host, status, task, seconds, end, pandaid in rows:
        out.append({'queue': str(queue) if queue else None, 'host': host,
                    'jobstatus': status, 'jeditaskid': task,
                    'duration_s': float(seconds) if seconds is not None else None,
                    'endtime': end.isoformat() if end is not None else None,
                    'pandaid': pandaid})
    return out


def finished_elsewhere(attribution_h, queues=()):
    """Per queue and task, the hosts on which the task finished over the
    attribution window: ``{queue: {task: {host, ...}}}``."""
    from canary.guard import normalize_host
    sql = f"""
        SELECT "computingsite", "jeditaskid", "modificationhost"
        FROM "{PANDA_SCHEMA}"."jobsarchived4"
        WHERE "processingtype" = 'epicproduction'
          AND "endtime" > NOW() - INTERVAL %s
          AND "jobstatus" = 'finished'
        GROUP BY 1, 2, 3
    """
    params = [f'{attribution_h} hours']
    if queues:
        sql = sql.replace('        GROUP BY 1, 2, 3', '          AND "computingsite" = ANY(%s)\n        GROUP BY 1, 2, 3')
        params.append(list(queues))
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    out = {}
    for queue, task, host in rows:
        if not queue or task is None:
            continue
        h = normalize_host(host)
        if h:
            out.setdefault(str(queue), {}).setdefault(task, set()).add(h)
    return out


def median_finished_s():
    """Per queue, the census calibration's median finished walltime in
    seconds (None where the calibration has none)."""
    from monitor_app.panda.census import calibration
    calib = (calibration() or {}).get('queues') or {}
    out = {}
    for queue, c in calib.items():
        med_h = (c or {}).get('median_walltime_h')
        out[queue] = float(med_h) * 3600.0 if med_h else None
    return out


def _last_decision(queue, host):
    from monitor_app.models import AppLog
    return (AppLog.objects.filter(app_name='epicprod',
                                  extra_data__action='node_guard_decision',
                                  extra_data__subject_key=f'{queue}/{host}')
            .order_by('-timestamp').values('id', 'timestamp', 'extra_data').first())


def run_cycle(*, dry_run=False, created_by='node-guard'):
    """One cycle. Returns (nodes, summary): the judged nodes with their
    verdicts as shown, and the cycle summary; writes the records and
    the stored state unless dry_run."""
    from canary import guard
    from monitor_app.epicprod_logging import log_epicprod_action

    t0 = timezone.now()
    errors = []

    def failed(label):
        def _f(exc):
            errors.append(f'{label}: {type(exc).__name__}: {exc}')
            return None
        return _f

    cfg = _safe('settings', settings, lambda exc: dict(DEFAULTS))
    enabled = bool(cfg['enabled'])
    mode_requested = str(cfg['mode'])
    mode = mode_requested
    if mode not in MODES:
        errors.append(f'node_guard.mode {mode_requested!r} is not available (the actuation '
                      f'is not built); decided as shadow')
        mode = 'shadow'
    tripped_state = TRIPPED_STATES[mode]

    rows = calib = elsewhere = None
    if enabled:
        rows = _safe('window', lambda: window_rows(cfg['window_h'], cfg['queues']),
                     failed('window'))
        calib = _safe('calibration', median_finished_s, failed('calibration')) or {}
        elsewhere = _safe('attribution',
                          lambda: finished_elsewhere(cfg['attribution_h'], cfg['queues']),
                          failed('attribution')) or {}
    verdicts = None
    if rows is not None:
        verdicts = _safe('decision', lambda: guard.decide_nodes(
            rows, calib, {k: cfg[k] for k in DECISION_KEYS}, finished_elsewhere=elsewhere),
            failed('decision'))
    verdicts = verdicts or {'queues': {}, 'tripped': [], 'judged': 0,
                            'hosts': 0, 'jobs': 0, 'malformed': 0}

    nodes = []
    written = 0
    for queue, q in sorted(verdicts['queues'].items()):
        for host, v in sorted(q['nodes'].items()):
            tripped = v['state'] == 'tripped'
            state = tripped_state if tripped else 'clear'
            shown = {'queue': queue, 'host': host, 'state': state,
                     'reason': v['reason'], 'evidence': v['evidence'],
                     'recorded': False, 'log_id': None}
            nodes.append(shown)
            if dry_run:
                continue
            last = _safe(f'{queue}/{host} last decision',
                         lambda: _last_decision(queue, host), None)
            last_extra = (last or {}).get('extra_data') or {}
            last_state = last_extra.get('state')
            stale = (last is not None
                     and (t0 - last['timestamp']).total_seconds() >= HEARTBEAT_S)
            # A trip is always news; a clear is news only after a trip.
            changed = (last_state != state) if (tripped or last_state in TRIPPED_STATES.values()) else False
            recorded = bool(changed or (tripped and stale))
            shown['recorded'] = recorded
            if not recorded:
                shown['log_id'] = (last or {}).get('id')
                continue
            e = v['evidence']
            shown['log_id'] = _safe(f'{queue}/{host} record', lambda: log_epicprod_action(
                'node-guard', 'node_guard_decision', subject_type='panda_node',
                subject_key=f'{queue}/{host}', username=created_by, outcome=state,
                sublevel='normal' if tripped else 'low', live_default=tripped,
                level=logging.WARNING if tripped else logging.INFO,
                message=(f'node guard {queue} {host}: {state} ({v["reason"]}); '
                         f'{e["failed"]} of {e["jobs"]} jobs failed, {e["fast_failed"]} fast, '
                         f'in the last {cfg["window_h"]} h; '
                         f'{len(e["tasks_finished_elsewhere"])} of {len(e["tasks_failed"])} '
                         f'failed tasks finish on other nodes'),
                state=state, reason=v['reason'], mode=mode, host=host, queue=queue,
                window_h=cfg['window_h'], **e), failed(f'{queue}/{host} record'))
            written += 1

    tripped_nodes = [n for n in nodes if n['state'] in TRIPPED_STATES.values()]
    summary = {'cycle_at': t0.isoformat(), 'mode': mode, 'mode_requested': mode_requested,
               'enabled': enabled, 'window_h': cfg['window_h'],
               'jobs': verdicts['jobs'], 'hosts': verdicts['hosts'],
               'judged': verdicts['judged'], 'tripped': len(tripped_nodes),
               'malformed': verdicts['malformed'], 'decisions_recorded': written,
               'errors': errors,
               'duration_s': round((timezone.now() - t0).total_seconds(), 1)}
    if not dry_run:
        queues_state = {}
        for queue, q in verdicts['queues'].items():
            queues_state[queue] = {k: v for k, v in q.items() if k != 'nodes'}
            queues_state[queue]['storm_nodes'] = ([h for h, v in q['nodes'].items()
                                                   if v['reason'] == guard.QUEUE_EVENT]
                                                  if q.get('queue_event') else [])
        state_payload = {'cycle_at': summary['cycle_at'], 'mode': mode,
                         'mode_requested': mode_requested, 'enabled': enabled,
                         'settings': {k: cfg[k] for k in DEFAULTS},
                         'queues': queues_state, 'nodes': nodes,
                         'tripped': [(n['queue'], n['host']) for n in tripped_nodes],
                         'errors': errors, 'duration_s': summary['duration_s']}
        from monitor_app.cached_product import get_product
        _safe('state store', lambda: get_product(
            STATE_KEY, lambda: state_payload, ttl_seconds=STATE_TTL_S, refresh=True),
            failed('state store'))
        _safe('cycle record', lambda: log_epicprod_action(
            'node-guard', 'node_guard_cycle', username=created_by,
            outcome='error' if errors else 'ok',
            duration_ms=int(summary['duration_s'] * 1000),
            sublevel='normal' if errors else 'low', live_default=bool(errors),
            level=logging.ERROR if errors else logging.INFO,
            message=(f'node guard cycle ({mode}{"" if enabled else ", off"}): '
                     f'{summary["jobs"]} jobs on {summary["hosts"]} '
                     f'hosts, {summary["judged"]} judged, {summary["tripped"]} tripped'
                     + (': ' + ', '.join(f'{q} {h}' for q, h in state_payload['tripped'])
                        if tripped_nodes else '')
                     + ''.join(f'; {q} storm on {s["storm_hosts"]} hosts, the queue\'s event'
                               for q, s in queues_state.items() if s.get('queue_event'))
                     + (f'; errors: {"; ".join(errors)}' if errors else '')),
            **{k: v for k, v in summary.items() if k != 'errors'}),
            failed('cycle record'))
    return nodes, summary
