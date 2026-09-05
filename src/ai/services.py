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
import logging as _logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as _timezone

_log = _logging.getLogger(__name__)

from pcs.models import Dataset
from pcs.services import (
    CAMPAIGN_PLAN_DISPOSITIONS, PROPAGATION_STATES, STANDARD_CONFIG_TEMPLATE,
    ServiceError, campaign_plan_entries_set, campaign_plan_get,
    dataset_propagation_set, standard_prodconfig_create,
    standard_prodconfig_name, standard_prodconfig_values,
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


def _refresh_catalog_table_cache():
    """Rebuild the current-campaign catalog table fragment after proposal
    activity, so the page a human reloads shows the state they just
    changed — a stale cached table is otherwise served indefinitely
    (page-load rebuild is suppressed). Failure is logged and reported in
    the caller's result, never raised: the decision stands regardless."""
    try:
        from pcs.models import Campaign
        from pcs.views import (_campaigns_with_inflow,
                               rebuild_current_task_list_html_cache)
        campaigns = list(Campaign.objects.filter(lifecycle='current')
                         .order_by('name')[:1])
        # Producing campaigns render the same cached table (the unified
        # view), so decisions refresh them too.
        campaigns += [camp for camp, _ in _campaigns_with_inflow()]
        for campaign in campaigns:
            rebuild_current_task_list_html_cache(campaign, 'catalog')
        return ''
    except Exception as e:                                    # noqa: BLE001
        _log.warning('catalog table cache refresh failed: %s', e)
        return f'catalog table cache refresh failed: {e}'


def _write_proposal_projection(row, head):
    """Write the pending proposal's render projection on its target's
    head row — at propose time and again when an undo returns the row
    to pending."""
    pre = row.precondition or {}
    metadata = dict(head.metadata or {})
    metadata['proposal'] = {
        'id': row.id,
        'action': row.action,
        'payload': row.payload,
        'comment': row.comment,
        'proposer': row.proposer,
        'scan_version': row.scan_version,
        'batch_id': row.batch_id,
        'prev_state': pre.get('prev_state'),
        'prev_replaced_by': pre.get('prev_replaced_by', ''),
        'proposed_at': row.created_at.isoformat(),
    }
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
            _write_proposal_projection(row, head)
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
    result = {'proposed': proposed, 'noop': noop, 'denied': denied,
              'unknown': unknown, 'state': state}
    if proposed:
        cache_error = _refresh_catalog_table_cache()
        if cache_error:
            result['cache_refresh_error'] = cache_error
    return result


def propose_campaign_plan(campaign_name, items, *, proposer='',
                          scan_version=1, batch_id='', created_by=''):
    """Create campaign-assembly plan proposals (CONTINUOUS_PRODUCTION.md,
    Campaign assembly) — creation subjects keyed on (campaign, PC).

    ``items``: [{pc, disposition, target_events, priority, evidence,
    comment}]. Validation mirrors the executor
    (``campaign_plan_entries_set``): an unexecutable proposal is refused
    at birth. The target campaign row is created if absent (lifecycle
    ``future``), so the plan page can show the campaign at proposal
    stage. Denial memory, heartbeat supersession, and one
    ``proposal_created`` event follow the subsystem conventions. The
    precondition anchor is the current plan entry's decision fields (or
    None for a true creation)."""
    from monitor_app.epicprod_logging import log_epicprod_action

    from pcs.models import Campaign
    from pcs.services import plan_entry_anchor, validate_plan_entry

    campaign_name = (campaign_name or '').strip()
    if not campaign_name:
        raise ServiceError('a campaign name is required')
    if not items:
        raise ServiceError('no plan items supplied')
    now = _timezone.now()
    Campaign.objects.get_or_create(
        name=campaign_name,
        defaults={'created_by': created_by or 'campaign-assembly'})
    plan = campaign_plan_get(campaign_name)

    proposed, noop, denied_skips, invalid = [], [], [], []
    with transaction.atomic():
        for item in items:
            pc = (item.get('pc') or '').strip()
            comment = (item.get('comment') or '').strip()
            if not pc or not comment:
                invalid.append(pc or '(missing pc)')
                continue
            try:
                entry = validate_plan_entry(pc, {
                    'disposition': item.get('disposition'),
                    'target_events': item.get('target_events'),
                    'priority': item.get('priority'),
                    'evidence': item.get('evidence', ''),
                }, require_complete=False)
            except ServiceError as e:
                invalid.append(f'{pc}: {e}')
                continue
            payload = {'campaign': campaign_name, **entry}
            current = plan_entry_anchor(plan.get(pc))
            if current == plan_entry_anchor(entry):
                noop.append(pc)
                continue
            input_hash = _proposal_input_hash(payload, comment)
            if Proposal.objects.filter(
                    action='campaign_plan', subject_key=pc,
                    counterpart_key=campaign_name,
                    status='denied', input_hash=input_hash).exists():
                denied_skips.append(pc)
                continue
            # An identical pending proposal stands: leave it in place —
            # a regeneration heartbeat must not reissue refs for
            # unchanged recommendations.
            if Proposal.objects.filter(
                    action='campaign_plan', subject_key=pc,
                    counterpart_key=campaign_name,
                    status='proposed', input_hash=input_hash).exists():
                noop.append(pc)
                continue
            Proposal.objects.filter(
                action='campaign_plan', subject_key=pc,
                counterpart_key=campaign_name,
                status='proposed').update(status='withdrawn',
                                          decided_at=now)
            Proposal.objects.create(
                action='campaign_plan',
                subject_type='physics_config',
                subject_key=pc,
                counterpart_key=campaign_name,
                payload=payload,
                comment=comment,
                proposer=proposer or '',
                scan_version=scan_version,
                batch_id=batch_id or '',
                executor='service',
                precondition={'prev_entry': current},
                input_hash=input_hash,
                created_by=created_by or '',
            )
            proposed.append(pc)

    log_epicprod_action(
        'web', 'proposal_created',
        username=created_by,
        sublevel='normal', live_default=True,
        message=(f'AI proposal: campaign plan {campaign_name}, '
                 f'{len(proposed)} configuration(s) '
                 f'[{batch_id or "no batch"}]'),
        proposed=len(proposed), noop=len(noop), denied=len(denied_skips),
        invalid=len(invalid), campaign=campaign_name,
        proposer=proposer or '', batch_id=batch_id or '',
        scan_version=scan_version,
    )
    return {'proposed': proposed, 'noop': noop, 'denied': denied_skips,
            'invalid': invalid, 'campaign': campaign_name}


def _decide_campaign_plan(rows, decision, decided_by, quality, amendments,
                          now):
    """Decide pending campaign-plan proposals: revalidate each against
    the current plan entry (the ``prev_entry`` anchor), apply any
    reviewer amendments (target_events, priority — recorded on the
    payload under ``amended``), and execute approvals through
    ``campaign_plan_entries_set`` — one executor call and one
    origin-stamped event per campaign per decision act."""
    amendments = {str(k): v for k, v in (amendments or {}).items()}
    stale, denied, approved, incomplete = [], [], [], []
    by_campaign = {}
    with transaction.atomic():
        for row in rows:
            payload = dict(row.payload or {})
            campaign = payload.get('campaign', '')
            current = campaign_plan_get(campaign).get(row.subject_key)
            current_anchor = ({'disposition': current.get('disposition'),
                               'target_events': current.get('target_events'),
                               'priority': current.get('priority')}
                              if current else None)
            pre = (row.precondition or {}).get('prev_entry')
            if current_anchor != pre:
                row.status = 'stale'
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'decided_by',
                                        'decided_at'])
                stale.append(row.ref)
                continue
            if decision == 'deny':
                row.status = 'denied'
                row.quality = quality
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'quality', 'decided_by',
                                        'decided_at'])
                denied.append(row.ref)
                continue
            amended = amendments.get(str(row.pk)) or {}
            changes = {}
            for field in ('target_events', 'priority'):
                if field in amended and amended[field] not in (None, ''):
                    try:
                        changes[field] = int(amended[field])
                    except (TypeError, ValueError):
                        raise ServiceError(
                            f'{row.ref}: amended {field} must be an '
                            f'integer; got {amended[field]!r}')
            if 'disposition' in amended:
                value = amended['disposition']
                if value not in ('include', 'defer', 'retire'):
                    raise ServiceError(
                        f'{row.ref}: amended disposition must be '
                        f'include, defer, or retire; got {value!r}')
                if value != payload.get('disposition'):
                    changes['disposition'] = value
            if changes:
                payload['amended'] = changes
                row.payload = payload
                row.save(update_fields=['payload'])
            entry = {
                'disposition': changes.get(
                    'disposition', payload.get('disposition')),
                'target_events': changes.get(
                    'target_events', payload.get('target_events')),
                'priority': changes.get('priority', payload.get('priority')),
                'evidence': payload.get('evidence', ''),
            }
            # Applying requires a complete entry: an include with no
            # target or no priority stays a proposal, named in the
            # result — never silently dropped, never applied
            # incomplete. Defer/retire rows have no run target and
            # apply without numbers. The gate reads the disposition
            # being applied, amended or not.
            if entry['disposition'] == 'include' and (
                    entry['target_events'] in (None, '')
                    or entry['priority'] in (None, '')):
                missing = [f for f in ('target_events', 'priority')
                           if entry[f] in (None, '')]
                incomplete.append(
                    f"{row.subject_key} ({row.ref}): "
                    f"{' and '.join(missing)} required")
                continue
            by_campaign.setdefault(campaign, []).append((row, entry))

    for campaign, pairs in by_campaign.items():
        result = campaign_plan_entries_set(
            campaign, {row.subject_key: entry for row, entry in pairs},
            f'AI assembly proposal(s) approved by {decided_by}',
            changed_by=decided_by,
            origin={'kind': 'ai_proposal',
                    'proposer': pairs[0][0].proposer,
                    'batch_id': pairs[0][0].batch_id})
        with transaction.atomic():
            for row, _entry in pairs:
                row.status = 'executed'
                row.quality = quality
                row.decided_by = decided_by
                row.decided_at = now
                row.executed_log_id = result.get('log_id')
                row.save(update_fields=['status', 'quality', 'decided_by',
                                        'decided_at', 'executed_log_id'])
                approved.append(row.ref)
    return approved, denied, stale, incomplete


