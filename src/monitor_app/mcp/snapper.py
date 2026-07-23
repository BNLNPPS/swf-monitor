"""
Snapper temporal-query MCP tools — thin wrappers over snapper_ai.queries.

Snapper records immutable, coherent snapshots ("snaps") of system-wide
state at aligned opportunities, turning "what did the system look like
at time T" from an inference problem into a retrieval problem. These
tools return the typed evidence envelope unchanged; they never infer
continuity that was not observed.
"""

from django.utils.dateparse import parse_datetime

from asgiref.sync import sync_to_async

from monitor_app.mcp import mcp
from snapper_ai import queries


def _time(raw, label):
    value = parse_datetime(str(raw or '').strip())
    if value is None or value.tzinfo is None:
        raise queries.InvalidQuery(
            f"{label} must be ISO 8601 with timezone, "
            f"e.g. '2026-07-23T04:00:00Z'")
    return value


def _call(fn):
    try:
        return fn().as_dict()
    except queries.SnapperError as e:
        return {'error': str(e)}


@mcp.tool()
async def snapper_latest(scope: str) -> dict:
    """
    Latest recorded system state for one Snapper scope.

    Use this to know what the whole system looked like at the most recent
    coherent capture — component states with their assessment times, not
    live probes.

    Args:
        scope: 'epicprod' (PanDA/production) or 'testbed'.

    Reading the result (shared by all snapper_* tools):
        state: the recorded component state documents, each with its own
            assessment time and, when applicable, source time.
        snap_time: when the returned state was ACTUALLY captured — always
            report this actual time.
        coverage: 'covered' means the observer was recording; 'gap' means
            a known observation gap (state across it is unknown);
            'unknown' means the evidence cannot say. Never treat gap or
            unknown intervals as if the last state persisted through them.
        Schema/policy versions, hashes, and provenance identify how to
            interpret each snap; old snaps keep the shape that was true
            when captured.
    """
    return await sync_to_async(_call)(lambda: queries.latest(scope))


@mcp.tool()
async def snapper_state_at(scope: str, time: str) -> dict:
    """
    Recorded system state at (or last before) a past instant.

    Use this for questions like "what was running when the incident
    began?". The answer is the latest snap at or before the requested
    time, returned with its ACTUAL snap time and honest coverage — it
    does not pretend the state was observed at the requested instant.

    Args:
        scope: 'epicprod' (PanDA/production) or 'testbed'.
        time: the past instant, ISO 8601 with timezone, e.g.
            '2026-07-22T14:30:00Z'.

    Reading the result: state documents plus snap_time (the actual
    capture time, possibly earlier than requested) and coverage —
    'covered', 'gap' (known observation gap; state across it is
    unknown), or 'unknown'. Never present state across a gap or unknown
    interval as observed fact.
    """
    return await sync_to_async(_call)(
        lambda: queries.state_at(scope, _time(time, 'time')))


@mcp.tool()
async def snapper_component_history(
    scope: str,
    component: str,
    start: str,
    end: str,
    include_unchanged: bool = False,
) -> dict:
    """
    One component's evolution over an interval.

    The first entry is the component's state at the interval start (with
    its actual snap time); subsequent entries are recorded changes.
    Absence and appearance are explicit; recovery evidence is never
    suppressed.

    Args:
        scope: 'epicprod' (PanDA/production) or 'testbed'.
        component: registered component name — 'health' (either scope),
            'datataking' (testbed), or 'panda' (epicprod).
        start: interval start, ISO 8601 with timezone.
        end: interval end, ISO 8601 with timezone.
        include_unchanged: also return semantically unchanged baseline
            entries (default False).

    Reading the result: entries carry actual snap times, content hashes,
    revisions, and schema versions; coverage is reported at both
    requested endpoints ('covered', 'gap', or 'unknown'). Never treat a
    gap or unknown interval as continuity of the last recorded value.
    """
    return await sync_to_async(_call)(
        lambda: queries.component_history(
            scope, component, _time(start, 'start'), _time(end, 'end'),
            suppress_unchanged_baselines=not include_unchanged))


@mcp.tool()
async def snapper_context_around(scope: str, time: str,
                                 window_seconds: float = 3600) -> dict:
    """
    Full temporal context at an instant: coherent state, nearby changes,
    and resolvable references to the exact event streams.

    Use this first when investigating an incident time: it returns the
    recorded system state at the instant, every component change in the
    window around it, and for each component a reference naming the
    authoritative service (REST URL and MCP tools in the reference's
    transport field) that holds the exact transitions — drill down
    there for event-level truth.

    Args:
        scope: 'epicprod' (PanDA/production) or 'testbed'.
        time: the instant, ISO 8601 with timezone.
        window_seconds: window centered on the instant (default 3600).

    Reading the result: state carries its ACTUAL snap time and coverage
    ('covered', 'gap', 'unknown' — never infer continuity across gap or
    unknown intervals); references carry availability and a transport
    with rest_url, rest_params, and mcp_tools naming exactly how to
    fetch the underlying events.
    """
    from monitor_app.snapper_resolvers import annotate_references

    def call():
        result = queries.context_around(
            scope, _time(time, 'time'), window_seconds).as_dict()
        result['references'] = annotate_references(result['references'])
        return result

    def guarded():
        try:
            return call()
        except queries.SnapperError as e:
            return {'error': str(e)}

    return await sync_to_async(guarded)()


@mcp.tool()
async def snapper_changes_between(scope: str, start: str, end: str) -> dict:
    """
    What changed across the whole system between two moments.

    Every component difference is classified added, changed, or removed,
    with previous and current documents, hashes, and versions.
    Value-identical baselines are omitted; recovery and capture-policy
    transitions remain as evidence.

    Args:
        scope: 'epicprod' (PanDA/production) or 'testbed'.
        start: comparison boundary, ISO 8601 with timezone.
        end: interval end, ISO 8601 with timezone.

    Reading the result: the comparison boundary snap and its actual time
    are returned with the changes; coverage is reported at both
    requested endpoints ('covered', 'gap', or 'unknown'). Never treat a
    gap or unknown interval as if nothing changed within it.
    """
    return await sync_to_async(_call)(
        lambda: queries.changes_between(
            scope, _time(start, 'start'), _time(end, 'end')))
