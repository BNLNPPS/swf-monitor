"""Publish a bounded epicprod PanDA activity projection to Snapper."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.db import connections, transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentNotFound,
    ComponentUpdate,
    publish_component,
    register_component,
)

from .panda.constants import JOB_STATUS_CATEGORIES, PANDA_SCHEMA
from .panda.queries import get_activity


PUBLISHER_IDENTITY = "swf-monitor:panda-activity"
ASSESSMENT_POLICY_VERSION = "swf-panda-activity-24h-v2"
EVENT_RESOLVER = "swf-panda-activity-history"
WINDOW_DAYS = 1
WINDOW_HOURS = 24
MAX_STATUSES = 32
MAX_SITES = 32
MAX_TASK_TYPES = 32
MAX_JOB_TYPES = 16
MAX_SERIALIZED_BYTES = 64 * 1024
TASK_TERMINAL_STATUSES = (
    "done",
    "finished",
    "failed",
    "broken",
    "aborted",
    "exhausted",
    "passed",
)

PANDA_REGISTRATION = {
    "title": "Curated epicprod PanDA activity",
    "description": (
        "Five-minute observations of bounded one-day PanDA job and task "
        "activity, together with all current in-flight job and task states, "
        "target sites, and running cores."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF PanDA production monitor",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "window_hours": {
            "path": "window_hours",
            "type": "integer",
            "required": True,
            "kind": "window",
            "description": "Trailing activity window in hours.",
        },
        "jobs_total_24h": {
            "path": "jobs.total_24h",
            "type": "integer",
            "required": True,
            "kind": "window_count",
            "description": "Jobs modified in the trailing 24-hour window.",
        },
        "jobs_by_status_24h": {
            "path": "jobs.by_status_24h",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_STATUSES,
            "description": "Trailing 24-hour job counts by PanDA status.",
        },
        "in_flight_jobs_now": {
            "path": "jobs.in_flight_now.total",
            "type": "integer",
            "required": True,
            "kind": "gauge",
            "description": "Jobs currently in any in-flight PanDA state.",
        },
        "in_flight_by_status_now": {
            "path": "jobs.in_flight_now.by_status",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_STATUSES,
            "description": "Current in-flight job counts by PanDA status.",
        },
        "in_flight_by_type_now": {
            "path": "jobs.in_flight_now.by_type",
            "type": "object",
            "required": False,
            "kind": "bounded_map",
            "max_items": MAX_JOB_TYPES,
            "description": (
                "Current in-flight job counts by processing type; the "
                "smallest types beyond the bound roll up into 'other'."
            ),
        },
        "in_flight_by_type_status_now": {
            "path": "jobs.in_flight_now.by_type_status",
            "type": "object",
            "required": False,
            "kind": "bounded_map",
            "max_items": MAX_JOB_TYPES,
            "description": (
                "Sparse current in-flight job counts by processing type "
                "and PanDA status — e.g. running analysis, waiting "
                "production."
            ),
        },
        "running_jobs_now": {
            "path": "jobs.in_flight_now.running_jobs",
            "type": "integer",
            "required": True,
            "kind": "gauge",
            "description": "Jobs currently in PanDA running state.",
        },
        "running_cores_now": {
            "path": "jobs.in_flight_now.running_cores",
            "type": "integer",
            "required": True,
            "kind": "gauge",
            "description": "Cores currently allocated to running PanDA jobs.",
        },
        "sites": {
            "path": "jobs.sites",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_SITES,
            "description": (
                "Top PanDA target sites with trailing job outcomes and current "
                "in-flight status counts and running jobs and cores."
            ),
        },
        "tasks_total_24h": {
            "path": "tasks.total_24h",
            "type": "integer",
            "required": True,
            "kind": "window_count",
            "description": "Tasks modified in the trailing 24-hour window.",
        },
        "tasks_by_status_24h": {
            "path": "tasks.by_status_24h",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_STATUSES,
            "description": "Trailing 24-hour task counts by PanDA status.",
        },
        "tasks_by_type_24h": {
            "path": "tasks.by_type_24h",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_TASK_TYPES,
            "description": "Trailing 24-hour task counts by processing type.",
        },
        "in_flight_tasks_now": {
            "path": "tasks.in_flight_now.total",
            "type": "integer",
            "required": True,
            "kind": "gauge",
            "description": "JEDI tasks currently in any nonterminal state.",
        },
        "in_flight_tasks_by_status_now": {
            "path": "tasks.in_flight_now.by_status",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_STATUSES,
            "description": "Current nonterminal JEDI task counts by status.",
        },
        "task_sites": {
            "path": "tasks.sites",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_SITES,
            "description": (
                "Current nonterminal JEDI task counts by target site and "
                "status."
            ),
        },
    },
    "event_sources": [
        {
            "name": "panda-task-job-activity",
            "resolver": EVENT_RESOLVER,
            "owner": "ePIC PanDA production",
            "event_kind": "panda-task-job-activity",
            "event_time_field": "modificationtime",
            "visibility": "public",
        }
    ],
}


@dataclass(frozen=True)
class PandaPublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict
    observed_at: datetime


def _in_flight_activity() -> list[dict]:
    statuses = JOB_STATUS_CATEGORIES["active"]
    placeholders = ", ".join(["%s"] * len(statuses))
    sql = f"""
        SELECT "jobstatus",
               COALESCE("computingsite", 'unknown'),
               COALESCE("processingtype", 'unknown'),
               COUNT(*),
               COALESCE(SUM(
                   COALESCE("actualcorecount", "corecount", 1)
               ), 0)
        FROM "{PANDA_SCHEMA}"."jobsactive4"
        WHERE "jobstatus" IN ({placeholders})
        GROUP BY "jobstatus", COALESCE("computingsite", 'unknown'),
                 COALESCE("processingtype", 'unknown')
        ORDER BY "jobstatus", 4 DESC
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, statuses)
        return [
            {
                "status": str(status or "unknown"),
                "site": str(site or "unknown"),
                "type": str(ptype or "unknown"),
                "jobs": int(jobs or 0),
                "cores": int(cores or 0),
            }
            for status, site, ptype, jobs, cores in cursor.fetchall()
        ]