def _ping_subject_key(title):
    """A ping's creation-subject key: the obligation, normalized."""
    import re
    key = re.sub(r'[^a-z0-9]+', '-', (title or '').strip().lower()).strip('-')
    return key[:255] or 'ping'


def propose_pings(items, *, proposer='', batch_id='', created_by=''):
    """Propose pings (PINGS.md): creation subjects keyed on the obligation,
    the due date as counterpart. ``items``: [{title, due, lead_days,
    owner, note, url, comment}]. Validation mirrors the executor
    (``alarms_data.create_ping``); the precondition anchor is the open
    ping with the same obligation at proposal time (None for a true
    creation). Denial memory and identical-pending checks follow the
    subsystem conventions; one ``proposal_created`` event per call."""
    from datetime import date

    from monitor_app import alarms_data
    from monitor_app.epicprod_logging import log_epicprod_action

    if not items:
        raise ServiceError('no pings supplied')
    now = _timezone.now()
    proposed, noop, denied_skips, invalid = [], [], [], []
    with transaction.atomic():
        for item in items:
            title = (item.get('title') or '').strip()
            comment = (item.get('comment') or '').strip()
            if not title or not comment:
                invalid.append(title or '(missing title)')
                continue
            try:
                due = date.fromisoformat(str(item.get('due') or '')[:10])
                lead = int(item.get('lead_days')
                           or alarms_data.PING_DEFAULT_LEAD_DAYS)
                if lead < 0:
                    raise ValueError('lead days cannot be negative')
            except ValueError as e:
                invalid.append(f'{title}: {e}')
                continue
            payload = {
                'title': title, 'due': due.isoformat(), 'lead_days': lead,
                'owner': (item.get('owner') or '').strip(),
                'note': (item.get('note') or '').strip(),
                'url': (item.get('url') or '').strip(),
            }
            subject_key = _ping_subject_key(title)
            existing = alarms_data.open_ping_with_title(title)
            precondition = {'existing_open': existing.id if existing else None}
            input_hash = _proposal_input_hash(payload, comment)
            base = Proposal.objects.filter(action='ping',
                                           subject_key=subject_key)
            if base.filter(status='denied', input_hash=input_hash).exists():
                denied_skips.append(title)
                continue
            if base.filter(status='proposed', input_hash=input_hash).exists():
                noop.append(title)
                continue
            base.filter(status='proposed').update(status='withdrawn',
                                                  decided_at=now)
            Proposal.objects.create(
                action='ping', subject_type='ping', subject_key=subject_key,
                counterpart_key=due.isoformat(), payload=payload,
                comment=comment, proposer=proposer or '', scan_version=1,
                batch_id=batch_id or '', executor='service',
                precondition=precondition, input_hash=input_hash,
                created_by=created_by or '')
            proposed.append(title)
    log_epicprod_action(
        'web', 'proposal_created', username=created_by,
        sublevel='normal', live_default=True,
        message=(f'AI proposal: {len(proposed)} ping(s) '
                 f'[{batch_id or "no batch"}]'),
        proposed=len(proposed), noop=len(noop), denied=len(denied_skips),
        invalid=len(invalid), proposer=proposer or '',
        batch_id=batch_id or '', category='ping', url='/alarms/#pings')
    return {'proposed': proposed, 'noop': noop, 'denied': denied_skips,
            'invalid': invalid}


