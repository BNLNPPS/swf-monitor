"""corun job notification callback endpoint: DISpatcher Mattermost notices,
and dispatch of campaign-assessment completions to the prod-ops agent's
enforcement handler (swf-epicprod docs/EPICPROD_ASSESSMENTS_V1.md)."""

import json
import logging

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from decouple import config
from mattermostdriver import Driver

logger = logging.getLogger('panda_bot')


def _short_text(value, limit=200):
    if value is None:
        return ''
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + '...'


def _format_duration(timing):
    if timing in (None, ''):
        return ''
    if isinstance(timing, dict):
        for key in ('duration_s', 'elapsed_s', 'seconds', 'total_seconds'):
            if key in timing:
                timing = timing[key]
                break
        else:
            return ''
    try:
        total_seconds = int(round(float(timing)))
    except (TypeError, ValueError):
        return ''
    if total_seconds < 0:
        return ''

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def _mattermost_driver():
    return Driver({
        'url': config('MATTERMOST_URL', default='chat.epic-eic.org'),
        'token': config('MATTERMOST_TOKEN'),
        'scheme': 'https',
        'port': 443,
    })


def _message_from_payload(payload):
    status = payload.get('status', 'unknown')
    display_name = (
        payload.get('result_page_title')
        or payload.get('definition_name')
        or payload.get('definition_id')
        or 'corun job'
    )
    result_url = payload.get('result_page_url')
    submitted_by = _short_text(payload.get('submitted_by'), limit=80)
    duration = _format_duration(payload.get('timing'))
    error = payload.get('error')

    title = f"corun job {status}: {_short_text(display_name)}"
    lines = [f"**{title}**"]
    if result_url:
        lines.append(f"Result: {result_url}")
    if submitted_by:
        lines.append(f"Submitted by: {submitted_by}")
    if duration:
        lines.append(f"Duration: {duration}")
    if error:
        lines.append(f"Error: `{str(error)[:1000]}`")
    return "\n".join(lines)


@csrf_exempt
@require_POST
def corun_callback(request):
    """Receive corun terminal-job callbacks and post visible results."""
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > 8192:
            return JsonResponse({'error': 'payload too large'}, status=413)
    except (TypeError, ValueError):
        pass

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return JsonResponse({'error': f'invalid JSON: {exc}'}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({'error': 'payload must be a JSON object'}, status=400)

    status = payload.get('status')
    if status not in {'completed', 'failed', 'cancelled'}:
        return JsonResponse({'error': 'ignored non-terminal status'}, status=400)

    # Campaign-assessment completions dispatch to the prod-ops agent's
    # enforcement handler before anything else — a Mattermost failure must
    # never cost an assessment slot.
    dispatched = _dispatch_assessment(payload)

    # Hidden result pages are internal machine artifacts. Their callbacks must
    # still drive assessment enforcement above, but must not become human bot
    # notices. Missing visibility metadata keeps the established behavior for
    # older corun senders and ordinary jobs.
    if payload.get('result_page_ui_visible') is False:
        logger.info(
            "Suppressed Mattermost notice for ui-hidden corun result job %s "
            "status=%s",
            payload.get('job_id'), status,
        )
        return JsonResponse({
            'ok': True,
            'assessment_dispatched': dispatched,
            'mattermost_notified': False,
        })

    try:
        driver = _mattermost_driver()
        driver.login()
        team = driver.teams.get_team_by_name(config('MATTERMOST_TEAM', default='main'))
        channel = driver.channels.get_channel_by_name(
            team['id'], config('MATTERMOST_CHANNEL', default='dispatcher')
        )
        driver.posts.create_post(options={
            'channel_id': channel['id'],
            'message': _message_from_payload(payload),
        })
    except Exception as exc:
        logger.exception("Failed to post corun callback to Mattermost")
        return JsonResponse({'error': str(exc)}, status=502)

    logger.info(
        "Posted corun callback notice for job %s status=%s",
        payload.get('job_id'), status,
    )
    return JsonResponse({
        'ok': True,
        'assessment_dispatched': dispatched,
        'mattermost_notified': True,
    })


def _dispatch_assessment(payload):
    """Queue assessment enforcement for a campaign_assessment job. Returns
    whether a dispatch happened; a failure is logged to the action stream —
    a slot that never fills must be visible, not silent."""
    # Matches campaign_assessment_daily / _weekly (one definition per
    # kind, each with its own system prompt; legacy _nightly also matches).
    definition_name = str(payload.get('definition_name') or '')
    if not definition_name.startswith(
            config('CORUN_ASSESSMENT_DEFINITION_NAME',
                   default='campaign_assessment')):
        return False
    message = {
        'msg_type': 'assessment_completed',
        'namespace': 'prodops',
        'job_id': str(payload.get('job_id') or ''),
        'prompt_group_id': str(payload.get('prompt_group_id') or ''),
        'page_group_id': str(payload.get('result_page_group_id') or ''),
        'status': str(payload.get('status') or ''),
        'timing': payload.get('timing'),
    }
    try:
        from monitor_app.activemq_connection import ActiveMQConnectionManager
        sent = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', json.dumps(message))
        if not sent:
            raise RuntimeError('ops-agent queue unreachable')
        return True
    except Exception as exc:
        logger.exception("assessment_completed dispatch failed")
        from monitor_app.epicprod_logging import log_epicprod_action
        log_epicprod_action(
            'web', 'assessment_enforce', outcome='error', sublevel='high',
            live_default=True,
            message=f"assessment_completed dispatch failed for job "
                    f"{message['job_id']}: {exc}")
        return False
