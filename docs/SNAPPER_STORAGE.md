# Snapper Storage — the Storage view

The placement state of production data on the JLab Rucio Storage
Elements (RSEs) is recorded in Snapper by the `storage` component
(swf-epicprod
[STORAGE.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/STORAGE.md)):
per RSE the inventory by replica state, the copying backlog and its
ages, ghosts, rules, capacity, and cumulative counters of arrivals,
transfers, deletions and ghost movement; per campaign the replica
protection, archival backlog, catalog quality, dataset state and
pipeline latencies. This document is the design of the Storage view
over that record: a dedicated focus view showing the data lifecycle
per RSE on one time axis, with the RSE's standing at the cut. It
follows the focus-view mechanism and display laws of the Site,
Errors and Platform views ([SNAPPER.md](SNAPPER.md),
[SNAPPER_ERRORS.md](SNAPPER_ERRORS.md),
[SNAPPER_PLATFORM.md](SNAPPER_PLATFORM.md); snapper-ai
[INTEGRATION.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/INTEGRATION.md),
[TIME_HISTORY_UI.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/TIME_HISTORY_UI.md)).

This is a design document; the sections below are the agreed plan of
record for implementation.

## The record the view reads

The `storage` component is published by the storage pass after every
run: a census once, a full pass nightly in the `catalog_sync` chain,
an incremental pass hourly. A pass in which nothing moved is affirmed
unchanged, so the component's snaps are the passes that changed
something, at most hourly. The view derives nothing from the pass's
store or from Rucio: every plotted value is a field of the published
projection (DESIGN.md, invariants 1 and 4). The exception listings
beyond the component's bounded heads live on the Storage exceptions
page, which reads the store (STORAGE.md, Retrieval).

The quantities, by kind, as the projection carries them:

- **Gauges at the pass instant**, per RSE: capacity (used, total,
  limit, fill fraction), inventory files and bytes by replica state,
  by campaign and by root, dataset placement counts, rule locks by
  state, the copying backlog with its age distribution, the ghost
  population by state and by campaign. Per campaign: files and bytes,
  replica protection, unattached and count-less files, archival
  backlog bytes, dataset state.
- **Cumulative counters**, monotonic from the census with an
  arbitrary origin, per RSE: arrived files and bytes as first copies
  and as replicas, transfers completed, deleted files and bytes,
  ghosts appeared and cleared, bad replicas appeared. Per campaign:
  arrived and archived files and bytes. Every consumer differences
  two instants; the view bins them at render.
- **Interval assessments**, per campaign: the latencies of the
  interval's arrivals as count, median and 90th percentile.
- **Exception heads**: the oldest fifty ghosts, stuck rules and
  stalled datasets with the exact remainder as overflow counts.
- **Assessment**: per-RSE and per-campaign verdicts against the
  SysConfig thresholds and the overall verdict.

The RSEs recorded today are ASGC-XRD, BNL-XRD, EIC-CLOUD-LOG,
EIC-XRD, EIC-XRD-LOG, JLAB-TAPE-SE (tape), MANITOBA-XRD and XRD6,
with the pseudo-RSE `none` holding registered files that have no
replica row. The roots are RECO, FULL and EVGEN. The target campaigns
are the delivery record's: the current and last campaigns and any
campaign producing.

## The Storage view

A dedicated focus view, `Storage`, on the mechanism the Site, Errors
and Platform views use: its own clean path under the epicprod scope
(`/snapper/epicprod/storage/`), a focus-sized cached series product
over the `storage` and `errors` components' snaps, and its own
detail rendering. It is the data-lifecycle counterpart of the Site
view, which is the job lifecycle per queue, and conforms to it: one
option per RSE with tick boxes, presentation ordered by activity, a
jump list, and the RSE's detail docked beneath its own panels.

**Parameters**, in conformance with the Site and Campaign views:

- focus `rse`: one option per recorded RSE, default all. With several
  shown, presentation follows holdings, the files on the RSE over the
  window: RSEs ordered by that peak, so an RSE with data lands open
  whatever the window's arrivals; RSEs holding nothing are the idle
  ones, last in alphabetical order with their sections closed; a jump
  list under the tick row in the same order with each peak in
  brackets.
  The log stores and the `none` pseudo-RSE are options like any other
  and sort by their own activity.
- **Counting** selector: bytes (default; TB, as Rucio states usage and
  limits) or files. The panel title states the unit.
- **Grouping** selector: by campaign, by replica state, or by data
  root. It applies to the backlog, ghost and inventory panels; the
  other panels are the same under every grouping. The campaign
  grouping shows the target campaigns with every other campaign
  folded into `other`, as the record folds them.
