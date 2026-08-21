# Snapper Errors — error-state component and errors view

PanDA job errors become part of the recorded system history: a curated
error-state component captured in Snapper snaps, and a dedicated errors
view over that history showing the time development of errors by
category — error floods as they rise — with drilldown from any moment
into the error breakdown and the underlying jobs. A per-task reading of
the same history is a filter on the view, and the PanDA task page links
to the filtered view, answering "when did this error show up in this
task" directly.

This is a design document; the sections below are the agreed plan of
record for implementation. It builds on the Snapper concepts and SWF
deployment in [SNAPPER.md](SNAPPER.md) and the generic package
documentation in the snapper-ai repository
([INTEGRATION.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/INTEGRATION.md)).
The error machinery it records is the PanDA monitoring layer of
swf-monitor — platform infrastructure per the architecture map
([ARCHITECTURE_MAP.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/ARCHITECTURE_MAP.md)),
serving every PanDA-using domain.

## Error categorization

The category vocabulary is the foundation; the view reports whatever
the vocabulary distinguishes.

PanDA records job errors in seven component fields, each an error code
plus a diagnostic string: brokerage, ddm, executor, dispatcher, pilot,
supervisor, and taskbuffer (`ERROR_COMPONENTS`,
`monitor_app/panda/constants.py`). The existing error summary
(`error_summary`, `monitor_app/panda/queries.py`; the
`/panda/errors/` page and `panda_error_summary` MCP tool) aggregates
failed jobs into patterns of component × code × leading diagnostic
text, with a classified mode that attributes each job to its first
nonzero component so one job lands in one category.

The Snapper category is **component × error code**:

- The code is the stable classification the producing subsystem
  itself assigned; it is available on every failed job record with no
  latency.
- Category labels come from the component code catalogs — the pilot's
  `errorcodes.py` message table and the equivalent server-side code
  definitions — so categories read as named conditions, not bare
  numbers.
- Diagnostic strings vary per job (paths, hostnames, identifiers) and
  are therefore moment detail presented in the breakdown, never part
  of category identity.

Every failed job row carries `jeditaskid` alongside the error fields,
so the same categorization aggregates per task, per site, or per any
other job attribute without additional classification work.

### Progressive refinement

Two refinement tiers improve the vocabulary over time without changing
the recorded history's structure:

