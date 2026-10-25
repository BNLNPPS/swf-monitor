"""Worker filesystem headroom from native-pilot heartbeats, with retained history."""
from datetime import datetime, timezone
import re

SITES = ('UM_GREX_PanDA_1',)
FIELDS = ('FreeMB', 'TotalMB', 'IFree', 'ITotal')


def summarize(rows, now=None):
    now = now or datetime.now(timezone.utc).timestamp()
    volumes, running, reporting = {}, 0, 0
    for site, pandaid, metrics in rows:
        running += 1
        values = {key: int(value) for key, value in
                  re.findall(r'\b(diskSample|(?:ce|pilot|scratch)(?:FreeMB|TotalMB|IFree|ITotal))=(\d+)', metrics or '')}
        stamp = values.get('diskSample')
        if not stamp or not -60 <= now - stamp <= 900:
            continue
        reporting += 1
        for label in ('ce', 'pilot', 'scratch'):
            if not all(label + key in values for key in FIELDS):
                continue
            total, free = values[label + 'TotalMB'], values[label + 'FreeMB']
            if total <= 0:
                continue
            key = site + ':' + label
            row = dict(site=site, volume=label, free_bytes=free * 2**20,
                       total_bytes=total * 2**20, free_fraction=free / total,
                       free_inodes=values[label + 'IFree'],
                       total_inodes=values[label + 'ITotal'],
                       observed_at=stamp, pandaid=pandaid)
            old = volumes.get(key)
            if old is None or row['free_fraction'] < old['free_fraction']:
                volumes[key] = row
    return dict(at=int(now), running_jobs=running, reporting_jobs=reporting,
                volumes=list(volumes.values()))


def build():
    from django.db import connections, transaction
    from django.utils import timezone as tz
    from .models import CachedProduct
    with connections['panda'].cursor() as cursor:
        cursor.execute('SELECT computingsite,pandaid,jobmetrics FROM doma_panda.jobsactive4 '
                       'WHERE computingsite=ANY(%s) AND jobstatus=%s', [list(SITES), 'running'])
        result = summarize(cursor.fetchall())
    from .cached_product import stores
    if not stores():
        return result
    with transaction.atomic():
        history, _ = CachedProduct.objects.select_for_update().get_or_create(
            key='worker_disk_space_history:v1', defaults={'value': {'samples': []}, 'built_at': tz.now()})
        samples = [s for s in history.value.get('samples', []) if s['at'] > result['at'] - 7 * 86400]
        if not samples or result['at'] - samples[-1]['at'] >= 60:
            samples.append(result)
            history.value = {'samples': samples}
            history.built_at = tz.now()
            history.save(update_fields=['value', 'built_at', 'updated_at'])
    return result


def reading():
    from .cached_product import get_product
    product = get_product('worker_disk_space:v1', build, 60)
    value = dict(product.get('value') or {})
    value['age_seconds'] = product.get('age_seconds')
    return value


from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(['GET'])
@permission_classes([AllowAny])
def worker_disk_space(request):
    return Response(reading())
