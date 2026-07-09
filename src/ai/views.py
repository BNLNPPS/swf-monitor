"""AI app pages: the AI proposal list."""
from django.db.models import Count
from django.shortcuts import render

from .models import Proposal


def ai_proposals(request):
    """The AI proposal list (AI_PROPOSALS.md).

    Pending proposals for review, decision history, and per-proposer track
    records. Read-open — visible-but-inert; decisions require sign-in and
    act through the same proposal-decide service as the catalog and
    compose surfaces.
    """
    def url_with(**updates):
        params = request.GET.copy()
        for key, value in updates.items():
            if value:
                params[key] = value
            else:
                params.pop(key, None)
        encoded = params.urlencode()
        return f'{request.path}?{encoded}' if encoded else request.path

    filters = {key: (request.GET.get(key) or '').strip()
               for key in ('status', 'action', 'change', 'decision',
                           'quality', 'proposer', 'batch')}
    status_filter = filters['status'] or 'all'

    qs = Proposal.objects.all()
    if status_filter != 'all':
        qs = qs.filter(status=status_filter)
    if filters['action']:
        qs = qs.filter(action=filters['action'])
    if filters['change'] and ':' in filters['change']:
        prev_state, _, new_state = filters['change'].partition(':')
        qs = qs.filter(precondition__prev_state=prev_state,
                       payload__state=new_state)
    if filters['decision']:
        qs = qs.filter(decided_by=filters['decision'])
    if filters['quality']:
        qs = qs.filter(quality=filters['quality'])
    if filters['proposer']:
        qs = qs.filter(proposer=filters['proposer'])
    if filters['batch']:
        qs = qs.filter(batch_id=filters['batch'])

    total_count = qs.count()
    rows = list(qs.order_by('-created_at')[:500])

    # Facet rows: per-dimension global counts, links preserving the other
    # active filters; every dimension leads with All (the default).
    everything = Proposal.objects.all()

    def facet_row(title, param, pairs, label_of=str):
        items = [{
            'label': label_of(value), 'count': count,
            'url': url_with(**{param: value}),
            'active': filters[param] == value,
        } for value, count in pairs if value]
        return {'title': title, 'items': items,
                'all_url': url_with(**{param: ''}),
                'all_active': not filters[param]}

    status_counts = dict(everything.values_list('status').annotate(Count('id')))
    status_order = ['proposed', 'executed', 'undone', 'denied', 'withdrawn',
                    'stale', 'approved_pending_execution']
    status_row = {
        'title': 'Status',
        'items': [{'label': s, 'count': status_counts.get(s, 0),
                   'url': url_with(status=s), 'active': status_filter == s}
                  for s in status_order
                  if status_counts.get(s, 0) or s == 'proposed'],
        'all_url': url_with(status=''),
        'all_active': status_filter == 'all',
    }

    action_labels = {'propagation': 'campaign propagation'}
    facet_rows = [
        status_row,
        facet_row('Action', 'action',
                  everything.values_list('action').annotate(Count('id'))
                  .order_by('action'),
                  lambda a: action_labels.get(a, a)),
        facet_row('Change', 'change', [
            (f'{prev}:{new}', count)
            for prev, new, count in everything
            .values_list('precondition__prev_state', 'payload__state')
            .annotate(Count('id')).order_by()
            if prev and new],
            lambda v: v.replace(':', ' → ')),
        facet_row('Decision', 'decision',
                  everything.exclude(decided_by='')
                  .values_list('decided_by').annotate(Count('id'))
                  .order_by('decided_by')),
        facet_row('Quality', 'quality',
                  everything.exclude(quality='')
                  .values_list('quality').annotate(Count('id'))
                  .order_by('quality')),
        facet_row('Proposer', 'proposer',
                  everything.exclude(proposer='')
                  .values_list('proposer').annotate(Count('id'))
                  .order_by('proposer')),
        facet_row('Batch', 'batch',
                  everything.exclude(batch_id='')
                  .values_list('batch_id').annotate(Count('id'))
                  .order_by('batch_id')),
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
            'undone': base.filter(status='undone').count(),
            'denied': base.filter(status='denied').count(),
            'wrong': base.filter(quality='wrong').count(),
        })

    return render(request, 'ai/proposals.html', {
        'rows': rows,
        'total_count': total_count,
        'shown_count': len(rows),
        'facet_rows': facet_rows,
        'proposer_stats': proposer_stats,
    })


