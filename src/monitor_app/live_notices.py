"""Human-channel selection, separate from the complete action record.

Only the Mattermost delivery uses this policy (docs/NOTICE_ROUTING.md).
The caller persists ``failures`` alongside its stream position; it contains
only outstanding automated failures, not a second incident archive.
"""
import json
import re

from monitor_app.short_names import short_name


STATE_KEY = 'epicprod_live_failures'
ROUTINE_SUCCESSES = frozenset({
    'catalog_sync', 'catalog_import', 'past_import', 'questionnaire_import',
    'association_sweep', 'rucio_sweep', 'evgen_sweep', 'segfault_inventory',
    'segfault_dig', 'segfault_study', 'segfault_notice', 'storage_door_cycle',
})
# Maintenance passes: their automated runs never reach the channel, failures
# and recoveries included (Torre, 2026-10-08); the action log and the alarms
# keep them.
MAINTENANCE = frozenset({
    'stash_drain', 'storage_sweep', 'storage_door_cycle', 'log_rescue', 'log_grant',
    'registrar', 'report_sweep', 'node_measure_ingest', 'harvester_stdout_capture',
    'batch_log_capture', 'batch_log_learn', 'file_events_measure',
    'panda_sandbox_keepalive', 'es_closeout_cycle', 'system_status_refresh',
    'snapper_capture', 'rucio_snapshot_update', 'rucio_arrivals_sweep',
    'delivery_daily_rebuild', 'campaign_progress_refresh',
})
FAILURE_OUTCOMES = frozenset({'error', 'timeout', 'partial', 'unrecorded'})
PROGRESS_PATTERNS = {
    'catalog_import': r'\b(\d+) new\b',
    'past_import': r'\bcreated=(\d+)',
    'questionnaire_import': r'\b(\d+) (?:new|updated)\b',
    'association_sweep': r'\b(?:new|intaken)=(\d+)',
    'segfault_inventory': r'\b(\d+) (?:crashed jobs|rows added|new)\b',
    'segfault_dig': r'\b(\d+) (?:dug|traced)\b',
    'segfault_study': r'\b(\d+) (?:studies queued|traced signatures marked)\b',
    'segfault_notice': r'\b(\d+) (?:need a reproduction|traced unread)\b',
}


# The room's epicprod-live screen (COMP_SNAPSHOT.md, Live) shows a short
# title, what and where, with the informative message under it.
BOARD_LABELS = {
    'node_guard_decision': 'Node guard', 'node_guard_cycle': 'Node guard cycle',
    'panda_task_operation': 'Task operation', 'evgen_task_submit': 'EVGEN submit',
    'evgen_register': 'EVGEN registration', 'evgen_events': 'EVGEN events',
    'storage_door_cycle': 'Storage doors', 'storage_door_verdict': 'Storage door',
    'storage_sweep': 'Storage sweep', 'stash_drain': 'Stash drain',
    'system_status_refresh': 'System status', 'report_sweep': 'Report sweep',
    'log_rescue': 'Log rescue', 'catalog_sync': 'Catalog sync',
    'payload_log_fetch': 'Payload log fetch', 'declared_state_sync': 'Declared state',
    'es_closeout_cycle': 'ES close-out', 'snapper_capture': 'Snapper capture',
    'file_events_measure': 'File events', 'credential_expiry_check': 'Credential expiry',
    'panda_sandbox_keepalive': 'Sandbox keepalive',
    'osg_throttle': 'OSG throttle',
}
OPERATION_LABELS = {'reassign': 'Move', 'pause': 'Pause', 'resume': 'Resume',
                    'retry': 'Retry', 'finish': 'Finish', 'kill': 'Kill',
                    'abort': 'Abort'}
NODE_REASONS = {
    'black_hole': 'Black hole',
    'fixed_time': 'Kills jobs at a fixed run time',
}
CONFIG_TAGS = re.compile(r'\.(p\d+\.e\d+)\.')


def board_text(message, extra):
    """(title, detail) for one record on the room's epicprod-live screen:
    the title names what happened and where (the execute site, storage
    element or queue), the detail is the informative message."""
    action = str(extra.get('action') or '')
    label = BOARD_LABELS.get(action) or action.replace('_', ' ').capitalize()
    if action == 'node_guard_decision':
        return _node_guard_name(extra), node_guard_text(extra)
    if action == 'panda_task_operation':
        op = str(extra.get('operation') or '')
        task = extra.get('task_name') or f"task {extra.get('subject_key')}"
        return (f"{OPERATION_LABELS.get(op, op.capitalize() or label)} · "
                f"task {extra.get('subject_key')}",
                f"{task}: {extra.get('reason') or message}")
    where = short_name(extra.get('site') or extra.get('rse') or extra.get('queue')
                       or extra.get('subject_label'))
    if not where:
        tags = CONFIG_TAGS.search(str(extra.get('subject_key') or ''))
        where = tags.group(1) if tags else ''
    detail = str(extra.get('reason') or extra.get('summary') or message)
    subject = str(extra.get('subject_label') or extra.get('subject_key') or '')
    if subject and subject not in detail:
        detail = f'{subject}: {detail}'
    return (f'{label} · {where}' if where else label), detail


