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

Cylinder height represents running jobs on one 2,500-job scale. A compact
execution-site cylinder retains its fixed height and shows the shared
slash break when the value exceeds its display range. The green and red
proportions represent finished and failed jobs in the preceding hour.
The slash treatment is shared by both cylinder types. Plots convert
quarter-hour completion bins to hourly rates. Task finish and fail
columns retain JEDI input-file accounting; fail counts final failures
after retries. Task rows extend downward without an internal scrollbar.

Weekly average running cores appear on the active OSG and GREX title
rows. They are time-weighted from Snapper's recorded running-core curves,
excluding collection gaps and stale intervals. OSG includes its retired
production queue over the historical window. The initial record is a
small static seed; a lazy request uses the existing Snapper series
product when the cached average is more than an hour old. The browser
reuses the result for an hour and refreshes it while attended.

All displayed times use a 24-hour clock in Eastern Time. Hover details
appear after a 150 ms pause. Daily assessment text is the record's
narration, limited to two lines with the full text on hover; its title
row carries verdict, finished-job count and creation time.

Main-site cut cylinders preserve the complete red segment's height.
The compressed execution-site cylinders have no slash marks: their
visible red and green segments show the hourly fail/finish proportions.
Their curved color boundary stays inside the measured red height,
leaving a small green remainder visible.

## Live record and links

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

## Implementation

- `monitor_app/viewdir/prod_dashboard.py` renders the shared document and
  the home tab.
- `monitor_app/templates/monitor_app/prod_dashboard.html` contains the
  tuned layout; `prod_hub_dashboard.html` embeds it.
- `monitor_app/static/monitor_app/prod_dashboard/` contains the panel
  controls, live binding, drilldown helpers and weekly core averages.
- `epic-pillar-model.js` there owns cylinder sizing, outcome fractions and
  curved red/green joins, with no DOM or startup side effects. Dashboard
  SVG and CSS adapters in `epic-pillars.js` use it. The
  computing-room publisher bundles this deployed module into its drawing
  content, checking its pinned hash; the room supplies its existing Three.js presentation and scale.
- `monitor_app/_prod_home_tabs.html` supplies the shared tab row.

The development URL `static/epic-dashboard-design.html` embeds the same
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
starts. Its number and cylinder open the worker-admission summary for that
queue and hour: start-cohort outcomes, current workers and payload jobs,
pre-job refusal evidence, repeated slot matches and measured time costs.
Individual expected declines stay below the dashboard's error threshold;
aggregate admission churn remains visible on the summary page.

Completed tasks for the last 24 hours appear below Paused tasks, from
the snapshot's `completed` record. Each task ID opens its task page;
the title-row count opens the day's production task list. Hover text
carries the task's completion status, queue, end time and file outcomes.
Done tasks are green; finished tasks with lost input files are amber.
