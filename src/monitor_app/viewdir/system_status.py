"""System status views for the production monitor."""

import json
import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ..activemq_connection import ActiveMQConnectionManager
from ..system_status import grouped_current_status, status_summary

logger = logging.getLogger(__name__)


def system_status_page(request):
    from ..models import SysConfig

    try:
        sysconfig_json = json.dumps(SysConfig.get_config(), indent=2, sort_keys=True)
    except Exception as exc:
        logger.warning('sysconfig read failed on system page: %s', exc)
        sysconfig_json = '{}'
    return render(request, 'monitor_app/system_status.html', {
        'groups': grouped_current_status(),
        'summary': status_summary(),
        'sysconfig_json': sysconfig_json,
    })


@require_POST
def sysconfig_save(request):
    """Replace the SysConfig document from the System page editor."""
    from ..epicprod_logging import log_epicprod_action
    from ..models import SysConfig

    if not request.user.is_authenticated:
        messages.error(request, 'Sign in to edit the system configuration.')
        return redirect('monitor_app:system_status')
    raw = request.POST.get('config_json') or ''
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError('top level must be a JSON object')
    except (ValueError, TypeError) as exc:
        messages.error(request, f'SysConfig not saved — invalid JSON: {exc}')
        return redirect('monitor_app:system_status')
    SysConfig.replace_config(parsed, username=request.user.username)
    log_epicprod_action(
        'web', 'sysconfig_edit',
        username=request.user.username,
        sublevel='high',
        live_default=True,
        keys=sorted(parsed.keys()),
    )
    messages.success(request, 'System configuration saved.')
    return redirect('monitor_app:system_status')


def system_status_json(request):
    summary = status_summary()
    latest = summary.get('latest_checked_at')
    return JsonResponse({
        'overall_status': summary.get('overall_status', 'unknown'),
        'overall_reason': summary.get('overall_reason', ''),
        'latest_checked_at': latest.isoformat() if latest else None,
        'counts': {
            'ok': summary.get('ok', 0),
            'warning': summary.get('warning', 0),
            'error': summary.get('error', 0),
            'unknown': summary.get('unknown', 0),
            'total': summary.get('total', 0),
        },
    })


@require_POST
def system_status_refresh(request):
    msg = {
        'msg_type': 'refresh_system_status',
        'namespace': 'prodops',
        'source': 'system_page',
    }
    try:
        ok = ActiveMQConnectionManager().send_message('/queue/epicprod.ops', json.dumps(msg))
    except Exception as exc:
        ok = False
        logger.error("system status refresh trigger failed: %s", exc)
    if ok:
        messages.info(request, 'System status refresh queued.')
    else:
        messages.error(request, 'System status refresh could not be queued.')
    return redirect('monitor_app:system_status')
