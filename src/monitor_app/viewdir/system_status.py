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
    return render(request, 'monitor_app/system_status.html', {
        'groups': grouped_current_status(),
        'summary': status_summary(),
    })


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