def propose_ping_fulfil(ping_id, comment, *, proposer='', batch_id='',
                        created_by=''):
    """Propose that an open ping be marked fulfilled (PINGS.md). The
    subject is the ping entry; the precondition is that it is open."""
    from monitor_app import alarms_data
    from monitor_app.epicprod_logging import log_epicprod_action

    comment = (comment or '').strip()
    if not comment:
        raise ServiceError('a comment is required')
    ping = alarms_data.get_ping(ping_id)
    if ping is None:
        raise ServiceError(f'no such ping: {ping_id}')
    if (ping.data or {}).get('status', 'open') != 'open':
        raise ServiceError(f'ping {ping.title!r} is not open')
    payload = {'ping_id': ping.id, 'title': ping.title}
    input_hash = _proposal_input_hash(payload, comment)
    base = Proposal.objects.filter(action='ping_fulfil', subject_key=ping.id)
    if base.filter(status='denied', input_hash=input_hash).exists():
        return {'proposed': [], 'noop': [], 'denied': [ping.title]}
    if base.filter(status='proposed').exists():
        return {'proposed': [], 'noop': [ping.title], 'denied': []}
    Proposal.objects.create(
        action='ping_fulfil', subject_type='ping', subject_key=ping.id,
        payload=payload, comment=comment, proposer=proposer or '',
        batch_id=batch_id or '', executor='service',
        precondition={'status': 'open'}, input_hash=input_hash,
        created_by=created_by or '')
    log_epicprod_action(
        'web', 'proposal_created', username=created_by,
        sublevel='normal', live_default=True,
        message=f'AI proposal: mark ping fulfilled: {ping.title}',
        proposed=1, proposer=proposer or '', batch_id=batch_id or '',
        category='ping_fulfil', url='/alarms/#pings')
    return {'proposed': [ping.title], 'noop': [], 'denied': []}


