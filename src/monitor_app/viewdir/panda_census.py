"""The per-queue job census as JSON (monitor_app/panda/census.py): the
dispatcher's pressure input and the ready-queue page's source.
``?refresh=1`` rebuilds the hourly calibration."""
import logging

from django.http import JsonResponse

from monitor_app.panda.census import queue_census

logger = logging.getLogger(__name__)


def panda_queue_census_json(request):
    refresh = str(request.GET.get('refresh', '')).lower() in ('1', 'true', 'yes')
    try:
        data = queue_census(refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        logger.exception("queue census failed")
        return JsonResponse({'error': f'{type(exc).__name__}: {exc}'}, status=503)
    return JsonResponse(data)
