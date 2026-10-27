# Production dashboard

The production home has three tabs, **Nav**, **Dash** and **Ops**. Nav is
the default. Dash shows production at a glance and links to the live
records behind each quantity:

- Home tab: https://epic-devcloud.org/prod/?tab=dash
- Full-page dashboard: https://epic-devcloud.org/prod/panda/dashboard/

The square-and-arrow icon beside PROD opens the full-page dashboard in
a new window. The home tab and standalone page use the same document,
styles and JavaScript. The home embeds that document through `srcdoc`,
which isolates the dashboard styles from the application navigation;
the standalone route renders it without application headers. Both paths
pass through the existing swf-remote proxy.

## Display

The layout carries production supply, processing and science data across
the top, with the primary campaign summary and daily assessment. Paused
tasks appear below Tasks in flight. The campaign summary reads the same
cached completion product as Nav: first delivery date, delivered events
and bytes, and the number of physics configurations for the current
campaign. OSG and GREX have
running, queued, pilot and hourly outcome figures, cylinders and
12-hour history. OSG execution sites have their own compact strip.
Perlmutter, Google, Compute Canada and JLab follow in two columns.
Tasks in flight, epicprod-live, Production now and Science data occupy
the remaining columns. Site panes open by default; Open and Close act
on every site pane.

Site labels, reasons and phase colours come from the feed's `status_label`,
`status_reason` and `status_level`. Inactive Perlmutter reads **No allocation**,
Canada **Authentication block**, and JLab **Cybersecurity block**; all three
status labels use grey.
Google uses GREX's ordinary
production panel content: shared pillars, running and queued cores, outcomes,
and trend history. Google's pilot-count figure and waiting-pilot cylinder are
hidden; its worker cap remains in the title row. Both displays select queues
through the feed's `production_display` flag, preserving inactive JLab commissioning.
Google's standard queue remains off. ES runtime and controller diagnostics
remain in operator views. Google's status bullet is green when its production
queue is live and the controller reports healthy workers. Missing or stale
worker readings are amber; reported worker failures are red.
Canada, Perlmutter and JLab use the same standard
panel content in their existing dashboard positions, with inactive status
labels retained. `site.production_panel` controls the presentation independently
of queue activity. Reported counts remain visible while inactive; missing
trend samples remain unknown. The room places JLab's chart above its Rucio board.

Display names match the authored site discs: Canada and Perlmutter. The
shared short-name vocabulary also applies to cached site/trend names and
report prose; internal site and queue identifiers retain their canonical keys.

The Computing now, Work available and Campaign blocks consume
`report.header`: display-ready values, labels, colours, details and links.
The room places the same three blocks beneath the Production now title.

The right column is bounded by the visible viewport using the existing
screen-height measurement. Production now wraps its notices without growing
to fill unused space. When the notices would push Storage off screen, their
body scrolls within the available height. Storage follows directly below.
Notices use 10-pixel text, and every notice is accessible. There is no
separate inactive-site footer.

Cylinder height uses a common 2,500-core running scale for OSG and GREX,
preserving the approximately 80% height of 2,000 single-threaded jobs.
Google uses a 100-core scale, and Perlmutter a 1,000-core scale (128 work
lanes a node; its five-node production of 640 at about two-thirds height).
Each of these four pillar sections shows its scale in small text at the upper
left. Google's and Perlmutter's title rows show their production at the upper
right, **5 nodes × 14 cores** and **1 node × 128 cores**; Perlmutter's nodes are
the feeder's own (`perlmutter_production.NODES`), so the row follows when
production goes to five. An Event Service site, one whose production queues
are all in the ES close-out's list, shows **Mevents/1h** (millions, one
decimal) in place of finish/1h: the events of the closes that stood in the last hour, from its
jobs' shipped harness records (`processing_activity`), since its jobs finish
once in hours. Other site cylinders use
the default scale without a scale label. A compact
execution-site cylinder retains its fixed height and shows the shared
slash break when the value exceeds its display range. The green and red
proportions represent finished and failed core counts in the preceding hour.
The slash treatment is shared by both cylinder types. Plots convert
quarter-hour completion bins to hourly rates. Task finish and fail
columns retain JEDI input-file accounting; fail counts final failures
after retries. Task rows extend downward without an internal scrollbar.