def _in_flight_task_activity() -> list[dict]:
    placeholders = ", ".join(["%s"] * len(TASK_TERMINAL_STATUSES))
    sql = f"""
        SELECT COALESCE("status", 'unknown'),
               COALESCE("site", 'unknown'),
               COUNT(*)
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE "status" IS NULL OR "status" NOT IN ({placeholders})
        GROUP BY COALESCE("status", 'unknown'), COALESCE("site", 'unknown')
        ORDER BY 1, 3 DESC
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, TASK_TERMINAL_STATUSES)
        return [
            {
                "status": str(status or "unknown"),
                "site": str(site or "unknown"),
                "tasks": int(task_count or 0),
            }
            for status, site, task_count in cursor.fetchall()
        ]


def _count_map(values, label, maximum):
    result = {
        str(key or "unknown"): int(value or 0)
        for key, value in (values or {}).items()
    }
    if len(result) > maximum:
        raise ValueError(f"{label} exceeds {maximum} entries")
    return result


def panda_projection(
    activity: Optional[dict] = None,
    in_flight_activity: Optional[list[dict]] = None,
    in_flight_task_activity: Optional[list[dict]] = None,
) -> tuple[dict, datetime]:
    """Build the bounded revision-driving PanDA activity projection."""
    activity = activity if activity is not None else get_activity(days=WINDOW_DAYS)
    if not isinstance(activity, dict) or activity.get("error"):
        raise ValueError(
            f"PanDA activity query failed: "
            f"{activity.get('error') if isinstance(activity, dict) else activity!r}"
        )
    jobs = activity.get("jobs") or {}
    tasks = activity.get("tasks") or {}
    in_flight_rows = (
        in_flight_activity
        if in_flight_activity is not None
        else _in_flight_activity()
    )
    in_flight_task_rows = (
        in_flight_task_activity
        if in_flight_task_activity is not None
        else _in_flight_task_activity()
    )

    jobs_by_status = _count_map(
        jobs.get("by_status"), "job statuses", MAX_STATUSES
    )
    tasks_by_status = _count_map(
        tasks.get("by_status"), "task statuses", MAX_STATUSES
    )
    task_types = {}
    for row in tasks.get("by_type") or []:
        label = str(row.get("type") or "unknown")
        task_types[label] = task_types.get(label, 0) + int(row.get("count") or 0)
    if len(task_types) > MAX_TASK_TYPES:
        task_types = dict(
            sorted(task_types.items(), key=lambda item: (-item[1], item[0]))[
                :MAX_TASK_TYPES
            ]
        )

    recent_sites = {
        str(row.get("site") or "unknown"): row
        for row in jobs.get("by_site") or []
    }
    in_flight_by_status = {}
    in_flight_by_type = {}
    in_flight_by_type_status = {}
    current_sites = {}
    for row in in_flight_rows:
        status = str(row.get("status") or "unknown")
        site = str(row.get("site") or "unknown")
        ptype = str(row.get("type") or "unknown")
        jobs_now = int(row.get("jobs") or 0)
        cores_now = int(row.get("cores") or 0)
        in_flight_by_status[status] = (
            in_flight_by_status.get(status, 0) + jobs_now
        )
        in_flight_by_type[ptype] = (
            in_flight_by_type.get(ptype, 0) + jobs_now
        )
        type_states = in_flight_by_type_status.setdefault(ptype, {})
        type_states[status] = type_states.get(status, 0) + jobs_now
        site_state = current_sites.setdefault(
            site,
            {
                "in_flight_jobs": 0,
                "by_status": {},
                "running_jobs": 0,
                "running_cores": 0,
            },
        )
        site_state["in_flight_jobs"] += jobs_now
        site_state["by_status"][status] = (
            site_state["by_status"].get(status, 0) + jobs_now
        )
        if status == "running":
            site_state["running_jobs"] += jobs_now
            site_state["running_cores"] += cores_now
    if len(in_flight_by_status) > MAX_STATUSES:
        raise ValueError(
            f"in-flight job statuses exceed {MAX_STATUSES} entries"
        )
    # Bound the type vocabulary by owner curation: keep the largest
    # types, roll the remainder into 'other' — a rogue submission type
    # must not balloon the component.
    if len(in_flight_by_type) > MAX_JOB_TYPES:
        keep = set(sorted(in_flight_by_type,
                          key=lambda t: (-in_flight_by_type[t], t))
                   [:MAX_JOB_TYPES - 1])
        rolled_total = 0
        rolled_states: dict = {}
        for ptype in list(in_flight_by_type):
            if ptype in keep:
                continue
            rolled_total += in_flight_by_type.pop(ptype)
            for status, jobs_now in in_flight_by_type_status.pop(
                    ptype, {}).items():
                rolled_states[status] = (
                    rolled_states.get(status, 0) + jobs_now)
        in_flight_by_type["other"] = (
            in_flight_by_type.get("other", 0) + rolled_total)
        merged = in_flight_by_type_status.setdefault("other", {})
        for status, jobs_now in rolled_states.items():
            merged[status] = merged.get(status, 0) + jobs_now

    in_flight_tasks_by_status = {}
    current_task_sites = {}
    for row in in_flight_task_rows:
        status = str(row.get("status") or "unknown")
        site = str(row.get("site") or "unknown")
        tasks_now = int(row.get("tasks") or 0)
        in_flight_tasks_by_status[status] = (
            in_flight_tasks_by_status.get(status, 0) + tasks_now
        )
        site_state = current_task_sites.setdefault(
            site,
            {"in_flight_tasks": 0, "by_status": {}},
        )
        site_state["in_flight_tasks"] += tasks_now
        site_state["by_status"][status] = (
            site_state["by_status"].get(status, 0) + tasks_now
        )
    if len(in_flight_tasks_by_status) > MAX_STATUSES:
        raise ValueError(
            f"in-flight task statuses exceed {MAX_STATUSES} entries"
        )

    ranked_task_sites = sorted(
        current_task_sites,
        key=lambda name: (
            -int(current_task_sites[name]["in_flight_tasks"]),
            name,
        ),
    )[:MAX_SITES]
    task_sites = {
        name: {
            "in_flight_tasks_now": int(
                current_task_sites[name]["in_flight_tasks"]
            ),
            "by_status_now": current_task_sites[name]["by_status"],
        }
        for name in ranked_task_sites
    }

    site_names = set(recent_sites) | set(current_sites)
    ranked_sites = sorted(
        site_names,
        key=lambda name: (
            -int((current_sites.get(name) or {}).get("in_flight_jobs") or 0),
            -int((recent_sites.get(name) or {}).get("total") or 0),
            name,
        ),
    )[:MAX_SITES]
    sites = {}
    for name in ranked_sites:
        recent = recent_sites.get(name) or {}
        current = current_sites.get(name) or {}
        sites[name] = {
            "jobs_24h": int(recent.get("total") or 0),
            "finished_24h": int(recent.get("finished") or 0),
            "failed_24h": int(recent.get("failed") or 0),
            "in_flight_jobs_now": int(current.get("in_flight_jobs") or 0),
            "by_status_now": current.get("by_status") or {},
            "running_jobs_now": int(current.get("running_jobs") or 0),
            "running_cores_now": int(current.get("running_cores") or 0),
        }

    in_flight_total = sum(in_flight_by_status.values())
    running_jobs = in_flight_by_status.get("running", 0)
    running_cores = sum(
        int(row.get("cores") or 0)
        for row in in_flight_rows
        if str(row.get("status") or "unknown") == "running"
    )
    projection = {
        "window_hours": WINDOW_HOURS,
        "jobs": {
            "total_24h": int(jobs.get("total") or 0),
            "by_status_24h": jobs_by_status,
            "in_flight_now": {
                "total": in_flight_total,
                "by_status": in_flight_by_status,
                "by_type": in_flight_by_type,
                "by_type_status": in_flight_by_type_status,
                "running_jobs": running_jobs,
                "running_cores": running_cores,
            },
            "sites": sites,
        },
        "tasks": {
            "total_24h": int(tasks.get("total") or 0),
            "by_status_24h": tasks_by_status,
            "by_type_24h": task_types,
            "in_flight_now": {
                "total": sum(in_flight_tasks_by_status.values()),
                "by_status": in_flight_tasks_by_status,
            },
            "sites": task_sites,
        },
    }
    return projection, timezone.now()


def publish_panda_activity() -> PandaPublication:
    """Query, curate, and atomically publish epicprod PanDA activity."""
    projection, observed_at = panda_projection()
    with transaction.atomic():
        try:
            update = publish_component(
                scope="epicprod",
                name="panda",
                publisher_identity=PUBLISHER_IDENTITY,
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
        except ComponentNotFound:
            registration_update = register_component(
                scope="epicprod",
                name="panda",
                publisher_identity=PUBLISHER_IDENTITY,
                registration=PANDA_REGISTRATION,
                component_schema_version=4,
            )
            update = publish_component(
                scope="epicprod",
                name="panda",
                publisher_identity=PUBLISHER_IDENTITY,
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
        else:
            # Publish the expanded payload under the previous compatible
            # contract before making newly required quantities authoritative.
            registration_update = register_component(
                scope="epicprod",
                name="panda",
                publisher_identity=PUBLISHER_IDENTITY,
                registration=PANDA_REGISTRATION,
                component_schema_version=4,
            )
    return PandaPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
        observed_at=observed_at,
    )


def compact_panda_publication_report(publication: PandaPublication) -> str:
    return json.dumps(
        {
            "scope": publication.update.scope,
            "component": publication.update.name,
            "revision": max(
                publication.update.revision,
                publication.registration_update.revision,
            ),
            "content_changed": publication.update.content_changed,
            "registration_changed": (
                publication.registration_update.registration_changed
            ),
            "jobs_24h": publication.projection["jobs"]["total_24h"],
            "in_flight_jobs": publication.projection["jobs"]["in_flight_now"][
                "total"
            ],
            "running_jobs": publication.projection["jobs"]["in_flight_now"][
                "running_jobs"
            ],
            "running_cores": publication.projection["jobs"]["in_flight_now"][
                "running_cores"
            ],
            "tasks_24h": publication.projection["tasks"]["total_24h"],
            "in_flight_tasks": publication.projection["tasks"][
                "in_flight_now"
            ]["total"],
            "observed_at": publication.observed_at.isoformat(),
        },
        indent=2,
        sort_keys=True,
    )
