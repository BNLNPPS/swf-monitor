"""Production dashboard: the home tab and its standalone document."""

import logging

from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse

from monitor_app.comp_snapshot import get_snapshot

logger = logging.getLogger(__name__)


def dashboard_document(request):
    """Render the shared dashboard with the current cached production record."""
    try:
        record = get_snapshot()
    except Exception:
        logger.exception('Production dashboard snapshot unavailable')
        record = {'error': 'The production record is temporarily unavailable.'}
    config = {
        'snapshotUrl': reverse('monitor_app:comp_snapshot_json'),
        'historyUrl': reverse('monitor_app:prod_dashboard_history'),
        'resourcesUrl': reverse('monitor_app:prod_dashboard_resources'),
        'efficiencyUrl': reverse('monitor_app:compute_efficiency_data') + '?summary=1',
        'productiveCoresUrl': reverse('monitor_app:compute_efficiency_data') + '?daily_cores=1',
        'efficiencyHistoryUrl': reverse('monitor_app:compute_efficiency'),
        'taskUrl': reverse('monitor_app:panda_task_detail', args=[0]),
        'breakoutUrl': reverse('monitor_app:prod_dashboard'),
        'weeklyUrl': static('monitor_app/prod_dashboard/weekly-cores.json'),
        'pillarScales': {'OSG': 2500, 'GREX': 2500, 'Google': 100},
        'siteNodeCaps': {'Google': 5},
        'sitePanels': {'osg': 'OSG', 'grex': 'GREX', 'nersc': 'Perlmutter',
                       'google': 'Google', 'compute-canada': 'Compute Canada', 'jlab': 'JLab'},
    }
    return render_to_string('monitor_app/prod_dashboard.html', {
        'dashboard_record': record,
        'dashboard_config': config,
        'dashboard_version': '20261025-pillar-scales-1',
    }, request=request)


def prod_dashboard(request):
    """The full-page dashboard, without application navigation headers."""
    from django.http import HttpResponse

    return HttpResponse(dashboard_document(request))


def task_resources_json(request):
    from django.http import JsonResponse
    from monitor_app.task_resources import product

    try:
        result = product(refresh=request.GET.get('refresh') == '1')
        return JsonResponse({'record': result['value'],
                             'built_at': result['built_at'],
                             'refreshing': result['refreshing']})
    except Exception:
        logger.exception('Task resource history unavailable')
        return JsonResponse({'error': 'Task resource history is temporarily unavailable.'}, status=503)


def dashboard_home(request):
    """The dashboard tab within the production home."""
    return render(request, 'monitor_app/prod_hub_dashboard.html', {
        'active_tab': 'dash',
        'nav_mode': 'production',
        'dashboard_html': dashboard_document(request),
    })
