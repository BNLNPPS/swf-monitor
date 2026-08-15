"""Credential-free orchestration for durable PanDA pause/resume requests."""

import json
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from monitor_app.activemq_connection import ActiveMQConnectionManager
from monitor_app.models import PandaTaskOperation

PAUSE_REJECTED_TASK_STATUSES = frozenset(
    ('finished', 'failed', 'done', 'aborted', 'broken', 'paused'))
RESUMABLE_TASK_STATUSES = frozenset(('paused', 'throttled', 'staging'))
# The PanDA command gate accepts plain retry from finished/failed/
# exhausted only; an aborted task is retried via the reactivation path
# (retry with new_parameters -> incexec), which the executor selects by
# observed state. Verified live on task 38541, 2026-08-11.
RETRYABLE_TASK_STATUSES = frozenset(
    ('finished', 'failed', 'exhausted', 'aborted'))
# Stop-and-finish (the PanDA finish command): the task ends 'finished'
# with completed output kept, and plain retry remains available. The
# kill command is deliberately not offered — it strands the task in
# 'aborted', which plain retry refuses (docs/PCS_COMPOSED_NAME_INTEGRITY
# forensics, operator decision 2026-08-11).
FINISHABLE_TASK_STATUSES = frozenset(
    ('running', 'paused', 'throttled', 'staging', 'exhausted',
     'ready', 'pending', 'scouting', 'assigning', 'defined', 'registered'))
BULK_OPERATION_STATUSES = {
    'pause': frozenset(('running',)),
    'resume': frozenset(('paused',)),
    'retry_failures': RETRYABLE_TASK_STATUSES,
    'finish': FINISHABLE_TASK_STATUSES,
}
OPERATION_VERBS = frozenset(('pause', 'resume', 'retry_failures', 'finish'))
MAX_BULK_TASKS = 5000


class PandaTaskOperationError(Exception):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def serialize_operation(operation):
    return {
        'id': str(operation.id),
        'jedi_task_id': operation.jedi_task_id,
        'task_name': operation.task_name,
        'operation': operation.operation,
        'source': operation.source,
        'requested_by': operation.requested_by,
        'status': operation.status,
        'pending': operation.is_pending,
        'diagnostic': operation.diagnostic,
        'observed_status': operation.observed_status,
        'evidence': operation.evidence,
        'requested_at': operation.requested_at.isoformat(),
        'started_at': (operation.started_at.isoformat()
                       if operation.started_at else None),
        'accepted_at': (operation.accepted_at.isoformat()
                        if operation.accepted_at else None),
        'completed_at': (operation.completed_at.isoformat()
                         if operation.completed_at else None),
        'updated_at': operation.updated_at.isoformat(),
    }


def operation_controls(task, *, authenticated, internal_monitor, pending=None):
    """Return enabled state and concise disabled reasons for both controls."""
    status = str(task.get('status') or '').lower()
    pause_state_valid = bool(status) and status not in PAUSE_REJECTED_TASK_STATUSES
    resume_state_valid = status in RESUMABLE_TASK_STATUSES
    controls = {
        'visible': pause_state_valid or resume_state_valid,
        'pause': {'enabled': False, 'reason': ''},
        'resume': {'enabled': False, 'reason': ''},
    }
    if not controls['visible']:
        return controls
    if not internal_monitor:
        reason = 'Available on the internal monitor.'
        controls['pause']['reason'] = reason
        controls['resume']['reason'] = reason
        return controls
    if not authenticated:
        controls['pause']['reason'] = 'Sign in to pause this task.'
        controls['resume']['reason'] = 'Sign in to resume this task.'
        return controls
    if pending:
        reason = f'{pending.operation.title()} is already in progress.'
        controls['pause']['reason'] = reason
        controls['resume']['reason'] = reason
        return controls
    if resume_state_valid:
        controls['resume']['enabled'] = True
        controls['resume']['reason'] = f'Resume this {status} task.'
    else:
        controls['resume']['reason'] = 'Task is not resumable.'
    if status == 'paused':
        controls['pause']['reason'] = 'Task is already paused.'
        return controls
    if pause_state_valid:
        controls['pause']['enabled'] = True
        controls['pause']['reason'] = 'Pause this task.'
    return controls


def queue_task_operation(*, task, operation, requested_by, source='manual',
                         evidence=None):
    """Persist and queue one pause/resume request for the prod-ops agent."""
    record, created = _persist_task_operation(
        task=task,
        operation=operation,
        requested_by=requested_by,
        source=source,
        evidence=evidence,
    )
    if not created:
        return record, False
    jedi_task_id = record.jedi_task_id
    msg = {
        'msg_type': 'panda_task_operation',
        'namespace': 'prodops',
        'operation_id': str(record.id),
        'task_name': record.task_name or f'PanDA task {jedi_task_id}',
        'jedi_task_id': jedi_task_id,
        'operation': operation,
        'source': source,
        'created_by': requested_by,
    }
    _send_operation_message(msg, [record])
    return record, True


