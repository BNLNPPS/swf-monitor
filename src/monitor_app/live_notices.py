"""Human-channel selection, separate from the complete action record.

Only the Mattermost delivery uses this policy (docs/NOTICE_ROUTING.md).
The caller persists ``failures`` alongside its stream position; it contains
only outstanding automated failures, not a second incident archive.
"""
import json
import re


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
        key = json.dumps([action, row.instance_name,
                          extra.get('subject_type'), extra.get('subject_key'),
                          extra.get('source'), extra.get('operation')])
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
