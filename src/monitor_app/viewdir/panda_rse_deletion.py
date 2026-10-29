"""Remove datasets from one disk RSE (swf-epicprod docs/STORAGE.md,
Removing datasets from an RSE), reached from the Storage doors page.

The page computes nothing and holds no credential: it posts a pattern,
the production operations agent previews it against the live catalog,
and a typed confirmation queues the removal of what the preview found
eligible. Acting is for the operations role on the internal monitor. The
RSE's previews and removals are listed below the form from the action
stream, the record that stands (panda/rse_deletions.py).
"""
from django.http import Http404
from django.shortcuts import render

from monitor_app.middleware import is_tunnel_request


def panda_rse_deletion(request, rse):
    from swf_epicprod.rse_deletion import DISK_RSES, TAPE_RSE
    from monitor_app.authority import get_authority, is_ops
    from monitor_app.panda.rse_deletions import recent
    if rse not in DISK_RSES:
        raise Http404(f'{rse} is not a production disk RSE')
    signed_in = request.user.is_authenticated
    ops = signed_in and is_ops(get_authority(request.user.username))
    return render(request, 'monitor_app/panda_rse_deletion.html', {
        'rse': rse,
        'tape_rse': TAPE_RSE,
        'recent': recent(rse),
        'is_ops': ops,
        'can_act': ops and not is_tunnel_request(request),
        'active_nav': {'panda_storage_doors': True},
    })
