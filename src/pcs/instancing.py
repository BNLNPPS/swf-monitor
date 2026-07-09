"""Campaign instancing: the continuum's forward mechanism
(CAMPAIGN_CONTINUUM.md).

Instancing carries a campaign's physics forward: group the source
campaign's editions by physics configuration, apply each configuration's
disposition, and mint the target campaign's editions — merging onto
editions the ingest has already observed, never duplicating.

The plan is computed first and is the review surface: what would be
minted, what merges onto existing editions, what the dispositions retire
or hold, and what is not safe to act on (unresolved identities, editions
of one configuration with conflicting dispositions). Execution consumes
a reviewed plan; nothing here mutates until the operator fires it.
"""
import logging

from django.db import transaction

from .models import Campaign, Dataset, ProdTask
from .physics_config import group_editions

_log = logging.getLogger(__name__)

# Request context carried from the source edition's task onto the target
# edition's: everything that makes a row a working-catalog row.
_CARRY_FIELDS = ('prod_config', 'request', 'requestor', 'priority',
                 'pre_tdr_use', 'early_science_use', 'other_use',
                 'new_request', 'csv_file', 'description')


def _campaign_heads(campaign_name):
    """One head row per composed identity in the campaign."""
    return list(
        Dataset.objects.filter(campaign__name=campaign_name)
        .select_related('physics_tag', 'evgen_tag', 'background_tag',
                        'campaign')
        .order_by('composed_name', 'block_num', 'pk')
        .distinct('composed_name'))


def plan_campaign_instancing(source_campaign, target_campaign):
    """The instancing plan from source to target campaign.

    Classes, each item carrying its configuration key and edition names:

    - ``merge``  — continuing configuration whose target edition the
      ingest already observed: the working context attaches to the
      existing edition.
    - ``mint``   — continuing configuration with no target edition yet:
      a new edition is created.
    - ``hold``   — held in the catalog, no target production.
    - ``final``  — produced its last campaign; ``replaced_by`` carried
      when a successor is designated.
    - ``unresolved`` — the configuration identity could not be resolved
      (the curation pool); acting on it risks duplication, so it is
      excluded from action and listed.
    - ``conflict`` — editions grouping to one configuration disagree on
      disposition; a human reconciles before instancing touches it.
    - ``target_only`` — target editions with no source counterpart
      (genuinely new in the target, or unresolved on either side);
      informational.

    Pending AI disposition proposals are not consumed — the plan reads
    decided state only, so deciding proposals changes the plan.
    """
    source_groups = group_editions(_campaign_heads(source_campaign))
    target_groups = group_editions(_campaign_heads(target_campaign))

    plan = {'source_campaign': source_campaign,
            'target_campaign': target_campaign,
            'merge': [], 'mint': [], 'hold': [], 'final': [],
            'unresolved': [], 'conflict': [], 'target_only': []}

    for key, group in source_groups.items():
        editions = group['editions']
        item = {
            'pc': key,
            'source_editions': [d.composed_name for d, _ in editions],
        }
        if not all(detail['resolved'] for _, detail in editions):
            plan['unresolved'].append(item)
            continue
        dispositions = {d.propagation for d, _ in editions}
        if len(dispositions) > 1:
            item['dispositions'] = sorted(dispositions)
            plan['conflict'].append(item)
            continue
        disposition = dispositions.pop()
        if disposition == 'hold':
            plan['hold'].append(item)
            continue
        if disposition == 'final':
            replaced = sorted({d.replaced_by for d, _ in editions
                               if d.replaced_by})
            if replaced:
                item['replaced_by'] = replaced
            plan['final'].append(item)
            continue
        if key in target_groups:
            item['target_editions'] = [
                d.composed_name for d, _ in target_groups[key]['editions']]
            plan['merge'].append(item)
        else:
            plan['mint'].append(item)

    for key, group in target_groups.items():
        if key not in source_groups:
            plan['target_only'].append({
                'pc': key,
                'target_editions': [d.composed_name
                                    for d, _ in group['editions']],
            })

    plan['summary'] = {cls: len(plan[cls]) for cls in
                       ('merge', 'mint', 'hold', 'final', 'unresolved',
                        'conflict', 'target_only')}
    return plan


def _source_task(dataset):
    """The source edition's working task (request context carrier)."""
    return (ProdTask.objects.filter(dataset__composed_name=dataset)
            .exclude(status='past_output')
            .select_related('prod_config').order_by('pk').first())


