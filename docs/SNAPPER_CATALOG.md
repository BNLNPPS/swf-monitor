# Snapper Catalog — the JLab Rucio catalog component and view

The behavior of the JLab Rucio catalog, the catalog of record every
production job registers its outputs in, becomes part of the recorded
system history: a `catalog` component captured in Snapper snaps, and a
Catalog view showing what the jobs asked of the catalog, what they got,
and how the catalog was answering, on one time axis. It follows the
platform component's shape ([SNAPPER_PLATFORM.md](SNAPPER_PLATFORM.md):
a maintainer on the five-minute refresh, measured gauges beside
assessed interval readings) and the errors component's evidence idiom
([SNAPPER_ERRORS.md](SNAPPER_ERRORS.md): an interval's events as rows
that any window sums), on the mechanism of [SNAPPER.md](SNAPPER.md).

The component, the view and the backfill are in production since
2026-09-16 (the latency and registrar groups in their first form; the
queue selector, the alarm and the registrar's completions follow).
Design of record, written 2026-09-16 on the day the catalog collapsed
under three 50,000-job tasks: the Rucio server's WSGI daemon saturated
(JLab: "listener backlog limit exceeded"), registrations went from
none failing at 09:30 ET to 77% failing by 12:30, and 8,000 finished
jobs were lost before the tasks were paused. Nothing recorded showed
it as it happened; the Storage view's arrivals would have shown it
sagging, hours later.

## Historical questions

- At an instant, was the catalog answering, and how fast, from the
  platform's side and from the jobs' side?
- How many registrations were attempted in the interval, and what
  became of them: registered, pending (the catalog unreachable, the
  registrar owes it), stashed at BNL, or lost with the job?
- When the catalog degraded, which came first, the load or the
  latency, and by how much lead?
- What did the registrar owe, and was it draining?

## The catalog component

Internal name `catalog`, epicprod scope, published by a maintainer
module (`monitor_app/snapper_catalog.py`) beside the platform and
error-state maintainers on the same five-minute System-status refresh.
Publisher identity `swf-monitor:jlab-catalog`, assessment policy
`swf-jlab-catalog-v1`, schema version 1, canonical JSON bounded at
64 KiB. Four groups, each with its kind:

**Probe (gauges, measured by the maintainer).** From the swf-monitor
host, timed: a TLS handshake with the Rucio server (`rucio.cfg`'s
`auth_host`), the server's `/ping`, and one authenticated light read
through the production account (the metadata of a fixed dataset), each
as milliseconds with a timeout recorded as a timeout, never omitted.
Question: was the catalog answering, from outside the job wave.

**Registrations (interval assessment, from the job record).** Over
the half-open interval since the previous publication, every
production job that ended, read from the PanDA record's job-metrics
digest, which every job carries whatever its end
(`payloadRegistration`, `payloadExit`, `payloadVersion`,
EPICPROD_PAYLOAD.md § Reporting from a job that fails): counts of
jobs whose registration was `ok`, `pending`, `stashed`, `failed`
(the job ended with the registration lost), and `not_reached`;
of the failed, the split by payload exit code (1, 78, 81); per
queue in a bounded map. Question: what the jobs asked, and what they
got.

**Latency (interval assessment, from the payload reports).** The
registration stage's wall seconds over the jobs of the interval whose
report has been ingested (`swf_epicprod_jobs`, the report sweep and
the outputs ingest): count, median and 90th percentile, and the same
for the jobs whose registration succeeded alone. The report lags the
job by the ingest cadence, so this reading trails the registrations
group; the probe is the live measure. Question: how long a job waited
on the catalog.

**Registrar (gauges).** Pending registrations owed and their oldest
age, stashed outputs awaiting their JLab registration, from the
registrar's own record (RUCIO_RESILIENCE.md, Measure 2), and the
registrar's completions in the interval. Question: was the backlog
draining.

**Assessment.** Verdicts: the probe over its timeout or over a
SysConfig latency threshold; the failed share of registrations over a
threshold with at least a floor of attempts; the pending backlog older
than a threshold. The overall verdict is the worst. Publication
follows the maintained-assessment rule: a change in any assessed value
or verdict publishes; a quiet interval affirms.

## The Catalog view

A focus view, tab `Catalog`, page `Rucio Catalog`, at
`/snapper/epicprod/catalog/`, the
mechanism of the Site, Errors, Platform and Storage views: a
focus-sized cached series over the `catalog` component's snaps, its
own detail rendering, the window, cut, zoom and curve selection every
report page carries; the clean page lands on the last 24 hours.

**Families**, panel order following the question: what was asked,
what came back, how fast, what is owed.

1. *Registrations* — jobs ended per five minutes stacked by
   registration outcome: registered, diverted, pending, unfinished,
   failed, not reached (the job ended before its registration stage),
   no digest (the payload wrote no report: it died before the report
   or predates it); house state colors (registered blue, pending the
   warning color, failed the failure color, the last two solid greys
   that read on white). A reading covers the interval since the
   previous publication, five minutes on the drumbeat and longer
   across a missed cycle, so every interval's count is scaled to five
   minutes on the curves: a 34-minute interval after a deploy reads as
   its rate, not as a seven-fold spike. The cut card keeps the
   interval's own count and bounds. One panel per queue under a queue
   selector, default all queues summed.
2. *Failed by exit code* — the failed member alone, stacked by payload
   exit code and scaled the same way, so a lost registration (1, 81
   under payloads before 0.18.1) reads apart from a refused one (78).
3. *Catalog latency* — the probe's three timings as lines in
   milliseconds, timeouts drawn at the timeout value in the failure
   color; the registration stage's median and 90th percentile from
   the reports as a second panel in seconds, where the reports exist.
4. *Registrar* — pending owed and stashed awaiting as bands; the
   oldest pending age as a line; completions per bin.

**The cut.** A click renders the catalog's standing at that instant
on the card: the probe timings with the thresholds, the interval's
registration counts and shares with the failed split by exit code and
by queue, the latency figures, the registrar's owed counts and oldest
age, and the verdict chips with the threshold each names.

**Thresholds** are SysConfig keys seeded at their defaults:
`catalog_probe_timeout_s` (10), `catalog_probe_slow_ms` (2000),
`catalog_failed_share` (0.10), `catalog_failed_floor` (20),
`catalog_pending_old_hours` (6).

## Backfill

The registrations group is reconstructible: the digest is on every
job in the PanDA record. A one-off script
(`scripts/backfill-catalog-intervals.py`, the errors backfill's shape)
writes one synthetic snap per non-empty five-minute interval under
capture policy `backfill-catalog-v1`, reconstructed evidence
distinguishable from observed snaps, carrying the registrations group
and, where the ingested reports allow, the latency group; the probe
and registrar groups are absent in a backfilled snap, as the
reporter's fields are absent before it ran on the platform component.
Idempotent, dry run by default, bounded to intervals before the first
live snap. The first run covers 2026-09-15 onward, so the collapse of
2026-09-16 is on the record end to end.

## Retrieval

The view's series ride `snapper_series` and the REST series endpoint;
the component's state at any instant answers through
`snapper_state_at` and `snapper_changes_between`.

## Implementation notes

- `monitor_app/snapper_catalog.py`: the maintainer, registered beside
  the platform maintainer in the System-status refresh; the probe with
  the production proxy the refresh doer already holds; the interval
  read of the digest (`jobmetrics` regex, one query over the interval's
  ended jobs, jobsactive4 and jobsarchived4); the latency read over
  `swf_epicprod_jobs`; the registrar read.
- `monitor_app/snapper_providers.py`: curve extraction under the `cat`
  prefixes, the families, the focus view declaration with the queue
  selector, the card; the focus series cache TTL rule gains the
  catalog key class; the series cache version bumps.
- `_snapper_cards.html`: the `catalog` card kind.
- `scripts/backfill-catalog-intervals.py`.
- The alarm: the platform-health alarm's shape on the catalog verdict,
  after the component has been observed through one week.
- Order of delivery, each stage usable alone: the component with the
  registrations and probe groups and the view's first three families,
  then the backfill, then the latency and registrar groups and their
  families, then the alarm.

## Related

- [SNAPPER.md](SNAPPER.md), [SNAPPER_PLATFORM.md](SNAPPER_PLATFORM.md),
  [SNAPPER_ERRORS.md](SNAPPER_ERRORS.md), [SNAPPER_STORAGE.md](SNAPPER_STORAGE.md).
- swf-epicprod
  [RUCIO_RESILIENCE.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/RUCIO_RESILIENCE.md),
  [EPICPROD_PAYLOAD.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_PAYLOAD.md)
  (the digest, exit codes 78, 80, 81, 85).
