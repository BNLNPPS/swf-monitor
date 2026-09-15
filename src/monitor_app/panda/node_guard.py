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

The verdicts go to the node record in the canary store
(``canary.store.nodes.apply``): a trip opens a black hole that stays
latched until it expires into half open, where one landing decides;
a person may clear or pin. Decision records: one ``node_guard_decision``
per change of a node's record (a black hole opened or reopened, expired
into half open, cleared by a clean landing), and hourly as a heartbeat
for each standing black hole; one ``node_guard_cycle`` per cycle. Every
read is fenced: a failed window read or calibration is recorded as the
cycle's error and the cycle goes on with what it has.
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
    'fixed_min_jobs': 5,
    'fixed_time_ratio': 1.2,
    'expiry_h': 24,
}
DECISION_KEYS = ('min_jobs', 'failed_fraction', 'fast_fraction', 'fast_ratio',
                 'storm_nodes', 'not_nodes', 'fixed_min_jobs', 'fixed_time_ratio')
TRIPPED_STATES = {'shadow': 'would_exclude', 'live': 'excluded'}
# The record's status as the decision record names it, per mode.
RECORD_STATES = {'black_hole': TRIPPED_STATES, 'half_open': {'shadow': 'half_open', 'live': 'half_open'},
                 'clear': {'shadow': 'clear', 'live': 'clear'}}


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


def execute_site(dest_site, host):
    """The actual site a job ran at: the pilot-reported glidein site when
    the record carries one (the OSG pool queues, since 2026-08-13), else
    the site the worker's domain names, else the domain itself; '' for a
    bare host name (monitor_app.panda.queries, the execute-site key)."""
    from canary.guard import normalize_host
    from monitor_app.panda.queries import (EXECUTE_SITE_NAMES, PRIVATE_DOMAIN_TAILS)
    if dest_site:
        return str(dest_site)
    h = normalize_host(host)
    parts = h.split('.')
    if len(parts) < 2 or parts[-1].lower() in PRIVATE_DOMAIN_TAILS:
        return ''
    key = '.'.join(parts[-2:]).lower()
    return EXECUTE_SITE_NAMES.get(key, key)


def error_label(pilot, exe, trans):
    """One label for a failed job's error: the pilot error code when set,
    else the payload's exit code, else the transformation's; 'no code'
    when none is set."""
    if pilot:
        return f'pilot {pilot}'
    if exe:
        return f'exe {exe}'
    if trans:
        return f'trans {trans}'
    return 'no code'


