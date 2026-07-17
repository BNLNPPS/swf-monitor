"""Publish bounded SWF System Status projections to Snapper."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from django.db import transaction
from django.utils import timezone

from snapper_ai.services import ComponentUpdate, publish_component, register_component

from .models import SystemStatus
from .system_status import STATUS_STALE_AFTER


PUBLISHER_IDENTITY = "swf-monitor:system-status"
ASSESSMENT_POLICY_VERSION = "swf-system-status-v1"
EVENT_RESOLVER = "swf-system-status-history"
MAX_CHECKS = 128
MAX_SUMMARY_LENGTH = 500
MAX_SERIALIZED_BYTES = 64 * 1024

HEALTH_SCOPE_CHECKS = {
    "testbed": (
        "swf-monitor-mcp-asgi",
        "httpd",
        "github-actions",
    ),
    "epicprod": (
        "swf-monitor-mcp-asgi",
        "httpd",
        "github-actions",
        "epicprod-ops-agent",
        "swf-panda-bot",
        "campaign-assessments",
        "epic-devcloud-prod",
        "epic-devcloud-doc",
    ),
}

HEALTH_REGISTRATION = {
    "title": "SWF assessed system health",
    "description": (
        "The bounded health assessment maintained by SWF System Status for one "
        "operational scope."
    ),
    "visibility": "public",
    "owning_subsystem": "swf-monitor System Status",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "overall_status": {
            "path": "overall.status",
            "type": "string",
            "required": True,
            "kind": "assessment",
            "enum": ["ok", "warning", "error", "unknown"],
            "description": "Deterministic aggregate status of included checks.",
        },
        "overall_reason": {
            "path": "overall.reason",
            "type": "string",
            "required": True,
            "kind": "assessment",
            "max_length": MAX_SUMMARY_LENGTH,
            "description": "Bounded deterministic explanation of aggregate status.",
        },
        "counts": {
            "path": "overall.counts",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": 4,
            "description": "Included check counts by assessed status.",
        },
        "checks": {
            "path": "checks",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_CHECKS,
            "description": "Bounded map of the checks determining overall health.",
        },
    },
    "event_sources": [
        {
            "name": "assessed-health-transitions",
            "resolver": EVENT_RESOLVER,
            "owner": "swf-monitor System Status",
            "event_kind": "health-transition",
            "event_time_field": "checked_at",
            "visibility": "public",
        }
    ],
}

_STATUSES = ("ok", "warning", "error", "unknown")


@dataclass(frozen=True)
class HealthPublication:
    scope: str
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict
    source_as_of: Optional[datetime]


def _bounded_summary(value) -> str:
    summary = str(value or "No summary was provided.")
    return summary[:MAX_SUMMARY_LENGTH]


def _project_check(row: Optional[SystemStatus], assessed_at: datetime) -> dict:
    if row is None:
        return {
            "category": "unknown",
            "status": "unknown",
            "summary": "No System Status row is available.",
        }
    status = row.status if row.status in _STATUSES else "unknown"
    summary = _bounded_summary(row.summary)
    if row.checked_at is None:
        status = "unknown"
        summary = "The System Status row has no assessment time."
    elif row.checked_at > assessed_at:
        status = "unknown"
        summary = "The System Status assessment time is in the future."
    elif assessed_at - row.checked_at > STATUS_STALE_AFTER:
        status = "error"
        summary = "The System Status assessment is older than 15 minutes."
    return {
        "category": str(row.category or "unknown")[:80],
        "status": status,
        "summary": _bounded_summary(summary),
    }


def _overall(counts: dict) -> tuple[str, str]:
    if counts["error"]:
        return "error", f"{counts['error']} included check(s) are red."
    if counts["warning"]:
        suffix = (
            f"; {counts['unknown']} unknown"
            if counts["unknown"]
            else ""
        )
        return "warning", f"{counts['warning']} included check(s) are warning{suffix}."
    if counts["unknown"]:
        return "unknown", f"{counts['unknown']} included check(s) are unknown."
    return "ok", "All included checks are OK."


def health_projection(scope: str, assessed_at: Optional[datetime] = None):
    """Build one scoped projection and its oldest authoritative source time."""
    names = HEALTH_SCOPE_CHECKS.get(scope)
    if names is None:
        raise ValueError(f"unknown Snapper health scope {scope!r}")
    if len(names) > MAX_CHECKS:
        raise ValueError(f"health scope {scope!r} exceeds {MAX_CHECKS} checks")
    assessed_at = assessed_at or timezone.now()
    rows = {
        row.name: row
        for row in SystemStatus.objects.filter(name__in=names)
    }
    checks = {
        name: _project_check(rows.get(name), assessed_at)
        for name in names
    }
    counts = {status: 0 for status in _STATUSES}
    for check in checks.values():
        counts[check["status"]] += 1
    overall_status, overall_reason = _overall(counts)
    checked_times = [
        row.checked_at
        for row in rows.values()
        if row.checked_at is not None and row.checked_at <= assessed_at
    ]
    source_as_of = min(checked_times) if checked_times else None
    projection = {
        "overall": {
            "status": overall_status,
            "reason": overall_reason,
            "counts": counts,
        },
        "checks": checks,
    }
    return projection, source_as_of, assessed_at


@transaction.atomic
def publish_health_scope(
    scope: str,
    assessed_at: Optional[datetime] = None,
) -> HealthPublication:
    """Idempotently register and publish one SWF health component."""
    projection, source_as_of, assessed_at = health_projection(scope, assessed_at)
    registration_update = register_component(
        scope=scope,
        name="health",
        publisher_identity=PUBLISHER_IDENTITY,
        registration=HEALTH_REGISTRATION,
        component_schema_version=1,
    )
    update = publish_component(
        scope=scope,
        name="health",
        publisher_identity=PUBLISHER_IDENTITY,
        data=projection,
        assessed_at=assessed_at,
        source_as_of=source_as_of,
        assessment_policy_version=ASSESSMENT_POLICY_VERSION,
    )
    return HealthPublication(
        scope=scope,
        registration_update=registration_update,
        update=update,
        projection=projection,
        source_as_of=source_as_of,
    )


def publish_health_components(
    scopes: Optional[Iterable[str]] = None,
) -> list[HealthPublication]:
    """Publish the explicit health component set after a completed refresh."""
    selected = tuple(scopes or HEALTH_SCOPE_CHECKS)
    return [publish_health_scope(scope) for scope in selected]


def compact_publication_report(publications: Iterable[HealthPublication]) -> str:
    """Render a bounded operator-facing publication report."""
    return json.dumps(
        [
            {
                "scope": item.scope,
                "revision": item.update.revision,
                "content_changed": item.update.content_changed,
                "registration_changed": (
                    item.registration_update.registration_changed
                ),
                "overall_status": item.projection["overall"]["status"],
                "source_as_of": (
                    item.source_as_of.isoformat() if item.source_as_of else None
                ),
            }
            for item in publications
        ],
        indent=2,
        sort_keys=True,
    )
