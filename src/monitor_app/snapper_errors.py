"""Publish the epicprod error-state component to Snapper.

Design: docs/SNAPPER_ERRORS.md. The component records running failed-job
counters per error category (component x code) for the scope and per
tracked task, plus the trailing hour's top diagnostic patterns. Counters
follow the counter-flow form established by the PanDA activity
component (snapper_panda.py): monotonic running values whose consecutive
snap differences are the per-interval accruals; the absolute origin is
arbitrary and never displayed.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import connections, transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentNotFound,
    ComponentUpdate,
    publish_component,
    register_component,
)

from .panda.constants import (
    ERROR_COMPONENTS,
    FAULTY_STATUSES,
    PANDA_SCHEMA,
)

PUBLISHER_IDENTITY = "swf-monitor:panda-errors"
ASSESSMENT_POLICY_VERSION = "swf-panda-errors-v1"
COMPONENT_NAME = "errors"
SCOPE = "epicprod"

RECENT_FINAL_DAYS = 7
DETAIL_WINDOW_MINUTES = 60
MAX_TASKS = 48
MAX_CATEGORIES = 96
MAX_TASK_CATEGORIES = 16
MAX_PATTERNS = 20
MAX_PATTERN_TASKS = 8
PATTERN_DIAG_CHARS = 160
MAX_SERIALIZED_BYTES = 96 * 1024

TASK_TERMINAL_STATUSES = (
    "done",
    "finished",
    "failed",
    "broken",
    "aborted",
    "exhausted",
    "passed",
)

ERRORS_REGISTRATION = {
    "title": "Curated epicprod PanDA error state",
    "description": (
        "Five-minute observations of PanDA job errors: running failed-job "
        "counters per error category (component and code) for the scope "
        "and per tracked task, and the trailing hour's top diagnostic "
        "patterns with representative jobs. Counter differences between "
        "consecutive snaps are the per-interval error accruals."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF PanDA production monitor",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "categories": {
            "path": "categories",
            "type": "object",
            "required": True,
            "kind": "cumulative_counter_map",
            "max_items": MAX_CATEGORIES,
            "description": (
                "Running failed-job counts per error category "
                "'component:code', classified by the first nonzero error "
                "component in the standard order. Monotonic; the absolute "
                "origin is arbitrary - consumers difference two instants "
                "to count the errors between them. Categories beyond the "
                "bound fold into 'other:0'."
            ),
        },
        "tasks": {
            "path": "tasks",
            "type": "object",
            "required": True,
            "kind": "bounded_map",
            "max_items": MAX_TASKS,
            "description": (
                "Per-task running error counters, keyed by JEDI task id: "
                "task status and 'cum' per category. Tracked tasks are "
                "those in flight plus those final within the trailing "
                "retention window; a task entering tracking is seeded "
                "with its complete from-birth counts, and a task leaving "
                "tracking leaves the map while its history remains in "
                "earlier snaps."
            ),
        },
        "detail": {
            "path": "detail",
            "type": "object",
            "required": True,
            "kind": "moment_detail",
            "description": (
                "Trailing-window top diagnostic patterns: category, "
                "diagnostic snippet, count, representative PanDA job id, "
                "and affected task ids, with the window's total error "
                "and pattern counts."
            ),
        },
    },
    "event_sources": [
        {
            "name": "panda-job-errors",
            "resolver": "swf-panda-errors-history",
            "owner": "ePIC PanDA production",
            "event_kind": "panda-job-errors",
            "event_time_field": "endtime",
            "visibility": "public",
        }
    ],
}


@dataclass(frozen=True)
class ErrorsPublication:
    registration_update: ComponentUpdate
    update: ComponentUpdate
    projection: dict
    observed_at: datetime


def _classify_sql():
    """CASE expressions classifying a job by its first nonzero error
    component in ERROR_COMPONENTS order - the classified-mode
    convention of the error summary, so one job lands in one
    category."""
    comp_case = " ".join(
        f'WHEN "{c["code"]}" > 0 THEN \'{c["name"]}\''
        for c in ERROR_COMPONENTS
    )
    code_case = " ".join(
        f'WHEN "{c["code"]}" > 0 THEN "{c["code"]}"'
        for c in ERROR_COMPONENTS
    )
    diag_case = " ".join(
        f'WHEN "{c["code"]}" > 0 THEN "{c["diag"]}"'
        for c in ERROR_COMPONENTS
    )
    return comp_case, code_case, diag_case


def _faulty_union(mark, until, taskids=None):
    """Bounds SQL and parameters for the deduplicated union of faulty
    jobs across the active and archived tables."""
    err_fields = ", ".join(f'"{c["code"]}"' for c in ERROR_COMPONENTS)
    diag_fields = ", ".join(f'"{c["diag"]}"' for c in ERROR_COMPONENTS)
    status_placeholders = ", ".join(["%s"] * len(FAULTY_STATUSES))
    any_nonzero = " OR ".join(
        f'"{c["code"]}" > 0' for c in ERROR_COMPONENTS
    )
    bounds = (
        f'"endtime" <= %s AND "jobstatus" IN ({status_placeholders}) '
        f"AND ({any_nonzero})"
    )
    params = [until, *FAULTY_STATUSES]
    if mark is not None:
        bounds = f'"endtime" > %s AND {bounds}'
        params = [mark, *params]
    if taskids is not None:
        id_placeholders = ", ".join(["%s"] * len(taskids))
        bounds = f'{bounds} AND "jeditaskid" IN ({id_placeholders})'
        params.extend(int(t) for t in taskids)
    union = f"""
        SELECT "pandaid", "jeditaskid", {err_fields}, {diag_fields}
        FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {bounds}
        UNION
        SELECT "pandaid", "jeditaskid", {err_fields}, {diag_fields}
        FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {bounds}
    """
    return union, params + params


def _category_rows(mark, until, taskids=None, per_task=True):
    """(jeditaskid, component, code, count) for faulty jobs ending in
    (mark, until] - or the full recorded history when mark is None -
    optionally restricted to the given task ids. per_task=False groups
    by category only (jeditaskid returned as None)."""
    if taskids is not None and not taskids:
        return []
    comp_case, code_case, _ = _classify_sql()
    union, params = _faulty_union(mark, until, taskids)
    task_col = '"jeditaskid",' if per_task else "NULL,"
    task_group = '"jeditaskid",' if per_task else ""
    sql = f"""
        SELECT {task_col}
               CASE {comp_case} ELSE 'other' END,
               CASE {code_case} ELSE 0 END,
               COUNT(*)
        FROM ({union}) faulty
        GROUP BY {task_group} 2, 3
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _pattern_rows(window_start, until):
    """Top diagnostic patterns among faulty jobs ending in
    (window_start, until]. Grouping collapses digit runs in the
    diagnostic so job-specific paths, ids, and line numbers merge into
    one pattern; the sample diagnostic shown is one member's raw
    text."""
    comp_case, code_case, diag_case = _classify_sql()
    union, params = _faulty_union(window_start, until)
    sql = f"""
        SELECT CASE {comp_case} ELSE 'other' END AS comp,
               CASE {code_case} ELSE 0 END AS code,
               COALESCE(LEFT(regexp_replace(
                   CASE {diag_case} ELSE '' END,
                   '[0-9]+', '#', 'g'), {PATTERN_DIAG_CHARS}), '')
                   AS diag_pattern,
               MIN(COALESCE(LEFT(CASE {diag_case} ELSE '' END,
                                 {PATTERN_DIAG_CHARS}), '')) AS diag,
               COUNT(*) AS count,
               MAX("pandaid") AS representative_pandaid,
               array_agg(DISTINCT "jeditaskid") AS taskids
        FROM ({union}) faulty
        GROUP BY 1, 2, 3
        ORDER BY 5 DESC, 1, 2
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _tracked_tasks(until):
    """Bounded task ids to carry per-task counters for: every
    nonterminal task, then the most recently modified terminal tasks
    within the retention window."""
    terminal_placeholders = ", ".join(["%s"] * len(TASK_TERMINAL_STATUSES))
    sql = f"""
        SELECT "jeditaskid", COALESCE("status", 'unknown'),
               ("status" IS NULL
                OR "status" NOT IN ({terminal_placeholders})) AS in_flight
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE "status" IS NULL
           OR "status" NOT IN ({terminal_placeholders})
           OR "modificationtime" > %s
        ORDER BY in_flight DESC, "modificationtime" DESC
        LIMIT %s
    """
    params = (
        list(TASK_TERMINAL_STATUSES)
        + list(TASK_TERMINAL_STATUSES)
        + [until - timedelta(days=RECENT_FINAL_DAYS), MAX_TASKS]
    )
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, params)
        return {
            int(taskid): {"status": str(status or "unknown")}
            for taskid, status, _ in cursor.fetchall()
        }


def _previous_state():
    """Category and task counter maps and the delta mark from the
    component's current state. Counters and source time advance
    atomically in publication, so counting end times in
    (source_as_of, now] never double-counts across cycles."""
    from snapper_ai.models import CurrentComponent

    row = (CurrentComponent.objects
           .filter(scope=SCOPE, name=COMPONENT_NAME)
           .values("data", "source_as_of").first())
    if not row:
        return {}, {}, None
    data = row["data"] or {}
    categories = {
        str(k): int(v or 0) for k, v in (data.get("categories") or {}).items()
    }
    if not categories:
        return {}, {}, None
    tasks = {}
    for taskid, entry in (data.get("tasks") or {}).items():
        tasks[str(taskid)] = {
            str(k): int(v or 0)
            for k, v in ((entry or {}).get("cum") or {}).items()
        }
    return categories, tasks, row["source_as_of"]


def _category_key(comp, code):
    return f"{comp}:{int(code or 0)}"


def _add(counters, key, count):
    counters[key] = int(counters.get(key) or 0) + int(count or 0)


def _fold_categories(counters, maximum, fold_key="other:0"):
    """Fold the smallest counters into the fold key when the map
    exceeds its bound. Folding only ever adds to the fold key, so the
    folded map stays monotonic."""
    if len(counters) <= maximum:
        return counters
    ranked = sorted(counters.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = dict(ranked[: maximum - 1])
    folded = sum(v for _, v in ranked[maximum - 1:])
    kept[fold_key] = int(kept.get(fold_key) or 0) + folded
    return kept


def errors_projection(now=None):
    """Build the bounded error-state projection without publishing."""
    observed_at = now or timezone.now()
    prev_categories, prev_tasks, mark = _previous_state()
    tracked = _tracked_tasks(observed_at)

    categories = dict(prev_categories)
    task_counters = {}

    if mark is None:
        # Seed: scope counters from the full recorded history, task
        # counters from birth for every tracked task.
        for _, comp, code, count in _category_rows(
                None, observed_at, per_task=False):
            _add(categories, _category_key(comp, code), count)
        seed_ids = set(tracked)
    else:
        # Carry forward tracked tasks' previous counters; tasks no
        # longer tracked leave the map (their history stays in snaps,
        # and scope counters already include their counts).
        for taskid in tracked:
            if str(taskid) in prev_tasks:
                task_counters[taskid] = dict(prev_tasks[str(taskid)])
        seed_ids = {t for t in tracked if str(t) not in prev_tasks}
        for taskid, comp, code, count in _category_rows(
                mark, observed_at):
            key = _category_key(comp, code)
            _add(categories, key, count)
            taskid = int(taskid or 0)
            # Newly tracked tasks are seeded from birth below; adding
            # their increment rows as well would double-count.
            if taskid in task_counters and taskid not in seed_ids:
                _add(task_counters[taskid], key, count)

    for taskid, comp, code, count in _category_rows(
            None, observed_at, taskids=sorted(seed_ids)):
        entry = task_counters.setdefault(int(taskid or 0), {})
        _add(entry, _category_key(comp, code), count)

    categories = _fold_categories(categories, MAX_CATEGORIES)

    tasks = {}
    for taskid, meta in tracked.items():
        cum = _fold_categories(
            task_counters.get(taskid) or {}, MAX_TASK_CATEGORIES)
        tasks[str(taskid)] = {"status": meta["status"], "cum": cum}

    window_start = observed_at - timedelta(minutes=DETAIL_WINDOW_MINUTES)
    patterns = []
    window_errors = 0
    pattern_count = 0
    for comp, code, _diag_pattern, diag, count, rep, taskids in (
            _pattern_rows(window_start, observed_at)):
        window_errors += int(count or 0)
        pattern_count += 1
        if len(patterns) < MAX_PATTERNS:
            patterns.append({
                "category": _category_key(comp, code),
                "diag": str(diag or ""),
                "count": int(count or 0),
                "representative_pandaid": int(rep or 0),
                "tasks": sorted(
                    {int(t) for t in (taskids or []) if t}
                )[:MAX_PATTERN_TASKS],
            })

    projection = {
        "categories": categories,
        "tasks": tasks,
        "detail": {
            "window_minutes": DETAIL_WINDOW_MINUTES,
            "window_errors": window_errors,
            "window_patterns": pattern_count,
            "patterns": patterns,
        },
    }
    serialized = len(json.dumps(projection, separators=(",", ":")))
    if serialized > MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"errors projection serializes to {serialized} bytes, over "
            f"the {MAX_SERIALIZED_BYTES} bound"
        )
    return projection, observed_at


def publish_errors_state() -> ErrorsPublication:
    """Query, curate, and atomically publish the error-state component."""
    projection, observed_at = errors_projection()
    with transaction.atomic():
        try:
            update = publish_component(
                scope=SCOPE,
                name=COMPONENT_NAME,
                publisher_identity=PUBLISHER_IDENTITY,
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
        except ComponentNotFound:
            registration_update = register_component(
                scope=SCOPE,
                name=COMPONENT_NAME,
                publisher_identity=PUBLISHER_IDENTITY,
                registration=ERRORS_REGISTRATION,
                component_schema_version=1,
            )
            update = publish_component(
                scope=SCOPE,
                name=COMPONENT_NAME,
                publisher_identity=PUBLISHER_IDENTITY,
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
        else:
            registration_update = register_component(
                scope=SCOPE,
                name=COMPONENT_NAME,
                publisher_identity=PUBLISHER_IDENTITY,
                registration=ERRORS_REGISTRATION,
                component_schema_version=1,
            )
    return ErrorsPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
        observed_at=observed_at,
    )


def compact_errors_publication_report(publication: ErrorsPublication) -> str:
    detail = publication.projection["detail"]
    return json.dumps(
        {
            "scope": SCOPE,
            "component": COMPONENT_NAME,
            "revision": max(
                publication.update.revision,
                publication.registration_update.revision,
            ),
            "content_changed": publication.update.content_changed,
            "categories": len(publication.projection["categories"]),
            "tracked_tasks": len(publication.projection["tasks"]),
            "window_errors": detail["window_errors"],
            "window_patterns": detail["window_patterns"],
            "observed_at": publication.observed_at.isoformat(),
        },
        indent=2,
        sort_keys=True,
    )
