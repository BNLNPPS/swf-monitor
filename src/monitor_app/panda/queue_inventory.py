"""The monitor's PanDA queue inventory (``PandaQueue``), kept current from
PanDA's own queue table (``schedconfig_json``, the configuration PanDA
itself reads) rather than from a static file.

Every EIC queue PanDA knows is created or updated with its configuration
and status; the monitor's own additions (``metadata``: descriptions,
classes) are left alone. A queue PanDA no longer lists keeps its row,
marked ``absent``, so its history and descriptions stay reachable.
Run with every pressure-front cycle (``swf_epicprod.front.run_cycle``)
and behind the queue list's update button.
"""
import logging

from django.db import connections, transaction

from ..models import PandaQueue
from .constants import PANDA_SCHEMA

logger = logging.getLogger(__name__)

ABSENT = 'absent'


def sync_queue_inventory(vo='eic'):
    """Bring ``PandaQueue`` in line with PanDA's queue table for ``vo``.
    Returns {created, updated, absent, queues}."""
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT "panda_queue", "data" FROM "{PANDA_SCHEMA}"."schedconfig_json" '
            "WHERE \"data\"->>'vo_name' = %s", [vo])
        rows = cursor.fetchall()
    live = {}
    for name, data in rows:
        if isinstance(data, str):
            import json
            data = json.loads(data)
        live[str(name)] = data or {}
    created = updated = 0
    with transaction.atomic():
        existing = {q.queue_name: q for q in PandaQueue.objects.all()}
        for name, data in live.items():
            fields = {'site': str(data.get('site') or data.get('panda_site') or ''),
                      'queue_type': str(data.get('type') or ''),
                      'status': str(data.get('status') or ''),
                      'config_data': data}
            row = existing.get(name)
            if row is None:
                PandaQueue.objects.create(queue_name=name, **fields)
                created += 1
                continue
            changed = [k for k, v in fields.items() if getattr(row, k) != v]
            if changed:
                for k in changed:
                    setattr(row, k, fields[k])
                row.save(update_fields=changed + ['updated_at'])
                updated += 1
        gone = [name for name, row in existing.items()
                if name not in live and row.status != ABSENT]
        if gone:
            PandaQueue.objects.filter(queue_name__in=gone).update(status=ABSENT)
    if created or gone:
        logger.info('queue inventory: %d created, %d updated, %d absent (%s)',
                    created, updated, len(gone), ', '.join(gone))
    return {'created': created, 'updated': updated, 'absent': len(gone),
            'queues': len(live)}
