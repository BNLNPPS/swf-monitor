"""Publish the epicprod error-state component to Snapper.

Design: docs/SNAPPER_ERRORS.md. Each publication records the error
events of one interval: every job that ended faulty within
(previous publication, now], as bounded entry rows of
(pandaid, jeditaskid, category, endtime, status, exitcode, held). The
status is the job's terminal state (failed, cancelled, closed) — the
system's own disposition semantics: closed marks jobs the server
disposed of for workflow reasons, by design not actual errors. The
exit code is the payload's transformation exit code as the pilot
reported it, raw (docs/ERROR_ATTRIBUTION.md): its reading — 78 is the
run script's output upload or registration failure — is applied at
read time, so a reader tells a storage failure whatever label the
pilot gave it. Held is the core-seconds the job held, cores times wall
time from its start to its recorded end by the resource_usage rule
(panda/queries.py), the allocation a faulty job wasted; a job that
never started held nothing. A job reports errors once, upon
completion, so each failed job appears in exactly one interval.
Counts over any period are sums of entry counts over the intervals it
spans; per-task readings filter the same entries by task id. An
interval whose entry count exceeds the bound keeps a representative
subset and folds the exact remainder into per-category, status and
exit-code overflow counts, with the held core-seconds summed under
the same keys, so aggregate counts never lose a job.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.db import connections, transaction
from django.utils import timezone

from snapper_ai.services import (
    ComponentUpdate,
    publish_component,
    register_component,
    report_component_unchanged,
)

from .panda.constants import (
    ERROR_COMPONENTS,
    FAULTY_STATUSES,
    PANDA_SCHEMA,
)

PUBLISHER_IDENTITY = "swf-monitor:panda-errors"
ASSESSMENT_POLICY_VERSION = "swf-panda-errors-v5"
# The entry-row shape: v3 added the terminal status, v4 the payload
# exit code, v5 the core-seconds held. The backfill stamps its snaps
# with the same version.
COMPONENT_SCHEMA_VERSION = 5
COMPONENT_NAME = "errors"
SCOPE = "epicprod"

ENTRY_FIELDS = ["pandaid", "jeditaskid", "category", "endtime", "status",
                "exitcode", "held"]
# The core-seconds a job held: cores times wall time from its start to
# its recorded end (for a lost-heartbeat job the last heartbeat), by
# the resource_usage rule (panda/queries.py); nothing before it
# started, never negative.
HELD_SQL = (
    'CASE WHEN "starttime" IS NOT NULL AND "ended" IS NOT NULL '
    'THEN GREATEST(0, ROUND(EXTRACT(EPOCH FROM ("ended" - "starttime")) '
    '* COALESCE("actualcorecount", "corecount", 1))) ELSE 0 END'
)
# The event time of a faulty job: its end time, except that a
# lost-heartbeat failure (dispatcher code 100) records the last
# heartbeat as the end time and the failure instant as the modification
# time — the failure instant is the event.
EVENT_TIME_SQL = (
    'CASE WHEN "jobdispatchererrorcode" = 100 '
    'THEN "modificationtime" ELSE "endtime" END'
)
MAX_ENTRIES = 2000
MAX_PATTERNS = 20
MAX_PATTERN_TASKS = 8
MAX_PATTERN_SITES = 6
PATTERN_DIAG_CHARS = 160
# A fresh component (or one whose record was reset) starts its first
# interval this far back rather than swallowing all recorded history
# into one interval; earlier history is the backfill's to write.
FIRST_INTERVAL_MINUTES = 5
# A full interval of MAX_ENTRIES rows serializes near 150 KB.
MAX_SERIALIZED_BYTES = 256 * 1024

ERRORS_REGISTRATION = {
    "title": "Curated epicprod PanDA error state",
    "description": (
        "Per-interval PanDA job error events: each publication covers "
        "one interval and records every job that ended faulty within "
        "it, as entry rows of (pandaid, jeditaskid, category, "
        "endtime, status, exitcode, held). The category is "
        "'component:code', classified by the first nonzero error "
        "component in the standard order, so one job lands in one "
        "category. The status is the job's terminal state (failed, "
        "cancelled, closed); closed marks jobs the server disposed of "
        "for workflow reasons, by design not actual errors. The exit "
        "code is the payload's transformation exit code as the pilot "
        "reported it, raw; its reading (78 is the run script's output "
        "upload or registration failure) is applied at read time. "
        "Held is the core-seconds the job held, cores times wall time "
        "from start to recorded end, the allocation a faulty job "
        "wasted. Counts and held sums over any period are sums over "
        "the intervals it spans."
    ),
    "visibility": "public",
    "owning_subsystem": "SWF PanDA production monitor",
    "assessment_policy": ASSESSMENT_POLICY_VERSION,
    "max_serialized_bytes": MAX_SERIALIZED_BYTES,
    "quantities": {
        "interval": {
            "path": "interval",
            "type": "object",
            "required": True,
            "kind": "window",
            "description": (
                "The half-open accrual interval (start, end] this "
                "publication covers. Live intervals run from the "
                "previous publication's source time to this one's; "
                "backfilled intervals are grid-aligned."
            ),
        },
        "entries": {
            "path": "entries",
            "type": "array",
            "required": True,
            "kind": "interval_events",
            "max_items": MAX_ENTRIES,
            "description": (
                "One row per job that ended faulty in the interval, as "
                "arrays in ENTRY_FIELDS order: pandaid, jeditaskid, "
                "category 'component:code', event time, terminal "
                "status, the payload's transformation exit code as a "
                "string, empty where none was reported, and the "
                "core-seconds held (cores times wall time from start "
                "to recorded end; zero for a job that never started). "
                "The event time is the job's end time, except "
                "for lost-heartbeat failures (dispatcher 100), whose "
                "recorded end time is the last heartbeat: their event "
                "time is the failure instant (the modification time). "
                "Each failed job appears in exactly one "
                "interval. When the interval exceeds the entry bound, "
                "entries keep the earliest rows and 'overflow' carries "
                "the exact remainder."
            ),
        },
        "overflow": {
            "path": "overflow",
            "type": "object",
            "required": False,
            "kind": "fold_remainder",
            "description": (
                "Absent normally. In an interval exceeding the entry "
                "bound: 'total' and 'by_category' counts of the rows "
                "not listed in entries, keyed "
                "'component:code@status@exitcode' so status-resolved "
                "and exit-resolved aggregate counts never lose a job, "
                "and 'held_by_category', the summed core-seconds "
                "under the same keys."
            ),
        },
    },
    "entry_fields": ENTRY_FIELDS,
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


def _faulty_union(mark, until, diags=False, sites=False):
    """Bounds SQL and parameters for the deduplicated union of faulty
    jobs ending in (mark, until] across the active and archived
    tables. diags=True adds the diagnostic text columns for pattern
    aggregation; sites=True adds the computing site."""
    err_fields = ", ".join(f'"{c["code"]}"' for c in ERROR_COMPONENTS)
    # The payload's exit code rides every faulty row: the entry rows
    # record it and the patterns profile by it. The start, the recorded
    # end and the core counts give the held core-seconds (HELD_SQL).
    err_fields += (', "transexitcode", "starttime", "endtime" AS "ended", '
                   '"actualcorecount", "corecount"')
    if diags:
        err_fields += ", " + ", ".join(
            f'"{c["diag"]}"' for c in ERROR_COMPONENTS)
    if sites:
        err_fields += ', "computingsite"'
    status_placeholders = ", ".join(["%s"] * len(FAULTY_STATUSES))
    any_nonzero = " OR ".join(
        f'"{c["code"]}" > 0' for c in ERROR_COMPONENTS
    )
    # The event time is when the job ended faulty. For a lost-heartbeat
    # failure the record's end time is the LAST HEARTBEAT (the Watcher's
    # convention); the failure instant is the modification time, so
    # that is the event time for those jobs — otherwise a kill storm
    # lands on the plots hours before it happened.
    bounds = (
        f"{EVENT_TIME_SQL} > %s AND {EVENT_TIME_SQL} <= %s "
        f'AND "jobstatus" IN ({status_placeholders}) '
        f"AND ({any_nonzero})"
    )
    params = [mark, until, *FAULTY_STATUSES]
    union = f"""
        SELECT "pandaid", "jeditaskid", {EVENT_TIME_SQL} AS "endtime",
               "jobstatus", {err_fields}
        FROM "{PANDA_SCHEMA}"."jobsactive4" WHERE {bounds}
        UNION
        SELECT "pandaid", "jeditaskid", {EVENT_TIME_SQL} AS "endtime",
               "jobstatus", {err_fields}
        FROM "{PANDA_SCHEMA}"."jobsarchived4" WHERE {bounds}
    """
    return union, params + params


def error_axes(mark, until, taskids=None, statuses=None):
    """Totals per category, per task, and per site for the faulty
    jobs ending in (mark, until], from one scan (GROUPING SETS) —
    the concentration facts behind the breakdown's attribution
    reading: whether the errors concentrate in one condition, one
    task, or one site, or spread. taskids optionally restricts to a
    task list; statuses to a terminal-state list."""
    comp_case, code_case, _ = _classify_sql()
    union, params = _faulty_union(mark, until, sites=True)
    conditions = []
    if taskids:
        placeholders = ", ".join(["%s"] * len(taskids))
        conditions.append(f'"jeditaskid" IN ({placeholders})')
        params = params + [int(t) for t in taskids]
    status_list = _known_statuses(statuses)
    if status_list:
        placeholders = ", ".join(["%s"] * len(status_list))
        conditions.append(f'"jobstatus" IN ({placeholders})')
        params = params + status_list
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT CASE {comp_case} ELSE 'other' END AS comp,
               CASE {code_case} ELSE 0 END AS code,
               "jeditaskid",
               COALESCE("computingsite", 'unknown') AS site,
               COUNT(*)
        FROM ({union}) faulty
        {where}
        GROUP BY GROUPING SETS ((1, 2), (3), (4))
    """
    categories = {}
    tasks = {}
    sites = {}
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, params)
        for comp, code, taskid, site, count in cursor.fetchall():
            count = int(count or 0)
            if comp is not None:
                categories[_category_key(comp, code)] = count
            elif taskid is not None:
                tasks[int(taskid)] = count
            elif site is not None:
                sites[str(site)] = count
    return {"categories": categories, "tasks": tasks, "sites": sites}


