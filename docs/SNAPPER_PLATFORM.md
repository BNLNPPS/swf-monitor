# Snapper Platform — PanDA platform health component and view

The health of the PanDA platform — server, database, web tier — becomes
part of the recorded system history: a platform component captured in
Snapper snaps, and a dedicated Platform view showing load, platform
state, and consequences on one time axis, with an aggregated summary of
every metric at the cut. It builds on the Snapper concepts and SWF
deployment in [SNAPPER.md](SNAPPER.md), the generic package
documentation
([DESIGN.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/DESIGN.md),
[INTEGRATION.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/INTEGRATION.md)),
and the error-state component in [SNAPPER_ERRORS.md](SNAPPER_ERRORS.md),
whose recorded events it reuses. The server-host reporter is documented
in [PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md).

This is a design document; the sections below are the agreed plan of
record for implementation.

## Historical questions

Registration begins with the questions the record must answer
(DESIGN.md, invariant 6):

- At an instant, was the platform degraded — connections near the
  limit, requests slow or failing, daemons silent — and under what
  load?
- Were pilot heartbeats reaching the server, at the rate the running
  population implies?
- Which recorded quantity moves first when degradation follows load,
  and by how much lead?
- What consequences followed — lost-heartbeat kills, worker-ended
  failures, finished jobs not recorded — and when relative to the
  platform signal?

Each quantity below names the question it serves; the useful
resolution is the five-minute refresh cadence throughout.

## The platform component

A component, internal name `platform`, in the epicprod scope, published
by a maintainer module (`monitor_app/snapper_platform.py`) beside the
PanDA activity and error-state maintainers on the same 5-minute
System-status refresh. Publisher identity `swf-monitor:panda-platform`,
assessment policy `swf-panda-platform-v1`, schema version 1, canonical
JSON bounded at 64 KiB. Every publication carries the owner's assessed
values; the view derives nothing from raw records (DESIGN.md,
invariants 1 and 4).

The projection has five groups of quantities, each with its kind:

**Database (gauges, read from the PanDA database).** Connections
total, active, idle, and waiting, with `max_connections`; the longest
open transaction in seconds; jobsactive4 live and dead tuples; minutes
since its last autovacuum. A bounded map of connections by application
and state (16 entries, remainder folded into `other`). Question: was
the database the constraint, and who held its connections.

**Heartbeats (assessed, from the job records).** For running jobs: the
count whose last modification is older than 30, 60, and 120 minutes —
scope-level and per site in a bounded map keyed as the PanDA
component's site maps are — and the count of running jobs whose
modification time advanced in the publication interval (heartbeats
received, on the error component's interval idiom: source time
advances atomically with content, so intervals tile). Heartbeat yield
is published as an assessed ratio: received over expected, expected
being the running population times the interval over the pilot's
30-minute heartbeat period. The per-interval yield beats against the
heartbeat phase — a five-minute interval sees whichever pilots' 30-minute
clocks fall inside it — so the component also publishes the yield
over a window of two heartbeat periods (60 minutes at the configured
period) as a ratio of sums: the received and expected counts of the
intervals ending inside the window, carried in the record and summed,
never a mean of per-interval ratios (the running population moves
between intervals). The window yield is the assessed figure the
verdict and the alarm read; the per-interval yield stays in the
record. Job starts in the interval complete the
group. Question: were pilots being heard, and at the expected rate.

**Server (gauge, measured by the maintainer).** One timed request to
the PanDA server's status endpoint from the swf-monitor host, recorded as
milliseconds, with a timeout recorded as a timeout, never omitted.
Question: was the server answering.

**pandamon (gauge, measured by the maintainer).** Two timed requests
to pandamon, the PanDA monitor (BigPanDA) web face on pandamon01: its
front page, and the harvester worker-stats query over the last hour —
the request swf-monitor's own tools make. Recorded as the server
measurement is, with its own timeout. pandamon is a distinct tier
from the server: its queries are read load on the same database, so
its latency is both a symptom and a cause. Question: was pandamon
answering, at the cost its consumers pay.

