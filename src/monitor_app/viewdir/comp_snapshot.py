"""The comp snapshot as JSON (docs/COMP_SNAPSHOT.md): the computing
centre room's live record. ``?refresh=1`` rebuilds before answering."""
import logging

from django.http import JsonResponse

from monitor_app.comp_snapshot import get_snapshot

logger = logging.getLogger(__name__)

# The room is drawn in the etaverse world, which fetches the record from
# the browser.
ALLOWED_ORIGIN = 'https://etaverse.com'


def comp_snapshot_json(request):
    refresh = str(request.GET.get('refresh', '')).lower() in ('1', 'true', 'yes')
    try:
        data = get_snapshot(refresh=refresh)
        response = JsonResponse(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("comp snapshot failed")
        response = JsonResponse({'error': f'{type(exc).__name__}: {exc}'}, status=503)
    response['Access-Control-Allow-Origin'] = ALLOWED_ORIGIN
    response['Cache-Control'] = 'no-cache'
    return response
