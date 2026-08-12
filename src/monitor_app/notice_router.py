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


def _matches(sub, action, extra):
    """One subscription against one event: name (exact or trailing-*
    prefix), then equality over the structured fields."""
    if sub.event.endswith('*'):
        if not action.startswith(sub.event[:-1]):
            return False
    elif action != sub.event:
        return False
    for key, want in (sub.filters or {}).items():
        if extra.get(key) != want:
            return False
    return True


def _compose(row, extra):
    """Deterministic notice content from the event (NOTICE_ROUTING.md §
    Delivery): title from the action and subject, detail from the
    record's explanatory fields, URL to the log record, severity from
    the outcome."""
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
    return {
        'source': 'swf-events',
        'severity': 'info' if outcome in ('', 'ok') else 'warning',
        'title': title[:300],
        'detail': detail[:2000],
        'url': f'{external_face_base_url()}/prod/logs/{row.id}/',
    }


def route_new_events():
    """One routing pass: deliver new matching events, advance the mark.

    Called from the tailer cycle; exceptions propagate to the caller's
    cycle guard and are logged there — a failed pass retries from the
    same mark next cycle.
    """
    from monitor_app.models import (AppLog, CapcomNotice,
                                    NoticeSubscription, PersistentState)

    _init_high_water()
    last_id = int(PersistentState.get_state().get(STATE_KEY) or 0)
    subs = list(NoticeSubscription.objects.filter(enabled=True))
    rows = list(AppLog.objects.filter(id__gt=last_id,
                                      extra_data__has_key='action')
                .order_by('id')[:BATCH_MAX])
    if not rows:
        return 0
    delivered = 0
    for row in rows:
        extra = row.extra_data if isinstance(row.extra_data, dict) else {}
        action = str(extra.get('action') or '')
        for sub in subs:
            if not action or not _matches(sub, action, extra):
                continue
            if sub.delivery != 'buffer':
                logger.error(
                    "notice router: unknown delivery %r on subscription "
                    "%s ← %s; row %s not delivered",
                    sub.delivery, sub.subscriber, sub.event, row.id)
                continue
            content = _compose(row, extra)
            CapcomNotice.objects.create(
                subscriber=sub.subscriber,
                dedup_key=f'event:{row.id}:{sub.subscriber}',
                **content)
            delivered += 1
        PersistentState.update_state({STATE_KEY: int(row.id)})
    if delivered:
        logger.info("notice router: delivered %d notices (through row %s)",
                    delivered, rows[-1].id)
    return delivered
