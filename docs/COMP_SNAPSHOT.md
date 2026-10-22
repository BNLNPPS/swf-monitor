# The comp snapshot — the computing centre's live record

The computing centre is a room in the etaverse dome. It lays out the ePIC
computing model for an operator standing at the door: DAQ and its exit
buffer near the door, the two E1s with their Rucio storage, epicprod and
the pressure front in the middle, and the production sites on the rear
arc (Torre Wenaus, 2026-10-21). The room is built and drawn on the
etaverse side. This document describes the one record it reads live: the
comp snapshot, which swf-monitor builds and serves. The room refreshes
everything it shows from that record without a reload: about once a
minute while someone is in the room, every thirty minutes otherwise.

## Where it is served

One builder, three faces, the same record:

- MCP: `swf_comp_snapshot()` on the swf-testbed server.
- REST: `/swf-monitor/panda/comp/snapshot.json` on pandaserver02.
- Public: `https://epic-devcloud.org/prod/panda/comp/snapshot.json`,
  through swf-remote's `panda/` catch-all (EXTERNAL_ACCESS.md). This is
  the face the room reads, from the browser, anonymously. Two swf-remote
  settings make that possible: the path is open past its login wall
  (`login_wall.py`, `OPEN_PREFIXES`), and its response carries
  `Access-Control-Allow-Origin: https://etaverse.com`, since the proxy
  relays no upstream header but the content type.

The record is a cached product ([CACHED_PRODUCTS.md](CACHED_PRODUCTS.md)),
key `comp_snapshot:v1`, TTL 60 s. A request is always answered at once
from the stored record. A request that finds it older than the TTL
starts one rebuild behind the answer. The rebuild cadence therefore
follows the room's polling and is never faster than once a minute, and
an empty room costs nothing. `refresh=1` rebuilds before answering.

The builder reads the PanDA and swf databases only: no remote call and
no LLM. Its cost is measured on every build and stored with the record
(`build_seconds`): 15 to 25 seconds on 2026-10-21, most of it the
census and the last hour's ended jobs. Only the first fill makes a
request wait for it.

## The record (schema 1)

Every number is the value at `observed_at` or over the hour before it.
Every entity carries a stable `id`. The room keys its layout on these
ids, so an id never changes once published. Display names may change.

```
schema            "comp-snapshot/1"
observed_at       ISO UTC
build_seconds     seconds the build took
units             {"capacity": "cores", "core_hours": "core-hours in the last hour"}
pressure_front    {ready_tasks, released_1h, enabled, mode, jedi_throttled, link}
sites[]           {id, name, status, capacity, capacity_source, in_use,
                   health, links, queues[], execute_sites[]}
  queues[]        {id, name, status, status_reason,
                   running, starting, queued, ungenerated,
                   harvester_slots_opened_1h, harvester_pilots_submitted_1h,
                   pilots: {waiting, waiting_schedd, limit, limit_at, running,
                            per_slot, report, report_age_seconds},
                   finished_1h, failed_1h, capacity, capacity_source, in_use,
                   finished_core_h_1h, failed_core_h_1h,
                   front: {state, reason, feed},
                   health, links}
  execute_sites[] {id, name, running, finished_1h, failed_1h,
                   capacity, capacity_source, in_use,
                   finished_core_h_1h, failed_core_h_1h, health,
                   links: {jobs}}
storage[]         {id, name, holds, status, counts,
                   arrived_1h, bytes_1h, events_1h, arrived_by_kind_1h,
                   failed_1h, pending, pending_parts, pending_at,
                   latest_arrival_s, by_site[], capacity, rses[],
                   health, links}
  by_site[]       {site, arrived_1h, bytes_1h, failed_1h}
  capacity        {rse, used_bytes, limit_bytes, fraction, as_of}
  rses[]          {id, name, type, used_bytes, limit_bytes, fraction,
                   files, as_of}
report            {text, items: [{level, text, link}],
                   assessment: {verdict, level, at, link, campaign,
                                title, narration}}
live              {link, items: [{id, at, level, importance, text, link}]}
errors[]          {part, error}
```

### Pressure front

