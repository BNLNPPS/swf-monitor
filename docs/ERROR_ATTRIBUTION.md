# Error Attribution and Correction

Unreliable PanDA job error labels are corrected at a single service
root through which every error-presentation surface reads. This is a
design document; the sections below are the agreed plan of record for
implementation. It extends the error machinery of
[SNAPPER_ERRORS.md](SNAPPER_ERRORS.md) — in particular its
progressive-refinement and knowledge-base tiers — with the concrete
correction mechanism, the evidence model behind it, and the payload
and pilot channels that feed it.

## Problem

PanDA job error labels come from the pilot's error extraction, which
can misattribute failures. Confirmed case: 42,870 NERSC jobs over four
days in August 2026 recorded as pilot error 1305 ("bind mounting
/global", a stale apptainer stderr line) that had run reconstruction
for ~2 hours and failed at Rucio registration — payload exit 78,
confirmed against the payload log of a representative job (2584600)
and self-contradicted by the pilot's own timing fields (`pilottiming`
exe = 5450 s under a "launch failure" label).

Error labels feed many presentation surfaces: the error summary
(`/panda/errors/` and the `panda_error_summary` MCP tool), job study,
the Snapper errors view, system status, daily reports, assessments,
and Capcom notices. A correction applied inside any one aggregation
leaves the others wrong. Correction therefore applies at one root
through which every surface reads.

## The correction service

A single module and rules store in swf-monitor; every
error-presentation path is a client.

- **Read-time application.** Corrections arrive after the fact, so
  recorded history (job records, Snapper snaps) stays raw as reported;
  every read path applies corrections on the way out. A rule added
  today corrects last week's presentation everywhere.
- **Provenance always.** The corrected reading leads; the original
  label stays visible ("reported as pilot:1305"); every verdict
  carries its evidence link, scope, author, and date.
- **REST face** on the same root, so out-of-process consumers
  (assessment harness, swf-remote face, standalone tools) apply the
  identical corrections without importing Django.
- **Fast action.** A management page for rule entry: one row added at
  diagnosis time corrects every surface at the next page load.

## Rules — two kinds, two lifetimes

- **Label-reliability rules** (durable): "this label is untrustworthy
  in this scope" — a fact about the producing extractor, standing
  until the extractor is fixed. Match on component × code × diagnostic
  substring, scoped by queue/site.
- **Cause verdicts** (episode-scoped): what the unreliable label
  currently masks. Never cached against the signature alone — a
  signature can mask different causes in different months. Verdicts
  attach to episodes; the storm detector planned in
  [SNAPPER_ERRORS.md](SNAPPER_ERRORS.md) provides episode boundaries
  (start, escalation, end). Episode end retires the verdict into the
  knowledge base as history; a new flood of the same signature opens a
  new episode and earns a fresh verdict.

## Evidence ladder — witness grades

Every corrected reading carries the grade of its evidence, best first:

1. **Directed canary probe** — ground truth about a claimed site
   condition (site-canary increment 8): a passing container/storage
   probe refutes a claimed mount or storage failure.
2. **Payload log** — the payload's own full record; fetched per
   representative job (Rucio + xrootd, cached per job); the
   calibration standard.
