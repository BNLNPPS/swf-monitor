"""corun job notification callback endpoint for PanDA bot Mattermost notices."""

import json
import logging

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from decouple import config
from mattermostdriver import Driver

logger = logging.getLogger('panda_bot')


def _mattermost_driver():
    return Driver({
        'url': config('MATTERMOST_URL', default='chat.epic-eic.org'),
        'token': config('MATTERMOST_TOKEN'),
        'scheme': 'https',
        'port': 443,
    })


def _message_from_payload(payload):
    status = payload.get('status', 'unknown')
    definition = payload.get('definition_name') or payload.get('definition_id') or 'corun job'
    result_url = payload.get('result_page_url')
    error = payload.get('error')

    title = f"corun job {status}: {definition}"
    lines = [f"**{title}**"]
    if result_url:
        lines.append(f"Result: {result_url}")
    if error:
        lines.append(f"Error: `{str(error)[:1000]}`")
    return "\n".join(lines)


@csrf_exempt
@require_POST
def corun_callback(request):
    """Receive corun terminal-job callbacks and post them to the pandabot channel."""
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

    try:
        driver = _mattermost_driver()
        driver.login()
        team = driver.teams.get_team_by_name(config('MATTERMOST_TEAM', default='main'))
        channel = driver.channels.get_channel_by_name(
            team['id'], config('MATTERMOST_CHANNEL', default='pandabot')
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
    return JsonResponse({'ok': True})
