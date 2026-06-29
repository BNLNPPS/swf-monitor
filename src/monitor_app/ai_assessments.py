"""Append-only AI assessment helpers for production objects."""
from datetime import datetime

import bleach
import markdown
from django.db import transaction
from django.utils.safestring import mark_safe

from .utils import format_datetime


AI_CONTENT_IDS_KEY = 'ai_content_ids'
AI_CONTENT_RETRIEVE_TOOL = 'epic_get_ai_content'
AI_CONTENT_QUALITY_KEY = 'quality'
AI_CONTENT_QUALITY_VALUES = ('wrong', 'poor', 'good')
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


def _display_time(value):
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, str):
        try:
            return format_datetime(datetime.fromisoformat(value))
        except ValueError:
            return value
    return value or ''


def ai_content_items(rows):
    """Normalize AIContent rows for display."""
    items = []
    for row in rows:
        assessment = str(getattr(row, 'assessment', '') or '').strip()
        if not assessment:
            continue
        subject_type = str(getattr(row, 'subject_type', '') or '').strip()
        subject_key = str(getattr(row, 'subject_key', '') or '').strip()
        subject_label = str(getattr(row, 'subject_label', '') or '').strip()
        subject_display = subject_label or subject_key
        if subject_type == 'panda_task' and subject_key:
            subject_display = f'PanDA task {subject_key}'
            if subject_label and subject_label != subject_key:
                subject_display = f'{subject_display}: {subject_label}'
        elif subject_type == 'panda_job' and subject_key:
            subject_display = f'PanDA job {subject_key}'
            if subject_label and subject_key not in subject_label:
                subject_display = f'{subject_display}: {subject_label}'
        created_at = getattr(row, 'created_at', '')
        data = getattr(row, 'data', None) or {}
        quality = str(data.get(AI_CONTENT_QUALITY_KEY) or '').strip()
        items.append({
            'id': row.pk,
            'username': str(getattr(row, 'username', '') or '').strip(),
            'ai': str(getattr(row, 'ai', '') or '').strip(),
            'quality': quality if quality in AI_CONTENT_QUALITY_VALUES else '',
            'assessment': assessment,
            'assessment_html': render_assessment_markdown(assessment),
            'created_at': created_at,
            'created_display': _display_time(created_at),
            'subject_type': subject_type,
            'subject_key': subject_key,
            'subject_label': subject_label,
            'subject_display': subject_display,
            'subject_url': str(getattr(row, 'subject_url', '') or '').strip(),
        })
    return items


def ai_content_summary(data):
    """JSON-serializable AIContent summaries for client-rendered pages."""
    out = []
    for item in ai_content_items(ai_content_for_json(data)):
        out.append({
            'id': item['id'],
            'username': item['username'],
            'ai': item['ai'],
            'quality': item['quality'],
            'assessment': item['assessment'],
            'created_at': item['created_display'] or str(item['created_at'] or ''),
            'subject_type': item['subject_type'],
            'subject_key': item['subject_key'],
            'subject_label': item['subject_label'],
            'subject_display': item['subject_display'],
            'subject_url': item['subject_url'],
        })
    return out


def ai_content_retrieval_guidance(data):
    """Return MCP retrieval guidance for AIContent ids in a JSON field."""
    ids = ai_content_ids(data)
    return {
        'available': bool(ids),
        'count': len(ids),
        'ids': ids,
        'retrieval': {
            'tool': AI_CONTENT_RETRIEVE_TOOL,
            'arguments': {'ids': ids},
        } if ids else None,
    }


def ai_content_for_json(data):
    """Fetch AIContent rows pointed to by a model JSON field."""
    ids = ai_content_ids(data)
    if not ids:
        return []
    from .models import AIContent
    by_id = {row.pk: row for row in AIContent.objects.filter(pk__in=ids)}
    return [by_id[item_id] for item_id in ids if item_id in by_id]


def assessment_presentation(data, *, title='AI Assessments'):
    """Return template-ready presentation data for ``ai_content_ids`` JSON."""
    items = ai_content_items(ai_content_for_json(data))
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


def create_ai_content(*, subject_type, subject_key, username, ai, assessment,
                      subject_label='', subject_url='', data=None,
                      target_obj=None, target_json_field=None):
    """Create one AIContent row and optionally link it from object JSON.

    The AI content itself is append-only. Followups create additional rows.
    If a local object is supplied, its JSON field gets an ``ai_content_ids``
    pointer in the same transaction.
    """
    from .models import AIContent
    payload_data = dict(data or {})
    payload_data[AI_CONTENT_QUALITY_KEY] = ''
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
