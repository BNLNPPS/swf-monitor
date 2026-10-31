"""What's new: developments as versioned entry-store items (docs/WHATS_NEW.md).

Every write to the What's new entries goes through this module: the pages,
the REST API, the MCP tools and the AI proposal executor call it, and
nothing else writes them. Items are ``Entry`` rows of kind ``development``
in the ``whats-new`` context; comments are ``comment`` entries whose parent
is the item. The entry versioning signal (monitor_app/signals.py) keeps
each substantive edit; votes are an operational key and are not versioned.
"""
import re
import time
from datetime import date

from django.db import transaction

from monitor_app.models import Entry, EntryContext, EntryVersion
from monitor_app.signals import set_changed_by

CONTEXT = 'whats-new'
KIND_ITEM = 'development'
KIND_COMMENT = 'comment'
IMPORTANCE = ('urgent', 'high', 'routine', 'minor')
IMPORTANCE_LABELS = {
    'urgent': 'Urgent', 'high': 'High', 'routine': 'Routine', 'minor': 'Minor',
}
SUMMARY_MAX = 600
_TAG_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
# Item fields an edit or a proposal may set.
FIELDS = ('title', 'summary', 'details', 'refs', 'tags', 'importance',
          'shipped', 'contact', 'builds_on')


class WhatsNewError(Exception):
    """A refused write, with an HTTP-shaped status and a reader's message."""

    def __init__(self, detail, status=400):
        self.detail = detail
        self.status = status
        super().__init__(detail)


def _today():
    from monitor_app.alarms_data import _today_eastern
    return _today_eastern()


def _ensure_context():
    EntryContext.objects.get_or_create(
        name=CONTEXT,
        defaults={'title': "What's new",
                  'description': 'Developments in SWF that users, operators '
                                 'and developers should know about '
                                 '(docs/WHATS_NEW.md).'})


def _items():
    return Entry.objects.filter(context_id=CONTEXT, kind=KIND_ITEM,
                                deleted_at__isnull=True)


def slug_for(title):
    """The URL slug a title gives: lowercase words joined by hyphens."""
    slug = re.sub(r'[^a-z0-9]+', '-', (title or '').strip().lower()).strip('-')
    return slug[:80].rstrip('-') or 'item'


def get_entry(slug, *, include_archived=True):
    """The item entry with this slug, or None."""
    qs = _items().filter(data__entry_id=slug)
    if not include_archived:
        qs = qs.filter(archived=False)
    return qs.first()


def _require(slug):
    entry = get_entry(slug)
    if entry is None:
        raise WhatsNewError(f'no What\'s new item {slug!r}', status=404)
    return entry


def normalize_tags(raw):
    """Tags as a sorted list of lowercase hyphenated words; an importance
    level given as a tag is not a tag. Raises on a malformed tag."""
    if isinstance(raw, str):
        raw = re.split(r'[,\s]+', raw)
    tags = set()
    for t in raw or []:
        tag = re.sub(r'[\s_]+', '-', str(t).strip().lower()).strip('-')
        if not tag or tag in IMPORTANCE:
            continue
        if not _TAG_RE.match(tag) or len(tag) > 40:
            raise WhatsNewError(
                f'tag {t!r}: a tag is lowercase letters, digits and hyphens')
        tags.add(tag)
    return sorted(tags)


def contacts():
    """The accounts an item's contact may name: people known here who hold
    the operations role (docs/AUTHORITY.md), sorted."""
    from monitor_app.authority import all_authority, is_ops, may_act
    return sorted(u for u, record in all_authority().items()
                  if is_ops(record) and may_act(record))