def _persist_task_operation(*, task, operation, requested_by, source,
                            evidence=None, allowed_statuses=None):
    """Validate and create one record without sending an ActiveMQ message."""
    if operation not in OPERATION_VERBS:
        raise PandaTaskOperationError(
            'operation must be one of: ' + ', '.join(sorted(OPERATION_VERBS)))
    try:
        jedi_task_id = int(task.get('jeditaskid'))
    except (TypeError, ValueError):
        raise PandaTaskOperationError('Task has no valid JEDI task ID.', 409)

    pending = (PandaTaskOperation.objects
               .filter(jedi_task_id=jedi_task_id,
                       status__in=PandaTaskOperation.PENDING_STATUSES)
               .first())
    if pending:
        if pending.operation == operation:
            return pending, False
        raise PandaTaskOperationError(
            f'{pending.operation.title()} is already in progress for this task.',
            409,
        )

    task_status = str(task.get('status') or '').lower()
    if allowed_statuses is not None and task_status not in allowed_statuses:
        raise PandaTaskOperationError(
            f'Task is not eligible for bulk {operation} '
            f'from status {task_status}.', 409)
    if operation == 'pause':
        if task_status in PAUSE_REJECTED_TASK_STATUSES:
            raise PandaTaskOperationError(
                f'Task cannot be paused from status {task_status}.', 409)
    elif operation == 'resume':
        if task_status not in RESUMABLE_TASK_STATUSES:
            raise PandaTaskOperationError(
                f'Task cannot be resumed from status {task_status}.', 409)
    elif operation == 'retry_failures':
        if task_status not in RETRYABLE_TASK_STATUSES:
            raise PandaTaskOperationError(
                f'Task failures cannot be retried from status {task_status}.',
                409)
    elif task_status not in FINISHABLE_TASK_STATUSES:
        raise PandaTaskOperationError(
            f'Task cannot be finished from status {task_status}.', 409)

    try:
        with transaction.atomic():
            record = PandaTaskOperation.objects.create(
                jedi_task_id=jedi_task_id,
                task_name=str(task.get('taskname') or ''),
                operation=operation,
                source=source,
                requested_by=requested_by,
                evidence=evidence or {'task_status': task_status},
            )
    except IntegrityError:
        pending = (PandaTaskOperation.objects
                   .filter(jedi_task_id=jedi_task_id,
                           status__in=PandaTaskOperation.PENDING_STATUSES)
                   .first())
        if pending and pending.operation == operation:
            return pending, False
        raise PandaTaskOperationError(
            'Another operation is already in progress for this task.', 409)
    return record, True


def _send_operation_message(message, records):
    """Send one operation message and fail every new record if it cannot queue."""
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', json.dumps(message))
    except Exception as exc:
        triggered = False
        failure = str(exc)
    else:
        failure = 'ops-agent queue unreachable'
    if not triggered:
        diagnostic = f'Could not queue operation: {failure}'
        now = timezone.now()
        PandaTaskOperation.objects.filter(
            pk__in=[record.pk for record in records]).update(
                status='failed', diagnostic=diagnostic, completed_at=now,
                updated_at=now)
        raise PandaTaskOperationError(diagnostic, 503)


def queue_task_operations(*, tasks, operation, requested_by):
    """Persist eligible task records and queue one paced prod-ops batch."""
    if operation not in BULK_OPERATION_STATUSES:
        raise PandaTaskOperationError(
            'operation must be one of: '
            + ', '.join(sorted(BULK_OPERATION_STATUSES)))
    if not tasks:
        raise PandaTaskOperationError('Select at least one task.')
    if len(tasks) > MAX_BULK_TASKS:
        raise PandaTaskOperationError(
            f'At most {MAX_BULK_TASKS} tasks may be submitted at once.')

    batch_id = str(uuid.uuid4())
    records = []
    new_records = []
    rejected = []
    allowed_statuses = BULK_OPERATION_STATUSES[operation]
    for task in tasks:
        jedi_task_id = task.get('jeditaskid')
        task_status = str(task.get('status') or '').lower()
        try:
            record, created = _persist_task_operation(
                task=task,
                operation=operation,
                requested_by=requested_by,
                source='manual-bulk',
                evidence={'task_status': task_status, 'batch_id': batch_id},
                allowed_statuses=allowed_statuses,
            )
        except PandaTaskOperationError as exc:
            rejected.append({
                'jedi_task_id': jedi_task_id,
                'status': task_status,
                'error': exc.detail,
            })
            continue
        records.append(record)
        if created:
            new_records.append(record)

    if not records:
        return {'batch_id': batch_id, 'records': [], 'rejected': rejected,
                'queued': 0}
    if new_records:
        items = [
            {
                'operation_id': str(record.id),
                'jedi_task_id': record.jedi_task_id,
                'task_name': (record.task_name
                              or f'PanDA task {record.jedi_task_id}'),
            }
            for record in new_records
        ]
        _send_operation_message({
            'msg_type': 'panda_task_operations',
            'namespace': 'prodops',
            'batch_id': batch_id,
            'operation': operation,
            'items': items,
            'source': 'manual-bulk',
            'created_by': requested_by,
        }, new_records)
    return {
        'batch_id': batch_id,
        'records': [serialize_operation(record) for record in records],
        'rejected': rejected,
        'queued': len(new_records),
    }


def update_task_operation(operation_id, *, status, diagnostic='',
                          observed_status='', evidence=None):
    """Commit an agent-reported lifecycle transition to the durable record."""
    valid = {choice[0] for choice in PandaTaskOperation.STATUS_CHOICES}
    if status not in valid or status == 'queued':
        raise PandaTaskOperationError('Invalid operation status.')
    try:
        record = PandaTaskOperation.objects.get(pk=operation_id)
    except PandaTaskOperation.DoesNotExist:
        raise PandaTaskOperationError('Operation not found.', 404)

    now = timezone.now()
    record.status = status
    record.diagnostic = str(diagnostic or '')[:4000]
    record.observed_status = str(observed_status or '')[:50]
    if evidence:
        merged = dict(record.evidence or {})
        merged.update(evidence)
        record.evidence = merged
    if status == 'running' and not record.started_at:
        record.started_at = now
    if status in ('accepted', 'verified', 'unverified') and not record.accepted_at:
        record.accepted_at = now
    if status in ('verified', 'failed', 'timeout', 'unverified'):
        record.completed_at = now
    record.save()
    return record