def _known_statuses(statuses):
    """The restriction list for a terminal-state selection: the given
    statuses that live job-record queries can express. Synthetic
    presentation states (e.g. 'unrecorded' for pre-status entry rows)
    have no job-record equivalent and drop out; an empty or full
    selection means no restriction."""
    if not statuses:
        return []
    known = [s for s in statuses if s in FAULTY_STATUSES]
    if len(known) == len(FAULTY_STATUSES):
        return []
    return known


def error_patterns(mark, until, taskid=None, statuses=None):
    """Top diagnostic patterns among faulty jobs ending in
    (mark, until], optionally restricted to one task and/or a
    terminal-state list: category, sample diagnostic, count,
    representative PanDA job id, affected task ids, sites, and the
    pattern's payload exit-code profile (transexitcode counts — the
    correction root refines unreliable labels from it), most frequent
    first. Digit runs collapse in the pattern grouping so job-specific
    paths, ids, and line numbers merge into one pattern; the sample
    shown is one member's raw text. Aggregated live from the job
    records — the errors view's breakdown calls this at cut time
    (docs/SNAPPER_ERRORS.md)."""
    comp_case, code_case, diag_case = _classify_sql()
    union, params = _faulty_union(mark, until, diags=True, sites=True)
    conditions = []
    if taskid is not None:
        conditions.append('"jeditaskid" = %s')
        params = params + [int(taskid)]
    status_list = _known_statuses(statuses)
    if status_list:
        placeholders = ", ".join(["%s"] * len(status_list))
        conditions.append(f'"jobstatus" IN ({placeholders})')
        params = params + status_list
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        WITH classified AS (
            SELECT CASE {comp_case} ELSE 'other' END AS comp,
                   CASE {code_case} ELSE 0 END AS code,
                   COALESCE(LEFT(regexp_replace(
                       CASE {diag_case} ELSE '' END,
                       '[0-9]+', '#', 'g'), {PATTERN_DIAG_CHARS}), '')
                       AS diag_pattern,
                   COALESCE(LEFT(CASE {diag_case} ELSE '' END,
                                 {PATTERN_DIAG_CHARS}), '') AS diag,
                   "pandaid", "jeditaskid", "computingsite",
                   "transexitcode"
            FROM ({union}) faulty
            {where}
        ),
        pat AS (
            SELECT comp, code, diag_pattern,
                   MIN(diag) AS diag,
                   COUNT(*) AS count,
                   MAX("pandaid") AS representative_pandaid,
                   array_agg(DISTINCT "jeditaskid") AS taskids,
                   array_agg(DISTINCT COALESCE("computingsite", 'unknown'))
                       AS sites
            FROM classified
            GROUP BY 1, 2, 3
        ),
        exits AS (
            SELECT comp, code, diag_pattern,
                   COALESCE("transexitcode", '') AS exitcode,
                   COUNT(*) AS n,
                   MAX("pandaid") AS rep
            FROM classified
            GROUP BY 1, 2, 3, 4
        )
        SELECT p.comp, p.code, p.diag_pattern, p.diag, p.count,
               p.representative_pandaid, p.taskids, p.sites,
               COALESCE((
                   SELECT jsonb_object_agg(
                       x.exitcode,
                       jsonb_build_object('n', x.n, 'rep', x.rep))
                   FROM exits x
                   WHERE x.comp = p.comp AND x.code = p.code
                     AND x.diag_pattern = p.diag_pattern
                     AND x.exitcode != ''
               ), '{{}}'::jsonb) AS exit_counts
        FROM pat p
        ORDER BY p.count DESC, p.comp, p.code
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _entry_rows(mark, until):
    """(pandaid, jeditaskid, comp, code, endtime, status, exitcode,
    held) for faulty jobs ending in (mark, until], in endtime order."""
    comp_case, code_case, _ = _classify_sql()
    union, params = _faulty_union(mark, until)
    sql = f"""
        SELECT "pandaid", "jeditaskid",
               CASE {comp_case} ELSE 'other' END,
               CASE {code_case} ELSE 0 END,
               "endtime", "jobstatus", "transexitcode", {HELD_SQL}
        FROM ({union}) faulty
        ORDER BY "endtime", "pandaid"
    """
    with connections["panda"].cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _previous_source_time():
    """The interval start for the next publication. Source time and
    content advance atomically in publication, so intervals tile with
    no gap or double count across cycles."""
    from snapper_ai.models import CurrentComponent

    row = (CurrentComponent.objects
           .filter(scope=SCOPE, name=COMPONENT_NAME)
           .values("source_as_of").first())
    return row["source_as_of"] if row else None