def propose_standard_configs(items, *, proposer='', batch_id='',
                             created_by=''):
    """Propose the creation of editions' Standard Production
    configurations (AI_PROPOSALS.md, category standard_config): the
    remedy of a campaign-configuration ping (PINGS.md § Pings with a
    remedy). ``items``: [{edition, ping_title, comment}]. Validation is
    the executor's own (``standard_prodconfig_values``); a creation
    subject keyed on the configuration name; the precondition is that no
    configuration of that name exists. Denial memory and identical-pending
    checks follow the subsystem conventions."""
    from pcs.models import ProdConfig
    from monitor_app.epicprod_logging import log_epicprod_action

    if not items:
        raise ServiceError('no editions supplied')
    now = _timezone.now()
    proposed, noop, denied_skips, invalid = [], [], [], []
    with transaction.atomic():
        for item in items:
            edition = str(item.get('edition') or '').strip()
            comment = (item.get('comment') or '').strip()
            if not edition or not comment:
                invalid.append(edition or '(missing edition)')
                continue
            try:
                values = standard_prodconfig_values(edition)
            except ServiceError as e:
                invalid.append(f'{edition}: {e}')
                continue
            name = standard_prodconfig_name(edition)
            if ProdConfig.objects.filter(name=name).exists():
                noop.append(f'{name} exists')
                continue
            payload = {
                'edition': edition, 'name': name,
                'template': STANDARD_CONFIG_TEMPLATE,
                'container_image': values['container_image'],
                'jug_xl_tag': values['jug_xl_tag'],
                'rucio_rse': values['rucio_rse'],
                'ping_title': (item.get('ping_title') or '').strip(),
            }
            input_hash = _proposal_input_hash(payload, comment)
            base = Proposal.objects.filter(action='standard_config',
                                           subject_key=name)
            if base.filter(status='denied', input_hash=input_hash).exists():
                denied_skips.append(name)
                continue
            if base.filter(status='proposed', input_hash=input_hash).exists():
                noop.append(name)
                continue
            base.filter(status='proposed').update(status='withdrawn',
                                                  decided_at=now)
            Proposal.objects.create(
                action='standard_config', subject_type='prod_config',
                subject_key=name, counterpart_key=edition, payload=payload,
                comment=comment, proposer=proposer or '', scan_version=1,
                batch_id=batch_id or '', executor='service',
                precondition={'existing': None}, input_hash=input_hash,
                created_by=created_by or '')
            proposed.append(name)
    log_epicprod_action(
        'web', 'proposal_created', username=created_by,
        sublevel='normal', live_default=True,
        message=(f'AI proposal: {len(proposed)} standard configuration(s) '
                 f'[{batch_id or "no batch"}]'),
        proposed=len(proposed), noop=len(noop), denied=len(denied_skips),
        invalid=len(invalid), proposer=proposer or '',
        batch_id=batch_id or '', category='standard_config',
        url='/alarms/#pings')
    return {'proposed': proposed, 'noop': noop, 'denied': denied_skips,
            'invalid': invalid}


