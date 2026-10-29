"""Remove datasets from one disk RSE (swf-epicprod docs/STORAGE.md,
Removing datasets from an RSE), reached from the Storage doors page.

The page computes nothing and holds no credential: it posts a pattern,
the production operations agent previews it against the live catalog,
and a typed confirmation queues the removal of what the preview found
eligible. The record of each request is listed below the form.
"""
from django.http import Http404
from django.shortcuts import render

from monitor_app.middleware import is_tunnel_request
from monitor_app.models import RseDeletion


def panda_rse_deletion(request, rse):
    from swf_epicprod.rse_deletion import DISK_RSES, TAPE_RSE
    if rse not in DISK_RSES:
        raise Http404(f'{rse} is not a production disk RSE')
    recent = list(RseDeletion.objects.filter(rse=rse)[:20])
    for r in recent:
        totals = (r.result or {}).get('totals') or {}
        r.removed_tb = (totals['removed_bytes'] / 1e12
                        if 'removed_bytes' in totals else None)
    return render(request, 'monitor_app/panda_rse_deletion.html', {
        'rse': rse,
        'tape_rse': TAPE_RSE,
        'recent': recent,
        'can_act': (request.user.is_authenticated
                    and not is_tunnel_request(request)),
        'active_nav': {'panda_storage_doors': True},
    })