- `ready_tasks`: the PCS tasks with a queued submission. These are the
  front's ready backlog (swf-epicprod CONTINUOUS_PRODUCTION.md, the
  front's placement).
- `released_1h`: the front's `fed` decisions in the last hour.
- `enabled`, `mode`, `jedi_throttled`: the front's SysConfig switches.

### Sites and queues

The grouping is the SysConfig key `comp.sites`, seeded with the layout
below, so a site or queue is added or regrouped without a deploy. OSG is
a site of its own, not BNL's: BNL is only the conduit its pilots are
submitted through, and its queues and execute sites belong to OSG.

| Site id | Name | Queues | Notes |
|---|---|---|---|
| `osg` | OSG | BNL_OSG_PanDA_1, BNL_OSG_EPIC_PROD_1 | `execute_sites[]` from the jobs' pilot-reported glidein site |
| `grex` | GREX | UM_GREX_PanDA_1 | University of Manitoba |
| `compute-canada` | Compute Canada | ComputeCanada_PanDA_1 | not built: awaiting SFU's identity mapping at the CEs |
| `nersc` | NERSC | NERSC_Perlmutter_epic, NERSC_Perlmutter_epic_es | |
| `google` | Google | BNL_ePIC_GOOGLE, BNL_ePIC_GOOGLE_es | |
| `jlab` | JLab | JLAB_PanDA_1 | not live: waiting for JLab's CE to route our jobs |

A queue's `status` is `live`, `off` (in PanDA, not taking production),
`not_live` (defined, not yet running work) or `not_built` (not in PanDA
yet). `status_reason` says why when the status is not `live`.

Per queue, from the PanDA database:

- `running` and `starting` separately. A job in `starting` holds a pilot
  slot but runs nothing yet; the room's cylinders use `running`.
- `queued`: activated, assigned, defined, waiting and sent jobs, the
  census's not-started set.
- `ungenerated`: input rows JEDI has not yet made into jobs.
- `harvester_slots_opened_1h`: harvester workers that started in the
  last hour, each a slot opened at the site.
- `harvester_pilots_submitted_1h`: harvester workers submitted in the
  last hour, into the site's batch system or the OSG pool, where they
  wait for a slot. (Until 2026-10-21 16:30 ET `harvester_slots_opened_1h`
  carried this count.)
- `pilots`: the queue's pilot slots by harvester's own count, from the
  harvester reporter's latest record (HARVESTER_REPORTER.md); `null` for
  a queue no harvester reports. `waiting` is the slots submitted and not
  yet started (an evicted pilot back in the batch queue included),
  `running` the slots running, `per_slot` the pilots in one slot (8 at
  GREX, 1 elsewhere). `limit` is the waiting limit harvester last
  applied, from its worker-adjuster log, and `limit_at` when it logged
  it: harvester computes the limit from its running slots under the
  queue's ratio, floor and cap, and the configured `nQueueLimitWorker`
  is only the cap (on 2026-10-22 OSG applied 200 under a cap of 300,
  GREX 610 under 625, JLab 10 under 50). `limit` is `null` until the
  reporter has seen the adjuster log it. Harvester submits new slots
  only while `waiting` is below its limit, so `waiting` is what the site
  has not yet started: at GREX
  on 2026-10-21, 592 slots waited while GREX ran 118 and started 3 in an
  hour. For the OSG queues `waiting_schedd` is the idle pilots the
  submit schedd actually holds, from the OSG submit reporter
  (OSG_SUBMIT_REPORTER.md). The two disagree when pilots leave the
  schedd without harvester's knowledge: on 2026-10-21 harvester counted
  4,311 waiting at BNL_OSG_PanDA_1 against 131 in the schedd, after
  waiting pilots were removed there directly, and submitted none.
  `report_age_seconds` is the harvester record's age; the reporters run
  every five minutes.
- `finished_1h`, `failed_1h`: production jobs that ended in the last
  hour.
- `front`: the front's latest decision for the queue.

### Capacity

The unit is the core. Every production job today uses one core, so jobs
and cores agree. A multicore job later counts its cores.