def _validate(fields, *, current=None, changed_by=''):
    """The item's data after applying ``fields`` to ``current`` (None for
    a new item). Every rule an item obeys is checked here."""
    cur = dict(current or {})
    out = {}
    for key in FIELDS:
        if key in fields and fields[key] is not None:
            out[key] = fields[key]
        else:
            out[key] = cur.get(key)

    title = ' '.join(str(out['title'] or '').split())
    if not title:
        raise WhatsNewError('a title is required')
    if len(title) > 255:
        raise WhatsNewError('the title is one line, at most 255 characters')
    summary = str(out['summary'] or '').strip()
    if not summary:
        raise WhatsNewError('a summary of one to three lines is required')
    if len(summary) > SUMMARY_MAX or summary.count('\n') > 2:
        raise WhatsNewError(
            f'the summary is one to three lines, at most {SUMMARY_MAX} '
            f'characters; longer text belongs in the details')
    tags = normalize_tags(out['tags'])
    if not tags:
        raise WhatsNewError('at least one tag is required (e.g. users, ops, '
                            'dev, or the area of the system)')
    importance = str(out['importance'] or '').strip().lower()
    if importance not in IMPORTANCE:
        raise WhatsNewError(
            f'importance must be one of {", ".join(IMPORTANCE)}')
    shipped_raw = str(out['shipped'] or '').strip()
    if shipped_raw:
        try:
            shipped = date.fromisoformat(shipped_raw[:10]).isoformat()
        except ValueError:
            raise WhatsNewError(
                f'shipped must be a date (YYYY-MM-DD); got {shipped_raw!r}')
    else:
        shipped = _today().isoformat()
    contact = str(out['contact'] or changed_by or '').strip()
    if not contact:
        raise WhatsNewError('a contact (the principal developer) is required')
    if contact not in contacts():
        raise WhatsNewError(
            f'contact {contact!r}: the contact is an account with the operations '
            f'role, one of {", ".join(contacts())}')
    builds_on = out['builds_on'] or []
    if isinstance(builds_on, str):
        builds_on = re.split(r'[,\s]+', builds_on)
    links = []
    for raw in builds_on:
        slug = str(raw).strip().rstrip('/').rsplit('/', 1)[-1]
        if not slug or slug in links:
            continue
        if get_entry(slug) is None:
            raise WhatsNewError(f'builds on {slug!r}: no such item')
        links.append(slug)
    return {
        'title': title, 'summary': summary,
        'details': str(out['details'] or '').strip(),
        'refs': str(out['refs'] or '').strip(),
        'tags': tags, 'importance': importance, 'shipped': shipped,
        'contact': contact, 'builds_on': links,
    }


def validate_new(fields, *, changed_by=''):
    """Check a new item's fields without writing; returns the normalized
    fields with the slug the item would take. Used by the proposal path."""
    clean = _validate(fields, changed_by=changed_by)
    clean['slug'] = slug_for(clean['title'])
    return clean


def _write(entry, clean):
    entry.title = clean['title']
    entry.content = clean['details']
    data = dict(entry.data or {})
    for key in ('summary', 'refs', 'tags', 'importance', 'shipped',
                'contact', 'builds_on'):
        data[key] = clean[key]
    entry.data = data


def item_create(fields, *, changed_by, origin=None):
    """Create an item: the call the New item form, the API, and an approved
    AI proposal make. Logs one origin-stamped ``whats_new_create`` event,
    which epicprod-live posts and the live screen shows at INFO (Torre
    Wenaus, 2026-10-30). Returns (entry, action log id)."""
    from monitor_app.alarms_data import _origin_attrs
    from monitor_app.epicprod_logging import log_epicprod_action

    if not changed_by:
        raise WhatsNewError('an authenticated person is required', status=403)
    clean = _validate(fields, changed_by=changed_by)
    slug = slug_for(clean['title'])
    attrs = _origin_attrs(origin)
    with transaction.atomic():
        _ensure_context()
        if get_entry(slug) is not None:
            raise WhatsNewError(
                f'an item with this title already exists ({slug}); edit it '
                f'or choose another title', status=409)
        now = time.time()
        entry = Entry(kind=KIND_ITEM, context_id=CONTEXT, name=slug,
                      status='active', timestamp_created=now,
                      timestamp_modified=now,
                      data={'entry_id': slug, 'votes': {},
                            'created_by': changed_by,
                            'origin': attrs.get('proposal_ref') or 'manual'})
        _write(entry, clean)
        entry.save()
    log_id = log_epicprod_action(
        'web', 'whats_new_create', subject_type='whats_new', subject_key=slug,
        subject_label=entry.title, username=changed_by,
        sublevel='normal', live_default=True,
        title=(entry.title if entry.title.lower().startswith("what's new")
               else f"What's new: {entry.title}"),
        summary=clean['summary'],
        importance=clean['importance'],
        url=f'/help/whats-new/{slug}/', **attrs)
    return entry, log_id