def _node_guard_name(f):
    """'Fir node guard 6': the site's short name and the node's number on
    that site's blacklist, where known."""
    site = short_name(f.get('site') or f.get('queue'))
    name = f'{site} node guard' if site else 'Node guard'
    return f"{name} {f['site_rank']}" if f.get('site_rank') else name


def site_node_guard_fields(fields, ranks):
    """Present existing queue-numbered records using site numbering."""
    out = dict(fields)
    out.pop('queue_rank', None)
    out['site_rank'] = ranks.get((out.get('queue'), out.get('host'))) or out.get('site_rank')
    return out


def node_guard_text(f):
    """A node guard decision, concisely: which guard at the site, the node
    and its site, why, and the evidence. The decision record's message and
    the screen's detail.

    Fir node guard 6 on BNL_OSG_PanDA_1: fc20513. Kills jobs at a fixed run
    time, ~109 min (memory or slot limit): 12/12 failed in 4 h; tasks
    40958, 40970 run fine elsewhere.
    """
    host, site, queue = f.get('host'), short_name(f.get('site')), f.get('queue')
    state = str(f.get('state') or '')
    reason = f.get('reason')
    why = NODE_REASONS.get(reason, str(reason or '').replace('_', ' '))
    if state != 'excluded':
        node = f'{host} at {site}' if site else str(host)
        return f"{node}: {state.replace('_', ' ')} on {queue}" + (f' ({why}).' if why else '.')
    text = f'{_node_guard_name(f)} on {queue}: {host}.'
    died = (f.get('failed_duration_s') or {}).get('median')
    if reason == 'fixed_time' and died:
        why += f', ~{round(died / 60)} min (memory or slot limit)'
    if f.get('jobs') is not None:
        why += f": {f.get('failed')}/{f['jobs']} failed in {f.get('window_h')} h"
        if reason != 'fixed_time' and f.get('fast_failed'):
            why += f", {f['fast_failed']} fast"
        done = f.get('tasks_finished_elsewhere') or []
        if done:
            why += (f"; task{'s' if len(done) > 1 else ''} "
                    f"{', '.join(str(t) for t in done)} run{'' if len(done) > 1 else 's'} "
                    f"fine elsewhere")
    return f'{text} {why}.' if why else text


def failure_key(instance_name, extra):
    """The identity a failure and its recovery share: the same action from
    the same source on the same subject. The room's epicprod-live screen
    reads recovery by this key too (COMP_SNAPSHOT.md, Live)."""
    return json.dumps([str(extra.get('action') or ''), instance_name,
                       extra.get('subject_type'), extra.get('subject_key'),
                       extra.get('source'), extra.get('operation')])


def select_notice(row, extra, failures, live_policy):
    """Return delivery attributes, or None for a quiet channel event.

    Observe quiet successes too, so recovery is not hidden by a source's
    routine-success default. Explicit live overrides remain authoritative;
    subscription filters are still applied to the returned attributes.
    Never mutate the original record or another subscriber's attributes.
    """
    if row.app_name != 'epicprod':
        return extra
    action = str(extra.get('action') or '')
    outcome = str(extra.get('outcome') or '')
    username = str(extra.get('username') or '')
    automated = username == 'cron' or username.endswith('_cron')
    notice = dict(extra)
    failure = outcome in FAILURE_OUTCOMES
    if failure and not notice.get('reason'):
        notice['reason'] = str(notice.get('summary') or row.message or '')[:300]

    quiet = False
    if automated and action in MAINTENANCE:
        quiet = True
    elif automated:
        key = failure_key(row.instance_name, extra)
        if failure:
            current = {'outcome': outcome, 'reason': notice.get('reason', '')}
            quiet = failures.get(key) == current
            failures[key] = current
            # A source's routine importance must not hide its first failure.
            notice.update(live_default=True, sublevel='high')
        elif outcome == 'ok':
            previous = failures.pop(key, None)
            if previous:
                notice.update(
                    live_default=True, sublevel='normal',
                    operation=f'{action}_recovered',
                    summary=('Recovered; previous failure: '
                             f"{previous['reason'] or previous['outcome']}"))
                return notice
            quiet = action in ROUTINE_SUCCESSES
            pattern = PROGRESS_PATTERNS.get(action)
            if pattern and any(int(v) for v in re.findall(
                    pattern, str(extra.get('summary') or ''))):
                quiet = False
        elif action == 'node_guard_decision' and outcome == 'would_exclude':
            quiet = True

    if quiet and live_policy.get(action) is not True:
        return None
    return notice
