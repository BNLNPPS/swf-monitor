"""AI proposal services (AI_PROPOSALS.md).

The propose / decide / withdraw / delete machinery behind the AI proposal
list. Proposals are frozen executable payloads; everything past authoring
is deterministic. Executors stay in their domain apps — approval here
dispatches to them (``pcs.services.dataset_propagation_set`` for the
campaign-propagation pilot) with the approving human as ``changed_by`` and
the origin stamp on the event.
"""
import hashlib as _hashlib
import json as _json

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as _timezone

from pcs.models import Dataset
from pcs.services import (
    PROPAGATION_STATES, ServiceError, dataset_propagation_set,
)

from .models import ACTION_REF_PREFIXES, Proposal


def parse_proposal_ref(ref):
    """Resolve a proposal ref ('cp-123') to its Proposal row.

    The prefix is corroboration, not decoration: it must match the row's
    category, so a garbled or mis-relayed reference is refused loudly
    instead of deciding the wrong proposal. Raises ServiceError on any
    mismatch; the message names the actual row when one exists.
    """
    text = (ref or '').strip().lower()
    prefix, sep, num = text.partition('-')
    known = set(ACTION_REF_PREFIXES.values())
    if not sep or not num.isdigit() or prefix not in known:
        raise ServiceError(
            f'unrecognized proposal ref {ref!r} — expected '
            f'<prefix>-<number> with prefix in {sorted(known)}')
    row = Proposal.objects.filter(pk=int(num)).first()
    if row is None:
        raise ServiceError(f'no proposal {text} exists')
    if row.ref != text:
        raise ServiceError(
            f'ref {text} does not match proposal #{row.pk}, which is a '
            f'{row.action} proposal with ref {row.ref} — refusing')
    return row


def _proposal_input_hash(payload, comment):
    blob = _json.dumps({'payload': payload, 'comment': comment}, sort_keys=True)
    return _hashlib.sha1(blob.encode()).hexdigest()


def _clear_proposal_projection(name):
    """Remove the render projection from the record a proposal targeted."""
    head = (Dataset.objects
            .filter(composed_name=name).order_by('block_num', 'pk').first())
    if head is None:
        return
    metadata = dict(head.metadata or {})
    if 'proposal' in metadata:
        metadata.pop('proposal', None)
        head.metadata = metadata
        head.save(update_fields=['metadata'])