- **Show** selector: RSE capacity (the default and the landing: each
  RSE's usage against its limit over time, and directly beneath the
  panels the capacity table across RSEs at the cut instant, the rows of
  `rucio account limit list eicprod`; the card carries nothing else
  under this reading), ghosts (each RSE's ghost population, flow and
  yield, with its capacity), or the whole data lifecycle. The choice
  rides the URL, so each reading is a bookmark.
- the window, cut, zoom and curve selection every report page
  carries. The clean page lands on the last seven days (the view's
  default window; a signed-in user's remembered window takes
  precedence), with no floor at the record's first snap: a young
  record leaves the left of the window empty rather than shrinking
  the window to its span.

**Families per RSE**, panel order following the lifecycle, rates
first, then backlogs, then state. Every family's control row is
docked above its own panel.

1. *Arrivals* — first copies and replicas per bin, stacked, from the
   cumulative arrival counters projected to per-interval deltas at
   render (the counter-flow mode). Under bytes counting the same from
   the byte counters.
2. *Transfers and deletions* — completed transfers and deleted files
   per bin, two flows on one panel. Under bytes counting, deleted
   bytes; the record carries no byte count for completed transfers.
3. *Copying backlog* — files in the copying state at the pass
   instant, stacked by the grouping (campaign or root from the
   inventory maps; under the state grouping the panel is the copying
   total), with the count over the stuck threshold as an overlay
   line. *Copying age* follows as a small panel: the median, 90th
   percentile and maximum age in hours.
4. *Ghosts* — the ghost population held at this RSE, stacked by the
   grouping: by state and by campaign from the record; the record
   carries no ghosts by root, so under the root grouping the panel
   shows the total until the pass records it (see Record additions).
   Under bytes counting the total only. *Ghosts appeared and cleared*
   follows: per-bin flows from the two counters. *Ghost yield* follows
   that: ghosts appeared over registrations (first copies arrived plus
   ghosts appeared) per bin, derived at series time, the upload
   failure rate of the RSE read against its arrival rate.
5. *Inventory* — files or bytes by replica state, stacked; under the
   campaign and root groupings, the per-campaign or per-root totals
   over all states.
6. *Rule locks* — replicating and stuck lock counts, two lines.
7. *Capacity* — the production account's usage at this RSE in TB,
   with the account's limit as an overlay line where the catalog
   reports one; under files counting, the account's file count. This
   is the quantity `rucio account limit list eicprod` prints, tracked
   over time. The fill fraction and the quota left are on the card and
   in hover, not curves. Until the pass records the account usage
   (see Record additions), the panel plots the RSE-wide usage the
   record carries, and says so in its title.

**Scope-level families**, after the RSE sections, one family per
target campaign where the record is per campaign:

- *Copies* — single-copy and two-or-more-copy files, stacked.
- *Placement* — disk-only, tape-only and disk-and-tape files, stacked.
- *Archival backlog* — bytes on disk and not on tape, one line per
  campaign on one panel.
- *Catalog quality* — unattached files and files without the event
  count the registration contract requires, per campaign.
- *Datasets* — open, partial-anywhere, quiet-open and stalled dataset
  counts per campaign.
- *Latency medians* — job end to registration, registration to
  availability, first to second copy, disk to tape, in hours, per
  campaign, plotted at the pass stamp as the interval's assessment.
  Kinds the pass has not yet observed are absent.
- *Arrived and archived* — the campaign's arrival and archive counters
  as per-bin flows.

**Consequences**: the error-state component's data-management job
failures (the `ddm` error component) as an event-flow strip beneath
the panels, with the terminal-state chips of the Errors view. The
error entries carry no RSE, so the strip is scope-level, once; the
attribution of a failure to its destination RSE is not recorded and
is not shown.

State curves hold their last recorded value to the present: the record
is hourly and a quiet pass affirms the state unchanged, so the last
pass's gauges are the state until the next pass; flows stay per-bin.
Units on every panel title; house state colors where a state is
drawn (available blue, copying the warning color, unavailable and
bad the failure color, tape grey); campaign and root members take the
palette. Counter-flow bins come from the window's round ladder on the
ET-midnight grid, as the Site completions panel draws them.

**The cut.** A click is a time cut. For each shown RSE the card
renders the RSE's standing at that instant, docked beneath the RSE's
own panels as the Site view docks its queue slice:

- the interval basis once: the pass that produced the state, its
  mode, and the interval it covers;
- the verdict chips for the RSE with the threshold each names, in
  the warning color where crossed;
- inventory by replica state, files and bytes, with the change
  against the previous snap;
- datasets: total, complete, partial, empty, unavailable;
- the copying backlog: files and bytes, ages, the count over the
  threshold;
- ghosts: files and bytes, by state, the oldest age, the mean ghost
  size against the mean first copy arrived at the RSE (the size
  fingerprint of a large-file timeout), and the reading of the states
  (a copying ghost is an upload that never completed, an unavailable
  ghost a replica lost after arrival), linking to the Storage
  exceptions page filtered to the RSE;
