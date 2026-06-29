"""MCP tools for append-only AI assessments in epicprod."""

import logging
from urllib.parse import urlencode

from asgiref.sync import sync_to_async
from django.urls import reverse

from monitor_app.ai_assessments import create_ai_content
from monitor_app.mcp import mcp
from monitor_app.mcp.common import _monitor_url

logger = logging.getLogger(__name__)


SUBJECT_TYPE_ALIASES = {
    'campaign_task': 'campaign_task',
    'ctask': 'campaign_task',
    'prod_task': 'campaign_task',
    'prodtask': 'campaign_task',
    'pcs.prod_task': 'campaign_task',
    'panda_task': 'panda_task',
    'ptask': 'panda_task',
    'jedi_task': 'panda_task',
    'jedi': 'panda_task',
    'panda_tasks': 'panda_task',
    'pcs.panda_tasks': 'panda_task',
    'panda_job': 'panda_job',
    'job': 'panda_job',
    'epicprod_job': 'panda_job',
    'monitor.epicprod_job': 'panda_job',
    'panda_queue': 'panda_queue',
    'queue': 'panda_queue',
    'site': 'panda_queue',
    'monitor.panda_queue': 'panda_queue',
}


def _canonical_subject_type(subject_type):
    key = str(subject_type or '').strip().lower()
    return SUBJECT_TYPE_ALIASES.get(key, str(subject_type or '').strip())


def _url(name, *args, query=None):
    path = reverse(name, args=args)
    if query:
        path = f'{path}?{urlencode(query)}'
    return _monitor_url(path)


def _resolve_prod_task(subject_key):
    from pcs.models import ProdTask
    from pcs import services

    qs = ProdTask.objects.select_related('dataset', 'prod_config')
    task = services.resolve_prodtask(subject_key, queryset=qs)
    name = task.composed_name
    return {
        'target_obj': task,
        'target_json_field': 'overrides',
        'subject_key': name,
        'subject_label': name,
        'subject_url': _url(
            'pcs:prod_task_compose',
            query={'tab': 'tasks', 'selected': name},
        ),
    }


def _resolve_panda_tasks(subject_key):
    from pcs.models import PandaTasks

    key = str(subject_key).strip()
    qs = PandaTasks.objects.select_related('prod_task', 'prod_task__dataset')
    if key.isdigit():
        row = qs.filter(jedi_task_id=int(key)).first()
    else:
        row = qs.filter(task_name=key).first()
    if row is None:
        raise PandaTasks.DoesNotExist(f'No PandaTasks row matches {subject_key!r}')
    display_key = row.jedi_task_id or row.task_name
    subject_url = ''
    if row.jedi_task_id:
        subject_url = _url('monitor_app:panda_task_detail', row.jedi_task_id)
    return {
        'target_obj': row,
        'target_json_field': 'metadata',
        'subject_key': str(display_key),
        'subject_label': row.task_name,
        'subject_url': subject_url,
    }


def _resolve_epicprod_job(subject_key):
    from monitor_app.models import EpicProdJob

    pandaid = int(str(subject_key).strip())
    row = EpicProdJob.objects.get(pandaid=pandaid)
    return {
        'target_obj': row,
        'target_json_field': 'data',
        'subject_key': str(row.pandaid),
        'subject_label': f'PanDA job {row.pandaid}',
        'subject_url': _url('monitor_app:panda_job_detail', row.pandaid),
    }


def _resolve_panda_queue(subject_key, data):
    from monitor_app.models import PandaQueue

    queue_name = str(subject_key).strip()
    defaults = {
        'site': str((data or {}).get('site') or queue_name),
        'status': str((data or {}).get('status') or 'active'),
        'queue_type': str((data or {}).get('queue_type') or ''),
        'config_data': (data or {}).get('config_data') or {},
    }
    row, _ = PandaQueue.objects.get_or_create(
        queue_name=queue_name,
        defaults=defaults,
    )
    return {
        'target_obj': row,
        'target_json_field': 'metadata',
        'subject_key': row.queue_name,
        'subject_label': row.queue_name,
        'subject_url': _url('monitor_app:epic_queue_detail', row.queue_name),
    }