Running numbers and pillars are labelled **cores** and count cores carrying
production payload work. Native
jobs use the payload's chosen `payloadThreads` report; one-core native jobs
use one thread, while an unreported multicore thread width remains unknown.
Event Service jobs count work lanes with assigned units in the shipped
harness record. Each lane runs one single-threaded payload; empty lanes and
ended harnesses contribute zero. Records older than ten minutes are unknown.
A count is never blanked. While any job of a queue or execution site is
unmeasured, as an Event Service job is before its first shipped record, the
last complete count stands until a complete one replaces it (`threads_at`
carries its measurement time), never below the part now measured; with none
yet, the measured part shows.
The latest measured counts also stand when the minute cache is late.
Assigned-unit occupancy includes unit startup and finalization; it measures
payload parallelism, rather than instantaneous CPU utilization. Pilot supply
retains worker counts. The shared `processing_activity:v1` minute cache supplies
current per-queue and OSG execution-site counts.

Site completion and failure figures count cores, summing the reported payload
thread width or actual core count of each ended job. Five completed 14-core
jobs therefore contribute 70 core completions, separately from their core-hours.
The completion, failure and queued figures and their curves come from the
`site_core_metrics:v1` minute cache, which rebuilds behind a read; its last
complete accounting stands until the next replaces it, so the first read
after the dashboard sat unattended never blanks them.
Job counts remain in the hover and drilldown. Queued values show core demand
for the single-core native queues. Google shows the core capacity its waiting
jobs can fill at the current node cap: ten jobs can feed all five 14-core
workers, so the display reads 70 queued cores; hover retains ten jobs and
identifies the capacity limit. The minute cache `site_core_metrics:v1` reads
deduplicated production-job outcomes and queued demand from PanDA.

Site plots label the white curve **cores** and read Snapper's recorded
running-core series, including Google's multicore jobs. Green and red curves
show core completions/hour and failed cores/hour, from reported job core counts
in fifteen-minute bins; brown shows queued cores. Native single-core queue
counts retain the same numerical values. The plots use the peak of cores,
core completions/hour and failed cores/hour,
rounded upward to two significant digits, as their common ceiling.
The scale number sits directly below the running legend, in a reserved
band above every curve and overflow dash. Legend markers and curves use
the same shared palette. Queued values retain their
true scale: solid
within the range, dashed along the ceiling above it, with interpolated
crossings. Gaps break both kinds of line. A wholly zero window uses a
ceiling of one.

Google's history uses core units throughout. Its minimum ceiling is 15;
the running-core and core-outcome peaks raise that ceiling as needed. This
history scale is separate from the 100-core running pillar.

Weekly average running cores appear on the active OSG and GREX title
rows. They are time-weighted from Snapper's recorded running-core curves,
excluding collection gaps and stale intervals. OSG includes its retired
production queue over the historical window. The initial record is a
small static seed; a lazy request uses the existing Snapper series
product when the cached average is more than an hour old. The browser
reuses the result for an hour and refreshes it while attended.

An active operator worker-limit override replaces the weekly average in
the same title-row position with **Core limit N** in red. The snapshot
converts worker allocations to their core capacity; GREX has eight
single-core pilots per allocation. The current control carries the level
and reason. Clearing the override restores the weekly average. Weekly history
refreshes preserve the override while it remains active; its link opens
the queue record.

While a pool's negotiator holds our OSG submitter at its fair-share
limit, the OSG title row shows **fair share limited** in yellow followed
by its weekly average cores in grey, matching GREX. Each keeps its own
link and hover details. The fair-share hover names the pool and the time
of the negotiation cycle.
The notice means our submitter reached its fair-share limit during the
last reported negotiation cycle. Existing pilots keep their slots;
later cycles may grant more. The reading is the OSG submit host reporter's
negotiator query ([OSG_SUBMIT_REPORTER.md](OSG_SUBMIT_REPORTER.md)),
carried as `fair_share` on the snapshot's OSG site.

The feed owns the fair-share notice in `site.fair_share.notice`: its
text, severity, hover detail and live queue link. The dashboard and room
display those fields without local wording. A cleared or unavailable
fair-share state has no notice. The adapter is applied when the cached
record is read, so wording changes do not wait for a census rebuild.

