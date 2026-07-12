"""MCP tools for append-only AI assessments — the platform mechanism.

Registration, retrieval, and corun storage live here; what an assessment
can be about is domain vocabulary, supplied through the subject-type
registry below (the mechanism/policy split the action registry uses).
The epicprod subject types register from ``swf_epicprod.ai_subjects``.
"""

import logging

from asgiref.sync import sync_to_async

from ai.assessments import (
    CORUN_ASSESSMENT_SECTION,
    append_corun_page_group_id,
    corun_page_items,
)
from ai.corun_client import CorunAPIError, CorunClient, corun_configured
from monitor_app.epicprod_logging import log_epicprod_action
from monitor_app.mcp import mcp

logger = logging.getLogger(__name__)


# Subject-type registry. A domain registers each assessable subject type
# once at import; unknown types still register unlinked (no local subject
# pointer), so the mechanism never gates on the registry's contents.
SUBJECT_TYPE_ALIASES = {}
SUBJECT_RESOLVERS = {}


def register_subject_type(canonical, resolver, aliases=()):
    """Register a subject type: ``resolver(subject_key, data)`` returns
    {target_obj, target_json_field, subject_key, subject_label,
    subject_url} or raises when the key resolves to no object."""
    SUBJECT_RESOLVERS[canonical] = resolver
    SUBJECT_TYPE_ALIASES[canonical] = canonical
    for alias in aliases:
        SUBJECT_TYPE_ALIASES[alias] = canonical


def _canonical_subject_type(subject_type):
    key = str(subject_type or '').strip().lower()
    return SUBJECT_TYPE_ALIASES.get(key, str(subject_type or '').strip())


def _resolve_subject(subject_type, subject_key, data):
    resolver = SUBJECT_RESOLVERS.get(subject_type)
    if resolver is not None:
        return resolver(subject_key, data)
    return {
        'target_obj': None,
        'target_json_field': '',
        'subject_key': str(subject_key).strip(),
        'subject_label': '',
        'subject_url': '',
    }


def _register_ai_assessment_sync(
    subject_type,
    subject_key,
    assessment,
    username,
    ai,
    subject_label,
    subject_url,
    data,
    title='',
):
    canonical_type = _canonical_subject_type(subject_type)
    if not canonical_type:
        return {'success': False, 'error': 'subject_type is required'}
    if not str(subject_key or '').strip():
        return {'success': False, 'error': 'subject_key is required'}
    if not str(assessment or '').strip():
        return {'success': False, 'error': 'assessment is required'}
    if data is not None and not isinstance(data, dict):
        return {'success': False, 'error': 'data must be an object when provided'}

    payload_data = dict(data or {})
    payload_data.setdefault('registered_via', 'mcp')
    payload_data.setdefault('mcp_tool', 'epic_register_ai_assessment')
    try:
        resolved = _resolve_subject(canonical_type, subject_key, payload_data)
    except Exception as exc:
        logger.warning(
            'AI assessment subject resolution failed: type=%s key=%s error=%s',
            canonical_type, subject_key, exc,
        )
        return {
            'success': False,
            'error': f'No local subject found for {canonical_type}:{subject_key}',
            'subject_type': canonical_type,
            'subject_key': str(subject_key),
        }

    if not corun_configured():
        return {
            'success': False,
            'error': 'corun-ai config CORUN_BASE_URL and CORUN_API_TOKEN must be configured',
            'subject_type': canonical_type,
            'subject_key': resolved['subject_key'],
        }

    resolved_label = subject_label or resolved.get('subject_label') or ''
    resolved_url = subject_url or resolved.get('subject_url') or ''
    username_value = str(username or 'mcp').strip() or 'mcp'
    ai_value = str(ai or 'unknown').strip() or 'unknown'
    assessment_text = str(assessment).strip()
    linked = bool(resolved.get('target_obj') and resolved.get('target_json_field'))
    page_data = {
        **payload_data,
        'artifact_type': 'ai_assessment',
        'source_system': 'swf-monitor',
        'ui_visible': False,
        'subject_type': canonical_type,
        'subject_key': resolved['subject_key'],
        'subject_label': resolved_label,
        'subject_url': resolved_url,
        'created_by_system': 'epicprod',
        'created_by_user': username_value,
        'ai': ai_value,
    }
    try:
        page = CorunClient().create_page(
            section=CORUN_ASSESSMENT_SECTION,
            title=(str(title).strip()
                   or f"AI assessment: {canonical_type} {resolved['subject_key']}"),
            content=assessment_text,
            data=page_data,
            tags=[
                'epicprod',
                'ai-assessment',
                canonical_type.replace('_', '-'),
            ],
        )
    except CorunAPIError as exc:
        logger.warning(
            'corun AI assessment creation failed: type=%s key=%s error=%s',
            canonical_type, resolved['subject_key'], exc,
        )
        log_epicprod_action(
            'mcp', 'assessment_register',
            subject_type=canonical_type,
            subject_key=resolved['subject_key'],
            username=username_value,
            outcome='error',
            sublevel='normal',
            live_default=True,
            level=logging.ERROR,
            message=f'assessment registration failed: {exc}',
        )
        return {
            'success': False,
            'error': str(exc),
            'subject_type': canonical_type,
            'subject_key': resolved['subject_key'],
        }

    page_group_id = str(page.get('group_id') or '')
    if linked and page_group_id:
        append_corun_page_group_id(
            resolved['target_obj'],
            resolved['target_json_field'],
            page_group_id,
        )
    # A non-ok verdict raises the record to the live stream, so an
    # assessment calling for attention reaches operators without any
    # additional machinery (EPICPROD_ASSESSMENTS.md § Architecture).
    verdict = str(payload_data.get('verdict') or '').strip().lower()
    log_epicprod_action(
        'mcp', 'assessment_register',
        subject_type=canonical_type,
        subject_key=resolved['subject_key'],
        username=username_value,
        outcome='ok',
        sublevel='high' if verdict not in ('', 'ok') else 'normal',
        live_default=True,
        linked=linked,
        corun_page_group_id=page_group_id,
        **({'verdict': verdict} if verdict else {}),
    )
    return {
        'success': True,
        'storage': 'corun',
        'id': None,
        'corun_page_group_id': page_group_id,
        'corun_page_id': str(page.get('id') or ''),
        'subject_type': canonical_type,
        'subject_key': resolved['subject_key'],
        'subject_label': resolved_label,
        'subject_url': resolved_url,
        'username': username_value,
        'ai': ai_value,
        'created_at': page.get('created_at'),
        'linked': linked,
        'json_field': resolved.get('target_json_field') or '',
    }


