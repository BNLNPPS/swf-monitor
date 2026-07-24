"""Apply run lifecycle messages to RunState — the E0-E1 state write side.

The E0-E1 state machine (swf-testbed docs/e0-e1-state-machine.md) stamps
every run lifecycle message with the state and substate in effect, and
prescribes concurrent recording in the testbed database. The workflow
runner creates the initial RunState row at launch; this module advances
it from the messages as the monitor's ActiveMQ processor records them —
one consumer, one truth. Without it every namespace lane on the Snapper
Time history shows its launch-time state forever.

Vocabulary: state/substate copy the message stamps verbatim (beam/
not_ready, run/physics, run/standby). The terminal marker follows the
established fast-processing convention the read side already renders:
state 'ended', phase 'completed'. The lane active check accepts both
'run' (stamped) and 'running' (fast-processing legacy).
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

RUN_LIFECYCLE_TYPES = (
    'run_imminent', 'start_run', 'pause_run', 'resume_run', 'end_run',
)


def apply_run_lifecycle_message(data) -> bool:
    """Advance RunState from one run lifecycle message; True if applied."""
    from .models import RunState

    msg_type = data.get('msg_type')
    if msg_type not in RUN_LIFECYCLE_TYPES:
        return False
    try:
        run_number = int(str(data.get('run_id')))
    except (TypeError, ValueError):
        logger.error(
            "run lifecycle message %s carries no usable run_id: %r",
            msg_type, data.get('run_id'))
        return False

    now = timezone.now()
    # Agents may start mid-run or belong to workflows whose launcher did
    # not create the row; transitions must never fall on the floor.
    row, created = RunState.objects.get_or_create(
        run_number=run_number,
        defaults={
            'phase': 'initializing',
            'state': 'imminent',
            'substate': 'preparing',
            'target_worker_count': 0,
            'state_changed_at': now,
            'metadata': {},
        },
    )

    if msg_type == 'end_run':
        row.state = 'ended'
        row.substate = None
        row.phase = 'completed'
    else:
        stamped_state = data.get('state')
        if stamped_state:
            row.state = str(stamped_state)
        stamped_substate = data.get('substate')
        if stamped_substate:
            row.substate = str(stamped_substate)
        if msg_type in ('start_run', 'pause_run', 'resume_run'):
            row.phase = 'physics'
    row.state_changed_at = now

    # The datataking projection joins namespace through
    # metadata.execution_id; rows created here must carry it too.
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    execution_id = data.get('execution_id')
    if execution_id and not metadata.get('execution_id'):
        metadata['execution_id'] = execution_id
        row.metadata = metadata

    row.save(update_fields=[
        'phase', 'state', 'substate', 'state_changed_at', 'metadata',
        'updated_at',
    ])
    logger.info(
        "RunState %s: %s -> %s/%s (%s)%s",
        run_number, msg_type, row.state, row.substate or '-', row.phase,
        ' [row created]' if created else '')
    return True
