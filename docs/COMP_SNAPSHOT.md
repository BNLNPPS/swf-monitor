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
                   finished_1h, failed_1h, capacity, capacity_source, in_use,
                   finished_core_h_1h, failed_core_h_1h,
                   front: {state, reason, feed},
                   health, links}
  execute_sites[] {id, name, running, finished_1h, failed_1h,
                   capacity, capacity_source, in_use,
                   finished_core_h_1h, failed_core_h_1h, health}
storage[]         {id, name, holds, health, links}
report            {text, assessment: {verdict, at, link}}
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

A site's health is the worst of its queues'. An execute site's health
has the `prod_activity` part only.

### Storage

The E1s' Rucio storage, with health only (no volume yet):

| id | name | holds |
|---|---|---|
| `jlab-rucio` | JLab Rucio | science data |
| `bnl-rucio` | BNL Rucio | logs |

Parts for `jlab-rucio`:

- `registration`: jobs that ended in the last hour whose output
  registration failed or went to the failover stash, from the payload's
  job metrics.
- `latest_arrival`: the age of the newest file registered at JLab, from
  the storage record.

Parts for `bnl-rucio`:

- `log_stageout`: jobs that ended in the last hour with a log stage-out
  error.

The storage-door record's state joins both once the door check leaves
shadow mode.

### Report and errors

- `report.text`: a summary composed from the record on every build,
  without an LLM: the front, each live site's running and last-hour
  outcomes, and every non-green health part.
- `report.assessment`: the latest campaign assessment's verdict, time
  and link (daily, from the assessment record); `null` until it is
  wired.
- `errors[]`: a part that could not be read is listed here and left out
  of the record, never shown as zero.

## Links

Every queue links to its ePIC queue page (`panda/epic-queues/<queue>/`),
its errors view filtered to the queue (`panda/errors/?site=<queue>`) and
the canary probes page (`canary/probes/`: the canary root answers with a
redirect, which the proxy cannot relay). The pressure front links to the
front page (`panda/front/`) and every storage element to the storage
doors page (`panda/storage-doors/`). All links are on the public face
(`https://epic-devcloud.org/prod/...`), and each was checked to answer
200 there on 2026-10-21.
