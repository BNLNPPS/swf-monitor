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
the top, with the daily assessment and paused tasks. OSG and GREX have
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

A cut cylinder preserves the red segment's height from the complete
cylinder; truncation does not rescale the hourly finish/fail ratio. Red
caps are clipped to their segment so they cannot cover a small green
remainder.

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
ending interval when opened. Queued links use the queue's not-started
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
- `monitor_app/_prod_home_tabs.html` supplies the shared tab row.

The standalone route is a read-only `panda/` page, covered by
swf-remote's existing catch-all. Deployment uses
`deploy-lightweight-ui-mcp.sh --ui --static`
([PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md)).