def _row_to_dict(row):
    data = row.data or {}
    return {
        'id': row.pk,
        'storage': 'legacy',
        'corun_page_group_id': '',
        'subject_type': row.subject_type,
        'subject_key': row.subject_key,
        'subject_label': row.subject_label,
        'subject_url': row.subject_url,
        'username': row.username,
        'ai': row.ai,
        'quality': data.get('quality') or '',
        'comment': data.get('comment') or '',
        'assessment': row.assessment,
        'data': data,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _corun_item_to_dict(item, page):
    data = page.get('data') if isinstance(page.get('data'), dict) else {}
    return {
        'id': None,
        'storage': 'corun',
        'corun_page_group_id': item.get('corun_page_group_id') or '',
        'subject_type': item.get('subject_type') or '',
        'subject_key': item.get('subject_key') or '',
        'subject_label': item.get('subject_label') or '',
        'subject_url': item.get('subject_url') or '',
        'username': item.get('username') or '',
        'ai': item.get('ai') or '',
        'quality': item.get('quality') or '',
        'comment': item.get('comment') or '',
        'assessment': item.get('assessment') or '',
        'data': data,
        'created_at': str(item.get('created_at') or page.get('created_at') or ''),
    }


def _ordered_ai_content_ids(ids):
    if ids is None:
        return []
    if not isinstance(ids, list):
        raise ValueError('ids must be a list of AIContent ids')

    ordered_ids = []
    for raw in ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f'invalid AIContent id: {raw!r}')
        if item_id <= 0:
            raise ValueError(f'invalid AIContent id: {raw!r}')
        if item_id not in ordered_ids:
            ordered_ids.append(item_id)
    return ordered_ids


def _ordered_corun_page_group_ids(page_group_ids):
    if page_group_ids is None:
        return []
    if not isinstance(page_group_ids, list):
        raise ValueError('corun_page_group_ids must be a list of corun Page group ids')
    ordered_ids = []
    for raw in page_group_ids:
        value = str(raw or '').strip()
        if not value:
            raise ValueError(f'invalid corun Page group id: {raw!r}')
        if value not in ordered_ids:
            ordered_ids.append(value)
    return ordered_ids


