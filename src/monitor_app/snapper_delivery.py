"""Publish the campaign delivered-data record to Snapper.

The delivery record of CAMPAIGN_DELIVERY.md (swf-epicprod docs): per
producing/current campaign, one leaf per physics configuration keyed
by its pc label — events available where events/file is configured,
files and bytes placed always, and the expected-events denominator
with its provenance tier (the recorded chain: the edition's curated
target, else the largest PC-anchored request, else absent). Lenses are
not baked in; they project from the leaves at series-extraction time.
"""

import json
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentNotFound,
    ComponentUpdate,
    publish_component,
    register_component,
)

PUBLISHER_IDENTITY = "swf-monitor:campaign-delivery"
ASSESSMENT_POLICY_VERSION = "swf-campaign-delivery-v1"
MAX_CAMPAIGNS = 8
MAX_LEAVES = 1024
MAX_SERIALIZED_BYTES = 192 * 1024

DELIVERY_REGISTRATION = {
    "title": "Campaign delivered data",
    "description": (
        "Per producing/current campaign, one leaf per physics "
        "configuration (keyed by pc label): events available where "
        "events/file is configured, files and bytes placed, and the "
        "expected-events denominator with its provenance tier "
        "(included / requested / derived). The delivered-data record "
        "of CAMPAIGN_DELIVERY.md; categorization lenses project from "
        "the leaves at read time."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF production catalog",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "campaigns": {
            "path": "campaigns",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_CAMPAIGNS,
            "description": (
                "Campaign name to {totals, leaves}; leaves map pc "
                "labels to {events, expected, tier, files, bytes, "
                "complete}."
            ),
        },
    },
}


@dataclass
class DeliveryPublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict
    observed_at: object


def _campaign_leaves(campaign_name):
    """Leaves for one campaign, keyed by pc label."""
    from pcs.models import Dataset, ProdTask
    from pcs.services import pc_request_projection

    heads = list(
        Dataset.objects.filter(campaign__name=campaign_name)
        .select_related("physics_config")
        .order_by("composed_name", "block_num", "pk")
        .distinct("composed_name"))
    projection = pc_request_projection(heads)
    # Placement from the task output records with the progress
    # accounting rule: unique output DID, most recently checked record
    # wins (duplicate DIDs across task rows are the documented PCS
    # multiplicity), each unique output attributed to the winning
    # record's physics configuration. Verified to reproduce the
    # progress rollup's figures exactly. Outputs without a DID cannot
    # be placement-checked and are skipped.
    task_rows = list(
        ProdTask.objects.filter(dataset__campaign__name=campaign_name)
        .select_related("dataset__physics_config", "prod_config"))
    tasks = {task.dataset.composed_name: task for task in task_rows}
    # Attribution goes through the HEAD row's configuration per
    # composed name: multi-row past-output identities can resolve to
    # different configurations across their physical rows (per-row
    # metadata feeds the evgen resolution), and leaves are keyed by the
    # head's resolution.
    head_pc = {head.composed_name: head.physics_config.label
               for head in heads if head.physics_config_id}
    unique = {}
    for task in task_rows:
        pc = (head_pc.get(task.dataset.composed_name)
              or (task.dataset.physics_config.label
                  if task.dataset.physics_config_id else None))
        if pc is None:
            continue
        for output in task.outputs:
            did = str(output.get("did") or "").strip()
            if not did:
                continue
            checked = str(output.get("checked_at") or "")
            prior = unique.get(did)
            if prior is None or checked >= prior[0]:
                unique[did] = (checked, pc, output)
    placed_by_pc = {}
    for _checked, pc, output in unique.values():
        slot = placed_by_pc.setdefault(
            pc, {"files": 0, "bytes": 0, "complete": True})
        slot["files"] += int(output.get("file_count") or 0)
        slot["bytes"] += int(output.get("bytes") or 0)
        if not output.get("complete", True):
            slot["complete"] = False

    # One leaf per distinct physics configuration: a campaign can hold
    # several version editions of one PC, and per-edition iteration
    # would count the PC's placement once per edition.
    heads_by_pc = {}
    for head in heads:
        if head.physics_config_id:
            heads_by_pc.setdefault(
                head.physics_config.label, []).append(head)

    leaves = {}
    totals = {"configs": 0, "with_target": 0, "events": 0,
              "expected": 0, "files": 0, "bytes": 0}
    for pc, pc_heads in heads_by_pc.items():
        expected = tier = None
        for head in pc_heads:
            if head.expected_events is not None:
                expected = head.expected_events
                tier = head.expected_events_source
                break
        if expected is None:
            anchored = [r.nevents
                        for head in pc_heads
                        for r in projection.get(head.composed_name, ())
                        if r.nevents]
            if anchored:
                expected, tier = max(anchored), "requested"
        events_per_file = None
        for head in pc_heads:
            task = tasks.get(head.composed_name)
            if task is None:
                continue
            config = task.get_effective_config()
            try:
                events_per_file = int(
                    (config.get("data") or {}).get("events_per_file"))
                break
            except (TypeError, ValueError):
                events_per_file = None
        placed = placed_by_pc.get(pc) or {}
        files = int(placed.get("files") or 0)
        bytes_placed = int(placed.get("bytes") or 0)
        events = files * events_per_file if events_per_file else None
        leaves[pc] = {
            "events": events,
            "expected": expected,
            "tier": tier or "",
            "files": files,
            "bytes": bytes_placed,
            "complete": bool(placed.get("complete", True)),
        }
        totals["configs"] += 1
        if expected is not None:
            totals["with_target"] += 1
            totals["expected"] += expected
        if events:
            totals["events"] += events
        totals["files"] += files
        totals["bytes"] += bytes_placed
    if len(leaves) > MAX_LEAVES:
        raise ValueError(
            f"{campaign_name}: {len(leaves)} leaves exceed {MAX_LEAVES}")
    return {"totals": totals, "leaves": leaves}