def _decide_standard_configs(rows, decision, decided_by, quality, now):
    """Decide pending standard_config proposals: stale once the
    configuration exists; approval runs the executor, which creates the
    configuration and fulfils the linked ping in one origin-stamped act."""
    from pcs.models import ProdConfig

    stale, denied, approved = [], [], []
    for row in rows:
        with transaction.atomic():
            payload = dict(row.payload or {})
            if ProdConfig.objects.filter(name=row.subject_key).exists():
                row.status = 'stale'
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'decided_by', 'decided_at'])
                stale.append(row.ref)
                continue
            if decision == 'deny':
                row.status = 'denied'
                row.quality = quality
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'quality', 'decided_by',
                                        'decided_at'])
                denied.append(row.ref)
                continue
            origin = {'kind': 'ai_proposal', 'ref': row.ref,
                      'proposer': row.proposer, 'batch_id': row.batch_id,
                      'proposed_at': row.created_at.isoformat()}
            result = standard_prodconfig_create(
                payload.get('edition'), changed_by=decided_by, origin=origin,
                ping_title=payload.get('ping_title') or '')
            row.status = 'executed'
            row.quality = quality
            row.decided_by = decided_by
            row.decided_at = now
            row.executed_log_id = result.get('log_id')
            row.save(update_fields=['status', 'quality', 'decided_by',
                                    'decided_at', 'executed_log_id'])
            approved.append(row.ref)
    return approved, denied, stale


