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
30-minute heartbeat period. Job starts in the interval complete the
group. Question: were pilots being heard, and at the expected rate.

**Server (gauge, measured by the maintainer).** One timed request to
the PanDA server's status endpoint from the monitor host, recorded as
milliseconds, with a timeout recorded as a timeout, never omitted.
Question: was the server answering.

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

**Monitor host (measured locally by the maintainer).** The monitor's
own tier on pandaserver02: Apache WSGI process count and resident
memory, the ASGI service (swf-monitor-mcp-asgi) liveness and resident
memory, the prod-ops agent's resident memory, host load average,
memory and swap, root, /var, and /data volume use, and the monitor
database's connection count. Question: was the monitor itself under
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

**Panels, in order — load, platform, consequences — each family's
control row docked above its panel:**

1. *Jobs in flight* — the scope's in-flight jobs family (by state,
   stacked) with running cores as the overlay line, as the Site view
   draws them.
2. *Heartbeats* — received per interval and starts per interval,
   end-stamped; *Heartbeat yield* as its own small panel on a 0–1
   scale with a reference line at 1.
3. *DB connections* — active, idle, waiting stacked, with
   `max_connections` as the overlay line so proximity to the limit is
   visible without a second axis.
4. *Server latency* — milliseconds, timeouts drawn as markers at the
   panel top in the failure color.
5. *Heartbeat staleness* — the >30, >60, >120 minute counts stacked;
   a per-site grouping selector switches the stack to sites (the >120
   tier by site).
6. *Web tier* — request rates by endpoint class and the 5xx count,
   present when the reporter reports; daemon liveness renders as lanes
   above the panels (per-daemon continuous lanes on the health-lane
   mechanism: green alive, failure color silent), so a stalled
   copyArchive is a red band, not a number.
7. *Hosts* — one panel per host, server and monitor: load average with
   memory used as the overlay line; *Storage* — volume use per host as
   percent, one line per volume; *Processes* — WSGI and ASGI resident
   memory and process counts per host, with service liveness as lanes
   beside the daemon lanes.
8. *Kills* — the error-state component's events by component
   (dispatcher, taskbuffer, ddm, pilot, …), event-flow binned; the
   terminal-state chips apply as on the Errors view.
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
emits on transitions only — degradation start, escalation, end —
through the notice router as the error storm detector is planned to; a
freshness watch on the platform component itself, and the
reporter-status transition, report silence through the same path. A
`panda-platform` System Status collector reads the latest published
component so the platform state enters the System page and the health
lane without a second source.

## Retrieval

The component rides the existing Snapper retrieval surface unchanged:
the REST endpoints and MCP tools answer connection counts, staleness,
yield, and server-host state at any instant with the standard evidence
envelopes, and `changes_between` locates the transitions.

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
  unit file under `tools/`; this document and the SNAPPER.md maintainer
  list.
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
  ingest, then detection; correlation in a later round. Each stage is
  usable on its own.

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
