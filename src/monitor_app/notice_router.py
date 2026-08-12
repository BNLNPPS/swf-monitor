"""Notice routing: match new action-stream events to consumer-registered
subscriptions and deliver them (docs/NOTICE_ROUTING.md).

``route_new_events()`` runs once per cycle of the stream-tailer process
(the epicprod-live publisher). It keeps its own high-water mark, so
routing and Mattermost publication advance independently; a routing
failure never blocks publication and vice versa. Buffered-pull delivery
writes one CapcomNotice row per (matched event, subscriber); push
plugins are added as named delivery modes when they come.
"""
import logging

logger = logging.getLogger(__name__)

STATE_KEY = 'notice_router_last_id'
# Per-cycle row cap: a quiet-period backlog drains over a few cycles
# rather than one giant pass; nothing is dropped, the mark just advances
# as far as the cap allows.
BATCH_MAX = 500


def _init_high_water():
    """Start at the current stream head — never replay history."""
    from monitor_app.models import AppLog, PersistentState
    state = PersistentState.get_state()
    if STATE_KEY not in state:
        head = (AppLog.objects.order_by('-id')
                .values_list('id', flat=True).first()) or 0
        PersistentState.update_state({STATE_KEY: head})
        logger.info("notice router: initialized high-water mark at %s", head)


def _matches(sub, row, action, extra, live_policy):
    """One subscription against one event: name (exact or trailing-*
    prefix), then equality over the structured fields. A list-valued
    filter means membership, so one subscription covers a value set
    ({'operation': ['pause', 'resume']}). Two reserved keys reach beyond
    the event's own attributes: ``app_name`` matches the record's logging
    namespace, and ``live`` matches the event's effective live state —
    the runtime live-policy override where one exists, else the record's
    ``live_default`` — so a subscription can select the live stream as
    data (NOTICE_ROUTING.md § Subscriptions)."""
    if sub.event.endswith('*'):
        if not action.startswith(sub.event[:-1]):
            return False
    elif action != sub.event:
        return False
    for key, want in (sub.filters or {}).items():
        if key == 'app_name':
            have = row.app_name
        elif key == 'live':
            override = live_policy.get(action)
            have = bool(override if override is not None
                        else extra.get('live_default'))
        else:
            have = extra.get(key)
        if isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


SEVERITIES = ('info', 'warning', 'alarm', 'error')


def _compose(row, extra):
    """Deterministic notice content from the event (NOTICE_ROUTING.md §
    Delivery): title from the action and subject, detail from the
    record's explanatory fields, URL and severity from the event's own
    ``url``/``severity`` attributes when it carries them, else the log
    record link and outcome-derived severity."""
    from monitor_app.models import external_face_base_url

    action = str(extra.get('action') or row.funcname or 'event')
    subject = str(extra.get('subject_key') or '')
    title = action.replace('_', ' ')
    if subject:
        title = f'{title}: {subject}'
    outcome = str(extra.get('outcome') or '')
    detail = str(extra.get('narration') or extra.get('reason')
                 or extra.get('summary') or '')
    if outcome and outcome != 'ok':
        detail = f'{outcome.upper()} — {detail}' if detail else outcome.upper()
    severity = str(extra.get('severity') or '')
    if severity not in SEVERITIES:
        severity = 'info' if outcome in ('', 'ok') else 'warning'
    url = str(extra.get('url') or '')
    if url.startswith('/'):
        url = f'{external_face_base_url()}/prod{url}'
    elif not url:
        url = f'{external_face_base_url()}/prod/logs/{row.id}/'
    return {
        'source': 'swf-notices',
        'severity': severity,
        'title': title[:300],
        'detail': detail[:2000],
        'url': url[:500],
    }


def route_new_events():
    """One routing pass: deliver new matching events, advance the mark.

    Called from the tailer cycle; exceptions propagate to the caller's
    cycle guard and are logged there — a failed pass retries from the
    same mark next cycle.
    """
    from monitor_app.epicprod_logging import get_live_policy
    from monitor_app.models import (AppLog, CapcomNotice,
                                    NoticeSubscription, PersistentState)
    from monitor_app.notice_plugins import PLUGINS

    _init_high_water()
    last_id = int(PersistentState.get_state().get(STATE_KEY) or 0)
    subs = list(NoticeSubscription.objects.filter(enabled=True))
    rows = list(AppLog.objects.filter(id__gt=last_id,
                                      extra_data__has_key='action')
                .order_by('id')[:BATCH_MAX])
    if not rows:
        return 0
    live_policy = get_live_policy()
    delivered = 0
    active_plugins = []
    failed_plugins = set()
    for row in rows:
        extra = row.extra_data if isinstance(row.extra_data, dict) else {}
        action = str(extra.get('action') or '')
        for sub in subs:
            if not action or not _matches(sub, row, action, extra,
                                          live_policy):
                continue
            if sub.delivery == 'buffer':
                content = _compose(row, extra)
                _, created = CapcomNotice.objects.get_or_create(
                    subscriber=sub.subscriber,
                    dedup_key=f'event:{row.id}:{sub.subscriber}',
                    defaults=content)
                delivered += int(created)
                continue
            plugin = PLUGINS.get(sub.delivery)
            if plugin is None:
                logger.error(
                    "notice router: unknown delivery %r on subscription "
                    "%s ← %s; row %s not delivered",
                    sub.delivery, sub.subscriber, sub.event, row.id)
                continue
            # Push is at-most-once: a failure is logged and the pass
            # continues, so a push outage never stalls buffered delivery.
            # A plugin whose start_pass fails sits out the rest of the
            # pass rather than re-failing on every matched row.
            if sub.delivery in failed_plugins:
                continue
            try:
                if plugin not in active_plugins:
                    plugin.start_pass()
                    active_plugins.append(plugin)
                plugin.deliver(row, extra)
                delivered += 1
            except Exception:
                if plugin not in active_plugins:
                    failed_plugins.add(sub.delivery)
                logger.exception(
                    "notice router: push delivery %r failed for row %s "
                    "(subscription %s ← %s)",
                    sub.delivery, row.id, sub.subscriber, sub.event)
        PersistentState.update_state({STATE_KEY: int(row.id)})
    for plugin in active_plugins:
        try:
            plugin.end_pass()
        except Exception:
            logger.exception("notice router: end_pass failed for a plugin")
    if delivered:
        logger.info("notice router: delivered %d notices (through row %s)",
                    delivered, rows[-1].id)
    return delivered
