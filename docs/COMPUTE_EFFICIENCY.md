# Compute efficiency history

The Sites menu's **Compute efficiency** page (`panda/compute-efficiency/`) shows
successful-job core-hours divided by the core-hours of all ended jobs.
It contains a site-by-period heatmap with core-hour volumes and selectable
trend lines. The PanDA hub and the dashboard's Computing now heading link
to this history.

The dashboard's panel below Storage has three monthly plots, each with
OSG, Perlmutter and GREX curves: efficiency, total core-hours per day,
and productive average cores. Each plot also shows an overall reading
for the preceding 24 hours and one overall monthly average since May.
OSG combines execution sites and retired queues; Perlmutter combines its
historical queues. Hover identifies each curve and its measured values.

A larger **Productive average cores · daily** plot sits immediately above
Production resource peaks in the dashboard's left column. Its 90 completed
UTC days show additive filled areas: OSG blue, GREX green and Perlmutter
amber, with a white line on their summed upper boundary. Each daily value is
successful core-hours divided by 24. The current incomplete day is omitted;
the compact right-hand plots retain their preceding-24-hour readings.

This daily plot includes EIC managed jobs and legacy `epicproduction`
jobs, which carried PanDA's `test` source label before the production label
transition. Jobs with processing type `canary` are excluded. Missing
timings create gaps, while a completed day with no production completions
contributes zero. Hover gives the day, site contributions and summed total.
The existing PanDA Snapper counters include test jobs, so this filtered
record has its own Snapper component, `epicprod:production_compute`, and
cached product, `productive_cores_daily:v1`. The initial build queries 90
days; hourly refreshes reread seven days and preserve older daily values
from Snapper. Immutable system snaps retain each accepted history. The
same hourly scheduled producer refreshes both compute products.

A shallow **Pressure front ready tasks** plot below productive cores uses
the identical 90-day date positions. Its cyan line begins on the first
nonzero daily mean; earlier dates remain blank. Later measured zero days
remain visible, while unrecorded intervals stay missing. Its Y-axis ticks
are whole numbers; hover retains fractional daily means. The source is the
production front's ready-task depth and cycle records in the action stream,
not PanDA jobs waiting for a worker. A day's value is the time-weighted mean
over its recorded coverage, which hover reports in hours. Queue decisions
expire after 75 minutes and a cycle sample after 15 minutes, preserving
collection gaps. These daily means are retained with productive cores in
Snapper; the hourly producer rereads the latest seven days.

All three metrics use the same ended-job sample. Efficiency divides summed
successful core-hours by all ended core-hours. Total core-hours per day
includes failed work and divides by calendar days, including days with
no completions. Productive average cores divides successful core-hours
by elapsed hours; it equals core-hours/day times efficiency divided by 24.
No processor-performance weights are applied. The partial current month
uses elapsed time through the history's observation timestamp.

The last-24-hour reading uses exact UTC timestamps and the shared
`compute_efficiency_current:v2` cached product, refreshed every minute.
Monthly values read the retained hourly history. Dashboard requests serve
cached results and start stale or initial builds in the background.

The heatmap uses an explicit fixed 0–100% scale: red at zero, amber at
50%, and green at 100%. Dark labels remain readable on the light fills;
higher efficiency is always greener.

Hovering a trend line or marker identifies its site in a high-contrast
floater with the nearest measured period's efficiency and ended core-hours.
Line hover respects missing periods and hidden legend entries; it never
reports an interpolated percentage as a measurement.

Trend line thickness is proportional to the site's ended core-hours over
the selected date window, with the largest contributor drawn at six pixels
and a one-pixel minimum keeping smaller contributors visible. Focusing or
hiding a line preserves this scale. Hover details include the site's total
core-hours and share of processing for the same window.

## Measurement

The source is PanDA's active and archived job tables. Finished, failed,
closed and cancelled jobs are bucketed by
their UTC end date. Duration is end minus start; allocation is duration
times actual core count, falling back to declared cores and then one.
Missing or reversed timings do not contribute hours and are counted
separately. Running jobs and idle pilot allocations are outside the metric.
This is successful wall-time efficiency, distinct from the CPU-efficiency
measure on Site compute usage.

OSG queues are split by recorded execution site. Historical jobs without
that field use the existing worker-host site mapping; Fir and CNAF host
patterns are recognized explicitly. Unknown sites remain visible. Site
names use the shared short-name vocabulary. Retired queues remain in
historical accounting. Perlmutter's historical test, GPU and Event Service
queues share one site. Month and week totals sum the underlying hours;
they never average percentages. Cells below 50 core-hours are blank.
The current period is marked partial. An optional view excludes task
40856 to separate that incident from the complete record.

## Automatic refresh

The cached product `compute_efficiency:v1` retains daily aggregates from
May 2026 onward, including separate contributions from task 40856.
Its hourly refresh rereads the latest seven UTC days; older daily rows
are retained. A monthly full reconciliation catches older late arrivals.
Days older than the source archive's first remaining day are retained
during reconciliation, so archive expiry does not erase the history.
The same builder serves the page and the scheduled command. A missing
initial product builds behind the page response; read requests never
download job lists. An attended page checks for refreshed data every
five minutes and while the first build is running.

`scripts/refresh-compute-efficiency.py` refreshes the product through the
standard CachedProduct locking/writer rules. `--full` rebuilds all days.
The installed cron file is `swf-compute-efficiency`, running as the
deployment account against `/opt/swf-monitor/current`; its output is
kept in `/data/wenauseic/swf-monitor/compute-efficiency.log`. No production job, task or
worker controls are changed by this producer.

Install the scheduled producer after deploying the page:

```sh
sudo install -o root -g root -m 644 scripts/refresh-compute-efficiency.py /opt/swf-monitor/current/scripts/
sudo install -o root -g root -m 644 scripts/compute-efficiency.cron /etc/cron.d/swf-compute-efficiency
/opt/swf-monitor/current/.venv/bin/python /opt/swf-monitor/current/scripts/refresh-compute-efficiency.py
```
