"""Pages and cached-data reads for compute efficiency history."""
import logging
from datetime import date, datetime, timezone

from django.http import JsonResponse
from django.shortcuts import render

from monitor_app import compute_efficiency as efficiency

logger = logging.getLogger(__name__)


def compute_efficiency(request):
    today = datetime.now(timezone.utc).date()
    index = today.year * 12 + today.month - 1 - 5
    start = max(efficiency.START, date(index // 12, index % 12 + 1, 1))
    return render(request, 'monitor_app/compute_efficiency.html', {
        'history_start': efficiency.START.isoformat(),
        'default_start': start.isoformat(), 'today': today.isoformat(),
    })


def compute_efficiency_data(request):
    if request.GET.get('daily_cores') == '1':
        from monitor_app.productive_cores import product
        try:
            cached = product(refresh=request.GET.get('refresh') == '1')
            return JsonResponse({'record': cached['value'], 'built_at': cached['built_at'],
                                 'refreshing': cached['refreshing']},
                                status=200 if cached['value'] else 202)
        except Exception:
            logger.exception('Daily productive core history refresh failed')
            return JsonResponse({'error': 'Daily production history update failed.'}, status=503)
    today = datetime.now(timezone.utc).date()
    try:
        end = date.fromisoformat(request.GET.get('end') or str(today))
        start = date.fromisoformat(request.GET.get('start') or str(efficiency.START))
        bucket = request.GET.get('bucket', 'month')
        if not efficiency.START <= start <= end <= today or bucket not in ('month', 'week'):
            raise ValueError('Choose a valid date range and month or week grouping.')
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    try:
        cached = efficiency.product(refresh=request.GET.get('refresh') == '1')
        if request.GET.get('summary') == '1':
            current = efficiency.current_product()
            result = efficiency.dashboard_data(cached.get('value') or {}, current.get('value'))
            result.update(built_at=current['built_at'], refreshing=current.get('refreshing'),
                          history_built_at=cached['built_at'])
            return JsonResponse(result, status=200 if result['current'] else 202)
    except Exception:
        logger.exception('compute efficiency history refresh failed')
        return JsonResponse({'error': 'History update failed; the failure is recorded in the server log.'}, status=500)
    value = cached.get('value')
    result = efficiency.display_data(value, start, end, bucket,
                                    exclude=request.GET.get('exclude_task') == '40856') if value else {}
    result.update(built_at=cached['built_at'].isoformat() if cached.get('built_at') else None,
                  refreshing=cached.get('refreshing'), age_seconds=cached.get('age_seconds'))
    return JsonResponse(result, status=200 if value else 202)