def _decide_pings(rows, decision, decided_by, quality, amendments, now):
    """Decide pending ping and ping-fulfil proposals: revalidate against
    the ping store (an obligation already open, or a ping no longer
    open, is stale), apply a reviewer's amended due date (recorded on the
    payload under ``amended``), and execute approvals through the ping
    executors, one origin-stamped event each."""
    from datetime import date

    from monitor_app import alarms_data

    amendments = {str(k): v for k, v in (amendments or {}).items()}
    stale, denied, approved = [], [], []
    for row in rows:
        with transaction.atomic():
            payload = dict(row.payload or {})
            if row.action == 'ping':
                existing = alarms_data.open_ping_with_title(payload.get('title'))
                current = existing.id if existing else None
                moved = current != (row.precondition or {}).get('existing_open')
            else:
                ping = alarms_data.get_ping(row.subject_key)
                moved = (ping is None
                         or (ping.data or {}).get('status', 'open') != 'open')
            if moved:
                row.status = 'stale'
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'decided_by', 'decided_at'])
                stale.append(row.ref)
                continue
            if decision == 'deny':
                row.status = 'denied'
                row.quality = quality
                row.decided_by = decided_by
                row.decided_at = now
                row.save(update_fields=['status', 'quality', 'decided_by',
                                        'decided_at'])
                denied.append(row.ref)
                continue
            origin = {'kind': 'ai_proposal', 'ref': row.ref,
                      'proposer': row.proposer, 'batch_id': row.batch_id,
                      'proposed_at': row.created_at.isoformat()}
            if row.action == 'ping':
                amended = amendments.get(str(row.pk)) or {}
                changes = {}
                if amended.get('due'):
                    try:
                        changes['due'] = date.fromisoformat(
                            str(amended['due'])[:10]).isoformat()
                    except ValueError:
                        raise ServiceError(
                            f'{row.ref}: amended due must be a date '
                            f'(YYYY-MM-DD); got {amended["due"]!r}')
                if changes:
                    payload['amended'] = changes
                    row.payload = payload
                    row.save(update_fields=['payload'])
                try:
                    _entry, log_id = alarms_data.ping_create_execute(
                        {**payload, **changes}, changed_by=decided_by,
                        origin=origin)
                except alarms_data.PingError as e:
                    raise ServiceError(f'{row.ref}: {e}')
            else:
                try:
                    _entry, log_id = alarms_data.ping_fulfil_execute(
                        row.subject_key, changed_by=decided_by, origin=origin)
                except alarms_data.PingError as e:
                    raise ServiceError(f'{row.ref}: {e}')
            row.status = 'executed'
            row.quality = quality
            row.decided_by = decided_by
            row.decided_at = now
            row.executed_log_id = log_id
            row.save(update_fields=['status', 'quality', 'decided_by',
                                    'decided_at', 'executed_log_id'])
            approved.append(row.ref)
    return approved, denied, stale


def proposal_decide(composed_names, decision, *, decided_by='',
                            quality='', filter_state='', proposal_ids=None,
                            amendments=None):
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

    pending = Proposal.objects.filter(
        action__in=('propagation', 'campaign_plan', 'ping', 'ping_fulfil',
                    'standard_config'),
        status='proposed')
    selector = Q()
    if names:
        selector |= Q(subject_key__in=names)
    if ids:
        selector |= Q(pk__in=ids)
    all_rows = list(pending.filter(selector))
    # Dispatch by category: propagation executes below; campaign-plan
    # and ping rows decide through their own executor paths.
    rows = [r for r in all_rows if r.action == 'propagation']
    plan_rows = [r for r in all_rows if r.action == 'campaign_plan']
    ping_rows = [r for r in all_rows if r.action in ('ping', 'ping_fulfil')]
    config_rows = [r for r in all_rows if r.action == 'standard_config']
    found_names = {r.subject_key for r in all_rows}
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

    incomplete = []
    if plan_rows:
        (plan_approved, plan_denied, plan_stale,
         incomplete) = _decide_campaign_plan(
            plan_rows, decision, decided_by, quality, amendments, now)
        approved += plan_approved
        denied += plan_denied
        stale += plan_stale

    if ping_rows:
        ping_approved, ping_denied, ping_stale = _decide_pings(
            ping_rows, decision, decided_by, quality, amendments, now)
        approved += ping_approved
        denied += ping_denied
        stale += ping_stale

    if config_rows:
        cfg_approved, cfg_denied, cfg_stale = _decide_standard_configs(
            config_rows, decision, decided_by, quality, now)
        approved += cfg_approved
        denied += cfg_denied
        stale += cfg_stale

    if decision == 'deny':
        log_epicprod_action(
            'web', 'proposal_denied',
            username=decided_by,
            sublevel='normal', live_default=True,
            message=(f'AI proposal denied on {len(denied)} subject(s)'
                     + (f' [{quality}]' if quality else '')),
            denied=len(denied), stale=len(stale),
            no_proposal=len(no_proposal),
            **({'quality': quality} if quality else {}),
        )
    result = {'approved': approved, 'denied': denied, 'stale': stale,
              'no_proposal': no_proposal, 'incomplete': incomplete}
    if approved or denied or stale:
        cache_error = _refresh_catalog_table_cache()
        if cache_error:
            result['cache_refresh_error'] = cache_error
    return result