def propose_propagation(composed_names, state, comment, *, replaced_by='',
                            proposer='', scan_version=1, batch_id='',
                            created_by=''):
    """Create AI propagation proposals on dataset editions.

    Validates exactly as ``dataset_propagation_set`` does — an unexecutable
    proposal is refused at birth. The canonical record is a Proposal row
    (frozen payload, required comment, proposer identity,
    ``precondition.prev_state`` staleness anchor); the target's head row
    (first by block then pk, the same deterministic head every propagation
    writer uses) carries a render projection in ``metadata['proposal']``,
    written here
    and cleared by decision or withdrawal. Skips, all counted and returned:
    unknown names, no-ops (already in the target state), and identities
    with a denied proposal-list row matching this proposal's input hash (a
    denied proposal never returns until its inputs change). An existing
    pending proposal is superseded (withdrawn) by the fresh one — the
    heartbeat refresh. One ``proposal_created`` action-stream event
    per call.
    """
    from monitor_app.epicprod_logging import log_epicprod_action

    state = (state or '').strip()
    comment = (comment or '').strip()
    replaced_by = (replaced_by or '').strip()
    names = [n.strip() for n in (composed_names or []) if n and n.strip()]
    if state not in PROPAGATION_STATES:
        raise ServiceError(
            f'propagation state must be one of '
            f'{", ".join(PROPAGATION_STATES)}; got {state!r}')
    if not comment:
        raise ServiceError('comment is required on every proposal')
    if not names:
        raise ServiceError('no dataset names supplied')

    payload = {'state': state, 'replaced_by': replaced_by}
    input_hash = _proposal_input_hash(payload, comment)
    now = _timezone.now()
    proposed, noop, denied, unknown = [], [], [], []
    with transaction.atomic():
        for name in names:
            head = (Dataset.objects
                    .filter(composed_name=name).order_by('block_num', 'pk').first())
            if head is None:
                unknown.append(name)
                continue
            if head.propagation == state and (
                    not replaced_by or head.replaced_by == replaced_by):
                noop.append(name)
                continue
            if Proposal.objects.filter(
                    action='propagation', subject_key=name,
                    status='denied', input_hash=input_hash).exists():
                denied.append(name)
                continue
            # Heartbeat refresh: a fresh proposal supersedes the pending one.
            Proposal.objects.filter(
                action='propagation', subject_key=name,
                status='proposed').update(status='withdrawn', decided_at=now)
            row = Proposal.objects.create(
                action='propagation',
                subject_type='dataset',
                subject_key=name,
                payload=payload,
                comment=comment,
                proposer=proposer or '',
                scan_version=scan_version,
                batch_id=batch_id or '',
                executor='service',
                precondition={'prev_state': head.propagation,
                              'prev_replaced_by': head.replaced_by},
                input_hash=input_hash,
                created_by=created_by or '',
            )
            metadata = dict(head.metadata or {})
            metadata['proposal'] = {
                'id': row.id,
                'action': 'propagation',
                'payload': payload,
                'comment': comment,
                'proposer': proposer or '',
                'scan_version': scan_version,
                'batch_id': batch_id or '',
                'prev_state': head.propagation,
                'prev_replaced_by': head.replaced_by,
                'proposed_at': now.isoformat(),
            }
            head.metadata = metadata
            head.save(update_fields=['metadata'])
            proposed.append(name)

    log_epicprod_action(
        'web', 'proposal_created',
        username=created_by,
        sublevel='normal', live_default=True,
        message=(f'AI proposal: propagation -> {state} on {len(proposed)} '
                 f'dataset(s) [{batch_id or "no batch"}]: {comment}'),
        proposed=len(proposed), noop=len(noop), denied=len(denied),
        unknown=len(unknown), state=state, comment=comment,
        proposer=proposer or '', batch_id=batch_id or '',
        scan_version=scan_version,
    )
    return {'proposed': proposed, 'noop': noop, 'denied': denied,
            'unknown': unknown, 'state': state}


