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

A component, internal name `errors`, in the epicprod scope, published
by a maintainer module beside the existing PanDA activity maintainer
(`monitor_app/snapper_panda.py`) on the same 5-minute System-status
refresh. Five minutes is the floor; the cadence is governed by the
existing SysConfig capture policy and is raised, not lowered, if the
component proves heavy.

Each publication records the error events of one interval:

- **Interval** — the half-open interval (start, end] the publication
  covers, running from the previous publication's source time to this
  one's.
- **Entries** — one row per job that ended faulty in the interval:
  PanDA job id, JEDI task id, category, and end time, as arrays in a
  declared column order. A job reports errors once, upon completion,
  so each failed job appears in exactly one interval.
- **Overflow** — absent normally. An interval exceeding the entry bound
  (2,000 rows) keeps the earliest rows and folds the exact remainder
  into per-category counts, so aggregate counts never lose a job
  while the per-job listing stays bounded in storm floods.

The publisher is stateless: each pass reads the interval's faulty jobs
from the PanDA job records. Counts over any period are sums of entry
counts over the intervals it spans, and per-task readings filter the
same entries by task id — no counters are stored. An interval with no
errors is affirmed unchanged, advancing the source time with no new
snap, so quiet periods cost nothing while the interval chain stays
gapless. A missed or delayed publication loses nothing: the following
interval covers the gap.

### Backfill

The recorded job history carries end times and error fields for every
failed job, so the interval record is reconstructible for any past
period. The backfill script (`scripts/backfill-errors-entries.py`)
writes synthetic errors snaps on the 5-minute grid over the trailing
30 days: one snap per non-empty interval, with capture policy
`backfill-errors-v1` marking reconstructed evidence as distinct from
observed snaps. The backfilled record tiles exactly against the start
of the first live interval, so each failed job lands in exactly one
interval across the seam. The script is idempotent — a re-run
replaces prior backfill — and dry-run by default. The deployment
order is maintainer first, backfill after the first live publication.

## The errors view

A dedicated Snapper page for error history, using the focus-view
mechanism that serves the campaign page: its own clean path under the
epicprod scope, its own curve families, and its own detail rendering.
The error families are not added to the epicprod Time history report
page, which carries its own distinct information; dashboard
compositions may combine elements of both.

**Plot.** The category flood quilt: recorded error events by
category, stacked. The server bins events once, by each job's end
time, into sparse bins at the native 5-minute cadence; the page bins
those into the display rung — the smallest of 5, 10, 15, 20, 30, and
60 minutes keeping the plotted extent at or under 720 columns — and
re-bins in place as the view zooms, down to the native bins, with no
further server work. Every rung is an exact sum of native bins. A
grouping selector switches between the seven-component grouping and
the full component × code categories. Member tick boxes are omitted:
identification lives in hover and the breakdown below.

**Share donut.** Category shares of the accruals within the display
bounds, rendered with the annular SVG donut the site view's detail
section established (`_site_outcomes_pie`,
`monitor_app/snapper_providers.py`) — the at-a-glance signal beside
the tables. The donut follows the display bounds and the active
filters.

**Detail below the plot: the error breakdown.** A click on the plot is
a time cut. The detail section renders the error breakdown around
that moment, integrated over a window at least an hour wide — a
single 5-minute interval is too sparse to read — organized by
category:

- error counts and shares per category within the window;
- the window's top diagnostic patterns, aggregated live from the job
  records;
- representative job links — the job page, payload log, and job study;
- the affected tasks, as detail within each category's section;
- a link to the `/panda/errors/` pattern table windowed to the same
  bounds, for the full aggregation over live job records.

The breakdown is organized by error, not by task; the task reading
comes from the filter.

**Task filter.** A task selection (URL parameter, so the view is
bookmarkable and linkable) narrows the plot, donut, and breakdown to
that task's events — the per-task error history is the overall view
filtered, not a separate surface. The parameter is open: any task id
reached by link is valid, and no task list is offered on the view
itself. The PanDA task page (`panda/tasks/<jeditaskid>/`) links to
its filtered errors view. Refinements specific to the per-task
reading come later; the filter is the mechanism from the start.

## Proactive storm response

Planned, the next stage of this design: the recorded error stream is
the trigger surface for automatic storm response — detection,
notification, and bounded automatic investigation that prepares
information for human evaluation. Investigation latency is accepted
by design; the alternative cost is operator time.

### Storm detection

A detector rides the component publisher: every publication counts
one interval, and the detector evaluates it against the trailing
baseline. A storm starts when an interval's errors exceed the larger
of an absolute floor and a multiple of the trailing median; it ends
after a run of quiet intervals. Detection is stateful and emits on
transitions — storm start, escalation, storm end — never per
interval. Thresholds are SysConfig keys, present at their defaults.
The detector makes the maintainer a canary in place: it already
reads every interval of the production error stream, so no separate
sentinel is needed.

Silence is itself a signal. An infrastructure incident can stall the
maintainer's own pass, and the canary then reports nothing precisely
when it matters most. A freshness watch on the errors publication
emits through the same router when the component goes quiet beyond a
few cycles, at a severity distinct from a storm.

### Notice and alarm

Storm start emits a Capcom notice through the notice router carrying
the attribution reading — the category, task, and site concentration
verdicts the breakdown card computes — and a link to the errors view
windowed to the storm. Storm end reports totals. An alarm fires only
above a second, higher threshold, itself a configuration value.

### Bounded drilldown

Storm start also triggers the deterministic investigation tier
through the production-operations agent: Bamboo log classification
(the classify_failure analysis of the refinement tiers above) on one
representative job per top diagnostic pattern, with results entering
the action stream and enriching the notice. Directed canary probes
join this tier when canary jobs exist (site-canary). The work is
bounded per storm: a fixed number of representative jobs, one pass
per transition.

### AI analysis tier

An AI pass over the mined material — summarizing, hypothesizing,
drafting the evaluation brief — is the tier where analysis beyond
deterministic tooling can add value, applied only where the
deterministic tiers stop. The apparatus exists: a threshold-gated,
case-specific action launches a corun-ai evaluation of the storm
dossier — the attribution reading, the diagnostic patterns, and the
drilldown results — and the returned evaluation document links from
the notice for human review. Spend and gating remain explicit
operator configuration; the tiers above stand on their own without
it.

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
- Provider additions in `monitor_app/snapper_providers.py`: event
  extraction from the interval entries into category, component, and
  per-task curves; the errors focus view with its grouping selector
  and open task parameter; the breakdown card; the donut context.
- Generic mechanisms in the snapper-ai package: the event_values
  provider hook and event-flow rendering (`snapper_ai/series.py` and
  the observatory template), and the open_option focus hook
  (`snapper_ai/views.py`).
- Curve identifiers carry the category vocabulary; any vocabulary
  change bumps the series cache version per the standing rule.
- Component bounds: entries capped per interval with exact overflow
  folding. The component stays curated, bounded JSON per the Snapper
  design contract.

## Related

- [SNAPPER.md](SNAPPER.md) — Snapper operations in SWF: capture
  scheduler, component maintainers, web presentation.
- [ARCHITECTURE_MAP.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/ARCHITECTURE_MAP.md)
  — platform placement of the PanDA monitoring layer.
- `monitor_app/panda/queries.py` — `error_summary`, `study_job`; the
  live-record aggregations the view links into.
- [bamboo-mcp](https://github.com/BNLNPPS/bamboo-mcp) — the
  log-analysis classification used for progressive refinement.