def delivery_projection():
    from swf_epicprod.analytics.rollup import resolve_target_campaigns

    campaigns = resolve_target_campaigns()[:MAX_CAMPAIGNS]
    projection = {
        "campaigns": {name: _campaign_leaves(name) for name in campaigns},
    }
    serialized = len(json.dumps(projection, separators=(",", ":")))
    if serialized > MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"delivery projection {serialized} bytes exceeds "
            f"{MAX_SERIALIZED_BYTES}")
    return projection, timezone.now()


def publish_delivery() -> DeliveryPublication:
    """Assemble and atomically publish the campaign delivery record."""
    projection, observed_at = delivery_projection()
    with transaction.atomic():
        try:
            update = publish_component(
                scope="epicprod",
                name="delivery",
                publisher_identity=PUBLISHER_IDENTITY,
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
            registration_update = register_component(
                scope="epicprod",
                name="delivery",
                publisher_identity=PUBLISHER_IDENTITY,
                registration=DELIVERY_REGISTRATION,
                component_schema_version=1,
            )
        except ComponentNotFound:
            registration_update = register_component(
                scope="epicprod",
                name="delivery",
                publisher_identity=PUBLISHER_IDENTITY,
                registration=DELIVERY_REGISTRATION,
                component_schema_version=1,
            )
            update = publish_component(
                scope="epicprod",
                name="delivery",
                publisher_identity=PUBLISHER_IDENTITY,
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
    return DeliveryPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
        observed_at=observed_at,
    )


def compact_delivery_publication_report(
        publication: DeliveryPublication) -> str:
    campaigns = publication.projection["campaigns"]
    return json.dumps(
        {
            "scope": publication.update.scope,
            "component": publication.update.name,
            "revision": publication.update.revision,
            "content_changed": publication.update.content_changed,
            "campaigns": {
                name: block["totals"] for name, block in campaigns.items()
            },
            "observed_at": publication.observed_at.isoformat(),
        },
        indent=2,
        sort_keys=True,
    )