3. **Payload self-report** — `jobReport.json` written by the payload
   on failure. It travels in the job's log tarball, and the epic pilot
   plugin lifts its exitCode/exitMsg into the stored
   `exeErrorCode`/`exeErrorDiag` job fields
   ([pilot3 PR #212](https://github.com/PanDAWMS/pilot3/pull/212),
   merged and deployed on the ePIC pilot 2026-08-25).
4. **Pilot mechanical fields** — `transexitcode`, `pilottiming`,
   `cpuconsumptiontime`: measurements, population-wide, SQL-joinable,
   and self-cross-checking (a "launch" label on a job whose own timing
   shows hours of execution is refuted from within the pilot's
   report). Cross-field contradiction is an automatic label-unreliable
   trigger.
5. **Pilot-extracted label** — the lowest grade; the input being
   corrected.

The server's metatable stores pilot-shipped metadata for finished jobs
only (`addMetadata` discards failed-job metadata by policy), so it is
not a failure channel; the failed-job record path is the exeError
fields via the pilot lift.

## Representative-job digs

The dig unit is the pattern signature within an episode, never the
job. A bounded sample (5–10 jobs, covering each exit-code mode
present) is fetched and diagnosed through the existing payload-log
machinery; the verdict is joined at read time by all jobs matching the
signature, scope, and cheap-field profile. Jobs that fail the profile
check present as unverified and count toward a re-dig. Long-lived
episodes re-earn their verdict by trickle re-sampling (order one dig
per day). Cost scales with the number of distinct failure modes, not
with job count.

### Dig triggers

Digs are never routine. A dig runs on three triggers only: a storm
start from the error-stream detector (SNAPPER_ERRORS.md, storm
detection), a platform alarm detection whose detail concentrates at a
site — heartbeat staleness or yield at one site, or a node list from
the node health map (SNAPPER_PLATFORM.md) — and an operator request
from the job page. Each trigger opens one bounded dig: one
representative job per distinct pattern signature in the episode, and
for a lost-heartbeat episode one job per top silent node, chosen as
the job whose silence began first there. For those jobs the dig
reads every file at the site's published location (the NERSC portal
directory holds, per PanDA id, the Slurm output and error files, the
pilot log `pilotlog-task<N>.txt`, and the payload stdout and stderr),
in that order: the batch system's own verdict first, the pilot's
account second, the payload's last. The 2026-08-25 lost-heartbeat
storm is the reference case: the pilot log showed a 229-minute hole
and was read as a node-side I/O stall; the Slurm record, unread, said
the node had run out of memory — 128 single-core pilots at 5.7 GB
resident against a 476 GB node.

Before a dig names any node-side cause, it computes the node budget
from the job records: jobs per node (`modificationhost`) times their
resident memory (`maxrss`) against the node's memory from the queue
definition, and the equivalent for cores. A budget over the node is
the finding; a stall, a hole in a log, or a missing heartbeat is the
symptom.

A dig's findings enter the action stream and the alarm event's detail,
and the verdict attaches to the episode as above; a further dig in the
same episode runs only on escalation or on the daily trickle.

## Population-wide channels

- **Exit-code vocabulary.** The payload communicates its failure mode
  through coded exits, stored on every failed job as `transexitcode`.
  The hepmc3 campaign `run.sh` already codes 78 (Rucio registration
  failure) and 65 (validation failure); the vocabulary is completed so
  every distinct failure path has a documented code, kept in a
  registry in the production documentation.
- **Failure report.** On failure the payload writes `jobReport.json`
  (`exitCode`, `exitMsg`; extra fields welcome). In the PCS path the
  in-job dispatcher shipped in every task sandbox writes it, wrapping
  the unmodified campaign `run.sh`; direct submissions add the same
  via a small insert in `run.sh` (helper + ERR trap + explicit calls
  at coded exits, since bash suppresses the ERR trap in `||`
  branches).

## Canary roles

- **Consumer first**: the site-canary commissioning gate names failure
  attribution as a blocker — site health verdicts must not classify a
  site unhealthy on task-caused failures. Canary consumes corrected
  categories, never raw pilot labels; the duration and error-class
  recording the gate plans supplies the same cheap consistency fields
  the correction service uses.
- **Witness at increment 8**: directed probes test claimed site
  conditions during an episode, entering the dossier at the top
  evidence grade.
- **Transport later**: the rider collection ladder
  (heartbeat-attached packets, direct REST, staged files) is the
  designed channel for richer job-internal reporting beyond the
  final-update path.

## Implementation

- **swf-monitor**: rules model + `error_corrections` module (pure
  application API with batch form, rules cached); clients wired at the
  readers — `error_summary`, `study_job`/`diagnose_jobs`, Snapper
  errors providers, system status, inventory; REST endpoints;
  management page; witness grade shown in every rendering.
- **swf-epicprod**: dispatcher report writer for the PCS path; the
  exit-code registry; the `run.sh` insert for direct submissions.
- **Upstream, parallel and non-blocking**:
  [pilot3 PR #212](https://github.com/PanDAWMS/pilot3/pull/212)
  (merged and deployed on the ePIC pilot 2026-08-25); optionally a
  server-side lift of the failed-job metadata discard.
