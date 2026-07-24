"""Publish testbed workflow activity to Snapper (Phase 6 component).

The testbed's workflow layer — executions launched per namespace, and
the STF prompt-processing PanDA tasks they submit (processingtype
stfprocessing) — becomes a contracted Snapper component, so the Time
history shows workflow activity as curves alongside the datataking
lanes: executions in flight, and STF task counts split by target site,
which is the decision box's site assignment made visible.
"""

import json
from dataclasses import dataclass
from datetime import timedelta

from django.db import connections, transaction
from django.utils import timezone

from snapper_ai.services import ComponentUpdate, publish_component, register_component

from .panda.constants import PANDA_SCHEMA
from .workflow_models import WorkflowExecution


PUBLISHER_IDENTITY = "swf-monitor:workflow-activity"
ASSESSMENT_POLICY_VERSION = "swf-workflow-activity-v1"
MAX_MAP_ITEMS = 32
MAX_SERIALIZED_BYTES = 32 * 1024
STF_PROCESSING_TYPE = "stfprocessing"

# JEDI terminal task states; nonterminal STF tasks are the in-flight set.
TASK_TERMINAL_STATUSES = (
    "done", "finished", "failed", "aborted", "broken", "exhausted",
)

WORKFLOW_REGISTRATION = {
    "title": "Testbed workflow activity",
    "description": (
        "Workflow executions and their STF prompt-processing PanDA tasks "
        "(processingtype stfprocessing), with task counts split by target "
        "site — the decision box's site assignment as recorded state."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF testbed workflow layer",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "executions_active": {
            "path": "executions.active",
            "type": "integer",
            "required": True,
            "kind": "gauge",
            "description": "Workflow executions currently in running status.",
        },
        "executions_started_24h": {
            "path": "executions.started_24h",
            "type": "integer",
            "required": True,
            "kind": "window_count",
            "description": "Workflow executions started in the trailing 24 hours.",
        },
        "executions_by_workflow": {
            "path": "executions.by_workflow",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_MAP_ITEMS,
            "description": (
                "Trailing-24-hour execution starts by workflow name."
            ),
        },
        "stf_tasks_in_flight": {
            "path": "stf_tasks.in_flight_total",
            "type": "integer",
            "required": True,
            "kind": "gauge",
            "description": "STF processing PanDA tasks in nonterminal states.",
        },
        "stf_tasks_by_site_status": {
            "path": "stf_tasks.by_site_status",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_MAP_ITEMS,
            "description": (
                "In-flight STF processing task counts keyed site/status — "
                "the decision box's per-site assignment."
            ),
        },
        "stf_tasks_modified_24h": {
            "path": "stf_tasks.modified_24h",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_MAP_ITEMS,
            "description": (
                "Trailing-24-hour STF processing task counts keyed "
                "site/status, terminal states included."
            ),
        },
    },
}


@dataclass(frozen=True)
class WorkflowPublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict


def _bounded_map(pairs, label) -> dict:
    ordered = sorted(pairs.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) > MAX_MAP_ITEMS:
        kept = ordered[: MAX_MAP_ITEMS - 1]
        other = sum(count for _, count in ordered[MAX_MAP_ITEMS - 1:])
        ordered = kept + [(f"other {label}", other)]
    return dict(ordered)


def _execution_activity(now) -> dict:
    active = WorkflowExecution.objects.filter(status="running").count()
    day_ago = now - timedelta(hours=24)
    started = (
        WorkflowExecution.objects.filter(start_time__gte=day_ago)
        .values_list("workflow_definition__workflow_name", flat=True)
    )
    by_workflow: dict[str, int] = {}
    for name in started:
        key = str(name or "unknown")
        by_workflow[key] = by_workflow.get(key, 0) + 1
    return {
        "active": active,
        "started_24h": sum(by_workflow.values()),
        "by_workflow": _bounded_map(by_workflow, "workflows"),
    }


def _stf_task_counts(where_sql: str, params) -> dict[str, int]:
    sql = f"""
        SELECT COALESCE("site", 'unknown'),
               COALESCE("status", 'unknown'),
               COUNT(*)
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE "processingtype" = %s AND ({where_sql})
        GROUP BY 1, 2
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, [STF_PROCESSING_TYPE, *params])
        return {
            f"{site}/{status}": int(count or 0)
            for site, status, count in cursor.fetchall()
        }


def _stf_task_activity(now) -> dict:
    placeholders = ", ".join(["%s"] * len(TASK_TERMINAL_STATUSES))
    in_flight = _stf_task_counts(
        f'"status" IS NULL OR "status" NOT IN ({placeholders})',
        list(TASK_TERMINAL_STATUSES))
    modified = _stf_task_counts(
        '"modificationtime" >= %s', [now - timedelta(hours=24)])
    return {
        "in_flight_total": sum(in_flight.values()),
        "by_site_status": _bounded_map(in_flight, "site/status"),
        "modified_24h": _bounded_map(modified, "site/status"),
    }


def workflow_projection(now=None) -> dict:
    now = now or timezone.now()
    return {
        "executions": _execution_activity(now),
        "stf_tasks": _stf_task_activity(now),
    }


def publish_workflow_activity() -> WorkflowPublication:
    """Query and atomically publish testbed workflow activity."""
    projection = workflow_projection()
    assessed_at = timezone.now()
    with transaction.atomic():
        registration_update = register_component(
            scope="testbed",
            name="workflow",
            publisher_identity=PUBLISHER_IDENTITY,
            registration=WORKFLOW_REGISTRATION,
            component_schema_version=1,
        )
        update = publish_component(
            scope="testbed",
            name="workflow",
            publisher_identity=PUBLISHER_IDENTITY,
            data=projection,
            assessed_at=assessed_at,
            assessment_policy_version=ASSESSMENT_POLICY_VERSION,
        )
    return WorkflowPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
    )


def compact_workflow_publication_report(
    publication: WorkflowPublication,
) -> str:
    """Render a bounded operator-facing publication report."""
    executions = publication.projection["executions"]
    stf_tasks = publication.projection["stf_tasks"]
    return json.dumps({
        "revision": publication.update.revision,
        "content_changed": publication.update.content_changed,
        "executions_active": executions["active"],
        "executions_started_24h": executions["started_24h"],
        "stf_tasks_in_flight": stf_tasks["in_flight_total"],
    }, indent=2, sort_keys=True)