Waiting-pilot figures use the feed's `pilots.waiting_current`, resolved
from the submitter's idle queue where reported and otherwise Harvester.
Both displays use that field; the raw readings remain available for diagnosis.

All displayed times use a 24-hour clock in Eastern Time. Hover details
appear after a 150 ms pause. Daily assessment text is the record's
narration, limited to two lines with the full text on hover; its title
row carries verdict, finished-job count and creation time.

Main-site cut cylinders preserve the complete red segment's height.
The compressed execution-site cylinders have a single grey slant line:
their visible red and green segments show the hourly fail/finish proportions.
Their curved color boundary stays inside the measured red height,
leaving a small green remainder visible.

## Task resources

The task resource panel occupies the left column below the site panels.
It selects EIC production jobs (PanDA source label managed), excluding
test and canary jobs. It plots reported peak memory, scratch disk and
scratch file counts over the preceding twelve hours, with one colour per task across all three
plots. Hovering a curve gives the task name and window peak; hovering a
point gives its time bin, peak and reporting coverage. Each curve links
to the task. Memory and disk use GiB; file counts are integers.

Each fifteen-minute bin contains the maximum reported job peak. Completed
jobs are placed at their end time; active jobs use their latest update.
These are observations of per-job peaks, rather than samples of aggregate
concurrent usage. Memory reads PanDA maxrss in KiB, scratch disk reads
scratchPeakMB in MiB, and files read scratchPeakFiles. Missing telemetry
is a gap; a plot with no measurements says “No reported peaks”. Coverage
counts and build time remain visible.

Each resource scale follows the usual individual production-job peaks.
Values above the upper quartile plus three interquartile ranges are excluded from scale
selection; for a constant central population the limit is three times
that value. The largest remaining peak sets the rounded ceiling. Extreme
values retain their true measurements, with marked points at the ceiling
and a top-of-plot annotation giving the largest value and point count.
Its hover lists every affected task, value and time. Sparse records with
fewer than four observations use the full observed range.

The read-only panda/dashboard/resources.json endpoint serves the shared
task_resource_peaks:v2 cached product with a sixty-second lifetime. The
first fill runs in the background; the panel displays its building state.
The query reads EIC jobs from the active and recent archive tables and
deduplicates PanDA job IDs. The dashboard refreshes it with the existing
minute update. Both the home tab and standalone dashboard use the same
renderer and shared trend geometry.

## Live record and links

Production compute sits directly below Storage in the right column. Its
three plots show efficiency, total average cores (labelled cores/day) and
productive average cores, with OSG, Perlmutter and GREX curves in each. Each plot has an
overall last-24-hour figure and one overall monthly average since May.
The current month is marked with an asterisk. All three use ended-job
walltime times cores, without processor-performance weighting. Efficiency
is the successful fraction; cores/day divides all ended core-hours by elapsed
hours and includes failed work. Productive average cores is successful
core-hours divided by elapsed hours, so it equals cores/day times efficiency.
Running jobs and idle pilots are excluded. Monthly rates use calendar time,
including days with no completions, through the observation timestamp.
Hover gives the site, sample hours and job counts. Missing or low-volume
efficiency readings remain unknown. Current readings use the shared minute
cache; monthly history uses its existing hourly cache. The panel follows
Storage without changing existing panel positions or available heights.

The dashboard reads the shared comp snapshot, `comp_snapshot:v1`,
through `panda/comp/snapshot.json` ([COMP_SNAPSHOT.md](COMP_SNAPSHOT.md)).
The production document includes the current cached record; the shared
dev URL fetches the same record before showing its figures. It updates
once a minute while visible, including on an unfocused operations display,
and catches up when the tab returns. Each fetch reads the shared cache;
a stale record starts one background rebuild. A page reload also uses
that cache, rather than rebuilding every source. The header stays pinned
through the Open / Close controls.
A failed update is reported on the build-time line; an unavailable
initial record does not expose the design's captured figures.

