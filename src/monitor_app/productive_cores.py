"""Daily production delivery, retained as a bounded Snapper component."""
from datetime import datetime, timedelta, timezone

from django.db import transaction
from snapper_ai.models import CurrentComponent
from snapper_ai.services import publish_component, register_component

from .cached_product import get_product, stores
from .compute_efficiency import _dashboard_site, query_days

KEY = 'productive_cores_daily:v1'
COMPONENT = 'production_compute'
PUBLISHER = 'swf-monitor:production-compute'
POLICY = 'production-daily-core-hours-v1'
DAYS = 90
SITES = (('osg', 'OSG', '#6ca8e8'), ('grex', 'GREX', '#7fce95'),
         ('perlmutter', 'Perlmutter', '#e3b954'))
REGISTRATION = {
    'title': 'Daily production compute',
    'description': 'Successful EIC managed-job core-hours by completed UTC day; canaries excluded.',
    'visibility': 'public', 'owning_subsystem': 'SWF production computing',
    'assessment_policy': POLICY,
    'quantities': {
        'daily': {'path': 'daily', 'type': 'array', 'required': True,
                  'kind': 'bounded_history', 'max_items': DAYS,
                  'description': 'Daily successful core-hours, productive average cores and timing coverage.'},
    },
}


def build(now=None):
    now = now or datetime.now(timezone.utc)
    end = now.date()  # Completed UTC days only, never today's partial bin.
    start = end - timedelta(days=DAYS)
    component = CurrentComponent.objects.filter(scope='epicprod', name=COMPONENT).first()
    previous = (component.data or {}).get('daily', []) if component else []
    reread = max(start, end - timedelta(days=7)) if previous else start
    rows = query_days(reread, end, production=True, finished_only=True)
    by_day = {}
    for row in rows:
        sid = _dashboard_site(row)
        if sid is None:
            continue
        entry = by_day.setdefault(row[0], {})
        site = entry.setdefault(sid, {'core_hours': 0.0, 'jobs': 0, 'missing': 0})
        site['core_hours'] += row[4]
        site['jobs'] += int(row[6])
        site['missing'] += int(row[7])
    daily = [row for row in previous if str(start) <= row['day'] < str(reread)]
    day = reread
    while day < end:
        sites = {}
        for sid, _, _ in SITES:
            stats = by_day.get(str(day), {}).get(sid, {'core_hours': 0.0, 'jobs': 0, 'missing': 0})
            sites[sid] = {**stats, 'cores': stats['core_hours'] / 24 if not stats['missing'] else None}
        total = None if any(s['cores'] is None for s in sites.values()) else sum(s['cores'] for s in sites.values())
        daily.append({'day': str(day), 'sites': sites, 'total': total})
        day += timedelta(days=1)
    value = {'daily': daily, 'start': str(start), 'end': str(end - timedelta(days=1)),
             'sites': [{'id': sid, 'label': label, 'color': color} for sid, label, color in SITES],
             'through': now.isoformat(), 'day_hours': 24, 'timezone': 'UTC',
             'source': 'snapper:epicprod:production_compute',
             'selection': "workinggroup=EIC; prodsourcelabel=managed"}
    # Development reads never publish over the deployed producer's history.
    if stores():
        with transaction.atomic():
            register_component(scope='epicprod', name=COMPONENT, publisher_identity=PUBLISHER,
                               registration=REGISTRATION, component_schema_version=1)
            publish_component(scope='epicprod', name=COMPONENT, publisher_identity=PUBLISHER,
                              data=value, assessed_at=now, source_as_of=now,
                              assessment_policy_version=POLICY)
    return value


def product(refresh=False):
    return get_product(KEY, build, 3600, refresh=refresh, async_first_fill=True)