def _iso_utc(value):
    """ISO-8601 Z-suffixed UTC. Naive values are UTC by convention
    (the PanDA database stores naive UTC end times)."""
    if value.tzinfo is not None:
        value = value.astimezone(dt_timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _category_key(comp, code):
    return f"{comp}:{int(code or 0)}"


def _exit_key(exitcode):
    """The payload exit code as the entry records it: the stored text
    stripped, empty where the pilot reported none."""
    return str(exitcode or '').strip()


def errors_projection(now=None, mark=None):
    """Build the one-interval error projection without publishing.

    The interval is (mark, now]. When mark is not given it comes from
    the component's current source time; a fresh record starts with a
    short first interval rather than swallowing all recorded history
    (earlier history is the backfill's to write).
    """
    observed_at = now or timezone.now()
    if mark is None:
        mark = _previous_source_time()
    if mark is None:
        mark = observed_at - timedelta(minutes=FIRST_INTERVAL_MINUTES)

    entries = []
    overflow_total = 0
    overflow_categories = {}
    overflow_held = {}
    for pandaid, taskid, comp, code, endtime, status, exitcode, held in (
            _entry_rows(mark, observed_at)):
        category = _category_key(comp, code)
        if len(entries) < MAX_ENTRIES:
            entries.append([
                int(pandaid or 0),
                int(taskid or 0),
                category,
                _iso_utc(endtime),
                str(status or ''),
                _exit_key(exitcode),
                int(held or 0),
            ])
        else:
            overflow_total += 1
            fold_key = f"{category}@{status or ''}@{_exit_key(exitcode)}"
            overflow_categories[fold_key] = (
                overflow_categories.get(fold_key) or 0) + 1
            overflow_held[fold_key] = (
                overflow_held.get(fold_key) or 0) + int(held or 0)

    projection = {
        "interval": {"start": _iso_utc(mark), "end": _iso_utc(observed_at)},
        "entries": entries,
    }
    if overflow_total:
        projection["overflow"] = {
            "total": overflow_total, "by_category": overflow_categories,
            "held_by_category": overflow_held}
    serialized = len(json.dumps(projection, separators=(",", ":")))
    if serialized > MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"errors projection serializes to {serialized} bytes, over "
            f"the {MAX_SERIALIZED_BYTES} bound"
        )
    return projection, observed_at