def _resolve_subject(subject_type, subject_key, data):
    if subject_type == 'campaign_task':
        return _resolve_prod_task(subject_key)
    if subject_type == 'panda_task':
        return _resolve_panda_tasks(subject_key)
    if subject_type == 'panda_job':
        return _resolve_epicprod_job(subject_key)
    if subject_type == 'panda_queue':
        return _resolve_panda_queue(subject_key, data)
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
    payload_data.setdefault('mcp_tool', 'epicprod_register_ai_assessment')
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

    row = create_ai_content(
        subject_type=canonical_type,
        subject_key=resolved['subject_key'],
        subject_label=subject_label or resolved.get('subject_label') or '',
        subject_url=subject_url or resolved.get('subject_url') or '',
        username=str(username or 'mcp').strip() or 'mcp',
        ai=str(ai or 'unknown').strip() or 'unknown',
        assessment=str(assessment).strip(),
        data=payload_data,
        target_obj=resolved.get('target_obj'),
        target_json_field=resolved.get('target_json_field') or None,
    )
    linked = bool(resolved.get('target_obj') and resolved.get('target_json_field'))
    return {
        'success': True,
        'id': row.pk,
        'subject_type': row.subject_type,
        'subject_key': row.subject_key,
        'subject_label': row.subject_label,
        'subject_url': row.subject_url,
        'username': row.username,
        'ai': row.ai,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'linked': linked,
        'json_field': resolved.get('target_json_field') or '',
    }


def _row_to_dict(row):
    return {
        'id': row.pk,
        'subject_type': row.subject_type,
        'subject_key': row.subject_key,
        'subject_label': row.subject_label,
        'subject_url': row.subject_url,
        'username': row.username,
        'ai': row.ai,
        'assessment': row.assessment,
        'data': row.data or {},
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _get_ai_content_sync(ids):
    if not isinstance(ids, list):
        return {'success': False, 'error': 'ids must be a list of AIContent ids'}

    ordered_ids = []
    for raw in ids:
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            return {'success': False, 'error': f'invalid AIContent id: {raw!r}'}
        if item_id <= 0:
            return {'success': False, 'error': f'invalid AIContent id: {raw!r}'}
        if item_id not in ordered_ids:
            ordered_ids.append(item_id)

    if not ordered_ids:
        return {'success': False, 'error': 'ids must contain at least one id'}

    from monitor_app.models import AIContent
    rows = {
        row.pk: row
        for row in AIContent.objects.filter(pk__in=ordered_ids)
    }
    items = [_row_to_dict(rows[item_id]) for item_id in ordered_ids if item_id in rows]
    missing_ids = [item_id for item_id in ordered_ids if item_id not in rows]
    return {
        'success': True,
        'count': len(items),
        'items': items,
        'missing_ids': missing_ids,
    }


@mcp.tool()
async def epicprod_register_ai_assessment(
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
    JSON `ai_content_ids` pointer:
      - campaign_task: campaign/production task, keyed by composed name
      - panda_task: local PanDA-task association, keyed by JEDI task id or task name
      - panda_job: local production job record, keyed by pandaid
      - panda_queue: PanDA site/queue record, keyed by queue name; if
        the key is a site name, queue_name == site represents site-level content

    Args:
        subject_type: Canonical subject type or alias.
        subject_key: Human-readable object key such as composed task name,
            JEDI task id, pandaid, queue name, or site name.
        assessment: Markdown assessment text. It is stored append-only.
        username: Human account or service account creating the assessment.
            Bot harnesses should pass `bot`, not a mutable bot deployment name.
        ai: Model or agent identifier. Bot harnesses should pass the exact
            model used to generate the assessment.
        subject_label: Optional display label override.
        subject_url: Optional monitor URL override.
        data: Optional structured metadata captured with the assessment. The
            server stamps `registered_via='mcp'` and
            `mcp_tool='epicprod_register_ai_assessment'` on stored metadata.

    Returns:
        Success status, AIContent id, canonical subject reference, created_at,
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
async def epicprod_get_ai_content(ids: list) -> dict:
    """
    Retrieve append-only epicprod AI assessment content by AIContent ids.

    Detail tools that can have AI assessments return an `ai_content` block with
    this exact retrieval instruction:

        {
          "available": true,
          "count": 2,
          "ids": [17, 23],
          "retrieval": {
            "tool": "epicprod_get_ai_content",
            "arguments": {"ids": [17, 23]}
          }
        }

    Use the supplied `ids` directly. Do not reconstruct subject_type or
    subject_key from the parent object when a detail payload already includes
    this retrieval block.

    Args:
        ids: List of AIContent integer ids from an MCP detail payload's
            `ai_content.ids` or `ai_content.retrieval.arguments.ids`.

    Returns:
        success, count, items in requested id order, and missing_ids for any
        ids that no longer resolve locally. Each item includes subject metadata,
        username, ai/model identifier, Markdown assessment text, structured data,
        and created_at.
    """
    return await sync_to_async(_get_ai_content_sync)(ids=ids)