**Server host (delivered by the reporter).** Web-tier request counts
per endpoint class and status class for the interval (updateJob,
getJob, harvester, other; 2xx, 4xx, 5xx), error-log marker counts,
per-daemon liveness and seconds since last log line, Watcher kills per
interval; the WSGI tier as process count, total resident memory, and
restarts in the interval; host load average, memory used and
available, swap, and root and /var volume use; busy and idle web
workers when mod_status is enabled. These fields are absent until the
reporter runs and carry a `reported_at`; the maintainer publishes
`reporter_status` as `fresh`, `stale`, or `absent` against a SysConfig
threshold, and crossing that threshold is a semantic change that
publishes (DESIGN.md, Maintained assessments). Question: what the
server host itself saw.

**swf-monitor host (measured locally by the maintainer).**
swf-monitor's own tier on pandaserver02: Apache WSGI process count and
resident memory, the ASGI service (swf-monitor-mcp-asgi) liveness and
resident memory, the prod-ops agent's resident memory, host load
average, memory and swap, root, /var, and /data volume use, and the
swf-monitor database's connection count. Question: was swf-monitor itself under
strain when it recorded the platform — a degraded observer is part of
the evidence.

Load quantities — jobs in flight by state, running cores, in-flight by
site — are not recorded again: the PanDA activity component already
carries them at the same cadence, and the view reads them from there.
Consequences — lost-heartbeat and worker-ended kills, all faulty job
events — are the error-state component's interval entries, likewise
reused; finished and failed totals are the PanDA component's cumulative
counters. One record per fact.

Publication is unconditional each cycle: the gauges change every
interval, so the component drives one snap per refresh as the PanDA
component does. Registration precedes publication in the same
transaction, as the error maintainer does.

### Backfill

The consequence and load curves have recorded history already (the
panda counter backfill on an hourly grid, the error entries backfill on
the 5-minute grid). The platform gauges begin at first publication.
Heartbeat staleness and received counts are not reconstructible: job
modification times are overwritten, so no past instant's staleness
survives in the records. The view states the record's start rather
than implying earlier coverage.

## The server-host reporter

The reporter agent on pandaserver01 (PANDA_SERVER_REPORTER.md) posts
one record per 5-minute interval to a new authenticated ingest,
`POST /api/snapper/platform/report/`, on the pattern of the episode
write endpoints (token or session authentication, an authorized
reporter identity in the body). The ingest validates the record against
a declared shape and stores it as the current server-host report (one
row, replaced per post, with `reported_at`); the maintainer reads it at
publication and merges it into the component. A buffered backlog posts
as a batch and the ingest keeps the newest; the record's own interval
stamps stay with it. The reporter never publishes to Snapper directly:
the maintainer remains the single owner of the component.

## The Platform view

A dedicated focus view, `Platform`, on the focus-view mechanism the
Errors and Site views use: its own clean path under the epicprod scope
(`/snapper/epicprod/platform/`), a focus-sized cached series product
over the `platform`, `panda`, and `errors` components' snaps, and its
own detail rendering. Its families are absent from the compact scope
report; the scope's front door does not grow.

**Panels, in order — the platform's own quantities first, then the
load and consequence panels beneath them for correlation by eye —
each family's control row docked above its panel:**

1. *Heartbeats* — received per interval and starts per interval.
   Starts are in practice a subset of the heartbeats received (a
   job that starts in the interval and is still running has
   heartbeated), so the area beneath the starts curve carries a
   light hatch marking the subset relation. It is a hatch rather
   than a solid fill because on this page a solid fill is a stacked
   band. Starts above received are jobs that started and left the
   running state within one interval, the burn-through signature,
   and stay visible. *Heartbeat yield* as its own small panel on a
   0–1 scale: the 60-minute window yield bold; the per-interval yield
   is a faint member that starts unticked, since its phase spikes
   would set the axis and flatten the window curve. The window curve
   is derived at series time from
   the recorded per-interval received and expected counts (a rolling
   ratio of sums over the trailing hour of snaps), so it spans the
   record from its first publication; it equals the record's own
   window figure wherever both exist.
