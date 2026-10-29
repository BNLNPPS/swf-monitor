"""Credential-free orchestration for removing datasets from a disk RSE
(swf-epicprod docs/STORAGE.md, Removing datasets from an RSE).

Two phases, both run by the production operations agent: a preview, the
live catalog's answer for every dataset matching the pattern; and, once
the operator confirms by typing the RSE name, the removal of the
datasets that preview found eligible, each checked again live. The web
tier validates, records and queues; it holds no catalog credential.
"""

import json
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from monitor_app.activemq_connection import ActiveMQConnectionManager
from monitor_app.models import RseDeletion

# A preview older than this is read again before anything is removed.
PREVIEW_VALID = timedelta(hours=1)
# A queued or running request not updated for this long has lost its
# agent run (a lost message, an agent restart, a failed final post): past
# the agent's removal timeout (an hour) with margin, and the agent posts
# progress every half minute while it works.
STALE_AFTER = timedelta(hours=2)


class RseDeletionRequestError(Exception):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def serialize(record):
    plan = record.plan or {}
    return {
        'id': str(record.id),
        'rse': record.rse,
        'pattern': record.pattern,
        'requested_by': record.requested_by,
        'confirmed_by': record.confirmed_by,
        'status': record.status,
        'pending': record.is_pending,
        'diagnostic': record.diagnostic,
        'plan': plan,
        'result': record.result or {},
        'requested_at': record.requested_at.isoformat(),
        'previewed_at': (record.previewed_at.isoformat()
                         if record.previewed_at else None),
        'confirmed_at': (record.confirmed_at.isoformat()
                         if record.confirmed_at else None),
        'completed_at': (record.completed_at.isoformat()
                         if record.completed_at else None),
    }


def _validate(rse, pattern):
    from swf_epicprod.rse_deletion import RseDeletionError, validate
    try:
        return validate(rse, pattern)
    except RseDeletionError as exc:
        raise RseDeletionRequestError(str(exc))


def _send(message, record):
    try:
        sent = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', json.dumps(message))
        failure = 'ops-agent queue unreachable'
    except Exception as exc:                                  # noqa: BLE001
        sent, failure = False, str(exc)
    if not sent:
        diagnostic = f'Could not queue: {failure}'
        RseDeletion.objects.filter(pk=record.pk).update(
            status='failed', diagnostic=diagnostic,
            completed_at=timezone.now(), updated_at=timezone.now())
        raise RseDeletionRequestError(diagnostic, 503)


def request_preview(*, rse, pattern, requested_by):
    rse, pattern = _validate(rse, pattern)
    record = RseDeletion.objects.create(
        rse=rse, pattern=pattern, requested_by=requested_by)
    _send({'msg_type': 'rse_deletion', 'namespace': 'prodops',
           'phase': 'preview', 'deletion_id': str(record.id),
           'rse': rse, 'pattern': pattern, 'created_by': requested_by},
          record)
    return record


def request_execute(record, *, confirm_rse, confirmed_by):
    """Queue the removal of what ``record``'s preview found eligible."""
    if record.status != 'previewed':
        raise RseDeletionRequestError(
            f'This request is {record.get_status_display().lower()}; '
            'only a previewed request can be confirmed.', 409)
    if str(confirm_rse or '').strip() != record.rse:
        raise RseDeletionRequestError(
            f'Type {record.rse} exactly to confirm the removal.')
    if (not record.previewed_at
            or timezone.now() - record.previewed_at > PREVIEW_VALID):
        raise RseDeletionRequestError(
            'The preview is more than an hour old; preview again.', 409)
    names = [d['name'] for d in (record.plan or {}).get('datasets', [])
             if d.get('eligible')]
    if not names:
        raise RseDeletionRequestError('The preview found nothing eligible.', 409)
    release_stale(record.rse)
    try:
        with transaction.atomic():
            updated = (RseDeletion.objects
                       .filter(pk=record.pk, status='previewed')
                       .update(status='queued', confirmed_by=confirmed_by,
                               confirmed_at=timezone.now(),
                               updated_at=timezone.now()))
    except IntegrityError:
        raise RseDeletionRequestError(
            f'A removal from {record.rse} is already in progress.', 409)
    if not updated:
        raise RseDeletionRequestError('The request changed; reload.', 409)
    record.refresh_from_db()
    _send({'msg_type': 'rse_deletion', 'namespace': 'prodops',
           'phase': 'execute', 'deletion_id': str(record.id),
           'rse': record.rse, 'pattern': record.pattern, 'names': names,
           'created_by': confirmed_by},
          record)
    return record


def release_stale(rse):
    """Fail ``rse``'s queued or running requests that have not been
    updated within STALE_AFTER, so a lost agent run does not hold the RSE
    for good. Outcomes it recorded stay on the request."""
    now = timezone.now()
    return (RseDeletion.objects
            .filter(rse=rse, status__in=RseDeletion.EXECUTING_STATUSES,
                    updated_at__lt=now - STALE_AFTER)
            .update(status='failed', completed_at=now, updated_at=now,
                    diagnostic=('No outcome from the agent within '
                                f'{int(STALE_AFTER.total_seconds() // 3600)} h; '
                                'released. Any recorded outcomes are partial.')))


AGENT_TRANSITIONS = {
    'previewed': ('previewing',),
    # running repeats while the removal proceeds, carrying the outcomes so far.
    'running': ('queued', 'running'),
    'done': ('queued', 'running'),
    'partial': ('queued', 'running'),
    'failed': ('previewing', 'queued', 'running'),
}


def update_from_agent(deletion_id, *, status, diagnostic='', plan=None, result=None):
    """The agent's lifecycle callback."""
    if status not in AGENT_TRANSITIONS:
        raise RseDeletionRequestError(f'unknown status {status!r}')
    try:
        record = RseDeletion.objects.get(pk=deletion_id)
    except RseDeletion.DoesNotExist:
        raise RseDeletionRequestError('Request not found.', 404)
    if record.status not in AGENT_TRANSITIONS[status]:
        raise RseDeletionRequestError(
            f'cannot move from {record.status} to {status}', 409)
    now = timezone.now()
    record.status = status
    record.diagnostic = str(diagnostic or '')
    if plan is not None:
        record.plan = plan
    if result is not None:
        record.result = result
    if status == 'previewed':
        record.previewed_at = now
    if status in ('done', 'partial', 'failed'):
        record.completed_at = now
    record.save()
    return record
