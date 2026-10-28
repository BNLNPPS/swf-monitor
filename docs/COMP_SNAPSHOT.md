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

The record is also the feed for the production dashboard on the web, the
one collaborators will use; the room is its proving ground (Torre Wenaus,
2026-10-23: "every feed you send to the floor is a feed you will send to
our prod dash"). Whatever is added for the room is shaped for both
readers: counts, levels, plain-text detail and links, never the room's
geometry.

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
pressure_front    {ready_tasks, released_1h, enabled, mode, jedi_throttled, link,
                   detail, cycle_at, items[], queues[]}
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
  execute_sites[] {id, name, short, running, finished_1h, failed_1h,
                   capacity, capacity_source, in_use,
                   finished_core_h_1h, failed_core_h_1h, health,
                   links: {jobs}}
storage[]         {id, name, holds, status, counts,
                   arrived_1h, bytes_1h, events_1h, arrived_by_kind_1h,
                   failed_1h, pending, pending_parts, pending_at,
                   latest_arrival_s, flow, by_site[], capacity, rses[],
                   health, links}
  by_site[]       {site, arrived_1h, bytes_1h, failed_1h}
  capacity        {rse, used_bytes, limit_bytes, fraction, as_of}
  rses[]          {id, name, type, used_bytes, limit_bytes, fraction,
                   files, as_of}
report            {text, items: [{level, text, link}],
                   assessment: {verdict, level, at, link, campaign,
                                title, narration}}
live              {link, hours, items: [{id, at, level, title, detail, link}],
                   counts: [{level, count, link}]}
tasks             {link, items: [{id, name, at, level, title, counts, detail, sites, queues, link}]}
  counts          {ungenerated, queued, running, finished, failed}
trends            {window_hours, bin_minutes, t[], sampled_at, source_as_of,
                   gaps[], sites[], link}
  sites[]         {id, name, running[], queued[], finished[], failed[], detail}
errors[]          {part, error}
```

### Pressure front

- `ready_tasks`: the PCS tasks with a queued submission. These are the
  front's ready backlog (swf-epicprod CONTINUOUS_PRODUCTION.md, the
  front's placement).
- `released_1h`: the front's `fed` decisions in the last hour.
- `enabled`, `mode`, `jedi_throttled`: the front's SysConfig switches.
- What the pillar shows on hover and opens (Torre Wenaus, 2026-10-22),
  from the state the front's cycle stored (`front_state`, as of
  `cycle_at`): `items[]` is the ready buffer task by task, `{task,
  queue, order, kind, by_hand, priority, jobs, eligible, problems,
  link}`, in each queue's feed order, a task pinned to a closed queue
  not eligible and the closure among its problems; `queues[]` is each
  production queue's latest decision, `{queue, state, reason,
  committed_h, h_low, h_high, running, ready_total, ready_eligible,
  last_feed_age_h, closed}`; `detail` is the hover line; `link` opens
  the front page at its ready buffer.

### Sites and queues

`site.fair_share` carries the submit host's latest negotiator reading.
Its `notice` is null when not limited; otherwise it is
`{text, level, detail, link}`: the canonical wording, severity, hover
explanation and live queue page. Dashboard and room consume this notice
without composing their own text. The same formatter adds it when a
stored snapshot is read, so cached measurements cannot retain obsolete
notice wording. A stale or unavailable negotiator record has no notice.

The site groups are the SysConfig key `comp.sites` (id, name, order,
flags), seeded with the layout below, so a site is added or regrouped
without a deploy. Each site's queues, and their status, are the queue
registry's (swf-epicprod docs/QUEUE_REGISTRY.md): a queue belongs to the
site its entry names, in the registry's order, and a retired queue leaves
the room. OSG is
a site of its own, not BNL's: BNL is only the conduit its pilots are
submitted through, and its queues and execute sites belong to OSG.

| Site id | Name | Queues | Notes |
|---|---|---|---|
| `osg` | OSG | BNL_OSG_PanDA_1 | `execute_sites[]` from the jobs' pilot-reported glidein site (PanDA keeps it in 36 characters, so a longer name arrives cut and is restored from the full names `short_names.py` knows: 'Center for Advanced Research Computing', not '...Computi'); BNL_OSG_EPIC_PROD_1 left the floor on 2026-10-22, production having moved off it on 10-21 |
| `grex` | GREX | UM_GREX_PanDA_1 | University of Manitoba |
| `compute-canada` | Compute Canada | ComputeCanada_PanDA_1 | not built: awaiting SFU's identity mapping at the CEs |
| `nersc` | NERSC | NERSC_Perlmutter_epic, NERSC_Perlmutter_epic_es | |
| `google` | Google | BNL_ePIC_GOOGLE, BNL_ePIC_GOOGLE_es | |
| `jlab` | JLab | JLAB_PanDA_1 | not live: waiting for JLab's CE to route our jobs |

A queue's `status` is `live`, `commissioning` (accepting placed trials),
`off` (in PanDA, not taking production), `not_live` (defined, not yet
running work) or `not_built` (the registry's `not_built`, or not in PanDA
yet). A registry status of `production` leaves the reading to PanDA.
`status_reason` says why when the status is not `live`: the registry's
reason.

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
  GREX, 1 elsewhere). `waiting_current` is the canonical display count:
  the schedd's count when reported, otherwise Harvester's. `waiting_source`
  identifies that choice. The dashboard and room consume this resolved
  count; `waiting` and `waiting_schedd` retain the raw readings for diagnosis.
  `limit` is the waiting limit harvester last
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
  (SNAPPER_ERRORS.md): component x code, the job's first nonzero error
  component over all seven, labelled from the code catalogs
  (`panda/error_labels.category_label`) and read through the
  error-correction root (ERROR_ATTRIBUTION.md), so a label a rule marks
  unreliable shows its corrected reading. The note names the top category
  with its count; no failure reads "unattributed" (23 October 2026: the
  OSG failures were DDM 200, the log-name collision of 41039 and 41040).
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
  waited, was submitted or started, unless the queue's running slots fill
  the operator's worker limit (`pilot.queue.<queue>.worker_limit`): a
  full queue is due no pilot, so the part is `green` and the limit is its
  own Report line (Torre Wenaus, 2026-10-28, when GREX, full at its
  300-core limit, read red here). The two schedd rules need the OSG
  submit reporter's count and apply to the OSG queues only. `grey` when
  the report is more than fifteen minutes old or the limit harvester
  applies is not yet reported; otherwise `green`, with the counts in the
  note.

  A queue no harvester report names reads PanDA's own worker table
  instead (`harvester_workers`, the workers running or submitted now):
  NERSC's harvester is PanDA operations' and reports nothing to us, and
  the Event Service queue's one worker at a time, with the next job
  queued behind it, read `amber` until 26 October. `green` with a worker
  running or submitted; otherwise `amber` with jobs queued, `grey`
  without.

  A queue the ops agent supplies with pilots instead of harvester
  (swf-epicprod `queues.AGENT_SUPPLIED`: BNL_ePIC_GOOGLE_es) reads its
  part from the GKE controller's worker observation, the same reading as
  its `gke_workers` part: `red` when the controller's latest worker
  failed, `amber` when no fresh observation exists, otherwise `green`
  with its waiting and running pilots.

A site's health is the worst of its queues'. An execute site's health
has the `prod_activity` part only.

`site.level` is the site's bullet: the worst of its health, its status
level and the levels of its report items (Report, below), so a site held
for failures is never green (Torre Wenaus, 2026-10-28: GREX read green
with its feed off for failures and its only task paused). Grey items do
not raise it. A site that is not live keeps its status level.

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

For `jlab-rucio`, `flow` is the output flow on the trends axis
(`trends.t`, the same whole quarter-hour bins): `files` and `bytes`, the
files the finished production jobs ending in each bin registered and
their bytes, and `registration_incomplete_jobs`, those jobs whose
registration did not complete, all from the same payload-report reading
as `arrived_1h`. These are registrations as the payload reports them,
grouped by the job's end, not measured transfer bandwidth or the files'
availability times; volume per hour is the bin's bytes times four. A
bin's jobs are final once archived, so the bins are kept (cached product
`jlab_flow:v1`) and each build reads again only the last two bins before
the newest kept one, and each build reads an hour of the window's bins
not yet kept, so the twelve hours fill within twelve builds (a single
read of the whole window takes minutes); a bin not yet read is null, and
`flow` is null when the reading fails or the trends axis is absent.

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

Queue labels use the shared short-name vocabulary; BNL_OSG_PanDA_1 reads
OSG. Queue identifiers and links retain their canonical names.

- `report.text`: a summary composed from the record on every build,
  without an LLM: the front, each live site's running and last-hour
  outcomes, and every non-green health part.
- `report.items`: what the room's floor cannot show, one line each as
  `{n, key, level, text, link, site}`, in the order of their numbers, the
  level carried by the colour, with no per-site counts (the pillars carry
  those). `n` is the
  line's standing number (Torre Wenaus, 2026-10-28: "prod now numbering
  should be immutable"): `key` names what the line is about (a queue's
  feed, a paused task, an alarm's title...), a line keeps its number while
  it stands, a new line takes the lowest number not held, and a cleared
  line's number stays held for an hour, so a line that returns keeps it
  and no other line takes it meanwhile (CachedProduct
  `prod_now_numbers`). `site` is the id of the site a line belongs to,
  or absent; the line names the site by its display name (the shared
  short-name vocabulary; a queue name only where the site has more than
  one live queue), and `site.notices` lists that site's lines,
  `{n, level, text, link}`, for its panel (Torre Wenaus, 2026-10-28:
  every exceptional condition of a site is a numbered line, and the
  panel cites the numbers). Every state a site is in is a line with who,
  when and why, read from the current settings when the record is read:
  - its front feed **off**, red, with the recorded reason, who and when
    (`front.queue.<queue>.feed_off_reason`, `.feed_set_by`,
    `.feed_set_at`); a feed off with no recorded reason is a red line
    saying so;
  - its front feed **exempt** (its own feeder supplies it), grey, with
    the recorded reason;
  - a production task **paused** at one of its queues, red, with who
    paused it, when and why (`paused`, below);
  - an operator **core limit**, red, with the control's reason, who and
    when (`pilot.queue.<queue>.worker_limit`);
  - the pool's **fair-share** limit, grey, informational: OSG's normal
    mode of operation, which never colours the site (Torre Wenaus,
    2026-10-28), with the pool and the negotiation cycle's time
    (`site.fair_share.notice`).

  Then why the front feeds nothing (the global front is off, each live
  queue's front decision in the words of CONTINUOUS_PRODUCTION.md's
  states and reasons, a feed switch apart), every amber or red health
  part with its note,
  every active alarm event (red) and approaching deadline ping (amber),
  storage parts not green, and late or missing scheduled campaign
  assessments (the System
  page's campaign-assessments row; the verdicts are held by corun-ai,
  which this build does not call). The text before the first ": " names
  what the line is about.
  Routine off and commissioning states remain on the site boards. A global
  empty-ready-buffer summary and healthy assessment acknowledgements are omitted.
  A live queue below its target work depth with no eligible ready task is
  amber: the front would release suitable work if it were available.
  The alarm engine selects approaching pings using each ping's configured
  lead time. Their report text shows the due date without the recorded
  title's stale countdown. Overdue pings escalate to red alarm severity.
- `report.assessment`: the newest daily campaign assessment, from its
  `assessment_register` action record (swf-epicprod
  EPICPROD_ASSESSMENTS.md): the verdict, the verdict as a health level
  (`ok` green, `attention` amber, `alarm` red, anything else grey), the
  registration time, the assessment's page, the campaign, the report
  title and the narration; `null` when none is registered.
- `errors[]`: a part that could not be read is listed here and left out
  of the record, never shown as zero.

### Live

The room's epicprod-live screen (Torre Wenaus, 2026-10-22). The floor is
the control room: it shows what is live now, and `live.link` opens the
last day curated (`logs/epicprod-live/`, ACTION_STREAM.md, Curated view;
Torre Wenaus, 2026-10-28), which links on to the logs page's live view,
`logs/?app_name=epicprod&live=1`, the complete history, whose filters
count the live rows it lists, not every record of the app, offering the
busiest 15 users and instances, and whose table fits the page, never
scrolling sideways. The items are records of the epicprod
live stream (`live_stream_q`, ACTION_STREAM.md) at level WARNING, ERROR
or CRITICAL from the last 12 hours, newest first, at most ten, each with
its time, level, title, detail and page (`logs/<id>/`). The records ride
in the snapshot because the room reads it anonymously and the logs page
is behind the login wall; they name tasks, queues, nodes and errors and
carry no credential.

The screen shows the title and gives the detail on hover. The title says
what happened and where in a few words, names in their short form
(`monitor_app/short_names.py`, the one filter for concise settings): for
a node guard decision the execute site and the node's number on that
site's blacklist, in exclusion order (`Fir node guard 6`,
`node_guard.exclusion_ranks`), so nodes at one site read apart; the task
for a task operation (`Move · task 40857`), else the site, storage
element, queue or configuration the record names. The detail is the
informative message, concise. For a node guard decision it is the
decision record's own message: the guard, its queue, the node, the
reason and the evidence:

    Fir node guard 6 on BNL_OSG_PanDA_1: fc20513. Kills jobs at a fixed
    run time, ~109 min (memory or slot limit): 12/12 failed in 4 h;
    tasks 40958, 40970 run fine elsewhere.

For a task operation it is the task and the operation's reading;
otherwise the record's reason led by its subject. Both come from
`live_notices.board_text`.

A record leaves the floor when it is no longer news:

- a node guard decision is shown only for a node new to the blacklist
  (it was clear); a re-trip from half open is a node the floor knows;
- one record per subject, the newest (an action on its subject, or a
  pass with no subject from its source), and a record leaves when a
  later record on its subject reports another outcome, such as a node
  the guard has moved to half open;
- a failure (outcome error, timeout, partial or unrecorded) leaves once
  it has recovered: a later record of the same action from the same
  source on the same subject reports ok, the identity the epicprod-live
  channel uses for its recovery notices (`live_notices.failure_key`);
- any record leaves when the operator's question about it has been
  answered: a `live_cleared` record in the action stream names the
  cleared record ids and the reason. The `epicprod_live_clear` MCP tool
  writes one; out of process, `scripts/live-clear.py <id> ... --reason
  "..."` writes one through the log REST endpoint.

None of these changes the records on the logs page.

`counts` are the live stream's records of the window (`hours`, 12) at
INFO, WARNING and ERROR, in that order, zero included, each with its
`link`, the logs page's live view narrowed to that level and window
(`levelname=<level>&hours=12`), which lists exactly the records counted
(Torre Wenaus, 2026-10-28).

### Tasks

The room's task board, beside the live screen (Torre Wenaus,
2026-10-22): exactly the production tasks in flight on the floor, those
not finished in JEDI (done, finished, failed, broken, aborted) that have
jobs running, starting or queued (the census's states) at a queue of
the room shows, or rows JEDI has not yet made into jobs for such a queue.
A task with neither is off the board. Newest first by `at`, the task's
start (its creation before it starts), ISO UTC. `id` is the JEDI task id,
`name` is the task's composed PanDA name, read from the same query;
`link` the task's page; `tasks.link` lists the production tasks.

The board is the tasks in flight. A line is the task id (`title`) and
its counts, just the numbers: jobs still to generate (`ungenerated`,
the board's "generate": the task's input rows JEDI has not yet made into
jobs, the census's measure, `census.ungenerated_by_task`; a row is one
job under the production tasks' one-row-per-job manifest, one event
range per job), jobs queued
and running, files finished and files failed after their retries (a job
that failed and then succeeded on a retry is not a failure of the task),
finished shown green and failed red:

    40856   30,631  2,133  747  14,250  2,244

The detail, on hover, is the task's physics in the concise form of the
PCS description (`physics_description`, the one composer PCS pages use)
and the site's short name, then the composed name, status, queue and
start, and files done of the task's files and lost, with the alarm when
the task is in one:

    DIS, eAu 9x100, Q2 10 to 100, BeAGLE at GREX.
    group.EIC.26.07.3.epic_craterlake.p2428.e53.s11.r11, running on
    UM_GREX_PanDA_1 since 10-17 23:39 ET; 13,772 of 50,006 files done,
    2,184 lost after retries (13.7%), in alarm.

`level` is red for a task in a failure-rate alarm, amber for one that
has lost files, green while it runs, grey while its jobs only wait.

The board holds every production task in flight wherever it is placed
(Torre Wenaus, 2026-10-22, when the front placed two tasks on
BNL_OSG_EPIC_PROD_1 after it had left the floor), by the same rules as
any other; its detail names its queue, and its `sites` hold no id for a
queue no floor site holds.

`queues` are the floor's queues the task is active at: those holding its
running, starting or queued jobs, and the queue of its rows still to
generate. `sites` are those queues' site ids, the ids of `sites[].id`.
Both are sorted lists, one entry for a task at one site. Each site disc
writes the ids of the tasks whose `sites` hold it (Torre Wenaus,
2026-10-22: "Tasks: n1 n2 n3", nothing written when there are none).

### Paused

`paused` is the epicprod disc's paused pillar (Torre Wenaus,
2026-10-23): every production task (`processingtype` `epicproduction`)
whose PanDA status is `paused`, wherever it is placed, a retired queue
included, except a task ended quietly (swf-epicprod EPICPROD_OPS.md, Quiet end).

- `tasks`: the count; `link`: the task list narrowed to paused
  production tasks, its window reaching back to the oldest pause;
  `detail`: one line naming each task, its site and since when.
- `items`, oldest pause first, each with `id`, `name`, `queue`, `since`
  (ISO, the task's last state change, which for a paused task is its
  pause), `link` (the task's page) and `detail`: the site and queue,
  since when, and the input files done and lost.

PanDA records no reason for a pause, and its "pause by Torre Wenaus" is
the operations agent's credential, not the actor. A pause made through
our tools has its operation record (`PandaTaskOperation`, operation
`pause`), and every pause through our tools carries a reason (swf-epicprod
EPICPROD_OPS.md): each item carries `paused_by` (the person, or `front
task guard` for the front's guard) and `pause_reason` (the guard's trip
from its evidence, or the recorded reason; null only for a pause made
before 2026-10-28 without one). A paused task with no operation record
was paused outside our tools, directly in PanDA: `paused_by` reads
`outside our tools`.

### Completed

`completed` lists the production tasks completed in the last 24 hours,
for the dashboard's panel under the paused tasks (Torre Wenaus,
2026-10-24): every `epicproduction` task whose PanDA status is `done`
(every input file finished) or `finished` (some lost), trials included,
by its end time, except a task ended quietly (swf-epicprod EPICPROD_OPS.md,
Quiet end).

- `tasks`: the count; `window_hours`: 24; `link`: the day's production
  task list; `detail`: the count in words.
- `items`, newest first, each with `id`, `name`, `queue`, `status`,
  `at` (ISO, the task's end time), `physics`, `link` (the task's page)
  and `detail`: the status, site and queue, when, the input files done
  and lost, and the physics. `physics` reads the task's tags as physics:
  PCS's one-line summary of its physics configuration
  (`pcs.physics_config.summary`: process, beams, Q², generator, sample,
  background, requesters), found through the task's PCS link or the
  composed identity its name carries; empty for a task PCS does not know.

### Arrivals

`arrivals` is the science reaching JLab over the last 24 hours (the completed tasks' window) by physics
configuration, for the dashboard's panel under the completed tasks
(Torre Wenaus, 2026-10-24): the RECO files the finished production jobs
registered and their events, from each job's payload report (the record the
JLab Rucio pillar reads), grouped by task and the task's PC (`_task_pcs`, as
for `completed`).

- `pcs`, `files`, `events`, `window_hours` (24), `link` (the campaign view)
  and `detail` (the totals in words).
- `items`, newest arrival first, each with `pc`, `physics` (PCS's one-line
  summary), `label` (the physics in a row's width: the summary without its
  generator and requester tail, units or repeated nucleus), `state`
  (`finished` when every task delivering to it in the window has ended or
  the plan's target is met, past it included, else `running`), `files`, `events`, `tasks`, `sites`, `first_at` and `last_at`
  (ISO, the first and last registering job's end), `delivered_events` and
  `target_events` (the plan's, read from the stored completion record
  `prod_hub_campaign_completion` without building it), `link` (the PC's
  page) and `detail`: the physics, the 24-hour files and events with their
  times, tasks and sites, and the plan's delivered against target.

### Trends

The floor's trend plots (Torre Wenaus, 2026-10-22): utilization in the
moment over the last 12 hours, per site with a live queue, on one shared
axis. `t` is the ends of 48 whole quarter-hour bins in ISO UTC, the last
ending on the quarter hour at or before the series' newest sample
(`sampled_at`); `source_as_of` is when the PanDA activity the snaps
record was last read. For each site, summed over its live queues:
`running` and `queued` are the jobs running and the jobs not yet started
(the census's states: defined, waiting, assigned, activated, sent) at
each bin end; `finished` and `failed` are the jobs that ended in the bin.
`detail` is the site's hover line: running and queued at the last bin
end, finished and failed in the hour before it.

The source is the Snapper Site view's series product
(`snapper_ai.products.series_product`, focus `site`), one product for
the floor's live queues; `link` opens that view. Every sample of a
queue's PanDA state carries the queue's finished counter, so the
counter's points are the queue's sample times, and a value at a bin end
is read at the queue's newest sample at or before it. The series records
every plotted job state at every sample, zero for a state that holds no
jobs (`snapper_providers._plotted_job_states`; the PanDA state lists only
the states that hold jobs). The finished and failed counters count up,
so a bin's ended jobs are the difference of the count at its two ends.
A value is null, never carried forward, when the newest sample before
its bin end is more than 15 minutes older (the PanDA reading refreshes
every five) or the bin end lies in a gap of collection. The counters
being cumulative, a gap inside a bin loses none of its ended jobs; they
are null only where the count at either end of the bin is. Limits: the
state is sampled when the PanDA reading refreshes, every five minutes,
so a state is the one at the newest refresh before the bin end, not at
the bin end itself; and the series rebases the finished and failed
counters by summing their nonnegative increments, so a raw counter
reset reads as a bin with no ended jobs rather than as unknown. The gaps come from Snapper's own
coverage record (the gaps recovering snaps closed and the capture
cursor's open gap), listed in `gaps`. A queue with no sample is null
throughout.

The live right end (Torre Wenaus, 2026-10-28, after GREX showed 265 cores
over a plot ending at 8, its last point the 06:30 quarter-hour at 06:42):
`live_t` is the time of the panel's live figures (the processing
reading's `processing_observed_at`), and each site's `live` carries them,
`cores` (its `processing_threads`) and `queued_cores` (its queues'
`queued_cores`), added when the record is read. The dashboard ends the
cores and queued curves there, after the last whole bin; the ended-job
curves stay on whole bins.

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

## Shared board presentation

`report.header` contains the Computing now, Work available and Campaign
blocks for both displays. Each block has `id`, `title`, optional `subtitle`,
`metrics` (`value`, `label`, `colour`, `link`, `detail`), optional `footer`
(`text`, `link`, `detail`), `link` and `detail`. Display strings and CSS hex
colours are composed by the feed.

Sites carry `status_label`, `status_level` and `status_reason`. Commissioning
is labelled `COMMISSIONING` in blue; Perlmutter off is `OFF` in amber. Live
sites have no redundant status label and use their health colour, except
a standing note (`LIVE_SITE_LABELS`): live Perlmutter is `Single node`. The internal
`not_built` state is displayed as `OFF`. These display fields apply to cached
snapshots immediately and do not alter queue admission or worker controls.
Each queue also carries
`production_display`: true for live queues and the enabled, bounded Google
commissioning queue, false for inactive commissioning queues such as JLab.
Google uses the same production pillars, metrics and trend presentation as
GREX. The standard Google queue remains off. Controller and runtime
diagnostics remain in the controller record and operator views.

`site.production_panel` is true for each standard site group, including
inactive Canada, Perlmutter and JLab. It selects the shared panel geometry
and reported counts independently of operational activity. Every site has
a trend row on the shared axis; missing samples are null. Queue activity
flags, admission and pilot controls are unchanged by panel availability.

GKE worker evidence comes from `gke_pilot_runtime`, published by the
credentialed controller. `pilots.waiting_current` and `waiting_source`
identify its pending workers (`gke-pilots`). A missing or stale observation
remains unknown. Trend rows include displayed commissioning queues; bins
without samples remain null.

## Regulation telemetry, 22 October 2026

The 22 October regulation correction reads fixed ratio-zero waiting limits from the fresh Harvester database report. Ratio-derived limits retain the last applied adjuster observation, with its timestamp and `limit_source: adjuster`; the health note calls this the last applied limit. The adjuster skips that log line while the waiting queue is full, so an old event timestamp alone does not invalidate the value. The enclosing report must still be fresh, and a failed adjuster-log read is degraded. A limit never observed remains unknown. Missing or stale pilot evidence is amber when work waits, so another green component cannot conceal missing protection. The scheduled production notifier evaluates this record independently of browser requests.

### JLab commissioning evidence

While JLAB_PanDA_1 is not live, its reason and health come from the PanDA
Harvester worker record: statuses of workers submitted in the preceding
48 hours, plus the latest worker and diagnostic. Failed or cancelled
workers show red even before any PanDA payload starts. An unavailable
query says unknown and shows amber. The former static claim that JLab's
CE had not routed jobs was false and was removed on 22 October 2026.
Other queue status policies are unchanged. Worker start/end timestamps
are not used to infer runtime: Harvester can stamp them when discovering
an already finished worker. The 48-hour failure history can remain red
after a successful commissioning probe; the latest worker is stated
separately, and admission to production remains an operator decision.
