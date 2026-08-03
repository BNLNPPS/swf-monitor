"""Capcom state endpoint: display-ready SWF state for the tjai Capcom page.

Capcom polls this open read endpoint every few minutes (through the
swf-remote proxy for external reach) and renders the entries on its state
tiles. Each entry under 'states' is shaped exactly as tjai's
capcom.set_state(source, value, color, url) expects, following the
pax-eden Ahbazon producer, so the capcom-side collector can apply each
entry as delivered. swf-monitor is a poll-mode Capcom source by design:
BNL-side systems do not hold the personal feed's ingest token.
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
        value = f'{running_jobs} running'
        if pct is not None:
            value += f' · {pct:g}% ok/12h'
        entry = {'source': 'swf-panda', 'value': value,
                 'url': f'{REMOTE_FACE}/panda/jobs/'}
        if pct is not None:
            entry['color'] = ('green' if pct >= 90
                              else 'yellow' if pct >= 50 else 'red')
        states.append(entry)
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

    return JsonResponse({'built_at': now.isoformat(),
                         'states': states, 'detail': detail})
