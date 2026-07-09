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
from .models import Dataset
from .physics_config import group_editions


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