def _previous_data_is_current_shape():
    """Whether the component's current content already has the
    current entry-row shape (every ENTRY_FIELDS column). Until it
    does, a quiet interval still publishes, so the shape transition
    lands as real content."""
    from snapper_ai.models import CurrentComponent

    row = (CurrentComponent.objects
           .filter(scope=SCOPE, name=COMPONENT_NAME)
           .values("data").first())
    if not (row and isinstance(row["data"], dict)
            and "entries" in row["data"]):
        return False
    rows = row["data"]["entries"]
    return not rows or len(rows[0]) >= len(ENTRY_FIELDS)


def publish_errors_state() -> ErrorsPublication:
    """Query, curate, and atomically publish the error-state component.

    An interval with no errors is affirmed unchanged with its source
    time advanced, so quiet periods write no snaps while the next
    errorful interval still starts where the record left off.
    """
    projection, observed_at = errors_projection()
    quiet = (not projection["entries"] and not projection.get("overflow")
             and _previous_data_is_current_shape())
    with transaction.atomic():
        # Registration first: reconciliation is idempotent, and the
        # publication validates against the registration on record —
        # publishing first would validate new-shape data against a
        # superseded definition and fail.
        registration_update = register_component(
            scope=SCOPE,
            name=COMPONENT_NAME,
            publisher_identity=PUBLISHER_IDENTITY,
            registration=ERRORS_REGISTRATION,
            component_schema_version=COMPONENT_SCHEMA_VERSION,
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
                data=projection,
                assessed_at=observed_at,
                source_as_of=observed_at,
                assessment_policy_version=ASSESSMENT_POLICY_VERSION,
            )
    return ErrorsPublication(
        registration_update=registration_update,
        update=update,
        projection=projection,
        observed_at=observed_at,
    )


def compact_errors_publication_report(publication: ErrorsPublication) -> str:
    projection = publication.projection
    overflow = projection.get("overflow") or {}
    return json.dumps(
        {
            "scope": SCOPE,
            "component": COMPONENT_NAME,
            "revision": max(
                publication.update.revision,
                publication.registration_update.revision,
            ),
            "content_changed": publication.update.content_changed,
            "interval": projection["interval"],
            "entries": len(projection["entries"]),
            "overflow_total": int(overflow.get("total") or 0),
            "observed_at": publication.observed_at.isoformat(),
        },
        indent=2,
        sort_keys=True,
    )