def item_edit(slug, fields, *, changed_by):
    """Apply an edit to any of an item's fields; the previous state is kept
    as a version attributed to ``changed_by``. The slug never changes."""
    if not changed_by:
        raise WhatsNewError('an authenticated person is required', status=403)
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise WhatsNewError(f'unknown fields: {", ".join(sorted(unknown))}')
    with transaction.atomic():
        entry = Entry.objects.select_for_update().get(pk=_require(slug).pk)
        current = dict(entry.data or {}, title=entry.title,
                       details=entry.content)
        clean = _validate(fields, current=current, changed_by=changed_by)
        if slug in clean['builds_on']:
            raise WhatsNewError('an item cannot build on itself')
        _write(entry, clean)
        entry.timestamp_modified = time.time()
        set_changed_by(changed_by)
        entry.save()
    return entry


def item_restore(slug, version_num, *, changed_by):
    """Restore a version's title, details and fields as the current state;
    the state it replaces becomes a version in turn."""
    if not changed_by:
        raise WhatsNewError('an authenticated person is required', status=403)
    entry = _require(slug)
    version = EntryVersion.objects.filter(entry=entry,
                                          version_num=version_num).first()
    if version is None:
        raise WhatsNewError(f'{slug} has no version {version_num}', status=404)
    old = version.data or {}
    fields = {k: old.get(k) for k in FIELDS if k not in ('title', 'details')}
    fields.update(title=version.title, details=version.content)
    return item_edit(slug, fields, changed_by=changed_by)


def item_archive(slug, *, changed_by, archived=True):
    """Take an item off the list (or return it). Logged on the action
    stream, since the archive flag is not part of an item's versions."""
    from monitor_app.epicprod_logging import log_epicprod_action

    if not changed_by:
        raise WhatsNewError('an authenticated person is required', status=403)
    entry = _require(slug)
    entry.archived = bool(archived)
    entry.save(update_fields=['archived'])
    log_epicprod_action(
        'web', 'whats_new_archive', subject_type='whats_new',
        subject_key=slug, subject_label=entry.title, username=changed_by,
        sublevel='normal', live_default=False, archived=entry.archived,
        url=f'/help/whats-new/{slug}/')
    return entry


def vote(slug, *, username, value):
    """Set the person's vote on an item: +1, -1, or 0 to withdraw.
    Returns (up votes, down votes, the person's vote)."""
    if not username:
        raise WhatsNewError('an authenticated person is required', status=403)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = None
    if value not in (-1, 0, 1):
        raise WhatsNewError('a vote is 1, -1, or 0 to withdraw')
    with transaction.atomic():
        entry = Entry.objects.select_for_update().get(pk=_require(slug).pk)
        data = dict(entry.data or {})
        votes = dict(data.get('votes') or {})
        if value:
            votes[username] = value
        else:
            votes.pop(username, None)
        data['votes'] = votes
        entry.data = data
        entry.save(update_fields=['data'])
    up, down = _tally(votes)
    return up, down, votes.get(username, 0)


def _tally(votes):
    """(up votes, down votes); there is no combined score."""
    values = list(votes.values())
    return values.count(1), values.count(-1)


def comment_add(slug, text, *, author):
    """Add a comment to an item's thread."""
    if not author:
        raise WhatsNewError('an authenticated person is required', status=403)
    text = (text or '').strip()
    if not text:
        raise WhatsNewError('the comment is empty')
    item = _require(slug)
    now = time.time()
    return Entry.objects.create(
        kind=KIND_COMMENT, context_id=CONTEXT, parent=item,
        content=text, data={'author': author},
        timestamp_created=now, timestamp_modified=now)


def comment_edit(comment_id, text, *, editor):
    """Edit a comment; only its author may. The previous text is kept as
    a version."""
    text = (text or '').strip()
    if not text:
        raise WhatsNewError('the comment is empty')
    comment = Entry.objects.filter(
        pk=comment_id, context_id=CONTEXT, kind=KIND_COMMENT,
        deleted_at__isnull=True).first()
    if comment is None:
        raise WhatsNewError('no such comment', status=404)
    if (comment.data or {}).get('author') != editor:
        raise WhatsNewError('a comment is edited by its author', status=403)
    comment.content = text
    comment.timestamp_modified = time.time()
    set_changed_by(editor)
    comment.save()
    return comment


# ---------------------------------------------------------------- reading

def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ''


def _comment_threads(item_ids):
    """Comments per item id, oldest first."""
    threads = {}
    for c in (Entry.objects
              .filter(context_id=CONTEXT, kind=KIND_COMMENT,
                      parent_id__in=item_ids, deleted_at__isnull=True)
              .order_by('timestamp_created')):
        threads.setdefault(c.parent_id, []).append(c)
    return threads


