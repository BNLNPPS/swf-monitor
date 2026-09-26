"""AI app pages: the AI proposal list."""
from django.db.models import Count, Max
from django.shortcuts import render

from .models import Proposal


# What each kind of proposal asks of the reviewer, where it is best
# reviewed, and how its open rows sort (soonest obligation first for pings).
PROPOSAL_KINDS = {
    'campaign_plan': {'label': 'campaign plan',
                      'asks': 'whether each physics configuration goes into the campaign plan '
                              '(include, defer or retire), with target events and priority'},
    'ping': {'label': 'reminder (ping)',
             'asks': 'whether to enter a dated reminder with an owner'},
    'ping_fulfil': {'label': 'reminder fulfilled',
                    'asks': 'whether an open reminder is met'},
    'standard_config': {'label': 'standard configuration',
                        'asks': "whether to create an edition's Standard Production configuration"},
    'registered_sample': {'label': 'registered sample',
                          'asks': 'whether to take a registered EVGEN sample nobody requested into the campaign'},
    'propagation': {'label': 'campaign propagation',
                    'asks': "whether to set an edition's propagation"},
}


def _review_surface(action, counterpart):
    """(label, url) of the page a kind of proposal is best reviewed on."""
    from django.urls import reverse
    if action in ('ping', 'ping_fulfil', 'standard_config'):
        return 'Pings on the alarm dashboard', reverse('monitor_app:alarms_dashboard') + '#pings'
    if action == 'campaign_plan':
        return (f'the {counterpart} campaign plan',
                reverse('pcs:pcs_campaign_plan') + f'?campaign={counterpart}')
    if action == 'registered_sample':
        return 'EVGEN inputs', reverse('pcs:evgen_inputs')
    if action == 'propagation':
        return 'the task catalog', reverse('pcs:pcs_catalog')
    return '', ''