def _narrative_entry_from_page(page, client, *, with_versions=False,
                                with_comments=True):
    """Shared shaping of a corun narrative page for templates."""
    from .assessments import render_assessment_markdown
    from .corun_client import CorunAPIError

    def _strip_h1(text):
        # The page header carries the title; drop the document's own
        # leading H1 from the rendered body to avoid repeating it. The
        # stored document keeps its H1 (it stands alone).
        stripped = text.lstrip()
        if stripped.startswith('# '):
            return stripped.split('\n', 1)[1] if '\n' in stripped else ''
        return text

    data = page.get('data') or {}
    content = page.get('content') or ''
    group_id = page.get('group_id') or page.get('id')
    entry = {
        'name': data.get('name', ''),
        'title': page.get('title') or data.get('name', ''),
        'version': page.get('version'),
        'updated': (page.get('modified_at') or page.get('created_at') or ''),
        'content': content,
        'html': render_assessment_markdown(_strip_h1(content)),
        'group_id': group_id,
        'versions': [],
        'comments': [],
    }
    if with_versions:
        try:
            for v in sorted(client.list_versions(group_id) or [],
                            key=lambda x: x.get('version', 0), reverse=True):
                v_content = v.get('content') or ''
                entry['versions'].append({
                    'version': v.get('version'),
                    'date': (v.get('created_at') or '')[:16].replace('T', ' '),
                    'author': (v.get('data') or {}).get('author', ''),
                    'lines': len(v_content.splitlines()),
                    'is_current': bool(v.get('is_current')),
                    'html': render_assessment_markdown(_strip_h1(v_content)),
                })
        except CorunAPIError:
            entry['versions'] = []
    if with_comments:
        try:
            for c in client.list_comments(group_id) or []:
                # The signed-in swf-monitor user is stamped in data.author
                # at post time; corun's own author field is the API token's
                # account, not the human.
                stamped = (c.get('data') or {}).get('author', '')
                fallback = c.get('author')
                if isinstance(fallback, dict):
                    fallback = fallback.get('username', '')
                entry['comments'].append({
                    'author': stamped or fallback or '',
                    'date': (c.get('created_at') or '')[:16].replace('T', ' '),
                    'content': c.get('content') or '',
                })
        except CorunAPIError:
            entry['comments'] = []
    return entry


def _narrative_pages(client):
    payload = client.list_pages(
        section='epicprod.narrative',
        artifact_type='campaign_narrative',
        limit=100,
    )
    if isinstance(payload, dict):
        return payload.get('results') or payload.get('items') or []
    return payload or []


def ai_narratives(request):
    """The Campaign Narratives list (EPICPROD_NARRATIVES.md).

    Collapsible read view with comments; document management (editing,
    version history) lives on the per-document detail page. Read-open; a
    corun-ai failure renders as an error message, never an empty page.
    """
    from .corun_client import CorunAPIError, CorunClient, corun_configured

    entries, error = [], ''
    if not corun_configured():
        error = 'corun-ai is not configured on this deployment.'
    else:
        try:
            client = CorunClient()
            entries = [
                _narrative_entry_from_page(p, client, with_comments=True)
                for p in _narrative_pages(client)
            ]
        except CorunAPIError as exc:
            error = f'corun-ai retrieval failed: {exc}'
    # General items (standing context) precede campaign-specific ones;
    # newest first within each block. The series identity is data.name.
    general_entries = sorted(
        (e for e in entries if e['name'].startswith('campaign_general')),
        key=lambda e: e['updated'], reverse=True)
    campaign_entries = sorted(
        (e for e in entries if not e['name'].startswith('campaign_general')),
        key=lambda e: e['updated'], reverse=True)
    return render(request, 'ai/narratives.html',
                  {'general_entries': general_entries,
                   'campaign_entries': campaign_entries,
                   'entries': entries, 'error': error})


def ai_narrative_detail(request, name):
    """One narrative document: content, expert editing, the version
    history (tjai-style, at the bottom), and comments."""
    from django.http import Http404

    from .corun_client import CorunAPIError, CorunClient, corun_configured

    if not corun_configured():
        raise Http404('corun-ai is not configured')
    try:
        client = CorunClient()
        page = next((p for p in _narrative_pages(client)
                     if (p.get('data') or {}).get('name') == name), None)
    except CorunAPIError as exc:
        return render(request, 'ai/narrative_detail.html',
                      {'entry': None, 'error': f'corun-ai retrieval failed: {exc}'})
    if page is None:
        raise Http404(f'No narrative named {name!r}')
    entry = _narrative_entry_from_page(page, client, with_versions=True,
                                       with_comments=True)
    return render(request, 'ai/narrative_detail.html',
                  {'entry': entry, 'error': ''})