- rule locks by state, the oldest stuck age, rules expiring within
  thirty days;
- capacity: the account's usage, the limit, the quota left and the
  fill fraction, with the RSE-wide usage beside them; a total only
  where a storage source reports one (the catalog's own usage source
  reports none);
- the flow since the window's left edge, differenced from the
  counters at the basis snap: first copies, replicas, transfers,
  deletions, ghosts appeared and cleared, bad replicas.

After the RSE sections the card renders the scope section once. It
opens with the capacity table across every recorded RSE at the cut
instant, the rows of `rucio account limit list eicprod` with the
record's additions: RSE, type, usage, limit, quota left, fill
percent, files, and the time of the usage record. Then per target
campaign the files and bytes, protection, archival backlog,
dataset state, latencies and the flow since the window start; then
the exception heads the component carries, ghosts, stuck rules and
stalled datasets, each dataset a link to its DID page, with the
overflow counts and a link to the Storage exceptions page for the
full lists. Every swatch is the curve's color, as on every card.

**Thresholds** — the stuck, stalled and single-copy ages — are the
pass's SysConfig keys (`storage_copying_stuck_hours`,
`storage_stalled_hours`, `storage_single_copy_warn_days`), present at
their defaults; the card marks a crossed threshold in the warning
color and names the key.

## Retrieval

The view's products ride the Snapper retrieval surface unchanged:
`snapper_series` and the REST series endpoint serve the focus
view's series exactly as the page builds and caches it, and the
component's state at any instant answers through `snapper_state_at`
and `snapper_changes_between`. The exception listings are the
`epicprod_storage` tool and its REST counterpart (STORAGE.md,
Retrieval).

## Record additions requested from the pass

The view is designed to the projection as published. STORAGE.md
names four quantities the projection does not yet carry, and the
capacity family needs a fifth; the families that need them are added
when the pass records them:

- the production account's usage per RSE, bytes and files, from the
  account usage listing, beside the RSE-wide usage the record carries
  today (the catalog's `rucio` usage source counts every account, and
  reports `total` equal to `used`), for the Capacity family and the
  capacity table;
- ghosts by root per RSE, for the ghost panel under the root grouping;
- ghosts per target campaign, for the catalog-quality family;
- jobs finished per campaign, for the arrival yield family (files
  arrived over jobs finished);
- the stage-out map, first copies landed per compute site, for the
  stage-out matrix.

## Implementation notes

- swf-monitor `monitor_app/snapper_providers.py`: curve extraction
  from the storage component under the `sto` prefixes (`sto`, a
  two-letter code for the quantity and panel kind, the RSE or campaign
  as the second segment, the member last; campaign segments carry
  hyphens for dots), the labels and colors, the families resolved per
  render from the record's RSE and campaign inventories, the Storage
  focus view declaration with its two selector axes and the per-option
  activity curve, and the storage card. The component card
  registration and the focus-view tuple gain the storage entries. The
  scope-level campaign families ride a `Campaign totals` option
  declared `pin: 'last'` (snapper-ai INTEGRATION.md), so they render
  after the ranked RSE sections and never as idle. The small
  secondary panels (copying age, ghost flow, rule locks, catalog
  quality, datasets, latency) start closed behind their rotators.
- `monitor_app/templates/monitor_app/_snapper_cards.html`: the
  `storage` card kind, the RSE sections keyed for docking.
- The focus series cache TTL rule gains the storage key class
  (live, 90 s) beside the task and platform classes; the series cache
  version bumps with the new curve vocabulary (snapper-ai
  `_focus_cache_key`).
- [SNAPPER.md](SNAPPER.md) lists the storage maintainer among the
  component maintainers; this document joins the doc index.
- Order of delivery, each stage usable on its own: the curve
  extraction, families and focus view, which plot from the first
  published snap; the cut card; the consequences strip; the families
  awaiting record additions, after the pass records them.

## Related

- swf-epicprod
  [STORAGE.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/STORAGE.md)
  — the storage record: the pass, the component, the retrieval
  surface, the Storage exceptions page.
- [SNAPPER.md](SNAPPER.md) — Snapper operations in SWF;
  [SNAPPER_PLATFORM.md](SNAPPER_PLATFORM.md) and
  [SNAPPER_ERRORS.md](SNAPPER_ERRORS.md) — the sibling focus views
  and the error-state component whose events the consequences strip
  reuses.
- snapper-ai
  [DESIGN.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/DESIGN.md),
  [INTEGRATION.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/INTEGRATION.md),
  [TIME_HISTORY_UI.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/TIME_HISTORY_UI.md)
  — the contract, the provider seam, and the display laws.
