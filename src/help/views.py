"""Help pages: What's new (docs/WHATS_NEW.md)."""
from django.http import Http404
from django.shortcuts import render

from ai.assessments import render_assessment_markdown
from ai.models import Proposal
from monitor_app.inclusive_filter import Facet, InclusiveFilter

from . import whats_new as wn


def _username(request):
    user = getattr(request, 'user', None)
    return user.username if user and user.is_authenticated else ''


def _render_fields(row):
    """HTML of an item's Markdown fields, sanitized."""
    row['details_html'] = render_assessment_markdown(row.get('details', ''))
    row['refs_html'] = render_assessment_markdown(row.get('refs', ''))
    row['importance_label'] = wn.IMPORTANCE_LABELS.get(row.get('importance'), '')
    row['importance_rank'] = (wn.IMPORTANCE.index(row['importance'])
                              if row.get('importance') in wn.IMPORTANCE
                              else len(wn.IMPORTANCE))
    return row


def _facets(rows):
    tags = sorted({t for r in rows for t in r['tags']})
    return [Facet('tag', 'Tags',
                  lambda r: r['tags'] + ([r['importance']] if r['importance'] else []),
                  display=lambda v: wn.IMPORTANCE_LABELS.get(v, v),
                  order=list(wn.IMPORTANCE) + tags)]


def _proposals():
    out = []
    for p in (Proposal.objects.filter(action='whats_new', status='proposed')
              .order_by('created_at')):
        item = dict(p.payload or {})
        item.setdefault('tags', [])
        item['tags_text'] = ' '.join(item.get('tags') or [])
        item['builds_on_text'] = ' '.join(item.get('builds_on') or [])
        out.append({'id': p.pk, 'ref': p.ref, 'proposer': p.proposer,
                    'comment': p.comment, 'created_at': p.created_at,
                    'item': _render_fields(item)})
    return out


def whats_new(request):
    """The What's new list: newest shipped first, or by score."""
    username = _username(request)
    rows = [_render_fields(r) for r in wn.list_items(username=username)]
    sort = 'score' if request.GET.get('sort') == 'score' else 'date'
    if sort == 'score':
        rows.sort(key=lambda r: (r['score'], r['shipped']), reverse=True)
    facets = _facets(rows)
    flt = InclusiveFilter(request.GET, noun='items')
    flt.annotate(rows, facets)
    return render(request, 'help/whats_new.html', {
        'rows': rows,
        'inclusive_filter': flt.context(rows, facets, request),
        'sort': sort,
        'open_all': request.GET.get('open') == 'all',
        'proposals': _proposals(),
        'username': username,
        'importance_levels': wn.IMPORTANCE,
        'tags_in_use': wn.tags_in_use(),
    })


def whats_new_item(request, slug):
    username = _username(request)
    try:
        item = wn.get_item(slug, username=username)
    except wn.WhatsNewError:
        raise Http404(f"no What's new item {slug}")
    _render_fields(item)
    for c in item['comments']:
        c['html'] = render_assessment_markdown(c['text'])
    item['tags_text'] = ' '.join(item['tags'])
    item['builds_on_text'] = ' '.join(item['builds_on'])
    return render(request, 'help/whats_new_item.html', {
        'item': item, 'username': username,
        'importance_levels': wn.IMPORTANCE,
        'tags_in_use': wn.tags_in_use(),
    })


def whats_new_version(request, slug, version_num):
    try:
        version = wn.get_version(slug, version_num)
        entry = wn.get_entry(slug)
    except wn.WhatsNewError:
        raise Http404(f'no version {version_num} of {slug}')
    _render_fields(version)
    return render(request, 'help/whats_new_version.html', {
        'version': version, 'current_title': entry.title if entry else slug,
        'username': _username(request),
    })


def whats_new_new(request):
    return render(request, 'help/whats_new_new.html', {
        'username': _username(request),
        'importance_levels': wn.IMPORTANCE,
        'tags_in_use': wn.tags_in_use(),
        'today': wn._today().isoformat(),
    })
