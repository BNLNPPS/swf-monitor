"""GET /api/declared/ — the declared record as its out-of-process readers
take it (swf-epicprod docs/CONTINUOUS_PRODUCTION.md, Declared downtime):
per queue and per endpoint, the rule in force now with its line, the
next window coming, the spans the target was declared over (each to its
end or clearing plus the pilot's cache lag) and the latest span end.
The site canary's provider reads this (site-canary canary/declared.py),
so a standalone process needs no ORM. Anonymous, read-only.
"""
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from ..declared import _dt, _iso, canary_declared, declared_for, summary_line


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def declared_state(request):
    now = timezone.now()
    out = {'now': _iso(now), 'queues': {}, 'endpoints': {}}
    for kind, key in (('queue', 'queues'), ('endpoint', 'endpoints')):
        current = declared_for(kind, now=now)
        names = sorted(current)
        spans = canary_declared(None, now) if kind == 'queue' else {}
        for target in names:
            slot = current[target]
            coming = slot['future'][0] if slot['future'] else None
            out[key][target] = {
                'in_force': summary_line(slot['active'][0]) if slot['active'] else '',
                'coming': summary_line(coming) if coming else '',
                'coming_at': _iso(_dt(coming.get('start'))) if coming else None,
                'line': slot.get('line', ''),
                'spans': (spans.get(target) or {}).get('spans', []),
                'last_end': (spans.get(target) or {}).get('last_end'),
            }
        if kind == 'queue':
            # Targets with only past spans (no rule in force, none coming)
            # still matter to a reader placing a sample window in time.
            for target, info in spans.items():
                out[key].setdefault(target, {
                    'in_force': '', 'coming': '', 'coming_at': None, 'line': '',
                    'spans': info.get('spans', []), 'last_end': info.get('last_end')})
    return Response(out)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def storage_doors(request):
    """GET /api/storage-doors/ — the storage door canary's record as the
    last cycle stored it (site-canary docs/STORAGE_DOORS.md): every door,
    its verdict and evidence, the certificate it serves, and a
    valid_until so a reader can tell a current reading from a stale one.
    Anonymous, read-only; a 503 until a cycle has run. Nothing acts on
    this record — it is read by people and by the node guard, which uses
    it only to excuse a node for a door's failures."""
    from monitor_app.models import CachedProduct
    from monitor_app.panda.storage_doors import STATE_KEY
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    if row is None or not row.value:
        return Response({'error': 'no storage door record stored yet'}, status=503)
    return Response(row.value)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def node_guard_exclusion(request):
    """GET /api/node-guard/exclusion/ — the node guard's published
    exclusion as the last cycle stored it (site-canary docs/NODE_GUARD.md,
    Actuation): the same document the cycle puts on the bucket's pilot
    prefix. Anonymous, read-only; a 503 until a cycle has run."""
    from monitor_app.models import CachedProduct
    from monitor_app.panda.node_guard import EXCLUSION_KEY
    row = CachedProduct.objects.filter(key=EXCLUSION_KEY).first()
    if row is None or not row.value:
        return Response({'error': 'no exclusion document stored yet'}, status=503)
    return Response(row.value)