- `capacity`: the cores available to us, with `capacity_source` naming
  where the figure comes from:
  - `pool`: the OSG pool's slots that our pilots' requirements admit,
    from the submit host's report (SNAPPER_PLATFORM.md). The pool is
    one behind both OSG queues, so the OSG site carries it as its
    capacity and each OSG queue repeats it as `pool, shared`;
  - `allocation`: a batch allocation's cores (Perlmutter);
  - `configured`: the SysConfig key `comp.capacity.<queue>`, an
    operator's figure, which overrides the others;
  - `observed peak`: the most jobs seen running in the census's
    calibration window. This is the site's discovered limit where our
    supply is open-ended, as at GREX: harvester keeps 125 to 625 of our
    8-pilot slots waiting there and GREX's scheduler starts about 120 to
    128 at a time (2026-10-21), so the peak is GREX's limit, not ours.
    The pilot controller's running target (SysConfig
    `pilot.queue.<queue>.target_running`, GREX 1,000) is our figure, not
    a capacity, and is not used.
- `in_use`: running cores now.
- `finished_core_h_1h`, `failed_core_h_1h`: the core-hours spent in the
  last hour by jobs that finished and by jobs that failed. A running
  job's outcome is not known until it ends, so the room splits the used
  part of a pillar green and red by this ratio.

An execute site's capacity is `null` until the submit host reports the
pool's admitted slots per site. A queue that is not live has no
capacity, and its health is a single grey `status` part. A site's
capacity is its pool, or the sum of its queues' capacities.

### Health

Every site, queue, execute site and storage element carries
`health: {level, parts}`. `level` is `green`, `amber`, `red`, or `grey`
when there is no evidence either way (an idle queue, a canary with too
few jobs). It is the worst of its parts. Each part is
`{level, value, note}`.

A queue's parts:

- `prod_activity`, from the last hour's ended jobs: `grey` with none;
  `red` with no finished job and ten or more failed; `amber` when failed
  jobs are more than a quarter of those that ended; otherwise `green`.
- `canary`, the queue's canary verdict as the front reads it: `healthy`
  is green, `failing` red, `insufficient` and `unknown` grey.
- `snapper_errors`: the last hour's failures by Snapper error category
  (SNAPPER_ERRORS.md). The note names the top category with its count.
  The part is `red` when one category accounts for more than half the
  ended jobs, the signature of an outage, and `amber` when the failures
  are more than a quarter of them.
- `pilot_supply`, whether pilots keep reaching the queue, from the
  queue's `pilots` reading. Harvester submits only while its own count
  of waiting pilots is below its limit, and that count changes only
  when its monitor rechecks a pilot, so a count the schedd no longer
  bears out stops supply while the queue reads full. With jobs queued
  the part is `red` (`amber` with none queued) when:
  - harvester counts at least twice its limit while the schedd holds
    fewer than the limit: pilots left the schedd without harvester's
    knowledge (2026-10-21, 4,311 counted against 131 held);
  - the pilots the pool can start, those the schedd holds idle plus
    those harvester will still submit before its count reaches its
    limit, are fewer than half the limit: harvester has not yet seen
    its waiting pilots start. On 2026-10-21, with harvester's recheck at
    30 minutes, it counted 196 against a limit of 200 while the schedd
    held 39, so the pool could start 43, and OSG's running fell from
    1,137 to 433 in five hours; four minutes after the recheck went to
    five minutes the schedd held 163 of 200.

  It is `red` also when jobs are queued and in the last hour no pilot
  waited, was submitted or started. The two schedd rules need the OSG
  submit reporter's count and apply to the OSG queues only. `grey` when
  no harvester report names the queue, the report is more than fifteen
  minutes old, or the limit harvester applies is not yet reported;
  otherwise `green`, with the counts in the note.

A site's health is the worst of its queues'. An execute site's health
has the `prod_activity` part only.

### Storage

The E1s' Rucio storage, reported as the queues are: what arrived in the
last hour, what did not, what is still owed, and the capacity.

| id | name | holds | counts |
|---|---|---|---|
| `jlab-rucio` | JLab Rucio | science data | files |
| `bnl-rucio` | BNL Rucio | logs | logs |