2. *Heartbeat staleness* — the 30–60, 60–120, and over-120-minute
   bands stacked (the recorded nested tiers plotted as exclusive
   bands); a staleness selector switches the panel to the over-120
   count by site.
3. *DB activity* — active and waiting stacked, active in blue and
   waiting in the warning color, on their own scale. *DB connections*
   follows as a small panel with the pool total as one line: a drop to
   zero is a server restart, a climb is a leak or a second pool. Idle
   connections are not plotted: the PanDA server's persistent pools
   hold about 120 of them, a base that would compress the activity
   to a sliver if stacked beneath it. Idle appears on the card and in
   the summary. The connection limit is likewise stated on the card
   and in the summary, not drawn: on the plot it dwarfs both panels.
4. *Server latency* — milliseconds; a timeout records at the timeout
   value. *pandamon latency* follows on its own panel: the front
   page and the worker-stats query, same treatment.
5. *Web tier* — request rates by endpoint class and the 5xx count,
   present when the reporter reports; daemon liveness renders as lanes
   above the panels (per-daemon continuous lanes on the health-lane
   mechanism: green alive, failure color silent), so a stalled
   copyArchive is a red band, not a number.
6. *Hosts* — per host, PanDA server and swf-monitor: load average; memory used;
   volume use as percent, one line per volume; WSGI, ASGI, and agent
   resident memory; service liveness as lanes beside the daemon lanes.
7. *Jobs in flight* — the scope's in-flight jobs family (by state,
   stacked) with running cores as the overlay line, as the Site view
   draws them.
8. *Faulty job events* — the error-state component's events by
   component (dispatcher, taskbuffer, ddm, pilot, …), event-flow
   binned; the terminal-state chips apply as on the Errors view.
9. *Job outcomes* — finished and failed, window-relative.

Units on every panel title; house state colors where a state is drawn;
red only where failure lives. Member ticks stay on for the small
families and off for the event-flow family, as declared on the Errors
view.

**The cut.** A click is a time cut. The detail section renders the
platform card at that instant: the database breakdown (connections by
application and state, longest transaction, table health), the
heartbeat table (staleness tiers by site, received against expected,
yield), the server measurement, and the server-host table (daemons
with age of last log line, web-tier counts, host resources, reporter
freshness). Every site is a link to the Site view at the same cut; a
kills row links to the Errors view windowed to the same bounds, where
the error breakdown, patterns, and attribution reading already live.
The card states its interval basis once.

**Thresholds** — yield floor, connection fraction, latency ceiling,
staleness fraction, reporter staleness — are SysConfig keys present at
their defaults; the card marks a crossed threshold in the warning color
and names the key.

## The summary at the cut

Below the panels, at the bottom of the page, the cut renders one
aggregated summary across every metric the view plots — load,
platform, and consequences in one table — so the state of the whole
platform at an instant reads in one place and relationships between
metrics can be judged by eye. One row per metric, in panel order, each
carrying its curve swatch: the value at the cut, the change against
the previous snap, the metric's minimum, mean, and maximum over the
visible range with the cut value's position in that range, and a
threshold mark where one is crossed. Interval metrics (heartbeats
received, kills, starts) report over the detail window the event-flow
cut uses; gauges report at the cut instant; window-relative counters
report their accumulation from the view's left edge. The summary reads
the coherent snap at the cut — the platform, PanDA activity, and
error-state components of one registry cut — so its rows are
simultaneous readings of one recorded state, with the components' own
assessment times stated once beneath the table.

The per-component detail cards (database breakdown, heartbeat table,
server-host table) dock beneath their panels as on the Site view; the
summary is the one section that spans them.

Correlation as a computed function of the view — pairwise coefficients
with lag, and ranking of predictors for a chosen response — is a later
round, designed as a generic snapper-ai mechanism declared per focus
view; the summary table is the first round's instrument for reading
relationships.