def proposal_decide(composed_names, decision, *, decided_by='',
                            quality='', filter_state='', proposal_ids=None):
    """Approve or deny pending AI proposals.

    Selection by dataset composed names (the catalog and compose surfaces)
    and/or by proposal-list row ids (the AI proposal list page). Approval
    revalidates each proposal against current state (the
    ``precondition.prev_state`` anchor): a record that moved since the
    proposal saw it is marked stale and withdrawn from the record, never
    re-interpreted. Valid approvals execute through
    ``dataset_propagation_set`` — the identical call an operator makes by
    hand — grouped by identical (state, replaced_by, comment) so a family
    batch is one call and one origin-stamped event; the approving human is
    ``changed_by`` and the executed proposal rows record the event's log
    id. Denial marks the proposal row (denial memory is the proposal
    list); one ``proposal_denied`` event per call. ``quality``
    optionally tags the decision with the shared review vocabulary
    (wrong | poor | ok | good) — 'wrong' is the one-tap miscalibration
    signal that weighs against the proposer's track record.
    """
    from monitor_app.epicprod_logging import log_epicprod_action

    decision = (decision or '').strip()
    quality = (quality or '').strip()
    names = [n.strip() for n in (composed_names or []) if n and n.strip()]
    ids = [int(i) for i in (proposal_ids or [])]
    if decision not in ('approve', 'deny'):
        raise ServiceError(f"decision must be 'approve' or 'deny'; "
                           f"got {decision!r}")
    if quality and quality not in dict(Proposal.QUALITY_CHOICES):
        raise ServiceError(
            f"quality must be one of "
            f"{', '.join(dict(Proposal.QUALITY_CHOICES))}; got {quality!r}")
    if not names and not ids:
        raise ServiceError('no dataset names or proposal ids supplied')
    if not decided_by:
        raise ServiceError('an authenticated decider is required')

    pending = Proposal.objects.filter(action='propagation', status='proposed')
    selector = Q()
    if names:
        selector |= Q(subject_key__in=names)
    if ids:
        selector |= Q(pk__in=ids)
    rows = list(pending.filter(selector))
    found_names = {r.subject_key for r in rows}
    no_proposal = [n for n in names if n not in found_names]

    now = _timezone.now()
    stale, denied, approved = [], [], []
    groups = {}
    with transaction.atomic():
        for row in rows:
            head = (Dataset.objects
                    .filter(composed_name=row.subject_key)
                    .order_by('block_num', 'pk').first())
            pre = row.precondition or {}
            current = head.propagation if head else None
            record_moved = current != pre.get('prev_state')
            if not record_moved and head is not None and 'prev_replaced_by' in pre:
                record_moved = head.replaced_by != pre['prev_replaced_by']
            if record_moved:
                row.status = 'stale'
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'decided_by', 'decided_at'])
                _clear_proposal_projection(row.subject_key)
                stale.append(row.subject_key)
                continue
            if decision == 'deny':
                row.status = 'denied'
                row.quality = quality
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'quality', 'decided_by',
                                        'decided_at'])
                _clear_proposal_projection(row.subject_key)
                denied.append(row.subject_key)
                continue
            payload = row.payload or {}
            key = (payload.get('state', ''), payload.get('replaced_by', ''),
                   row.comment)
            groups.setdefault(key, {'rows': [], 'origin': {
                'proposer': row.proposer,
                'scan_version': row.scan_version,
                'batch_id': row.batch_id,
                'proposed_at': row.created_at.isoformat(),
            }})['rows'].append(row)

    for (state, replaced_by, comment), group in groups.items():
        group_names = [r.subject_key for r in group['rows']]
        result = dataset_propagation_set(
            group_names, state, comment, replaced_by=replaced_by,
            changed_by=decided_by, filter_state=filter_state,
            origin=group['origin'])
        executed = set(result['changed']) | set(result['unchanged'])
        with transaction.atomic():
            for row in group['rows']:
                if row.subject_key not in executed:
                    continue
                row.status = 'executed'
                row.quality = quality
                row.decided_by = decided_by
                row.decided_at = now
                row.executed_log_id = result.get('log_id')
                row.save(update_fields=['status', 'quality', 'decided_by',
                                        'decided_at', 'executed_log_id'])
                _clear_proposal_projection(row.subject_key)
                approved.append(row.subject_key)

    if decision == 'deny':
        log_epicprod_action(
            'web', 'proposal_denied',
            username=decided_by,
            sublevel='normal', live_default=True,
            message=(f'AI proposal denied on {len(denied)} dataset(s)'
                     + (f' [{quality}]' if quality else '')),
            denied=len(denied), stale=len(stale),
            no_proposal=len(no_proposal),
            **({'quality': quality} if quality else {}),
        )
    return {'approved': approved, 'denied': denied, 'stale': stale,
            'no_proposal': no_proposal}


