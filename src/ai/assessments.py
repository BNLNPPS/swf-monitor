"""AI assessment helpers for production objects."""
from datetime import datetime

import bleach
import markdown
from django.db import transaction
from django.utils.safestring import mark_safe

from monitor_app.utils import format_datetime


AI_CONTENT_IDS_KEY = 'ai_content_ids'
CORUN_PAGE_GROUP_IDS_KEY = 'corun_page_group_ids'
CORUN_ASSESSMENT_SECTION = 'epicprod.assessment'
AI_CONTENT_RETRIEVE_TOOL = 'epic_get_ai_content'
AI_CONTENT_QUALITY_KEY = 'quality'
# One shared review vocabulary across AI assessments and AI proposals,
# worst to best (EPICPROD_PROPOSALS.md).
AI_CONTENT_QUALITY_VALUES = ('wrong', 'poor', 'ok', 'good')
AI_CONTENT_COMMENT_KEY = 'comment'
_MARKDOWN_EXTENSIONS = ['extra', 'sane_lists']
_ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    'p', 'pre', 'code', 'br', 'hr',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'ul', 'ol', 'li',
}
_ALLOWED_ATTRIBUTES = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    'a': ['href', 'title', 'rel'],
}


def render_assessment_markdown(text):
    """Render assessment Markdown to sanitized HTML."""
    html = markdown.markdown(
        text or '',
        extensions=_MARKDOWN_EXTENSIONS,
        output_format='html5',
    )
    clean = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=['http', 'https', 'mailto'],
        strip=True,
    )
    return mark_safe(clean)


def ai_content_ids(data):
    """Return ordered AIContent ids from a model JSON field."""
    if not isinstance(data, dict):
        return []
    ids = []
    for raw in data.get(AI_CONTENT_IDS_KEY) or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    return ids


def corun_page_group_ids(data):
    """Return ordered corun Page group UUID strings from a model JSON field."""
    if not isinstance(data, dict):
        return []
    ids = []
    for raw in data.get(CORUN_PAGE_GROUP_IDS_KEY) or []:
        value = str(raw or '').strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def _display_time(value):
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, str):
        try:
            return format_datetime(datetime.fromisoformat(value))
        except ValueError:
            return value
    return value or ''


def _panda_task_parts(subject_key, subject_label):
    display_key = subject_key
    display_label = subject_label
    if subject_key and not str(subject_key).isdigit():
        try:
            from pcs.models import PandaTasks
            row = (
                PandaTasks.objects
                .filter(task_name__in=[subject_key, subject_label])
                .order_by('-jedi_task_id')
                .first()
            )
            if row:
                display_key = str(row.jedi_task_id or subject_key)
                display_label = row.task_name or subject_label
        except Exception:
            pass
    return str(display_key or '').strip(), str(display_label or '').strip()


def _subject_parts(subject_type, subject_key, subject_label):
    subject_kind = subject_type
    subject_id = subject_key
    subject_name = subject_label
    subject_display = subject_label or subject_key
    if subject_type == 'panda_task' and subject_key:
        subject_kind = 'PanDA task'
        subject_id, subject_name = _panda_task_parts(subject_key, subject_label)
        subject_display = f'{subject_kind} {subject_id}'
        if subject_name and subject_name != subject_id:
            subject_display = f'{subject_display}: {subject_name}'
    elif subject_type == 'panda_job' and subject_key:
        subject_kind = 'PanDA job'
        subject_id = subject_key
        subject_name = subject_label
        subject_display = f'PanDA job {subject_key}'
        if subject_label and subject_key not in subject_label:
            subject_display = f'{subject_display}: {subject_label}'
    elif subject_type == 'campaign_task':
        subject_kind = 'Campaign task'
        subject_id = subject_key
        subject_name = subject_label
    elif subject_type == 'panda_queue':
        subject_kind = 'PanDA queue'
        subject_id = subject_key
        subject_name = subject_label
    return subject_kind, subject_id, subject_name, subject_display


