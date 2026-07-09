"""AI app pages: the AI proposal list."""
from django.db.models import Count
from django.shortcuts import render

from .models import Proposal


def ai_proposals(request):
    """The AI proposal list (EPICPROD_PROPOSALS.md).

    Pending proposals for review, decided history, and per-proposer track
    records. Read-open — visible-but-inert; decisions require sign-in and
    act through the same proposal-decide service as the catalog and
    compose surfaces.
    """
    status_filter = (request.GET.get('status') or 'all').strip()
    action_filter = (request.GET.get('action') or '').strip()
    proposer_filter = (request.GET.get('proposer') or '').strip()
    batch_filter = (request.GET.get('batch') or '').strip()

    qs = Proposal.objects.all()
    if status_filter and status_filter != 'all':
        qs = qs.filter(status=status_filter)
    if action_filter:
        qs = qs.filter(action=action_filter)
    if proposer_filter:
        qs = qs.filter(proposer=proposer_filter)
    if batch_filter:
        qs = qs.filter(batch_id=batch_filter)

    total_count = qs.count()
    rows = list(qs.order_by('-created_at')[:500])

    status_counts = dict(
        Proposal.objects.values_list('status').annotate(Count('id')))
    status_order = ['proposed', 'executed', 'denied', 'withdrawn', 'stale',
                    'approved_pending_execution']
    status_facets = [
        {'status': s, 'count': status_counts.get(s, 0)}
        for s in status_order if status_counts.get(s, 0) or s == 'proposed'
    ]

    proposer_stats = []
    for proposer in (Proposal.objects.exclude(proposer='').order_by()
                     .values_list('proposer', flat=True).distinct()):
        base = Proposal.objects.filter(proposer=proposer)
        proposer_stats.append({
            'proposer': proposer,
            'total': base.count(),
            'pending': base.filter(status='proposed').count(),
            'executed': base.filter(status='executed').count(),
            'denied': base.filter(status='denied').count(),
            'wrong': base.filter(quality='wrong').count(),
        })

    return render(request, 'ai/proposals.html', {
        'rows': rows,
        'total_count': total_count,
        'shown_count': len(rows),
        'status_filter': status_filter,
        'action_filter': action_filter,
        'proposer_filter': proposer_filter,
        'batch_filter': batch_filter,
        'status_facets': status_facets,
        'proposer_stats': proposer_stats,
    })
