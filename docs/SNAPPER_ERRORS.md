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

### Terminal states

The job's terminal status is a second classification axis, recorded
per entry. PanDA's own kill path assigns it with distinct semantics
(`job_complex_module.py`, panda-server):

- **failed** — the job ran and ended in error: an actual error.
- **cancelled** — a person or controlling system deliberately killed
  the job.
- **closed** — the server disposed of the job for its own workflow
  reasons: pending expiry, reassignment, rebrokerage, task-done
  kills. By design not an actual error; a flood of closures signals
  an infrastructure condition (a stalled daemon, generation
  outrunning dispatch), not a payload problem.

Error presentations therefore exclude closed jobs by default and
offer the terminal states as a filter, discovered from the recorded
data with per-state counts always visible — a closure storm announces
itself in its count without displacing the actual errors. The
recorded history and the retrieval surface carry every state; the
default applies to presentation only.

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
  PanDA job id, JEDI task id, category, event time, and terminal
  status, as arrays in a declared column order. The event time is the
  job's end time, with one exception: a lost-heartbeat failure
  (dispatcher code 100) records the last heartbeat as its end time
  and the failure instant only as its modification time, so the
  failure instant is its event time — otherwise a kill storm appears
  on the plots hours before it happened. A job reports errors once,
  upon completion, so each failed job appears in exactly one
  interval. Since 2026-09-06 each row also carries the payload's
  transformation exit code, raw as the pilot reported it and empty
  where none was reported: the population-wide channel of
  [ERROR_ATTRIBUTION.md](ERROR_ATTRIBUTION.md), by which a reader
  tells a job that failed at output upload or registration (the run
  script's coded exit 78) whatever label the pilot gave it. The
  record stays raw; the code's reading is applied at read time. The
  first reader is the Storage view's consequences strip
  ([SNAPPER_STORAGE.md](SNAPPER_STORAGE.md)). At schema version 5 each
  row also carries the core-seconds the job held (Wasted resources
  below).
- **Overflow** — absent normally. An interval exceeding the entry bound
  (2,000 rows) keeps the earliest rows and folds the exact remainder
  into counts keyed `category@status@exitcode`, so status-resolved and
  exit-resolved aggregate counts never lose a job while the per-job
  listing stays bounded in storm floods.

The publisher is stateless: each pass reads the interval's faulty jobs
from the PanDA job records. Counts over any period are sums of entry
counts over the intervals it spans, and per-task readings filter the
same entries by task id — no counters are stored. An interval with no
errors is affirmed unchanged, advancing the source time with no new
snap, so quiet periods cost nothing while the interval chain stays
gapless. A missed or delayed publication loses nothing: the following
interval covers the gap.

### Backfill

The recorded job history carries end times, terminal statuses, and
error fields for every failed job, so the interval record is
reconstructible for any past period. The backfill script (`scripts/backfill-errors-entries.py`)
writes synthetic errors snaps on the 5-minute grid over the trailing
30 days: one snap per non-empty interval, with capture policy
`backfill-errors-v1` marking reconstructed evidence as distinct from
observed snaps. The backfilled record tiles exactly against the start
of the first live interval, so each failed job lands in exactly one
interval across the seam. The script is idempotent — a re-run
replaces prior backfill — and dry-run by default. The deployment
order is maintainer first, backfill after the first live publication.

When a field joins the entry rows, the backfill is re-run so the
synthetic snaps carry it, and the live snaps recorded before the
change are augmented in place by `scripts/augment-errors-entries.py`:
every trailing field a row lacks is looked up by job id from the job
records and added, every other field is left as recorded, the snap's
component and state hashes are recomputed by the capture contract,
and the component document is marked `augmented` with each field
added, when, and how many rows had no job record left. The payload
exit code was added this way on 2026-09-06 over the live snaps from
2026-08-21. Overflow fold keys from before a change name no job and
keep their recorded form.

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

**Terminal-state filter.** A chip row beside the grouping selector
filters the view by terminal state. The chips are discovered from the
loaded data — a state appears exactly when the record holds it — and
each carries its count over the visible range in parentheses, so an
excluded closure storm remains visible in its chip while the plot
shows the actual errors. Closed is off by default (see Terminal
states above); rows recorded before the status column report as
`unrecorded` and age out of the window. The selection lives in the
URL, filters client-side from the per-state breakdowns the bins
carry (no refetch), and the breakdown, donut, and diagnostic
patterns follow it.

**Task filter.** A task selection (URL parameter, so the view is
bookmarkable and linkable) narrows the plot, donut, and breakdown to
that task's events — the per-task error history is the overall view
filtered, not a separate surface. The parameter is open: any task id
reached by link is valid, and no task list is offered on the view
itself. The PanDA task page (`panda/tasks/<jeditaskid>/`) links to
its filtered errors view. Refinements specific to the per-task
reading come later; the filter is the mechanism from the start.

## Wasted resources

The error record carries what each failed job cost, and the errors
view shows that cost on the same time axis as the error counts. The
unit and the rule are those of `resource_usage`
(`monitor_app/panda/queries.py`, the `panda_resource_usage` MCP tool):
a job holds cores times wall time from its start to its end, cores as
the actual core count, else the declared count, else one; the
allocation held by a job that ended faulty is wasted; a job that never
started holds nothing. Core-hours is the unit. In the fourteen days to
2026-09-06, failed epicprod jobs held 87,970 of 217,479 core-hours,
40 percent, and 48 percent at NERSC Perlmutter; cancelled jobs that
ran held under a hundred, and closed jobs none.

**Record.** Each entry row carries `held`, the core-seconds the job
held, computed in the same scan as the other fields and clamped at
zero. For a lost-heartbeat failure the hold ends at the recorded end
time, the last heartbeat, as `resource_usage` reads it; the batch
slot's further hold to its walltime is not in the job record. The
overflow fold carries `held_by_category` beside `by_category`, the
summed core-seconds under the same keys. This is entry-row schema
version 5. The projection bound rises to 256 KB: a full interval of
2,000 rows serializes near 150 KB with the field. History: the
backfill re-run writes the synthetic snaps with the field, and the
live snaps recorded before it are augmented in place by
`scripts/augment-errors-entries.py`, the generalized form of the
exit-code rewrite, which adds any trailing entry field a snap lacks
by job id and recomputes the snap hashes.

**Series.** An event carries its weight as a third element, [stamp,
qualifier, weight], and a bin carries both measures: the count and
its per-qualifier breakdown, then the summed weight and its
breakdown. No curve is added: the category, component and per-task
curves each carry both numbers, and the series walk is unchanged in
cost. Overflow folds land at the interval end with their
summed weight spread evenly over the folded events, so the bin sum is
exact.

**View.** A Measure selector, errors or core-hours, beside the
grouping selector. It picks which bin measure the panels plot, the
chips count and the axis names; the panel title follows (errors by
category, wasted core-hours by category). A family declares the
selector parameter it follows and its units per measure
(`measure_param`, `units_by_measure`, `title_by_measure`; snapper-ai
INTEGRATION.md). The client's display re-binning sums whichever
measure is active. Under core-hours, cancelled jobs that ran count
and closed jobs hold nothing; the terminal-state default is
unchanged. The task filter applies as it does to counts, so a task's
waste history is the same page filtered. One measure at a time on
one panel is the form the display model supports: the page assigns
each curve to one family, so plotting both measures at once would
require a second set of curve ids carrying the same events, doubling
the series walk.

Beneath each panel an integrated panel draws the same bins summed
from the left edge of the window, stacked by the same categories in
the same measure and under the same chips, and re-based when the view
zooms. The height of each band at the right edge is that failure
kind's share of the window's total, so the slot deaths under running
jobs and the registration failures compare at a glance. The page
derives it from the panel's own bins (snapper-ai `cumulative_panel`);
no curve is added.

**Breakdown.** The cut's breakdown adds core-hours and its share of
the window's wasted core-hours per category beside the count, from
the same recorded entries, states the window's total held beside its
error count, and gives each diagnostic pattern its core-hours from
the job records, per failure mode where the correction root splits a
pattern by payload exit code.

**Storage view.** The consequences strip follows a Consequences
selector, jobs or core-hours, on the same mechanism
([SNAPPER_STORAGE.md](SNAPPER_STORAGE.md)), so the hours lost to
upload and registration failures read on the storage time axis.

**Productive baseline and per-site reading.** The error entries carry
no site and no finished jobs. The waste fraction and the site reading
come from the PanDA component (`monitor_app/snapper_panda.py`): the
scope and per-site cumulative outcome counters gain `cum_core_hours`
with finished and failed members, summed in the outcomes query from
the same rows. The Site view gains a core-hours panel of
window-relative staircases, productive and wasted, in the form of its
finished and failed job staircases, and the errors view shows the
scope total the same way beneath its panels. The counters backfill
gains the sums. Thresholds and verdicts on the waste fraction are a
later round.

Delivery order, each stage usable on its own: the record field with
the backfill and augmentation; the weighted bins in the series; the
Measure selector on the errors view and the Consequences selector on
the Storage view; the breakdown sums; the PanDA counters with the
Site panel.

## Proactive storm response

Planned, the next stage of this design: the recorded error stream is
the trigger surface for automatic storm response — detection,
notification, and bounded automatic investigation that prepares
information for human evaluation. Investigation latency is accepted
by design; the alternative cost is operator time.

### Storm detection

A detector rides the component publisher: every publication counts
one interval, and the detector evaluates it against the trailing
baseline. Detection covers every terminal state, closed included —
the presentation default that filters closures out of the errors
view never applies here, because a closure flood is itself an
infrastructure signal. A storm starts when an interval's errors exceed the larger
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

### Bounded action

The escalation path beyond evaluation: the storm AI first notifies,
then proposes, then — within explicit bounds — acts. The proposal
stage uses the established AI-proposals pattern (AI_PROPOSALS.md):
the evaluation concludes with a concrete deterministic action, such
as pausing the dominant task, that a human approves in one step.
Bounded autonomous action comes later and stays within a curated
vocabulary of reversible operations — task pause is the model case,
and the task-operation set already excludes irreversible actions —
gated by its own thresholds and an explicit allowlist. The goal
condition: within minutes of a major failure, the flow of jobs into
a failing configuration is stopped, and the human reviews an action
that one step undoes.

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