def window_rows(window_h, queues=()):
    """The terminal production jobs of the last ``window_h`` hours, one
    dict per job in the shape ``canary.guard.decide_nodes`` reads, with
    the actual site and the failure's error label resolved here."""
    sql = f"""
        SELECT "computingsite", "modificationhost", "jobstatus", "jeditaskid",
               EXTRACT(EPOCH FROM ("endtime" - "starttime")), "endtime", "pandaid",
               "destinationsite", "piloterrorcode", "exeerrorcode", "transexitcode"
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
    for (queue, host, status, task, seconds, end, pandaid,
         dest_site, pilot, exe, trans) in rows:
        out.append({'queue': str(queue) if queue else None, 'host': host,
                    'jobstatus': status, 'jeditaskid': task,
                    'duration_s': float(seconds) if seconds is not None else None,
                    'endtime': end.isoformat() if end is not None else None,
                    'pandaid': pandaid,
                    'site': execute_site(dest_site, host),
                    'error': error_label(pilot, exe, trans) if status == 'failed' else ''})
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


def _window_counts(rows, calib, fast_ratio):
    """Per (queue, host): the window's finished jobs and fast failures,
    for the nodes the record holds that did not reach the floor."""
    from canary.guard import normalize_host
    out = {}
    for r in rows or ():
        host = normalize_host(r.get('host'))
        if not r.get('queue') or not host:
            continue
        c = out.setdefault((r['queue'], host), {'finished': 0, 'fast_failed': 0, 'failed': 0})
        if r.get('jobstatus') == 'finished':
            c['finished'] += 1
        elif r.get('jobstatus') == 'failed':
            c['failed'] += 1
            med = calib.get(r['queue'])
            d = r.get('duration_s')
            if med and d is not None and float(d) < med * fast_ratio:
                c['fast_failed'] += 1
    return out


def _readings(verdicts, rows, calib, cfg):
    """What the record is told this cycle: every judged node's verdict,
    and for every node holding a record that was not judged, its window
    counts (zero when it had no job)."""
    from canary.store.models import NodeState
    counts = _window_counts(rows, calib, cfg['fast_ratio'])
    readings = {}
    for queue, q in verdicts['queues'].items():
        for host, v in q['nodes'].items():
            e = v['evidence']
            readings[(queue, host)] = {
                'tripped': v['state'] == 'tripped', 'reason': v['reason'], 'evidence': e,
                'finished': e.get('finished', 0), 'fast_failed': e.get('fast_failed', 0),
                'site': e.get('site', '')}
    for queue_name, host in (NodeState.objects.exclude(status='clear')
                             .values_list('queue__name', 'host')):
        key = (queue_name, host)
        if key in readings:
            continue
        c = counts.get(key, {'finished': 0, 'fast_failed': 0, 'failed': 0})
        readings[key] = {'tripped': False, 'reason': '', 'evidence': {},
                         'finished': c['finished'], 'fast_failed': c['fast_failed'], 'site': ''}
    return readings


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
    set_aside = {}
    if enabled:
        rows = _safe('window', lambda: window_rows(cfg['window_h'], cfg['queues']),
                     failed('window'))
        if rows is not None:
            # Jobs that ended under a declared downtime of their queue
            # are set aside before judgment: a node is not a black hole
            # for a downtime's deaths (swf-epicprod
            # CONTINUOUS_PRODUCTION.md, Declared downtime). An unreadable
            # record sets nothing aside.
            from monitor_app.declared import set_aside_declared
            kept = _safe('declared', lambda: set_aside_declared(rows), failed('declared'))
            if kept is not None:
                rows, set_aside = kept
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
    for queue, q in sorted(verdicts['queues'].items()):
        for host, v in sorted(q['nodes'].items()):
            tripped = v['state'] == 'tripped'
            nodes.append({'queue': queue, 'host': host,
                          'state': tripped_state if tripped else 'clear',
                          'reason': v['reason'], 'evidence': v['evidence'],
                          'recorded': False, 'log_id': None})

    # The record: the verdicts applied to the node map, with latch,
    # expiry and half open; the changes are the decisions recorded.
    written = 0
    changes, trips = [], []
    if enabled and rows is not None and not dry_run:
        from canary.store import nodes as node_record
        readings = _safe('readings', lambda: _readings(verdicts, rows, calib, cfg),
                         failed('readings'))
        if readings is not None:
            applied = _safe('record', lambda: node_record.apply(
                readings, now=t0, expiry_h=float(cfg['expiry_h']), mode=mode,
                username=created_by), failed('record'))
            if applied is not None:
                changes, trips = applied
        by_key = {(n['queue'], n['host']): n for n in nodes}
        for queue, host, old, new, why in changes:
            state = RECORD_STATES.get(new, {}).get(mode, new)
            shown = by_key.get((queue, host))
            e = (shown or {}).get('evidence') or {}
            log_id = _safe(f'{queue}/{host} record', lambda: log_epicprod_action(
                'node-guard', 'node_guard_decision', subject_type='panda_node',
                subject_key=f'{queue}/{host}', username=created_by, outcome=state,
                sublevel='normal' if new == 'black_hole' else 'low',
                live_default=new == 'black_hole',
                level=logging.WARNING if new == 'black_hole' else logging.INFO,
                message=(f'node guard {queue} {host}: {state} ({why}), was {old}'
                         + (f'; {e["failed"]} of {e["jobs"]} jobs failed, {e["fast_failed"]} fast, '
                            f'in the last {cfg["window_h"]} h; '
                            f'{len(e["tasks_finished_elsewhere"])} of {len(e["tasks_failed"])} '
                            f'failed tasks finish on other nodes' if e else '')),
                state=state, old_state=old, reason=why, mode=mode, host=host, queue=queue,
                window_h=cfg['window_h'], **e), failed(f'{queue}/{host} record'))
            if shown is not None:
                shown['recorded'] = True
                shown['log_id'] = log_id
            written += 1
        # Hourly heartbeat for each standing black hole with a verdict
        # this cycle and no change recorded.
        changed = {(q, h) for q, h, _, _, _ in changes}
        for shown in nodes:
            key = (shown['queue'], shown['host'])
            if shown['state'] == 'clear' or key in changed:
                continue
            last = _safe(f'{key} last decision', lambda: _last_decision(*key), None)
            if last is None or (t0 - last['timestamp']).total_seconds() < HEARTBEAT_S:
                shown['log_id'] = (last or {}).get('id')
                continue
            e = shown['evidence']
            shown['recorded'] = True
            shown['log_id'] = _safe(f'{key} heartbeat', lambda: log_epicprod_action(
                'node-guard', 'node_guard_decision', subject_type='panda_node',
                subject_key=f'{key[0]}/{key[1]}', username=created_by, outcome=shown['state'],
                sublevel='low', live_default=False,
                message=(f'node guard {key[0]} {key[1]}: {shown["state"]} standing; '
                         f'{e["failed"]} of {e["jobs"]} jobs failed in the last {cfg["window_h"]} h'),
                state=shown['state'], reason=shown['reason'], mode=mode, host=key[1],
                queue=key[0], window_h=cfg['window_h'], **e), failed(f'{key} heartbeat'))
            written += 1

    tripped_nodes = [n for n in nodes if n['state'] in TRIPPED_STATES.values()]
    summary = {'cycle_at': t0.isoformat(), 'mode': mode, 'mode_requested': mode_requested,
               'enabled': enabled, 'window_h': cfg['window_h'],
               'jobs': verdicts['jobs'], 'hosts': verdicts['hosts'],
               'judged': verdicts['judged'], 'tripped': len(tripped_nodes),
               'opened': len(trips), 'record_changes': len(changes),
               'malformed': verdicts['malformed'], 'decisions_recorded': written,
               'under_declaration': sum(set_aside.values()),
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
                         'under_declaration': set_aside,
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
                     + (f'; {len(trips)} black hole{"s" if len(trips) != 1 else ""} opened'
                        if trips else '')
                     + (f'; {summary["under_declaration"]} jobs under a declared downtime set aside ('
                        + ', '.join(f'{q} {n}' for q, n in sorted(set_aside.items())) + ')'
                        if set_aside else '')
                     + ''.join(f'; {q} storm on {s["storm_hosts"]} hosts, the queue\'s event'
                               for q, s in queues_state.items() if s.get('queue_event'))
                     + (f'; errors: {"; ".join(errors)}' if errors else '')),
            **{k: v for k, v in summary.items() if k != 'errors'}),
            failed('cycle record'))
    return nodes, summary