def _row(entry, comments, developed_in, username=''):
    data = entry.data or {}
    votes = data.get('votes') or {}
    last = comments[-1] if comments else None
    last_author = (last.data or {}).get('author', '') if last else ''
    contact = data.get('contact', '')
    return {
        'slug': data.get('entry_id', ''),
        'title': entry.title,
        'summary': data.get('summary', ''),
        'details': entry.content or '',
        'refs': data.get('refs', ''),
        'tags': list(data.get('tags') or []),
        'importance': data.get('importance', ''),
        'shipped': data.get('shipped', ''),
        'contact': contact,
        'builds_on': list(data.get('builds_on') or []),
        'developed_in': developed_in,
        'up': _tally(votes)[0],
        'down': _tally(votes)[1],
        'my_vote': votes.get(username, 0) if username else 0,
        'comment_count': len(comments),
        'awaiting_reply': bool(last and last_author and last_author != contact),
        'archived': entry.archived,
        'created': _iso(entry.timestamp_created),
        'modified': _iso(entry.timestamp_modified),
        'created_by': data.get('created_by', ''),
        'origin': data.get('origin', 'manual'),
    }


def list_items(*, username='', include_archived=False):
    """Every item as a dict, newest shipped first, with comment counts,
    up and down votes, and the links in both directions."""
    qs = _items()
    if not include_archived:
        qs = qs.filter(archived=False)
    entries = list(qs)
    threads = _comment_threads([e.pk for e in entries])
    titles = {(e.data or {}).get('entry_id'): e.title for e in entries}
    developed = {}
    for e in entries:
        for earlier in (e.data or {}).get('builds_on') or []:
            developed.setdefault(earlier, []).append(
                {'slug': (e.data or {}).get('entry_id'), 'title': e.title})
    rows = []
    for e in entries:
        slug = (e.data or {}).get('entry_id')
        row = _row(e, threads.get(e.pk, []), developed.get(slug, []), username)
        row['builds_on_items'] = [{'slug': s, 'title': titles.get(s, s)}
                                  for s in row['builds_on']]
        rows.append(row)
    rows.sort(key=lambda r: (r['shipped'], r['created']), reverse=True)
    return rows


def get_item(slug, *, username=''):
    """One item in full: fields, links both ways, comments and versions."""
    entry = _require(slug)
    comments = _comment_threads([entry.pk]).get(entry.pk, [])
    later = [{'slug': (e.data or {}).get('entry_id'), 'title': e.title}
             for e in _items().filter(archived=False)
             if slug in ((e.data or {}).get('builds_on') or [])]
    row = _row(entry, comments, later, username)
    row['builds_on_items'] = []
    for s in row['builds_on']:
        earlier = get_entry(s)
        row['builds_on_items'].append(
            {'slug': s, 'title': earlier.title if earlier else s})
    row['comments'] = [{
        'id': c.pk, 'author': (c.data or {}).get('author', ''),
        'text': c.content, 'created': _iso(c.timestamp_created),
        'edited': c.timestamp_modified - c.timestamp_created > 1,
    } for c in comments]
    row['versions'] = [{
        'num': v.version_num, 'by': v.changed_by, 'at': _iso(v.timestamp),
        'title': v.title,
    } for v in EntryVersion.objects.filter(entry=entry).order_by('-version_num')]
    return row


def get_version(slug, version_num):
    """A version of an item as it stood, in the item's field layout."""
    entry = _require(slug)
    v = EntryVersion.objects.filter(entry=entry, version_num=version_num).first()
    if v is None:
        raise WhatsNewError(f'{slug} has no version {version_num}', status=404)
    data = v.data or {}
    return {
        'slug': slug, 'num': v.version_num, 'by': v.changed_by,
        'at': _iso(v.timestamp), 'title': v.title, 'details': v.content,
        'summary': data.get('summary', ''), 'refs': data.get('refs', ''),
        'tags': list(data.get('tags') or []),
        'importance': data.get('importance', ''),
        'shipped': data.get('shipped', ''), 'contact': data.get('contact', ''),
        'builds_on': list(data.get('builds_on') or []),
    }


def tags_in_use():
    """{tag: number of items carrying it}, the vocabulary in use."""
    counts = {}
    for e in _items().filter(archived=False):
        for t in (e.data or {}).get('tags') or []:
            counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
