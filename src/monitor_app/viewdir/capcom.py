"""Capcom state endpoint: display-ready SWF state for the tjai Capcom page.

Capcom polls this open read endpoint every few minutes (through the
swf-remote proxy for external reach) and renders the entries on its state
tiles. Each entry under 'states' is shaped exactly as tjai's
capcom.set_state(source, value, color, url) expects, following the
pax-eden Ahbazon producer, so the capcom-side collector can apply each
entry as delivered. This endpoint is state-only: discrete SWF events
(the campaign-delivery feed) are listen sources, posted to the Capcom
ingest endpoint by the prod-ops agent — the credential holder — at the
moment they occur.
"""

import logging
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

REMOTE_FACE = 'https://epic-devcloud.org/prod'


def capcom_state(request):
    """SWF state tiles: the System page verdict and current PanDA activity.

    states: tile-exact entries (source, value, color, url).
    detail: the numbers behind them, for any richer capcom-side use.
    Each section degrades independently: a failing source contributes an
    error tile rather than hiding or failing the whole response.
    """
    from ..snapper_panda import _in_flight_activity, _terminal_outcome_rows
    from ..system_status import status_summary

    now = timezone.now()
    states = []
    detail = {}

    try:
        summary = status_summary()
        status = summary.get('overall_status', 'unknown')
        bad = int(summary.get('warning', 0)) + int(summary.get('error', 0))
        value = status.upper() + (f' ({bad})' if bad else '')
        color = {'ok': 'green', 'warning': 'yellow',
                 'error': 'red'}.get(status)
        entry = {'source': 'swf-system', 'value': value,
                 'url': f'{REMOTE_FACE}/system/'}
        if color:
            entry['color'] = color
        states.append(entry)
        latest = summary.get('latest_checked_at')
        detail['system'] = {
            'status': status,
            'reason': summary.get('overall_reason', ''),
            'ok': summary.get('ok', 0),
            'warning': summary.get('warning', 0),
            'error': summary.get('error', 0),
            'checked_at': latest.isoformat() if latest else None,
        }
    except Exception as exc:
        logger.error('capcom state: system summary failed: %s', exc)
        states.append({'source': 'swf-system', 'value': 'UNAVAILABLE',
                       'url': f'{REMOTE_FACE}/system/'})
        detail['system'] = {'error_text': str(exc)}

    try:
        running_jobs = sum(
            row['jobs'] for row in _in_flight_activity()
            if row['status'] == 'running')
        finished = 0
        failed = 0
        for _site, status, _cls, count in _terminal_outcome_rows(
                now - timedelta(hours=12), now):
            if status == 'finished':
                finished += int(count or 0)
            elif status == 'failed':
                failed += int(count or 0)
        decided = finished + failed
        pct = round(100.0 * finished / decided, 1) if decided else None
        value = f'{running_jobs} jobs'
        if pct is not None:
            value += f' · {pct:.0f}%'
        states.append({'source': 'swf-panda', 'value': value,
                       'url': f'{REMOTE_FACE}/panda/jobs/'})
        detail['panda'] = {
            'running_jobs': running_jobs,
            'finished_12h': finished,
            'failed_12h': failed,
            'success_pct_12h': pct,
        }
    except Exception as exc:
        logger.error('capcom state: panda queries failed: %s', exc)
        states.append({'source': 'swf-panda', 'value': 'UNAVAILABLE',
                       'url': f'{REMOTE_FACE}/panda/jobs/'})
        detail['panda'] = {'error_text': str(exc)}

    try:
        from ..alarms_data import active_event_count, alarm_configs

        counts = {}
        for cfg in alarm_configs():
            entry_id = cfg.get('entry_id') or ''
            if entry_id:
                counts[cfg.get('name') or entry_id] = (
                    active_event_count(entry_id))
        active = sum(counts.values())
        states.append({
            'source': 'swf-alarms',
            'value': f'{active} active' if active else 'OK',
            'color': 'red' if active else 'green',
            'url': f'{REMOTE_FACE}/alarms/'})
        detail['alarms'] = {
            'active': active,
            'by_alarm': {name: n for name, n in counts.items() if n}}
    except Exception as exc:
        logger.error('capcom state: alarm counts failed: %s', exc)
        states.append({'source': 'swf-alarms', 'value': 'UNAVAILABLE',
                       'url': f'{REMOTE_FACE}/alarms/'})
        detail['alarms'] = {'error_text': str(exc)}

    try:
        from ..epicprod_logging import SUBLEVEL_VALUES, live_stream_q
        from ..models import AIMemory, AppLog, SysConfig

        since = now - timedelta(hours=24)
        # Posts: what the epicprod-live publisher put on the channel —
        # the same SysConfig-governed selection it publishes from.
        min_sublevel = str(SysConfig.get_setting(
            'epicprod_live_min_sublevel', 'normal') or '')
        if min_sublevel not in SUBLEVEL_VALUES:
            min_sublevel = 'normal'
        posts = AppLog.objects.filter(
            live_stream_q(min_sublevel), timestamp__gte=since).count()
        # Queries: DISpatcher-handled questions (channel, mentions, DMs)
        # — one user-role memory row is recorded per handled exchange.
        queries = AIMemory.objects.filter(
            username='pandabot', session_id='mattermost', role='user',
            created_at__gte=since).count()
        states.append({
            'source': 'swf-dispatcher',
            'value': (f'{posts} post{"s" if posts != 1 else ""} · '
                      f'{queries} quer{"ies" if queries != 1 else "y"}/24h'),
            'url': 'https://chat.epic-eic.org/main/channels/dispatcher'})
        detail['dispatcher'] = {'posts_24h': posts, 'queries_24h': queries,
                                'min_sublevel': min_sublevel}
    except Exception as exc:
        logger.error('capcom state: dispatcher counts failed: %s', exc)
        states.append({
            'source': 'swf-dispatcher', 'value': 'UNAVAILABLE',
            'url': 'https://chat.epic-eic.org/main/channels/dispatcher'})
        detail['dispatcher'] = {'error_text': str(exc)}

    return JsonResponse({'built_at': now.isoformat(),
                         'states': states, 'detail': detail})