def _assessment_item(*, item_id, storage, username, ai, assessment, created_at,
                     subject_type, subject_key, subject_label, subject_url,
                     quality='', comment='', corun_page_group_id=''):
    subject_kind, subject_id, subject_name, subject_display = _subject_parts(
        subject_type, subject_key, subject_label)
    return {
        'id': item_id,
        'storage': storage,
        'corun_page_group_id': corun_page_group_id,
        'username': username,
        'ai': ai,
        'quality': quality if quality in AI_CONTENT_QUALITY_VALUES else '',
        'comment': comment,
        'assessment': assessment,
        'assessment_html': render_assessment_markdown(assessment),
        'created_at': created_at,
        'created_display': _display_time(created_at),
        'subject_type': subject_type,
        'subject_key': subject_key,
        'subject_label': subject_label,
        'subject_kind': subject_kind,
        'subject_id': subject_id,
        'subject_name': subject_name,
        'subject_display': subject_display,
        'subject_url': subject_url,
    }


def ai_content_items(rows):
    """Normalize legacy AIContent rows for display."""
    items = []
    for row in rows:
        assessment = str(getattr(row, 'assessment', '') or '').strip()
        if not assessment:
            continue
        subject_type = str(getattr(row, 'subject_type', '') or '').strip()
        subject_key = str(getattr(row, 'subject_key', '') or '').strip()
        subject_label = str(getattr(row, 'subject_label', '') or '').strip()
        created_at = getattr(row, 'created_at', '')
        data = getattr(row, 'data', None) or {}
        quality = str(data.get(AI_CONTENT_QUALITY_KEY) or '').strip()
        comment = str(data.get(AI_CONTENT_COMMENT_KEY) or '').strip()
        items.append(_assessment_item(
            item_id=row.pk,
            storage='legacy',
            username=str(getattr(row, 'username', '') or '').strip(),
            ai=str(getattr(row, 'ai', '') or '').strip(),
            quality=quality,
            comment=comment,
            assessment=assessment,
            created_at=created_at,
            subject_type=subject_type,
            subject_key=subject_key,
            subject_label=subject_label,
            subject_url=str(getattr(row, 'subject_url', '') or '').strip(),
        ))
    return items


def corun_page_items(pages):
    """Normalize corun Page API payloads for display."""
    items = []
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        data = page.get('data') if isinstance(page.get('data'), dict) else {}
        assessment = str(page.get('content') or '').strip()
        if not assessment:
            continue
        items.append(_assessment_item(
            item_id='',
            storage='corun',
            corun_page_group_id=str(page.get('group_id') or ''),
            username=str(
                data.get('created_by_user')
                or data.get('username')
                or data.get('submitted_by')
                or ''
            ).strip(),
            ai=str(data.get('ai') or data.get('model') or '').strip(),
            quality=str(data.get(AI_CONTENT_QUALITY_KEY) or '').strip(),
            comment=str(data.get(AI_CONTENT_COMMENT_KEY) or '').strip(),
            assessment=assessment,
            created_at=str(page.get('created_at') or ''),
            subject_type=str(data.get('subject_type') or '').strip(),
            subject_key=str(data.get('subject_key') or '').strip(),
            subject_label=str(data.get('subject_label') or '').strip(),
            subject_url=str(data.get('subject_url') or '').strip(),
        ))
    return items


def ai_content_summary(data):
    """JSON-serializable AIContent summaries for client-rendered pages."""
    out = []
    for item in ai_content_items(ai_content_for_json(data)) + corun_assessment_items_for_json(data):
        out.append({
            'id': item['id'],
            'storage': item['storage'],
            'corun_page_group_id': item['corun_page_group_id'],
            'username': item['username'],
            'ai': item['ai'],
            'quality': item['quality'],
            'comment': item['comment'],
            'assessment': item['assessment'],
            'created_at': item['created_display'] or str(item['created_at'] or ''),
            'subject_type': item['subject_type'],
            'subject_key': item['subject_key'],
            'subject_label': item['subject_label'],
            'subject_kind': item['subject_kind'],
            'subject_id': item['subject_id'],
            'subject_name': item['subject_name'],
            'subject_display': item['subject_display'],
            'subject_url': item['subject_url'],
        })
    return out