def ai_proposals(request):
    """The AI proposal list (AI_PROPOSALS.md).

    Opens on the proposals waiting for a decision, summarized by kind with
    what each asks and where it is best reviewed; the History tab holds
    the decided and withdrawn rows. Facet counts are counted within the
    current selection (the house narrowing filter). Read-open;
    decisions require sign-in and act through the same proposal-decide
    service as the catalog and compose surfaces.
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

    params = ('status', 'action', 'change', 'decision', 'quality',
              'proposer', 'batch', 'subject')
    filters = {key: (request.GET.get(key) or '').strip() for key in params}
    tab = 'history' if (request.GET.get('tab') == 'history'
                        or filters['status'] not in ('', 'proposed')) else 'open'

    def select(qs, skip=None):
        """The rows the page's filters select, all but ``skip``."""
        if tab == 'open':
            qs = qs.filter(status='proposed')
        else:
            qs = qs.exclude(status='proposed')
            if filters['status'] and skip != 'status':
                qs = qs.filter(status=filters['status'])
        if filters['subject'] and skip != 'subject':
            qs = qs.filter(subject_key=filters['subject'])
        if filters['action'] and skip != 'action':
            qs = qs.filter(action=filters['action'])
        if filters['change'] and ':' in filters['change'] and skip != 'change':
            prev_state, _, new_state = filters['change'].partition(':')
            qs = qs.filter(precondition__prev_state=prev_state, payload__state=new_state)
        if filters['decision'] and skip != 'decision':
            qs = qs.filter(decided_by=filters['decision'])
        if filters['quality'] and skip != 'quality':
            qs = qs.filter(quality=filters['quality'])
        if filters['proposer'] and skip != 'proposer':
            qs = qs.filter(proposer=filters['proposer'])
        if filters['batch'] and skip != 'batch':
            qs = qs.filter(batch_id=filters['batch'])
        return qs

    everything = Proposal.objects.all()
    qs = select(everything)
    total_count = qs.count()
    if filters['action'] == 'ping' and tab == 'open':
        ordered = qs.order_by('counterpart_key', 'created_at')      # soonest due first
    else:
        ordered = qs.order_by('-created_at')
    rows = list(ordered[:500])
    for p in rows:
        if p.action == 'campaign_plan':
            p.disposition_label = str((p.payload or {}).get('disposition') or '').replace('_', ' ')

    def facet_row(title, param, pairs, label_of=str, keep=0):
        items = [{'label': label_of(value), 'count': count,
                  'url': url_with(**{param: value}), 'active': filters[param] == value}
                 for value, count in pairs if value]
        shown, folded = (items[:keep], items[keep:]) if keep else (items, [])
        if any(i['active'] for i in folded):
            shown, folded = items, []
        return {'title': title, 'items': shown, 'folded': folded,
                'all_url': url_with(**{param: ''}), 'all_active': not filters[param]}

    def counts(param, field, qs_extra=None):
        base = select(everything, skip=param)
        if qs_extra:
            base = qs_extra(base)
        return base.values_list(field).annotate(Count('id')).order_by(field)

    facet_rows = []
    if tab == 'history':
        facet_rows.append(facet_row('Status', 'status', counts('status', 'status')))
    facet_rows += [
        facet_row('Kind', 'action', counts('action', 'action'),
                  lambda a: PROPOSAL_KINDS.get(a, {}).get('label', a)),
        facet_row('Proposer', 'proposer',
                  counts('proposer', 'proposer', lambda q: q.exclude(proposer=''))),
        facet_row('Change', 'change', [
            (f'{prev}:{new}', count)
            for prev, new, count in select(everything, skip='change')
            .values_list('precondition__prev_state', 'payload__state')
            .annotate(Count('id')).order_by() if prev and new],
            lambda v: v.replace(':', ' \u2192 ')),
        facet_row('Decided by', 'decision',
                  counts('decision', 'decided_by', lambda q: q.exclude(decided_by=''))),
        facet_row('Quality', 'quality',
                  counts('quality', 'quality', lambda q: q.exclude(quality=''))),
        facet_row('Batch', 'batch',
                  [(bid, n) for bid, n, _ in select(everything, skip='batch')
                   .exclude(batch_id='').values_list('batch_id')
                   .annotate(n=Count('id'), last=Max('created_at')).order_by('-last')],
                  keep=6),
    ]

    # Waiting for a decision: one line per kind and proposer, what it asks,
    # how many, the soonest due for reminders, where to review it.
    waiting = []
    groups = (everything.filter(status='proposed')
              .values('action', 'proposer').annotate(n=Count('id'))
              .order_by('action', 'proposer'))
    for g in groups:
        kind = PROPOSAL_KINDS.get(g['action'], {'label': g['action'], 'asks': ''})
        open_rows = everything.filter(status='proposed', action=g['action'],
                                      proposer=g['proposer'])
        counterparts = sorted(set(open_rows.exclude(counterpart_key='')
                                  .values_list('counterpart_key', flat=True)))
        soonest = counterparts[0] if g['action'] == 'ping' and counterparts else ''
        campaign = counterparts[0] if g['action'] == 'campaign_plan' and len(counterparts) == 1 else ''
        surface_label, surface_url = _review_surface(g['action'], campaign)
        waiting.append({
            'label': kind['label'], 'asks': kind['asks'], 'count': g['n'],
            'proposer': g['proposer'] or '(session)', 'soonest': soonest,
            'campaign': campaign,
            'list_url': f"{request.path}?action={g['action']}"
                        + (f"&proposer={g['proposer']}" if g['proposer'] else ''),
            'surface_label': surface_label, 'surface_url': surface_url,
        })
    waiting.sort(key=lambda w: (w['soonest'] or '9999', w['label']))

    superseded = 0
    if tab == 'history':
        from .services import superseded_copies
        superseded = len(superseded_copies()[0])

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

    open_count = everything.filter(status='proposed').count()
    return render(request, 'ai/proposals.html', {
        'tab': tab,
        'open_url': request.path,
        'history_url': f'{request.path}?tab=history',
        'open_count': open_count,
        'history_count': everything.count() - open_count,
        'waiting': waiting,
        'superseded': superseded,
        'kinds': PROPOSAL_KINDS,
        'rows': rows,
        'total_count': total_count,
        'shown_count': len(rows),
        'facet_rows': facet_rows,
        'subject_filter': filters['subject'],
        'subject_clear_url': url_with(subject=''),
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
    # General items (standing context) precede campaign-specific ones,
    # newest first. Campaign narratives order by campaign version,
    # newest campaign first — stable reverse-chronological order that an
    # edit to an older campaign's narrative cannot reshuffle. The series
    # identity is data.name.
    import re as _re

    def _campaign_version_key(entry):
        match = _re.fullmatch(r'campaign_(\d+(?:\.\d+)*)', entry['name'])
        if match:
            return tuple(int(p) for p in match.group(1).split('.'))
        return (0,)

    general_entries = sorted(
        (e for e in entries if e['name'].startswith('campaign_general')),
        key=lambda e: e['updated'], reverse=True)
    campaign_entries = sorted(
        (e for e in entries if not e['name'].startswith('campaign_general')),
        key=lambda e: (_campaign_version_key(e), e['updated']), reverse=True)
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
