"""What's new MCP tools (docs/WHATS_NEW.md): read the list of developments,
propose an item, edit an item. Every write goes through help.whats_new or
the AI proposal services; nothing here writes the entries itself."""
from asgiref.sync import sync_to_async

from monitor_app.mcp import mcp
from monitor_app.mcp.common import (_get_username, _monitor_url,
                                    requires_authority)


def _brief(row):
    return {
        'slug': row['slug'], 'title': row['title'], 'summary': row['summary'],
        'tags': row['tags'], 'importance': row['importance'],
        'shipped': row['shipped'], 'contact': row['contact'],
        'up': row['up'], 'down': row['down'],
        'comment_count': row['comment_count'],
        'awaiting_reply': row['awaiting_reply'],
        'url': _monitor_url(f"/help/whats-new/{row['slug']}/"),
    }


def _words(value):
    import re
    if isinstance(value, (list, tuple)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    return [w.lower() for w in re.split(r'[,\s]+', value or '') if w]


@mcp.tool()
async def whats_new_list(tags: str = '', importance: str = '', since: str = '',
                         search: str = '', limit: int = 20,
                         include_details: bool = False) -> dict:
    """
    What's new in ePIC production, the epicprod monitor, PCS, PanDA
    operations and the streaming testbed: the curated list of recent
    developments users, operators and developers should know about,
    newest first. Use it for "what's new", "what are the recent epicprod
    developments", "what changed lately for operators", "anything new for
    users". Each item has a one-line title and a short summary; show those
    and the item URL, and fetch one item's details with whats_new_get.

    Args:
        tags: Only items carrying every one of these tags, separated by
            commas or spaces, e.g. "ops" or "users pcs". Common tags: users,
            ops, dev, testbed, pcs, panda, ddm, alarms.
        importance: Only these importance levels, e.g. "urgent,high".
            Levels: urgent, high, routine, minor.
        since: Only items shipped on or after this date, YYYY-MM-DD.
        search: Only items whose title, summary or details contain this text.
        limit: Maximum items returned (default 20).
        include_details: Also return each item's details and references.

    Returns:
        items (newest shipped first), total_count, has_more, tags_in_use
        (tag: count, the vocabulary to reuse), and the page URL.
    """
    @sync_to_async
    def run():
        from help import whats_new as wn
        rows = wn.list_items()
        want_tags = _words(tags)
        want_imp = _words(importance)
        text = (search or '').strip().lower()
        out = []
        for r in rows:
            if want_tags and not set(want_tags) <= set(r['tags']):
                continue
            if want_imp and r['importance'] not in want_imp:
                continue
            if since and r['shipped'] < since[:10]:
                continue
            if text and text not in ' '.join(
                    (r['title'], r['summary'], r['details'])).lower():
                continue
            item = _brief(r)
            if include_details:
                item['details'] = r['details']
                item['refs'] = r['refs']
            out.append(item)
        n = max(1, min(int(limit or 20), 200))
        return {'items': out[:n], 'total_count': len(out),
                'has_more': len(out) > n, 'tags_in_use': wn.tags_in_use(),
                'monitor_urls': [{'title': "What's new",
                                  'url': _monitor_url('/help/whats-new/')}]}
    return await run()


@mcp.tool()
async def whats_new_get(slug: str) -> dict:
    """
    One What's new item in full: title, summary, details and references
    (Markdown), tags, importance, shipped date, contact, the items it builds
    on and those that develop it further, the comment thread, and the
    number of earlier versions.

    Args:
        slug: The item's address, the last part of its URL, e.g.
            "help-menu-before-system" (from whats_new_list).

    Returns:
        The item, or an error naming the missing slug.
    """
    @sync_to_async
    def run():
        from help import whats_new as wn
        try:
            item = wn.get_item((slug or '').strip())
        except wn.WhatsNewError as e:
            return {'error': e.detail}
        item['version_count'] = len(item.pop('versions'))
        item['url'] = _monitor_url(f"/help/whats-new/{item['slug']}/")
        return item
    return await run()


@mcp.tool()
@requires_authority
async def whats_new_propose(title: str, summary: str, tags: list[str],
                            importance: str, shipped: str, contact: str,
                            comment: str, proposer: str, details: str = '',
                            refs: str = '', builds_on: list[str] = None) -> dict:
    """
    Propose a What's new item: a development in production that users,
    operators or developers should know about. Nothing is added until a
    person approves the proposal on the What's new page, where any field
    can be changed first. Propose once the change is in production; check
    whats_new_list first so an existing item is not proposed again (edit
    it with whats_new_edit instead).

    Args:
        title: One line: what changed, in the reader's terms, e.g.
            "Help menu in the epicprod menu bar".
        summary: One to three lines: what it does and who it is for.
        tags: Topical tags: the primary audience (users, ops, dev) and the
            area (pcs, panda, testbed, ddm, alarms, ...). Reuse the tags in
            use (whats_new_list returns them).
        importance: urgent (readers must act), high, routine, or minor.
        shipped: The date the change reached production, YYYY-MM-DD.
        contact: The account of the principal developer, an account with
            the operations role.
        comment: Why this is proposed, with the evidence: the commit,
            release notes or document it rests on.
        proposer: The proposing AI's own name, e.g. "claude-swf-testbed".
            Never a human's username.
        details: Optional Markdown: how to use it, what it replaces, limits.
        refs: Markdown: links to pages, documents, pull requests. An item
            on the operation or use of the system links first the section of
            the WFMS documentation (https://epic-wfms-docs.readthedocs.io)
            where its impact is described.
        builds_on: Optional slugs of earlier items this one develops further.

    Returns:
        outcome text, the propose result, and the page URL.
    """
    @sync_to_async
    def run():
        from ai import services
        from pcs.services import ServiceError
        who = (proposer or '').strip()
        if not who:
            return {'success': False,
                    'error': 'proposer (the proposing AI, e.g. '
                             '"claude-swf-testbed") is required'}
        try:
            result = services.propose_whats_new(
                [{'title': title, 'summary': summary, 'tags': tags,
                  'importance': importance, 'shipped': shipped,
                  'contact': contact, 'comment': comment,
                  'details': details or '', 'refs': refs or '',
                  'builds_on': builds_on or []}],
                proposer=who, created_by=f'mcp:{who}')
        except ServiceError as e:
            return {'success': False, 'error': e.detail}
        if result.get('invalid'):
            return {'success': False,
                    'error': f"refused: {'; '.join(result['invalid'])}"}
        if result.get('denied'):
            return {'success': False,
                    'error': 'this item was denied before and is unchanged; '
                             'not re-proposed'}
        outcome = ('an identical proposal is already pending'
                   if result.get('noop') else
                   "proposed; a person approves it on the What's new page")
        return {'success': True, 'outcome': outcome, 'result': result,
                'monitor_urls': [{'title': "What's new",
                                  'url': _monitor_url('/help/whats-new/')}]}
    return await run()


@mcp.tool()
@requires_authority
async def whats_new_edit(slug: str, title: str = None, summary: str = None,
                         details: str = None, refs: str = None,
                         tags: list[str] = None, importance: str = None,
                         shipped: str = None, contact: str = None,
                         builds_on: list[str] = None,
                         username: str = None) -> dict:
    """
    Edit an existing What's new item: any field given replaces the
    current value; fields left out are unchanged. The previous state is
    kept as a version attributed to the editing person.

    Args:
        slug: The item's address (from whats_new_list).
        title, summary, details, refs, tags, importance, shipped, contact,
            builds_on: New values; see whats_new_propose for each field.
        username: The person editing. Through epic-devcloud.org it is the
            signed-in caller and need not be given.

    Returns:
        The item's slug and URL, or the reason the edit was refused.
    """
    @sync_to_async
    def run():
        from help import whats_new as wn
        try:
            who = _get_username(username)
        except RuntimeError as e:
            return {'success': False, 'error': str(e)}
        fields = {k: v for k, v in {
            'title': title, 'summary': summary, 'details': details,
            'refs': refs, 'tags': tags, 'importance': importance,
            'shipped': shipped, 'contact': contact, 'builds_on': builds_on,
        }.items() if v is not None}
        if not fields:
            return {'success': False, 'error': 'no fields to change'}
        try:
            wn.item_edit((slug or '').strip(), fields, changed_by=who)
        except wn.WhatsNewError as e:
            return {'success': False, 'error': e.detail}
        return {'success': True, 'slug': slug,
                'url': _monitor_url(f'/help/whats-new/{slug}/')}
    return await run()