Links open queue-specific Snapper history, filtered job lists, task
pages, the production front, storage history, live logs and individual
assessment or alarm records. Hourly completion links compute their
ending interval when opened. Failure counts open the error summary for
the selected queue and hour; task failure counts select that task. Error
summaries apply execution-queue filters in SQL, including their status
totals. Site plot legends follow the metric order: running, finish, fail,
queued. Queued links use the queue's not-started
status set. In-flight task IDs open all of that task's job history,
with server-side task filtering and pagination. The jobs title and
completion chart follow task and execute-site filters; a whole-queue
pie is omitted for a narrower selection. Task file totals open the task accounting rather than a
job count. Storage capacity links select BNL-XRD and the capacity view.
A queue without Snapper history opens its queue details. Incomplete
registrations are amber when nonzero and open the jobs with pending or
unfinished payload registration in the same completion hour.

Drilldown queue filters come from each snapshot site's queues. The site's
primary queue page is also selected in the feed. The dashboard's panel-name
mapping controls presentation only; it carries no queue catalogue.

## Implementation

- `monitor_app/viewdir/prod_dashboard.py` renders the shared document and
  the home tab.
- `monitor_app/templates/monitor_app/prod_dashboard.html` contains the
  tuned layout; `prod_hub_dashboard.html` embeds it.
- `monitor_app/static/monitor_app/prod_dashboard/` contains the panel
  controls, live binding, drilldown helpers and weekly core averages.
- `epic-trend-model.js` there supplies the ceiling, normalized solid and
  overflow paths, and palette through `createEpicTrendModel()`. Its
  `hourly(values, binMinutes)` converts counts using the feed's bin duration,
  shared by site and storage plots in both displays.
  `site(trend, times, binMinutes)` uses that conversion for outcomes;
  `series(columns, {count})` accepts displayed values, with
  `overflow: true` on columns excluded from the ceiling. The dashboard and room
  consume this same pure module; neither computes separate clipping.
- `epic-pillar-model.js` there owns cylinder sizing, outcome fractions and
  curved red/green joins, with no DOM or startup side effects. Dashboard
  SVG and CSS adapters in `epic-pillars.js` use it. The
  computing-room publisher bundles this deployed module into its drawing
  content, checking its pinned hash; the room supplies its existing Three.js presentation and scale.
- `monitor_app/_prod_home_tabs.html` supplies the shared tab row.

The development URL `static/epic-dashboard-design.html` loads the same
standalone document. It has no copied markup, styles or drawing scripts.
The duplicate project-level dashboard scripts are removed. The pre-commit
architectural check rejects a second document or pillar implementation.
After changing the shared model, update its pinned hash in the room source,
deploy dashboard static assets and run Etaverse's `publish_comp_room.sh` so
both views receive the same revision. Publication refuses a missing module
or a hash mismatch.

The standalone route is a read-only `panda/` page, covered by
swf-remote's existing catch-all. Deployment uses
`deploy-lightweight-ui-mcp.sh --ui --static`
([PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md)).

Plot colors are fixed by metric: running white, queued brown, finished
green, failed red. Gaps split a curve into segments without changing
its color. The hourly outcome curves are rates from fifteen-minute
bins. Site histories use the live 90-second series cache. Missing data
is shown as unavailable rather than retaining captured design values.

The slots/hour figure counts Harvester worker starts, including declined
starts. It does not yet count Google's GKE worker starts. Its number and
cylinder open the worker-admission summary for that
queue and hour: start-cohort outcomes, current workers and payload jobs,
pre-job refusal evidence, repeated slot matches and measured time costs.
Individual expected declines stay below the dashboard's error threshold;
aggregate admission churn remains visible on the summary page.

Completed tasks for the last 24 hours appear below Paused tasks, from
the snapshot's `completed` record. Each task ID opens its task page;
the title-row count opens the day's production task list. Hover text
carries the task's completion status, queue, end time and file outcomes.
Done tasks are green; finished tasks with lost input files are amber.

Data arrivals, the science reaching JLab over the last 24 hours (the
Completed tasks window), appear
below Completed tasks,
one row per physics configuration, newest arrival first, from the
snapshot's `arrivals` record (Torre Wenaus, 2026-10-24). A row reads the
physics in short (`label`), the RECO files registered in the window and
the time of the last arrival, over a thin bar of the plan's delivered
against target events: bright green when the configuration is finished,
every task delivering to it in the window having ended or its target met
(past the target counts as met; the hover gives the numbers), blue while
it is incomplete. Each row opens the PC's
page; its hover text carries the full physics line, files and events,
times, tasks, sites and the plan's delivered against target. The title-row
count opens the campaign view. The panel grows downward with the number
of configurations receiving data.
