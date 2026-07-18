"""Publish the testbed datataking state projection to Snapper."""

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from typing import Optional

from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from snapper_ai.services import (
    ComponentUpdate,
    publish_component,
    register_component,
)

from .models import RunState
from .workflow_models import WorkflowExecution


PUBLISHER_IDENTITY = "swf-monitor:run-state"
ASSESSMENT_POLICY_VERSION = "swf-datataking-state-v1"
EVENT_RESOLVER = "swf-testbed-system-state-events"
MAX_NAMESPACES = 128
MAX_SERIALIZED_BYTES = 64 * 1024

DATATAKING_REGISTRATION = {
    "title": "Testbed datataking state by namespace",
    "description": (
        "The latest run state for each namespace sharing the testbed platform, "
        "forming its datataking lanes in the E0-E1 global state at an instant."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF testbed datataking state",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "namespaces": {
            "path": "namespaces",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_NAMESPACES,
            "description": (
                "Map keyed by testbed namespace; each value contains the "
                "current run number, phase, state, optional substate, and last "
                "transition time for that independently operated testbed."
            ),
        },
    },
    "event_sources": [
        {
            "name": "datataking-state-transitions",
            "resolver": EVENT_RESOLVER,
            "owner": "SWF testbed datataking state",
            "event_kind": "datataking-state-transition",
            "event_time_field": "timestamp",
            "visibility": "public",
        }
    ],
}


@dataclass(frozen=True)
class DatatakingPublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict
    run_numbers: dict[str, int]


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(datetime_timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _latest_run_states_by_namespace() -> list[RunState]:
    execution_key = KeyTextTransform("execution_id", "metadata")
    namespace = Subquery(
        WorkflowExecution.objects.filter(
            execution_id=OuterRef("execution_key")
        ).values("namespace")[:1]
    )
    rows = list(
        RunState.objects.annotate(execution_key=execution_key)
        .annotate(testbed_namespace=namespace)
        .exclude(testbed_namespace__isnull=True)
        .exclude(testbed_namespace="")
        .order_by("testbed_namespace", "-run_number")
        .distinct("testbed_namespace")[: MAX_NAMESPACES + 1]
    )
    if len(rows) > MAX_NAMESPACES:
        raise ValueError(
            f"datataking projection exceeds {MAX_NAMESPACES} namespaces"
        )
    return rows


def datataking_projection() -> tuple[dict, dict[str, int]]:
    """Return the latest bounded run state for each testbed namespace."""
    run_states = _latest_run_states_by_namespace()
    if not run_states:
        raise ValueError(
            "cannot publish datataking state without a namespaced RunState row"
        )

    namespaces = {}
    run_numbers = {}
    for run_state in run_states:
        namespace = run_state.testbed_namespace
        state = {
            "run_number": run_state.run_number,
            "phase": run_state.phase,
            "state": run_state.state,
            "last_transition_at": _timestamp(run_state.state_changed_at),
        }
        if run_state.substate is not None:
            state["substate"] = run_state.substate
        namespaces[namespace] = state
        run_numbers[namespace] = run_state.run_number
    return {"namespaces": namespaces}, run_numbers


@transaction.atomic
def publish_datataking_state(
    assessed_at: Optional[datetime] = None,
) -> DatatakingPublication:
    """Register and publish current datataking state across namespaces."""
    projection, run_numbers = datataking_projection()
    assessed_at = assessed_at or timezone.now()
    registration_update = register_component(
        scope="testbed",
        name="datataking",
        publisher_identity=PUBLISHER_IDENTITY,
        registration=DATATAKING_REGISTRATION,
        component_schema_version=2,
    )
    update = publish_component(
        scope="testbed",
        name="datataking",
        publisher_identity=PUBLISHER_IDENTITY,
        data=projection,
        assessed_at=assessed_at,
        assessment_policy_version=ASSESSMENT_POLICY_VERSION,
    )
    return DatatakingPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
        run_numbers=run_numbers,
    )