def ai_content_retrieval_guidance(data):
    """Return MCP retrieval guidance for assessment pointers in a JSON field."""
    ids = ai_content_ids(data)
    page_group_ids = corun_page_group_ids(data)
    args = {}
    if ids:
        args['ids'] = ids
    if page_group_ids:
        args['corun_page_group_ids'] = page_group_ids
    return {
        'available': bool(ids or page_group_ids),
        'count': len(ids) + len(page_group_ids),
        'ids': ids,
        'corun_page_group_ids': page_group_ids,
        'retrieval': {
            'tool': AI_CONTENT_RETRIEVE_TOOL,
            'arguments': args,
        } if args else None,
    }


def ai_content_for_json(data):
    """Fetch AIContent rows pointed to by a model JSON field."""
    ids = ai_content_ids(data)
    if not ids:
        return []
    from monitor_app.models import AIContent
    return list(
        AIContent.objects
        .filter(pk__in=ids)
        .order_by('-created_at', '-id')
    )


def corun_assessment_items_for_json(data):
    """Fetch corun-backed assessment Pages pointed to by a model JSON field."""
    page_group_ids = corun_page_group_ids(data)
    if not page_group_ids:
        return []
    try:
        from ai.corun_client import CorunAPIError, CorunClient, corun_configured
        if not corun_configured():
            return []
        client = CorunClient()
        pages = []
        for page_group_id in page_group_ids:
            try:
                pages.append(client.get_page(page_group_id))
            except CorunAPIError:
                continue
        return corun_page_items(pages)
    except CorunAPIError:
        return []


def assessment_presentation(data, *, title='AI Assessments'):
    """Return template-ready presentation data for assessment pointer JSON."""
    items = ai_content_items(ai_content_for_json(data)) + corun_assessment_items_for_json(data)
    return {
        'title': title,
        'items': items,
        'count': len(items),
        'has_assessments': bool(items),
        'quality_choices': AI_CONTENT_QUALITY_VALUES,
    }


def append_ai_content_id(target_obj, json_field, content_id):
    """Append an AIContent id to ``target_obj.<json_field>``."""
    data = dict(getattr(target_obj, json_field) or {})
    ids = ai_content_ids(data)
    if int(content_id) not in ids:
        ids.append(int(content_id))
    data[AI_CONTENT_IDS_KEY] = ids
    setattr(target_obj, json_field, data)
    target_obj.save(update_fields=[json_field, 'updated_at'])


def append_corun_page_group_id(target_obj, json_field, page_group_id):
    """Append a corun Page group id to ``target_obj.<json_field>``."""
    data = dict(getattr(target_obj, json_field) or {})
    ids = corun_page_group_ids(data)
    value = str(page_group_id or '').strip()
    if value and value not in ids:
        ids.append(value)
    data[CORUN_PAGE_GROUP_IDS_KEY] = ids
    setattr(target_obj, json_field, data)
    target_obj.save(update_fields=[json_field, 'updated_at'])


def create_ai_content(*, subject_type, subject_key, username, ai, assessment,
                      subject_label='', subject_url='', data=None,
                      target_obj=None, target_json_field=None):
    """Create one AIContent row and optionally link it from object JSON.

    The AI content itself is append-only. Followups create additional rows.
    If a local object is supplied, its JSON field gets an ``ai_content_ids``
    pointer in the same transaction.
    """
    from monitor_app.models import AIContent
    payload_data = dict(data or {})
    payload_data[AI_CONTENT_QUALITY_KEY] = ''
    payload_data[AI_CONTENT_COMMENT_KEY] = ''
    with transaction.atomic():
        row = AIContent.objects.create(
            subject_type=subject_type,
            subject_key=str(subject_key),
            subject_label=subject_label or '',
            subject_url=subject_url or '',
            username=username,
            ai=ai,
            assessment=assessment,
            data=payload_data,
        )
        if target_obj is not None and target_json_field:
            append_ai_content_id(target_obj, target_json_field, row.pk)
    return row