def _get_ai_content_sync(ids=None, corun_page_group_ids=None):
    try:
        ordered_ids = _ordered_ai_content_ids(ids)
        ordered_corun_ids = _ordered_corun_page_group_ids(corun_page_group_ids)
    except ValueError as exc:
        return {'success': False, 'error': str(exc)}

    if not ordered_ids and not ordered_corun_ids:
        return {
            'success': False,
            'error': 'ids or corun_page_group_ids must contain at least one id',
        }

    from monitor_app.models import AIContent
    rows = {
        row.pk: row
        for row in AIContent.objects.filter(pk__in=ordered_ids)
    }
    items = [_row_to_dict(rows[item_id]) for item_id in ordered_ids if item_id in rows]
    missing_ids = [item_id for item_id in ordered_ids if item_id not in rows]

    missing_corun_ids = []
    if ordered_corun_ids:
        if not corun_configured():
            return {
                'success': False,
                'error': 'corun-ai config CORUN_BASE_URL and CORUN_API_TOKEN must be configured',
            }
        client = CorunClient()
        for page_group_id in ordered_corun_ids:
            try:
                page = client.get_page(page_group_id)
            except CorunAPIError:
                missing_corun_ids.append(page_group_id)
                continue
            normalized = corun_page_items([page])
            if normalized:
                items.append(_corun_item_to_dict(normalized[0], page))
            else:
                missing_corun_ids.append(page_group_id)

    return {
        'success': True,
        'count': len(items),
        'items': items,
        'missing_ids': missing_ids,
        'missing_corun_page_group_ids': missing_corun_ids,
    }


@mcp.tool()
async def epic_register_ai_assessment(
    subject_type: str,
    subject_key: str,
    assessment: str,
    username: str = 'mcp',
    ai: str = 'unknown',
    subject_label: str = '',
    subject_url: str = '',
    data: dict = None,
) -> dict:
    """
    Register an append-only AI assessment for an epicprod object.

    Known subject types are canonicalized and linked into the target object's
    JSON `corun_page_group_ids` pointer:
      - campaign_task: campaign/production task, keyed by composed name
      - panda_task: local PanDA-task association, keyed by JEDI task id or task name
      - panda_job: local production job record, keyed by pandaid
      - panda_queue: PanDA site/queue record, keyed by queue name; if
        the key is a site name, queue_name == site represents site-level content
      - campaign: production campaign, keyed by campaign name (e.g. 26.05.0);
        the natural subject for campaign-level reports and assessments

    Args:
        subject_type: Canonical subject type or alias.
        subject_key: Human-readable object key such as composed task name,
            JEDI task id, pandaid, queue name, or site name.
        assessment: Markdown assessment text. It is stored as a corun-ai Page.
        username: Human account or service account creating the assessment.
            Bot harnesses should pass `bot`, not a mutable bot deployment name.
        ai: Model or agent identifier. Bot harnesses should pass the exact
            model used to generate the assessment.
        subject_label: Optional display label override.
        subject_url: Optional monitor URL override.
        data: Optional structured metadata captured with the assessment. The
            server stamps `registered_via='mcp'` and
            `mcp_tool='epic_register_ai_assessment'` on stored metadata.

    Returns:
        Success status, corun-ai Page ids, canonical subject reference, created_at,
        and whether the target object JSON pointer was updated.
    """
    return await sync_to_async(_register_ai_assessment_sync)(
        subject_type=subject_type,
        subject_key=subject_key,
        assessment=assessment,
        username=username,
        ai=ai,
        subject_label=subject_label,
        subject_url=subject_url,
        data=data,
    )


@mcp.tool()
async def epic_get_ai_content(ids: list = None, corun_page_group_ids: list = None) -> dict:
    """
    Retrieve append-only epicprod AI assessment content.

    Detail tools that can have AI assessments return an `ai_content` block with
    this exact retrieval instruction:

        {
          "available": true,
          "count": 2,
          "ids": [],
          "corun_page_group_ids": ["..."],
          "retrieval": {
            "tool": "epic_get_ai_content",
            "arguments": {"corun_page_group_ids": ["..."]}
          }
        }

    Use the supplied `ids` and/or `corun_page_group_ids` directly. Do not
    reconstruct subject_type or subject_key from the parent object when a
    detail payload already includes this retrieval block.

    Args:
        ids: Optional list of legacy AIContent integer ids from an MCP detail payload's
            `ai_content.ids` or `ai_content.retrieval.arguments.ids`.
        corun_page_group_ids: Optional list of corun-ai Page group ids from
            `ai_content.corun_page_group_ids` or
            `ai_content.retrieval.arguments.corun_page_group_ids`.

    Returns:
        success, count, items in requested id order, and missing_ids for any
        ids that no longer resolve. Each item includes storage type, subject
        metadata, username, ai/model identifier, Markdown assessment text,
        structured data, and created_at.
    """
    return await sync_to_async(_get_ai_content_sync)(
        ids=ids,
        corun_page_group_ids=corun_page_group_ids,
    )