def _carry_context(source_task, target_task):
    """Attach the source task's request context to the target task,
    preserving the target's own outputs record."""
    for field in _CARRY_FIELDS:
        setattr(target_task, field, getattr(source_task, field))
    overrides = dict(target_task.overrides or {})
    csv_block = (source_task.overrides or {}).get('csv_import')
    if csv_block:
        overrides['csv_import'] = csv_block
    target_task.overrides = overrides


def execute_campaign_instancing(source_campaign, target_campaign, *,
                                created_by=''):
    """Execute the instancing plan: populate the target campaign's
    working catalog from the source campaign (CAMPAIGN_CONTINUUM.md).

    The plan is recomputed at execution — a reviewed plan that has gone
    stale is never blindly applied. Per class:

    - **merge**: the ingested target edition's task becomes a working
      task — renamed to its composed name (composed-name-as-identity;
      the legacy name keeps resolving), status ``submitted`` (adopted
      production, already producing), the source task's request context
      attached, its recorded outputs preserved.
    - **mint**: a new target edition — Dataset named by the builder with
      the target version segment (source tags and sample carried, the
      target campaign bound) and a ``draft`` task carrying the request
      context: planned, not yet produced.
    - hold / final / unresolved / conflict / target_only: untouched,
      counted.

    Idempotent by construction: a re-run classifies previously minted
    editions as merges and re-applies context. One
    ``campaign_instancing`` action-stream event per run.
    """
    from monitor_app.epicprod_logging import log_epicprod_action

    plan = plan_campaign_instancing(source_campaign, target_campaign)
    target = Campaign.objects.get(name=target_campaign)
    merged, minted, skipped_no_task, errors = [], [], [], []

    with transaction.atomic():
        for item in plan['merge']:
            source_task = _source_task(item['source_editions'][0])
            if source_task is None:
                skipped_no_task.append(item['source_editions'][0])
                continue
            for name in item['target_editions']:
                task = (ProdTask.objects.filter(dataset__composed_name=name)
                        .order_by('pk').first())
                if task is None:
                    skipped_no_task.append(name)
                    continue
                _carry_context(source_task, task)
                task.name = name
                task.status = 'submitted'
                task.save()
                merged.append(name)

        for item in plan['mint']:
            source_head = (Dataset.objects
                           .filter(composed_name=item['source_editions'][0])
                           .order_by('block_num', 'pk')
                           .select_related().first())
            source_task = _source_task(item['source_editions'][0])
            if source_head is None or source_task is None:
                skipped_no_task.append(item['source_editions'][0])
                continue
            edition = Dataset(
                scope=source_head.scope,
                detector_version=target_campaign,
                detector_config=source_head.detector_config,
                campaign=target,
                physics_tag=source_head.physics_tag,
                evgen_tag=source_head.evgen_tag,
                simu_tag=source_head.simu_tag,
                reco_tag=source_head.reco_tag,
                background_tag=source_head.background_tag,
                sample_name=source_head.sample_name,
                propagation='continue',
                blocks=1, block_num=1,
                metadata={'source': {'kind': 'campaign_instancing',
                                     'location': source_head.composed_name}},
                created_by=created_by or 'campaign_instancing',
            )
            name = edition.build_dataset_name()
            edition.dataset_name = name
            edition.composed_name = name
            edition.did = f'{edition.scope}:{name}.b1'
            try:
                edition.save()
            except Exception as e:                            # noqa: BLE001
                errors.append(f'{name}: {e}')
                continue
            task = ProdTask(
                name=name, status='draft', dataset=edition, campaign=target,
                overrides={}, created_by=created_by or 'campaign_instancing',
            )
            _carry_context(source_task, task)
            task.save()
            minted.append(name)

    summary = {**plan['summary'], 'merged_tasks': len(merged),
               'minted_editions': len(minted),
               'skipped_no_task': len(skipped_no_task),
               'errors': len(errors)}
    log_epicprod_action(
        'web', 'campaign_instancing',
        subject_type='campaign', subject_key=target_campaign,
        username=created_by,
        sublevel='high', live_default=True,
        outcome='ok' if not errors else 'error',
        reason='; '.join(errors[:5]),
        message=(f'campaign instancing {source_campaign} -> '
                 f'{target_campaign}: {len(minted)} edition(s) minted, '
                 f'{len(merged)} task(s) adopted; '
                 f"{plan['summary']['hold']} held, "
                 f"{plan['summary']['final']} final, "
                 f"{plan['summary']['unresolved']} unresolved left to "
                 f'curation'),
        **summary,
    )
    return {'merged': merged, 'minted': minted,
            'skipped_no_task': skipped_no_task, 'errors': errors,
            'summary': summary}
