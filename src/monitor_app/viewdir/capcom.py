"""Capcom endpoint: display-ready SWF state and notices for the tjai
Capcom page.

Capcom polls this open read endpoint every few minutes (through the
swf-remote proxy for external reach). Each entry under 'states' is
shaped exactly as tjai's capcom.set_state(source, value, color, url)
expects, following the pax-eden Ahbazon producer; each entry under
'notices' is shaped as tjai's emit_notice(source, severity, title, url,
dedup_key) expects, and the collector emits only dedup keys it has not
seen. swf-monitor is a poll-mode Capcom source by design: BNL-side
systems do not hold the personal feed's ingest token.
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.http import JsonResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

REMOTE_FACE = 'https://epic-devcloud.org/prod'
ET = ZoneInfo('America/New_York')


def _delivery_notice():
    """The nightly campaign-delivery feed item: the daily record
    advancing is the event (one notice per recorded day), and a rebuild
    failure newer than the record is a warning instead. None when no
    daily record exists yet."""
    from snapper_ai.models import SystemSnap

    from ..models import AppLog

    newest = (SystemSnap.objects
              .filter(scope='epicprod', capture_policy='delivery-daily-v1')
              .order_by('-snap_time').first())
    action = (AppLog.objects
              .filter(app_name='epicprod',
                      extra_data__action='delivery_daily_rebuild')
              .order_by('-timestamp')
              .values('timestamp', 'extra_data').first())
    if action:
        outcome = str((action['extra_data'] or {}).get('outcome') or 'ok')
        failed_after_record = (
            outcome not in ('', 'ok')
            and (newest is None
                 or action['timestamp'] > newest.observed_at))
        if failed_after_record:
            reason = str((action['extra_data'] or {}).get('reason') or outcome)
            day = action['timestamp'].astimezone(ET).date()
            return {
                'source': 'swf-campaign-delivery',
                'severity': 'warning',
                'title': ('Campaign delivery nightly rebuild failed '
                          f'({reason})'),
                'url': f'{REMOTE_FACE}/logs/',
                'dedup_key': f'delivery-daily-fail:{day.isoformat()}',
            }
    if newest is None:
        return None
    day = newest.snap_time.astimezone(ET).date()
    campaigns = (((newest.state or {}).get('components') or {})
                 .get('delivery') or {}).get('data') or {}
    parts = []
    for name, block in sorted((campaigns.get('campaigns') or {}).items()):
        totals = block.get('totals') or {}
        files = int(totals.get('arrived_files') or 0)
        if not files:
            continue
        part = f'{name}: {files:,} files'
        events = int(totals.get('arrived_events') or 0)
        if events >= 1_000_000:
            part += f' ({events / 1e6:.1f}M events)'
        elif events:
            part += f' ({events:,} events)'
        parts.append(part)
    day_label = day.strftime('%b %-d')
    title = f'Campaign delivery updated through {day_label} ET'
    title += ' · ' + (' · '.join(parts) if parts else 'no new files')
    return {
        'source': 'swf-campaign-delivery',
        'severity': 'info',
        'title': title,
        'url': f'{REMOTE_FACE}/snapper/epicprod/campaign/',
        'dedup_key': f'delivery-daily:{day.isoformat()}',
    }


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
    notices = []
    detail = {}

    try:
        notice = _delivery_notice()
        if notice:
            notices.append(notice)
    except Exception as exc:
        logger.error('capcom state: delivery notice failed: %s', exc)
        detail['delivery'] = {'error_text': str(exc)}

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

    return JsonResponse({'built_at': now.isoformat(),
                         'states': states, 'notices': notices,
                         'detail': detail})
