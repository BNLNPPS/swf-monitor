"""Publish the epicprod storage component to Snapper.

Design: swf-epicprod docs/STORAGE.md. The storage pass
(``swf_epicprod.analytics.storage``) maintains the placement state of
production data on every JLab RSE in its own store and builds the
bounded projection; this module registers the component and publishes
each pass's projection. A pass in which no gauge and no counter moved
is affirmed unchanged with the source time advanced, so quiet hours
write no snap while intervals tile.
"""

import json
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentUpdate,
    publish_component,
    register_component,
    report_component_unchanged,
)

PUBLISHER_IDENTITY = "swf-monitor:storage"
ASSESSMENT_POLICY_VERSION = "swf-storage-v1"
COMPONENT_NAME = "storage"
SCOPE = "epicprod"
MAX_SERIALIZED_BYTES = 64 * 1024
# Keys that change on every pass without a change of recorded state.
PROVENANCE_KEYS = ("interval", "pass")

STORAGE_REGISTRATION = {
    "title": "Production data placement on JLab RSEs",
    "description": (
        "Placement state of production data on every JLab Rucio Storage "
        "Element, sampled by the storage pass: per RSE the inventory by "
        "replica state, campaign and root, dataset placement, rules and "
        "locks, the copying backlog with its ages, ghosts (registered "
        "files with no available replica anywhere), capacity against "
        "the production account's limit, and cumulative counters of "
        "arrivals, completed transfers, deletions and ghost movement; "
        "per campaign the replica protection, archival backlog, "
        "catalog quality, dataset state and pipeline latencies; bounded "
        "exception listings. Counters are monotonic from the census and "
        "differenced by every consumer."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF production catalog",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "interval": {
            "path": "interval",
            "type": "object",
            "required": True,
            "kind": "window",
            "description": (
                "The half-open interval (start, end] this publication "
                "covers, from the previous pass to this one."
            ),
        },
        "pass": {
            "path": "pass",
            "type": "object",
            "required": True,
            "kind": "provenance",
            "description": (
                "The pass that produced this publication: mode (census, "
                "full, incremental), campaigns covered, files and "
                "datasets checked, duration, and read failures."
            ),
        },
        "rses": {
            "path": "rses",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": 16,
            "description": (
                "Per RSE: type, capacity, inventory by state, campaign "
                "and root, datasets, rules, backlog, ghosts, and the "
                "cumulative flow counters. The key 'none' collects "
                "registered files with no replica row."
            ),
        },
        "campaigns": {
            "path": "campaigns",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": 8,
            "description": (
                "Per target campaign: files and bytes, replica "
                "protection, unattached and count-less files, archival "
                "backlog, dataset state, cumulative arrival and archive "
                "counters, and the interval's latencies."
            ),
        },
        "exceptions": {
            "path": "exceptions",
            "type": "object",
            "required": True,
            "kind": "fold_remainder",
            "description": (
                "Bounded listing heads, oldest first, of ghosts, stuck "
                "rules and stalled datasets, with the exact remainder "
                "as overflow counts; the full lists are served from the "
                "pass's store."
            ),
        },
        "thresholds": {
            "path": "thresholds",
            "type": "object",
            "required": True,
            "kind": "policy",
            "description": "The SysConfig thresholds applied.",
        },
        "assessment": {
            "path": "assessment",
            "type": "object",
            "required": True,
            "kind": "assessment",
            "description": (
                "Per-RSE and per-campaign verdicts against the "
                "thresholds and the overall verdict."
            ),
        },
    },
}


@dataclass(frozen=True)
class StoragePublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    quiet: bool


def _current_data():
    from snapper_ai.models import CurrentComponent

    row = (CurrentComponent.objects
           .filter(scope=SCOPE, name=COMPONENT_NAME)
           .values("data").first())
    return row["data"] if row and isinstance(row["data"], dict) else None


def _recorded_state(data):
    """The projection without its per-pass provenance, canonical."""
    stripped = {k: v for k, v in data.items() if k not in PROVENANCE_KEYS}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"))


def publish_storage(data, since=None) -> StoragePublication:
    """Register and publish one pass's projection; affirm unchanged when
    the recorded state equals the current component's."""
    serialized = len(json.dumps(data, separators=(",", ":")))
    if serialized > MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"storage projection {serialized} bytes exceeds "
            f"{MAX_SERIALIZED_BYTES}")
    observed_at = timezone.now()
    current = _current_data()
    quiet = current is not None and (
        _recorded_state(current) == _recorded_state(data))
    with transaction.atomic():
        registration_update = register_component(
            scope=SCOPE,
            name=COMPONENT_NAME,
            publisher_identity=PUBLISHER_IDENTITY,
            registration=STORAGE_REGISTRATION,
            component_schema_version=1,
        )
        if quiet:
            update = report_component_unchanged(
                scope=SCOPE,
                name=COMPONENT_NAME,
                publisher_identity=PUBLISHER_IDENTITY,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
        else:
            update = publish_component(
                scope=SCOPE,
                name=COMPONENT_NAME,
                publisher_identity=PUBLISHER_IDENTITY,
                data=data,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
    return StoragePublication(
        registration_update=registration_update, update=update, quiet=quiet)