`counts` names the unit of the counts. Production tasks register their
science outputs in JLab Rucio themselves, so PanDA's file table holds
none of them; the only file PanDA stages for a production job is the
pilot's log tarball, registered in BNL Rucio
(swf-epicprod LOG_STAGEOUT_FALLBACK.md).

For `jlab-rucio`, from the payload reports of the last hour's finished
production jobs (the PanDA metatable, swf-epicprod
EPICPROD_PAYLOAD.md):

- `arrived_1h`: the files the jobs registered, each DID the report's
  registration lists. A clash registered under a derived name (outcome
  `diverted`, swf-epicprod RUCIO_RESILIENCE.md, Measure 3) is a
  registered file.
- `bytes_1h`, `events_1h`: the bytes and events of those files, from
  the report's outputs joined to the DIDs by file name. A file
  registered under a derived name matches no output and adds no bytes.
- `arrived_by_kind_1h`: the files by output kind (`reco`, `full`, an
  internal EVGEN task's own kind), `other` for a derived name.
- `failed_1h`: jobs whose registration did not complete (`pending` or
  `unfinished`); the registrar completes a pending registration later.
- `pending`: the files owed the catalog now, the sum of
  `pending_parts`: `stash`, the failover stash's entries not yet home
  (the drain's record, cached product `stash_state`,
  RUCIO_FAILOVER_STASH.md), and `registrar`, the registrations the
  registrar's last pass deferred (its action record). The drain runs
  every 30 minutes and the registrar hourly; `pending_at` is the older
  of the two passes.
- `capacity`: the production output RSE, BNL-XRD, which every
  production configuration names as its output RSE; `rses[]`: every
  JLab RSE. Both are from the latest storage component
  (swf-epicprod STORAGE.md, per RSE capacity), refreshed every four
  hours: the production account's usage, its limit and the fraction,
  with the usage record's time. On 2026-10-22 BNL-XRD held 582.6 TB
  against a limit of 750 TB.

For `bnl-rucio`, from the PanDA job and file tables over the jobs that
ended in the last hour:

- `arrived_1h`, `bytes_1h`: the logs registered and their bytes. A
  failed job's log is registered too: PanDA marks every file of a failed
  job failed, but the log is at BNL_PROD_DISK_1 (checked on
  2026-10-22). Only a job failed at log stage-out, or one whose pilot
  held its log in the stage-out bucket (`logHeld`), has no log there.
- `failed_1h`: jobs failed at log stage-out.
- `pending`: logs held in the stage-out bucket and not yet moved into
  BNL Rucio, from the hourly log-rescue pass's record:
  `pending_parts.held` over its window less `pending_parts.moved`.
- `capacity` is `null` and `rses[]` empty: no usage record covers
  BNL_PROD_DISK_1.

For both, `latest_arrival_s` is the seconds since the newest
registration, and `by_site[]` splits `arrived_1h`, `bytes_1h` and
`failed_1h` by the producing site (a `sites[]` id). Each reading is a
part of its own: one that cannot be read is listed in `errors[]` and its
fields are `null`.

Health parts for `jlab-rucio`:

- `registration`: the share of the last hour's finished jobs whose
  registration did not complete; `amber` over 5%, `red` over half.
- `latest_arrival`: the age of the newest registration; `amber` over 30
  minutes, `red` over two hours or with none in two days.

Health parts for `bnl-rucio`:

- `log_stageout`: jobs that ended in the last hour with a log stage-out
  error; `amber` from 5, `red` from 50.

The storage-door record's state joins both once the door check leaves
shadow mode.

### Report and errors

- `report.text`: a summary composed from the record on every build,
  without an LLM: the front, each live site's running and last-hour
  outcomes, and every non-green health part.
- `report.items`: what the room's floor cannot show, one line each as
  `{level, text, link}`, red first, then amber, then grey and green,
  with no per-site counts (the pillars carry those): why the front
  feeds nothing (the pressure front with no ready task, each live
  queue's front decision in the words of CONTINUOUS_PRODUCTION.md's
  states and reasons), every amber or red health part with its note,
  every active alarm event (red; a ping, a dated obligation, amber),
  storage parts not green, why each site that is not live is off, and
  whether the scheduled campaign assessments arrived (the System
  page's campaign-assessments row; the verdicts are held by corun-ai,
  which this build does not call). The text before the first ": " names
  what the line is about.
- `report.assessment`: the newest daily campaign assessment, from its
  `assessment_register` action record (swf-epicprod
  EPICPROD_ASSESSMENTS.md): the verdict, the verdict as a health level
  (`ok` green, `attention` amber, `alarm` red, anything else grey), the
  registration time, the assessment's page, the campaign, the report
  title and the narration; `null` when none is registered.
- `errors[]`: a part that could not be read is listed here and left out
  of the record, never shown as zero.

### Live

The room's epicprod-live screen (Torre Wenaus, 2026-10-22): the ten
newest records of the epicprod live stream at level WARNING, ERROR or
CRITICAL, newest first, each with its time, level, importance (the
record's sublevel), message and page (`logs/<id>/`); `live.link` is the
logs page's live view (`logs/?app_name=epicprod&live=1`). The stream is
the logs page's own selection (`live_stream_q`, ACTION_STREAM.md). The
records ride in the snapshot because the room reads it anonymously and
the logs page is behind the login wall; the messages are those the
action stream records, which name tasks, queues, nodes and errors and
carry no credential.

The screen shows the ten newest such records that are still open. A
failure (outcome error, timeout, partial or unrecorded) leaves the screen
once it has recovered: a later record of the same action from the same
source on the same subject reports ok, the identity the epicprod-live
channel uses for its recovery notices (`live_notices.failure_key`). Any
record leaves the screen when the operator's question about it has been
answered: a `live_cleared` record in the action stream names the cleared
record ids and the reason. Neither changes the records on the logs page.
The `epicprod_live_clear` MCP tool writes one; out of process,
`scripts/live-clear.py <id> ... --reason "..."` writes one through the log
REST endpoint.

## Links

Every queue links to its ePIC queue page (`panda/epic-queues/<queue>/`),
its errors view filtered to the queue (`panda/errors/?site=<queue>`) and
the canary probes page (`canary/probes/`: the canary root answers with a
redirect, which the proxy cannot relay). The pillars' drilldowns: a
queue's `links.running` opens its running jobs
(`panda/jobs/?site=<queue>&status=running`), `links.queued` its queued
jobs (`status=queued`, the census's not-started set, what `queued`
counts), `links.slots` the Workers card of its queue page
(`#workers`); an execute site's `links.jobs` opens the jobs whose pilot
reported that site (`panda/jobs/?execsite=<site>`). A site's links are
those of its busiest live queue, for OSG the queue production runs on.
The pressure front links to the
front page (`panda/front/`) and every storage element to the storage
doors page (`panda/storage-doors/`). A storage element's pillars open:
for `jlab-rucio`, `links.arrived` the Storage view
(`snapper/epicprod/storage/`) and `links.pending` the stash page
(`pcs/stash/`), with `links.failed` `null`, since no page lists pending
registrations; for `bnl-rucio`, `links.arrived` the finished jobs
(`panda/jobs/?status=finished&days=1`) and `links.failed` the pilot
errors (`panda/errors/?error_source=pilot&days=1`), with
`links.pending` `null`, since no page lists held logs. The daily
assessment links to its own page (`ai/assessments/<campaign>/daily/<date>/`).
All links are on the public face (`https://epic-devcloud.org/prod/...`),
and each was checked to answer 200 there on 2026-10-21, the storage
and assessment links on 2026-10-22.

## Regulation telemetry, 22 October 2026

The 22 October regulation correction reads fixed ratio-zero waiting limits from the fresh Harvester database report. Ratio-derived limits retain the last applied adjuster observation, with its timestamp and `limit_source: adjuster`; the health note calls this the last applied limit. The adjuster skips that log line while the waiting queue is full, so an old event timestamp alone does not invalidate the value. The enclosing report must still be fresh, and a failed adjuster-log read is degraded. A limit never observed remains unknown. Missing or stale pilot evidence is amber when work waits, so another green component cannot conceal missing protection. The scheduled production notifier evaluates this record independently of browser requests.