def proposal_undo(proposal_ids, *, undone_by=''):
    """Undo executed AI proposals — the computed compensating action
    (AI_PROPOSALS.md).

    Each selected executed proposal is compensated through the identical
    executor: the prior state (and prior ``replaced_by``) captured in the
    precondition at propose time is restored, with a templated comment
    naming the proposal, ``origin: undo`` provenance carrying the proposal
    id, and the undoing human as ``changed_by`` — a new history entry,
    never erasure. Guarded like decide: if the record has moved past the
    executed payload, the undo offer has expired and the row is skipped
    (counted, never silent). The undone row keeps its decision record and
    becomes terminal status ``undone`` with the compensating event's log
    id.
    """
    ids = [int(i) for i in (proposal_ids or [])]
    if not ids:
        raise ServiceError('no proposal ids supplied')
    if not undone_by:
        raise ServiceError('an authenticated undoer is required')

    now = _timezone.now()
    undone, moved, not_executed = [], [], []
    for row in Proposal.objects.filter(pk__in=ids):
        if row.status != 'executed':
            not_executed.append(row.pk)
            continue
        head = (Dataset.objects
                .filter(composed_name=row.subject_key)
                .order_by('block_num', 'pk').first())
        payload = row.payload or {}
        pre = row.precondition or {}
        # The undo offer expires when the record moves past the payload.
        if head is None or head.propagation != payload.get('state') or (
                payload.get('replaced_by')
                and head.replaced_by != payload.get('replaced_by')):
            moved.append(row.subject_key)
            continue
        prev_replaced_by = pre.get('prev_replaced_by', '')
        touched_replaced_by = bool(payload.get('replaced_by'))
        result = dataset_propagation_set(
            [row.subject_key], pre.get('prev_state'),
            f'undo of AI proposal #{row.pk} '
            f'(approved by {row.decided_by or "unknown"})',
            replaced_by=prev_replaced_by if touched_replaced_by else '',
            clear_replaced_by=touched_replaced_by and not prev_replaced_by,
            changed_by=undone_by,
            origin={'kind': 'undo', 'undo_of': row.pk},
        )
        row.status = 'undone'
        row.undone_by = undone_by
        row.undone_at = now
        row.undone_log_id = result.get('log_id')
        row.save(update_fields=['status', 'undone_by', 'undone_at',
                                'undone_log_id'])
        undone.append(row.subject_key)
    return {'undone': undone, 'moved': moved, 'not_executed': not_executed}


def proposal_delete(proposal_ids, *, deleted_by=''):
    """Operator deletion of AI proposal list rows — housekeeping for test
    or noise entries that would confuse readers. Human-only and logged; a
    pending row also clears its render projection. This removes decision
    history, so it is a cleanup verb, never a decision verb."""
    from monitor_app.epicprod_logging import log_epicprod_action

    ids = [int(i) for i in (proposal_ids or [])]
    if not ids:
        raise ServiceError('no proposal ids supplied')
    if not deleted_by:
        raise ServiceError('an authenticated deleter is required')
    deleted = 0
    with transaction.atomic():
        for row in Proposal.objects.filter(pk__in=ids):
            if row.status == 'proposed':
                _clear_proposal_projection(row.subject_key)
            row.delete()
            deleted += 1
    log_epicprod_action(
        'web', 'proposal_deleted',
        username=deleted_by,
        sublevel='normal', live_default=False,
        message=f'{deleted} AI proposal list row(s) deleted',
        deleted=deleted,
    )
    return {'deleted': deleted}


def proposal_withdraw(*, batch_id=None, created_by=''):
    """Withdraw pending proposals — the recurring proposer's heartbeat
    (withdraw, then re-derive and re-propose from current inputs) or an
    operator clear. Counted and logged (``proposal_expired``),
    never silent."""
    from monitor_app.epicprod_logging import log_epicprod_action

    now = _timezone.now()
    withdrawn = 0
    with transaction.atomic():
        qs = Proposal.objects.filter(action='propagation', status='proposed')
        if batch_id:
            qs = qs.filter(batch_id=batch_id)
        for row in qs:
            row.status = 'withdrawn'
            row.decided_at = now
            row.save(update_fields=['status', 'decided_at'])
            _clear_proposal_projection(row.subject_key)
            withdrawn += 1
    log_epicprod_action(
        'web', 'proposal_expired',
        username=created_by,
        sublevel='normal', live_default=True,
        message=f'{withdrawn} pending AI proposal(s) withdrawn'
                + (f' [batch {batch_id}]' if batch_id else ''),
        withdrawn=withdrawn, batch_id=batch_id or '',
    )
    return {'withdrawn': withdrawn}
