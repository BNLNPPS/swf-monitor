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
        'breakoutUrl': reverse('monitor_app:prod_dashboard'),
        'weeklyUrl': static('monitor_app/prod_dashboard/weekly-cores.json'),
    }
    return render_to_string('monitor_app/prod_dashboard.html', {
        'dashboard_record': record,
        'dashboard_config': config,
        'dashboard_version': '20261023-14',
    }, request=request)


def prod_dashboard(request):
    """The full-page dashboard, without application navigation headers."""
    from django.http import HttpResponse

    return HttpResponse(dashboard_document(request))


def dashboard_home(request):
    """The dashboard tab within the production home."""
    return render(request, 'monitor_app/prod_hub_dashboard.html', {
        'active_tab': 'dash',
        'nav_mode': 'production',
        'dashboard_html': dashboard_document(request),
    })