## Detection and notice

The maintainer evaluates each publication against the thresholds and
records the verdicts in the component's `assessment`. Notice is the
alarm engine's job ([alarms.md](alarms.md)): the
`panda_platform_health` alarm reads the latest published component on
each engine tick and raises one detection per metric in warning —
heartbeat yield, heartbeat staleness, database connections, server
latency, pandamon latency, swf-monitor volumes, swf-monitor services —
plus one when the
component itself is absent, unreadable, or silent beyond
`stale_after_minutes`. Heartbeat verdicts are suppressed below
`min_running` running jobs, where the rates are noise. The thresholds
stay with the record (the `platform_*` SysConfig keys); the alarm
carries only its own two parameters. The engine's state-based dedup
gives the transition behaviour — an event opens when a metric enters
warning and clears when it leaves — and the per-alarm email gate,
recipients, and renotification window are edited on the alarms
dashboard. Capcom carries nothing from this path: the alarm is the
notice.

A `panda-platform` System Status collector reads the latest published
component so the platform state enters the System page and the health
lane without a second source.

## Node health map

The staleness tiers say how many running jobs are silent at each
site; the node health map says which nodes. Every job record carries
the worker node in `modificationhost` (`nid006841` at
NERSC_Perlmutter_epic, `n388` at UM_GREX_PanDA_1, a GKE node name at
BNL_ePIC_GOOGLE), so the heartbeat reading groups silent running jobs
by site and node. For each site the record carries the nodes whose
running jobs are all silent beyond the warning tier
(`platform_stale_warn_tier_minutes`) with the job count and the age
of the oldest silence, bounded to the 32 worst nodes per site by
silent count with the remainder folded into a count
(`heartbeats.nodes`, on the site map's bounding rule). A node whose
jobs are all silent while its neighbours heartbeat is a node fault —
the 2026-08-25 lost-heartbeat storm was per-node I/O stalls starting
at different times on different nodes; silence across every node of a
site is a site path or server fault. The distinction is the first
question a site asks, and it is answered from the record without a
log.

The staleness panel is unchanged; the map renders at the cut as a
per-site node table beneath the heartbeat table, and the
`heartbeat_staleness` alarm detection names the top silent nodes and
their silence onset in its detail, the text a site needs to act.
Where a site publishes per-job files at a known location (the NERSC
portal directory per PanDA id), the node table links the job whose
silence began first on that node, since its pilot log is the
discriminator (ERROR_ATTRIBUTION.md, dig triggers). The site-canary
rider's fingerprint map later joins node identity to environment,
so a faulty node reads with its platform, kernel, and mount state.

## Worker release for stalled jobs

When the Watcher fails a running job for lost heartbeat, the job's
worker is not released: PanDA's worker synchronization issues
`SYNC_WORKERS_KILL` only when the pilot has reported `finished` and
harvester lags behind it (worker_module.py, `get_workers_to_synchronize`),
and harvester marks a job's workers for killing only on a
`tobekilled` command (propagator.py), which the Watcher's failure
never issues. A pilot blocked on I/O reports nothing, so its worker
holds the node until the batch walltime — on 2026-08-25, up to two
hours per worker after the job was already failed, on nodes that were
producing nothing. The queue-level `sweepPQ` API kills every worker
of a queue in a given status and is too blunt for this.

The release uses the same command the sync daemon uses, with explicit
worker ids: the Watcher-failed PanDA ids of the interval, joined
through `harvester_rel_jobs_workers` to workers still `running` in
`harvester_workers`, issued as `SYNC_WORKERS_KILL` per harvester id in
shards of 100 through the server's `commandToHarvester`; harvester's
sweeper marks the workers and the site's sweeper plugin cancels the
batch jobs. The call is in-process on the PanDA server, so the
releaser is a host-side script on pandaserver01 beside the reporter
(PANDA_SERVER_REPORTER.md): standard library plus the server's own
taskbuffer under the panda service environment, run on demand, never
on a schedule of its own.

Triggering and bounds follow the bounded-action ladder of
SNAPPER_ERRORS.md. The `heartbeat_staleness` detection is the
trigger; the release is first an AI-proposals action
(AI_PROPOSALS.md) — the detection's detail lists the workers it
would release, by site and node, and a person approves in one step —
and becomes autonomous within an allowlist of sites and a per-episode
cap once the proposal record shows it acting correctly. Every release
is an action-stream record naming the detection, the workers, and the
command ids written; a worker that is not `running` by the time the
command is written is skipped and counted. The releaser never kills
workers whose jobs are not already failed by the server: it releases
resources the workflow has given up on, and takes no decision about
the jobs.

## Retrieval

The component rides the existing Snapper retrieval surface unchanged:
the REST endpoints and MCP tools answer connection counts, staleness,
yield, and server-host state at any instant with the standard evidence
envelopes, and `changes_between` locates the transitions.

The view's own products — the series (including the derived 60-minute
yield curve and the window-relative counters) and the summary at the
cut — are data as much as the components are, and AI clients receive
them by query rather than by reading the page: the series and
cut-summary tranche of the snapper-ai plan
([PLAN.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/PLAN.md),
section 9) serves the same products through REST and MCP in the
standard envelope, with correlation following once the series is a
query.

