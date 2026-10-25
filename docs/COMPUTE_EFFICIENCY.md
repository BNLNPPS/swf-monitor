# Compute efficiency history

The Sites menu's **Compute efficiency** page (`panda/compute-efficiency/`) shows
successful-job core-hours divided by the core-hours of all ended jobs.
It contains a site-by-period heatmap with core-hour volumes and selectable
trend lines. The PanDA hub and the dashboard's Computing now heading link
to this history. The live dashboard's layout and individual metric links
are unchanged.

The heatmap uses an explicit fixed 0–100% scale: red at zero, amber at
50%, and green at 100%. Dark labels remain readable on the light fills;
higher efficiency is always greener.

Hovering a trend line or marker identifies its site in a high-contrast
floater with the nearest measured period's efficiency and ended core-hours.
Line hover respects missing periods and hidden legend entries; it never
reports an interpolated percentage as a measurement.

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
