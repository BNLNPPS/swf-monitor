"""Credential-free orchestration for removing datasets from a disk RSE
(swf-epicprod docs/STORAGE.md, Removing datasets from an RSE).

Two phases, both run by the production operations agent: a preview, the
live catalog's answer for every dataset matching the pattern; and, once
an operator confirms by typing the RSE name, the removal of the datasets
that preview found eligible, each checked again live. The web tier
validates and queues; it holds no catalog credential and writes nothing.

No table holds the requests. The agent keeps each request's working
state in a file under ``$SWF_TMP_DIR/rse-deletions/<id>.json``, the way
it keeps fetched payload logs, and the page reads it; the record that
stands is the action stream, where the agent logs each preview
(``rse_deletion_preview``) and each removal (``rse_deletion``, high
sublevel). One removal per RSE runs at a time, held by the agent.
Acting is for the operations role (docs/AUTHORITY.md).
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from monitor_app.activemq_connection import ActiveMQConnectionManager

# A preview older than this is read again before anything is removed.
PREVIEW_VALID = timedelta(hours=1)
# A request the agent has not written for this long has lost its agent
# run (a lost message, an agent restart): past the agent's removal
# timeout (an hour) with margin, and a running removal is rewritten as
# each dataset is decided.
STALE_AFTER = timedelta(hours=2)
STATUS_LABELS = {
    'previewing': 'Previewing',
    'previewed': 'Previewed',
    'queued': 'Queued for removal',
    'running': 'Removing',
    'done': 'Done',
    'partial': 'Done with failures',
    'failed': 'Failed',
}
PENDING_STATUSES = ('previewing', 'queued', 'running')
ACTIONS = ('rse_deletion_preview', 'rse_deletion')


class RseDeletionRequestError(Exception):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def request_dir():
    return Path(getattr(settings, 'SWF_TMP_DIR', '/data/swf-tmp')) / 'rse-deletions'


def read_request(deletion_id):
    """The agent's working record of one request, or None."""
    try:
        path = request_dir() / f'{uuid.UUID(str(deletion_id))}.json'
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _parse(stamp):
    try:
        return datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


def serialize(record):
    """The page's view of a request. A pending request the agent stopped
    writing reads as failed, with what it recorded kept."""
    out = dict(record)
    updated = _parse(out.get('updated_at'))
    if (out.get('status') in PENDING_STATUSES and updated
            and timezone.now() - updated > STALE_AFTER):
        out['status'] = 'failed'
        out['diagnostic'] = (
            f'No word from the agent for {int(STALE_AFTER.total_seconds() // 3600)} h; '
            'abandoned. Any recorded outcomes are partial.')
    out.setdefault('plan', {})
    out.setdefault('result', {})
    out['pending'] = out.get('status') in PENDING_STATUSES
    out['status_display'] = STATUS_LABELS.get(out.get('status'), out.get('status'))
    return out


def _validate(rse, pattern):
    from swf_epicprod.rse_deletion import RseDeletionError, validate
    try:
        return validate(rse, pattern)
    except RseDeletionError as exc:
        raise RseDeletionRequestError(str(exc))


def _send(message):
    try:
        sent = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', json.dumps(message))
        failure = 'ops-agent queue unreachable'
    except Exception as exc:                                  # noqa: BLE001
        sent, failure = False, str(exc)
    if not sent:
        raise RseDeletionRequestError(f'Could not queue: {failure}', 503)


def request_preview(*, rse, pattern, requested_by):
    """Queue a live preview; the agent writes the request on receipt."""
    rse, pattern = _validate(rse, pattern)
    now = timezone.now().isoformat()
    record = {'id': str(uuid.uuid4()), 'rse': rse, 'pattern': pattern,
              'requested_by': requested_by, 'confirmed_by': '',
              'status': 'previewing', 'diagnostic': '',
              'requested_at': now, 'updated_at': now}
    _send({'msg_type': 'rse_deletion', 'namespace': 'prodops',
           'phase': 'preview', 'deletion_id': record['id'],
           'rse': rse, 'pattern': pattern, 'created_by': requested_by,
           'requested_at': now})
    return record


def request_execute(deletion_id, *, confirm_rse, confirmed_by):
    """Queue the removal of what the request's preview found eligible."""
    record = read_request(deletion_id)
    if record is None:
        raise RseDeletionRequestError('Request not found.', 404)
    record = serialize(record)
    if record['status'] != 'previewed':
        raise RseDeletionRequestError(
            f"This request is {record['status_display'].lower()}; "
            'only a previewed request can be confirmed.', 409)
    if str(confirm_rse or '').strip() != record['rse']:
        raise RseDeletionRequestError(
            f"Type {record['rse']} exactly to confirm the removal.")
    previewed = _parse(record.get('previewed_at'))
    if not previewed or timezone.now() - previewed > PREVIEW_VALID:
        raise RseDeletionRequestError(
            'The preview is more than an hour old; preview again.', 409)
    names = [d['name'] for d in (record.get('plan') or {}).get('datasets', [])
             if d.get('eligible')]
    if not names:
        raise RseDeletionRequestError('The preview found nothing eligible.', 409)
    now = timezone.now().isoformat()
    _send({'msg_type': 'rse_deletion', 'namespace': 'prodops',
           'phase': 'execute', 'deletion_id': record['id'],
           'rse': record['rse'], 'pattern': record['pattern'], 'names': names,
           'created_by': confirmed_by, 'confirmed_at': now})
    return dict(record, status='queued', confirmed_by=confirmed_by,
                confirmed_at=now, pending=True,
                status_display=STATUS_LABELS['queued'])


def recent(rse, limit=20):
    """The RSE's latest previews and removals from the action stream,
    newest first, for the page's Requests table."""
    from monitor_app.models import AppLog
    rows = (AppLog.objects
            .filter(app_name='epicprod', extra_data__action__in=ACTIONS,
                    extra_data__subject_key=rse)
            .order_by('-timestamp')[:limit])
    out = []
    for row in rows:
        x = row.extra_data or {}
        removal = x.get('action') == 'rse_deletion'
        out.append({
            'at': row.timestamp,
            'kind': 'Removal' if removal else 'Preview',
            'pattern': x.get('pattern') or '',
            'by': x.get('username') or '',
            'outcome': x.get('status') or x.get('outcome') or '',
            'matched': x.get('matched'),
            'eligible': x.get('eligible'),
            'removed': x.get('removed') if removal else None,
            'removed_tb': (x['removed_bytes'] / 1e12
                           if removal and x.get('removed_bytes') is not None else None),
            'note': x.get('reason') or '',
        })
    return out
