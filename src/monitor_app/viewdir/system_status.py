"""System status views for the production monitor."""

import json
import logging

from django.contrib import messages
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