- **Log-derived classification.** The Bamboo
  ([BNLNPPS/bamboo-mcp](https://github.com/BNLNPPS/bamboo-mcp))
  `classify_failure` analysis — already applied per job in
  `panda_study_job` — fetches a pilot-log excerpt and keyword-matches
  it together with the error fields into semantic failure categories
  such as `stagein_timeout`. Applied once per new pattern signature
  (one representative job, result cached), it annotates categories
  with a log-informed reading at a latency the 5-minute capture
  cadence absorbs.
- **Error-state knowledge base.** Accumulated category annotations,
  representative cases, and their resolutions form the error
  knowledge base foreseen for the site-canary buildout. The snap
  history is its evidence store; nothing in this design needs to
  change to feed it.

## The error-state component

A new component, internal name `errors`, in the epicprod scope,
published by a maintainer module beside the existing PanDA activity
maintainer (`monitor_app/snapper_panda.py`) on the same 5-minute
System-status refresh. Five minutes is the floor; the cadence is
governed by the existing SysConfig capture policy and is raised, not
lowered, if the component proves heavy.

Component content per publication:

- **Scope counters** — running failed-job counts per category.
- **Task counters** — running counts per task × category, for tasks
  in flight plus tasks final within a trailing retention window. A
  task leaving the retention window leaves the component; its
  recorded history remains in the snaps.
- **Moment detail** — for each currently active category: the top
  diagnostic patterns (component, code, diagnostic snippet,
  representative PanDA job id, affected task ids), folded to a
  bounded top-N with an explicit remainder entry.

The publisher is stateless: each pass recounts from the PanDA job
records. The published values are running counters; the recorded
quantity of interest is the **accrual within each snap interval**, read
as the difference between consecutive snaps. This is the established
counter-flow form (`counter_flow` families, `snapper_ai/series.py`,
used by the site-completions curves):

- Accruals are additive: any display bound — an hour, a day, the
  page's date range, a render bin — is a sum of snap-interval
  contributions, computed as a subtraction of counter values.
- A missed or delayed publication loses nothing; the gap's accrual
  lands in the following interval.
- A quiet interval leaves the component unchanged, so no snap is
  written; quiet periods cost nothing.
- No displayed quantity is a running total: every number, curve, and
  share shown is the accrual within declared bounds of interest.

Because the publisher recounts from job records, the component's first
publication is correct immediately; history before the component
existed remains recoverable from the job records themselves, which
carry end times and error fields for every failed job.

## The errors view

A dedicated Snapper page for error history, using the focus-view
mechanism that serves the campaign page: its own clean path under the
epicprod scope, its own curve families, and its own detail rendering.
The error families are not added to the epicprod Time history report
page, which carries its own distinct information; dashboard
compositions may combine elements of both.

**Plot.** The category flood quilt: per-interval accruals by category,
stacked, binned at render by the counter-flow projection. A grouping
selector switches between the seven-component grouping and the full
component × code categories; low-share categories fold into a labeled
remainder band per the established quilt laws.

**Share donut.** Category shares of the accruals within the display
bounds, rendered with the annular SVG donut the site view's detail
section established (`_site_outcomes_pie`,
`monitor_app/snapper_providers.py`) — the at-a-glance signal beside
the tables. The donut follows the display bounds and the active
filters.

**Detail below the plot: the error breakdown.** A click on the plot is
a time cut. The detail section renders the error breakdown at that
moment, organized by category:

- interval accrual and share per category;
- the stored top diagnostic patterns with snippets;
- representative job links — the job page, payload log, and job study;
- the affected tasks, as detail within each category's section;
- a link to the `/panda/errors/` pattern table windowed to the
  interval, for the full aggregation over live job records.

The breakdown is organized by error, not by task; the task reading
comes from the filter.

**Task filter.** A task selection (URL parameter, so the view is
bookmarkable and linkable) narrows the plot, donut, and breakdown to
that task's counters — the per-task error history is the overall view
filtered, not a separate surface. The PanDA task page
(`panda/tasks/<jeditaskid>/`) links to its filtered errors view.
Refinements specific to the per-task reading come later; the filter is
the mechanism from the start.

## Retrieval

The component rides the existing Snapper retrieval surface unchanged:
the REST endpoints and MCP tools (`snapper_component_history`,
`snapper_state_at`, `snapper_changes_between`) answer when a category
appeared, grew, or stopped in a scope or a task, with the standard
evidence envelopes. Assessment harnesses and other AI consumers read
the same history the view renders.

## Implementation notes

- Maintainer module `monitor_app/snapper_errors.py`, invoked from the
  System-status refresh beside `publish_panda_activity`; publication
  errors fail visibly per the existing maintainer convention.
- Provider additions in `monitor_app/snapper_providers.py`: the error
  curve families (counter-flow, folding), the focus-view declaration
  with its grouping and task selectors, the breakdown card sections,
  and the donut context.
- Curve identifiers carry the category vocabulary; any vocabulary
  change bumps the series cache version per the standing rule.
- Component bounds: counters keyed by active tasks (typically tens)
  and categories; moment detail folded to top-N. The component stays
  curated, bounded JSON per the Snapper design contract.

## Related

- [SNAPPER.md](SNAPPER.md) — Snapper operations in SWF: capture
  scheduler, component maintainers, web presentation.
- [ARCHITECTURE_MAP.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/ARCHITECTURE_MAP.md)
  — platform placement of the PanDA monitoring layer.
- `monitor_app/panda/queries.py` — `error_summary`, `study_job`; the
  live-record aggregations the view links into.
- [bamboo-mcp](https://github.com/BNLNPPS/bamboo-mcp) — the
  log-analysis classification used for progressive refinement.
