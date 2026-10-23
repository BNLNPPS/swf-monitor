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

    Counting job outcomes over an interval: the epicprod panda
    component carries monotonic cumulative terminal-job counters —
    jobs.cum (finished, failed, cancelled, closed) and, per site,
    jobs.sites.<site>.cum plus jobs.sites.<site>.cum_failed_by_class
    (error component classes). Subtract the counter at start from the
    counter at end to count that interval's outcomes, e.g. how many
    jobs finished and failed at one site during a production test and
    which failure classes dominated.
    """
    return await sync_to_async(_call)(
        lambda: queries.changes_between(
            scope, _time(start, 'start'), _time(end, 'end')))


# View products as queries (snapper-ai PLAN.md section 9): the same
# series and cut summary the page renders, as data.

def _call_dict(fn):
    try:
        return fn()
    except queries.SnapperError as e:
        return {'error': str(e)}


@mcp.tool()
async def snapper_series(scope: str, focus: str, window: str = '24h',
                         selection: str = '', selectors: dict = None) -> dict:
    """
    A Snapper focus view's series product — the curves the page plots,
    as data — over a named window.

    Use this instead of walking component history when the question is
    about time histories the views already distill: the Platform view's
    heartbeat yield (60-min window), DB activity, latency curves; the
    Site view's per-site in-flight and outcome curves; the Errors view's
    event flow; the Campaign view's delivery quilt.

    Args:
        scope: 'epicprod' or 'testbed'.
        focus: the view's name as on its tab: 'platform', 'site',
            'errors', 'campaign'.
        window: '6h', '24h', '48h', '7d', '14d', or '30d'.
        selection: the view's option value(s), comma-separated, when the
            view has them (a site name for 'site', a task id for
            'errors', a campaign for 'campaign'); default: the view's
            default option.
        selectors: {param: value} for the view's selector axes (for
            'platform', lens='tiers' or 'sites'); default: each axis's
            default.

    Reading the result:
        curves: {curve id: {label, points: [[stamp, value], ...]}} with
            stamps in the series' timezone (ET). Window-relative
            counters rise from zero at the window's left edge. Derived
            curves (e.g. plhy_window, the 60-min yield) are computed by
            the same transform the page uses.
        families: the family declarations naming the curves and how the
            page groups and stacks them.
        gaps: recorded observation gaps — the record has no state
            across them.
        cache: the product's cache state; 'refreshing' True means a
            newer build is landing behind this one.
    """
    from snapper_ai.products import series_product

    return await sync_to_async(_call_dict)(
        lambda: series_product(scope, focus, window=window,
                               selection=selection or None,
                               selectors=selectors or None))


@mcp.tool()
async def snapper_cut_summary(scope: str, focus: str, time: str,
                              since: str = '') -> dict:
    """
    The summary at a time cut of a Snapper focus view, as data: every
    metric the view plots with its value at the instant, its change
    against the previous snap, and its min/mean/max over the window.

    This is the table the page shows beneath the plots at a cut — the
    distilled reading of related parameters at one instant — served in
    the evidence envelope (actual snap time, coverage, provenance).
    Today the Platform view registers it.

    Args:
        scope: 'epicprod' or 'testbed'.
        focus: the view's name as on its tab, e.g. 'platform'.
        time: ISO 8601 with timezone; the cut instant.
        since: ISO 8601 with timezone; the window basis for the
            statistics and the outcome accumulations (default: 24 hours
            before the cut).

    Reading the result:
        snap_time: when the summarized state was ACTUALLY captured —
            report this, not the requested time, when they differ.
        coverage: the observer's coverage at the requested instant.
        summary.rows: one row per metric in panel order — label, unit,
            'raw' (the number), 'value' (formatted), 'previous_raw',
            'delta', 'stats' (min/mean/max raw and formatted, position
            of the value in that range, sample count), 'warn' (the
            metric's threshold is crossed at the cut).
        summary.verdicts: the component's own per-metric verdicts.
    """
    from snapper_ai.products import cut_summary

    return await sync_to_async(_call_dict)(
        lambda: cut_summary(scope, focus, _time(time, 'time'),
                            since=_time(since, 'since') if since else None))