def proposal_undo(composed_names, *, undone_by='', proposal_ids=None):
    """Undo executed AI proposals — the computed compensating action
    (AI_PROPOSALS.md).

    Selection mirrors decide: by dataset composed names (the catalog and
    compose surfaces) and/or by proposal-list row ids. Each selected
    executed proposal is compensated through the identical executor: the
    prior state (and prior ``replaced_by``) captured in the precondition
    at propose time is restored, with a templated comment naming the
    proposal, ``origin: undo`` provenance carrying the proposal id, and
    the undoing human as ``changed_by`` — a new history entry, never
    erasure. Guarded like decide: if the record has moved past the
    executed payload, the undo offer has expired and the row is skipped
    (counted, never silent). The undone proposal returns to ``proposed``
    — pending again, decision fields cleared, render projection restored
    — while the execution and undo events carry the record; the row keeps
    a trace of its most recent undo.
    """
    names = [n.strip() for n in (composed_names or []) if n and n.strip()]
    ids = [int(i) for i in (proposal_ids or [])]
    if not names and not ids:
        raise ServiceError('no dataset names or proposal ids supplied')
    if not undone_by:
        raise ServiceError('an authenticated undoer is required')

    selector = Q()
    if names:
        selector |= Q(subject_key__in=names)
    if ids:
        selector |= Q(pk__in=ids)
    # Executed rows undo by compensation; a denied campaign-plan row
    # also undoes — back to proposed, clearing the denial (a mis-click
    # deny would otherwise be locked out by denial memory).
    all_rows = list(Proposal.objects.filter(
        Q(status='executed')
        | Q(status='denied', action='campaign_plan')).filter(selector))
    rows = [r for r in all_rows if r.action == 'propagation']
    plan_rows = [r for r in all_rows if r.action == 'campaign_plan']
    found_names = {r.subject_key for r in all_rows}
    found_ids = {r.pk for r in all_rows}
    no_proposal = [n for n in names if n not in found_names]
    not_executed = [i for i in ids if i not in found_ids]

    now = _timezone.now()
    undone, moved = [], []
    for row in plan_rows:
        if row.status == 'denied':
            row.status = 'proposed'
            row.decided_by = ''
            row.decided_at = None
            row.quality = ''
            row.undone_by = undone_by
            row.undone_at = now
            row.save(update_fields=['status', 'decided_by', 'decided_at',
                                    'quality', 'undone_by', 'undone_at'])
            undone.append(row.ref)
            continue
        payload = dict(row.payload or {})
        campaign = payload.get('campaign', '')
        prev = (row.precondition or {}).get('prev_entry')
        result = campaign_plan_entries_set(
            campaign, {row.subject_key: dict(prev) if prev else None},
            f'undo of AI proposal {row.ref} '
            f'(approved by {row.decided_by or "unknown"})',
            changed_by=undone_by,
            origin={'kind': 'undo', 'undo_of': row.pk})
        row.status = 'proposed'
        row.decided_by = ''
        row.decided_at = None
        row.quality = ''
        row.executed_log_id = None
        row.undone_by = undone_by
        row.undone_at = now
        row.undone_log_id = result.get('log_id')
        row.save(update_fields=['status', 'decided_by', 'decided_at',
                                'quality', 'executed_log_id', 'undone_by',
                                'undone_at', 'undone_log_id'])
        undone.append(row.ref)
    for row in rows:
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
        row.status = 'proposed'
        row.decided_by = ''
        row.decided_at = None
        row.quality = ''
        row.executed_log_id = None
        row.undone_by = undone_by
        row.undone_at = now
        row.undone_log_id = result.get('log_id')
        row.save(update_fields=['status', 'decided_by', 'decided_at',
                                'quality', 'executed_log_id', 'undone_by',
                                'undone_at', 'undone_log_id'])
        restored_head = (Dataset.objects
                         .filter(composed_name=row.subject_key)
                         .order_by('block_num', 'pk').first())
        if restored_head is not None:
            _write_proposal_projection(row, restored_head)
        undone.append(row.subject_key)
    result = {'undone': undone, 'moved': moved, 'not_executed': not_executed,
              'no_proposal': no_proposal}
    if undone:
        cache_error = _refresh_catalog_table_cache()
        if cache_error:
            result['cache_refresh_error'] = cache_error
    return result


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
    result = {'withdrawn': withdrawn}
    if withdrawn:
        cache_error = _refresh_catalog_table_cache()
        if cache_error:
            result['cache_refresh_error'] = cache_error
    return result