## Implementation notes

- swf-monitor: `monitor_app/snapper_platform.py` (maintainer;
  registration, projection, thresholds), invoked from
  `scripts/refresh-system-status.py` after the error maintainer; the
  server-host report model and `viewdir/snapper_platform_api.py`
  ingest; provider additions in `snapper_providers.py` (curve
  extraction `plat_` ids, families, the Platform focus view declaration
  listing its three components, the platform card, daemon lanes through
  `lane_entries`); the card kind in `_snapper_cards.html`; a
  `panda-platform` collector in `system_status.py`; the reporter script
  `scripts/panda-server-reporter.py` (standard library only) and its
  unit file under `tools/`; the alarm module
  `alarms/swf_alarms/alarms/panda_platform_health.py` with its
  `alarm_panda_platform_health` config row; this document and the
  SNAPPER.md maintainer list.
- snapper-ai: a focus-view declaration for a page-bottom summary
  section fed by the cut (the cut request already carries the view's
  left edge and the detail window; the summary needs the visible
  range's statistics, computed client-side from the loaded series and
  posted with the cut fetch, or computed server-side from the same
  product), and a `lane_entries` convention for daemon and service
  lanes if the health-lane path needs a second lane family; PLAN.md
  and INTEGRATION.md entries.
- The focus series cache TTL rule gains the platform key class (live,
  90 s) beside the task key; the series cache version bumps with the
  new curve vocabulary.
- Component bounds and the site map follow the PanDA component's
  catalog rule; the connections-by-application map folds beyond 16
  entries.
- Order of delivery: maintainer and component first (the record starts
  accruing), then the view with its summary, then the reporter and
  ingest, then detection; then the node health map (record, card, and
  alarm detail), the worker releaser on pandaserver01 in its proposal
  form, and the dig triggers of ERROR_ATTRIBUTION.md; correlation in a
  later round. Each stage is usable on its own.

## Related

- [SNAPPER.md](SNAPPER.md) — Snapper operations in SWF;
  [SNAPPER_ERRORS.md](SNAPPER_ERRORS.md) — the error-state component
  and view whose events this view reuses.
- [PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md) — the
  server-host reporter's functions and access.
- [ERROR_ATTRIBUTION.md](ERROR_ATTRIBUTION.md) — the label-correction
  service that consumes the same platform evidence.
- snapper-ai
  [DESIGN.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/DESIGN.md),
  [INTEGRATION.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/INTEGRATION.md),
  [TIME_HISTORY_UI.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/TIME_HISTORY_UI.md)
  — the contract, the provider seam, and the display laws.
