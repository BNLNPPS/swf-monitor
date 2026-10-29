# Harvester host reporter

Pilot dispatch capabilities distinguish an image-bound OSG pilot from a
batch pilot that launches each payload in its requested image. A native
`per-job` template is reported as unrestricted only when its environment
enables per-job dispatch and container launching, and its transferred
launcher implements omission of the dispatch-image filter. The bootstrap
image is retained separately in `bootstrap_images`.

A reporter on each harvester host (pandaharvester01, pandaharvester02, osgsub01), and one for the Perlmutter harvester built from what it reports to the PanDA server (Perlmutter, below), delivers to swf-monitor
what only that host knows about how its queues get work: when the harvester
last asked PanDA for jobs for each queue, how many it asked for and got, and
why it got none; how many workers it submitted and for which jobs; and every
failed call to the PanDA server, by daemon and error class. It follows the
pattern of the PanDA server reporter
([PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md)): a standalone script,
standard library only, local state, a five-minute cron run, HTTPS delivery to
the host-report ingest under a per-host token, read-only with respect to the
harvester.

It exists because of 2026-10-02. From 14:23 to 18:43 ET the harvester could
not verify the PanDA server's renewed certificate. It fetched no jobs and
sent no updates for any queue it serves, while the queues read only as
"starved" from outside, and the one line that said why, `failed to POST with
SSLError ... CERTIFICATE_VERIFY_FAILED`, sat in a log on a host the monitor
cannot reach. A job that is activated and never fetched has no worker and so
no worker logs; its explanation exists only in the harvester's own logs.

## Functions

Each run posts one record covering the lines appended since the previous
run (the logs are read from the byte position the previous run left; a
rotated log restarts from its beginning and the record says so; the first
run sets the positions and counts nothing).

| Function | Source on the host | Fields delivered |
|---|---|---|
| Job fetching | `/var/log/harvester/panda-job_fetcher.log` | per queue: fetch attempts, jobs asked for and got, attempts that failed and their error classes in the interval; the last attempt with its time, label, numbers and outcome (`OK`, `No jobs in PanDA`, or the error) |
| Worker submission | `panda-submitter.log` | per queue: workers submitted in the interval, the last worker counts the submitter read (`nQueue`, `nReady`, `nRunning`, `nNewWorkers`), the last job-chunk count; the most recent worker-to-PandaID pairs |
| Worker adjustment | `panda-worker_adjuster.log` | per queue: the waiting-worker limit the adjuster last applied and the activated jobs it was set against, with the time it was logged. Harvester computes the limit from its running workers, so `pq_table`'s `nQueueLimitWorker` is only its cap. The adjuster logs it only in some cycles (half an hour apart on osgsub01, 2026-10-22), so the last value is kept in the reporter's state and delivered every run |
| Calls to the PanDA server | `panda-communicator.log`, `panda-propagator.log` | failed calls in the interval by daemon, call and error class, with the last such line |
| Daemon log freshness | `/var/log/harvester/panda-*.log` | seconds since each log's last write, and its size |
| Harvester processes | the process table | the count of harvester processes |
| Launch limits | the harvester database, `pq_table` | per queue: the queued-worker limit, the maximum workers, the new workers last decided, the last job fetch and submission times |
| Workers now | `work_table` | per site, the live workers by status |
| Workers ended | `work_table` | per site, the workers that ended in the interval by status and the median run of the finished ones |
| Workers not finished | `work_table` and each worker's condor event log | per site, over the last 24 hours, every worker that did not finish or finished with a nonzero batch exit, grouped by the batch system's reason, with up to three examples (worker, batch ID, node, CE, log URL) |
| Service metrics | `sm_table` | the harvester's latest metrics for its host |

The reason for a worker that did not finish is the last terminal or hold
event of its condor event log (aborted, held, terminated, shadow exception,
disconnected), with its detail lines: "Job was aborted. removed by
SYSTEM_PERIODIC_REMOVE due to job restarted undesirably." The harvester's
own diagnostic carries the remove or hold reason too; the condor log adds
the event, its time and the other terminations (exit codes, signals,
shadow exceptions) the diagnostic does not. The reasons cover the last 24
hours on every run; each worker's reason is read once and kept in the
reporter's state. The log path is the worker's `batchLog` URL mapped to the local log directory
through the queue configuration (`logBaseURL`, `logDir`); a worker without
a readable log falls back to the harvester's diagnostic, and the record
says which source each reason came from. A run reads at most 300 condor
logs, newest first. The database is read through the `mysql` client with
the harvester's own read settings from `panda_harvester.cfg`; nothing is
written to it.

Times in the harvester logs and database are UTC and are delivered as such. Every source
that cannot be read is a field, never dropped; an unreachable monitor
buffers records locally and posts the backlog on the next run.

## Where it shows

The record is stored by the ingest in the cached-product store under the
host's key. The PanDA queue page shows, for a queue the harvester serves,
its last fetch and its outcome, the interval's fetch and submission counts,
and the last failed call, with the record's age. The job page of a job that
is activated and not yet fetched shows the same last fetch for its queue:
when the harvester last asked, and what it got.

## Install

1. The script at `~/.local/bin/harvester-reporter.py` and its environment
   file `~/.swf-harvester-reporter.env` (mode 600, `SWF_MONITOR_URL` and
   `SWF_REPORT_TOKEN`); state and buffer in `~/.swf-harvester-reporter/`
   for pandaharvester01 and `~/.swf-harvester-reporter-<key>/` for any
   other host key. The account's home directory is shared across the SCDF
   hosts, so one copy of the script serves every host.
2. The token, of the Django user `pandaharvester01-reporter`, serves both
   hosts through the shared environment file.
3. A cron entry for the account on each host, every five minutes. On
   osgsub01 the record is stored as `osgsub01-harvester`
   (`--report-as osgsub01-harvester`), since `osgsub01` is the OSG submit
   reporter's key.

The reporter also runs on pandaharvester02, the third harvester
instance, under its own host key; everything it reads there is readable
by the account.

Verified on 2026-10-02 for the account (`wenauseic`, group `eic`): the
harvester logs are world-readable, cron is permitted, the host's `python3`
is 3.6.8, and the monitor answers over HTTPS. No change to the harvester,
its configuration or its submit descriptions is part of installing the
reporter.

## Perlmutter

The harvester `Perlmutter_test_1` serves the NERSC queues from a Perlmutter
login node under PanDA operations' account (distcomp-services
perlmutter-harvester/README.md). Its logs are in that account's scratch,
`/pscratch/sd/x/xin/panda/log`, which the m3763 project cannot read, and its
process table is not reachable. Its record, host key `perlmutter-harvester`,
is built on pandaserver02 from what the harvester reports to the PanDA
server and from its queue configuration, read through the NERSC IRI API
under an m3763 member's token (`swf_epicprod/nersc_iri.py`):

| Function | Source | Fields delivered |
|---|---|---|
| Heartbeat | `harvester_instances` | `instance`: start, last heartbeat and its age, version |
| Launch limits | `panda_queueconfig.json` through IRI | per queue: the queued-worker limit, ratio, minimum and maximum, the maximum workers, the map type; the new workers last decided (`to_submit` in `harvester_worker_stats`) and the last submission |
| Queue status | `panda_queueconfig.json` through IRI | `queue_status.status`: each queue's `queueStatus` |
| Workers now, started, ended | `harvester_workers` | as the other records, from the workers the harvester reports; a worker not terminal after seven days is not counted live |
| Workers submitted | `harvester_workers` | `submission`: per queue, the workers submitted in the interval |
| Workers not finished | `harvester_workers` | per site over 24 hours, grouped by the harvester's diagnostic |
| Errors | `harvester_dialogs` | `dialogs`: the harvester's ERROR and CRITICAL messages to the PanDA server over 24 hours, per module with the latest, and per kind of message (the message with its numbers set aside) with its count, first and latest (reporter 1.1) |
| Service metrics | `harvester_metrics` | the latest per login node: memory, CPU, scratch use, proxy lifetime |

Job fetching, the adjuster's applied limit, failed calls and log freshness
come from the logs and are error fields naming why. The queue page shows a
queue each kind of error that names it or names none of the harvester's
queues; a kind that names only another of its queues is not shown there,
so an error on a queue we do not use
(NERSC_Perlmutter_epic, the non-ES queue) does not read as the Event
Service queue's. The Perlmutter queues
run in pull mode (`mapType` NoJob), so their pilots fetch their own jobs
and the harvester fetches none. Every queue there sets no ratio, so the
limit in force is its `nQueueLimitWorker`. The job-demand cache is in the
harvester's own database and does not reach the PanDA server.

`scripts/perlmutter-harvester-reporter.py` runs every five minutes from the
deployed release under the deployment account's cron and stores the record
through `host_reports.store`, the ingest's write path; `--print` prints it
and stores nothing. The crontab line:

```
1-56/5 * * * * bash -lc 'source ~/.env && timeout 240 /opt/swf-monitor/current/.venv/bin/python /opt/swf-monitor/current/scripts/perlmutter-harvester-reporter.py' >> /opt/swf-monitor/shared/logs/perlmutter-harvester-reporter.log 2>&1
```
`host_reports.HARVESTER_HOSTS` is the one list of harvester records that
every reader takes: the comp snapshot, the queue page, worker admission,
the GREX pilot regulator and the queue registry's drift check.

## Related

- [PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md),
  [OSG_SUBMIT_REPORTER.md](OSG_SUBMIT_REPORTER.md) — the other host reporters.
- [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) § SSL/TLS — the
  2026-10-02 certificate outage.

## Regulation telemetry, 22 October 2026

Version 1.3 adds actual worker starts, job-demand cache update times, and the ratio/minimum/maximum fields of the worker limit. OSG reporting runs every minute; other hosts keep their existing cadence.

## Admission telemetry

Version 1.4 separates native OSG exit-73 pre-job refusals and recorded
cancelled SIGTERM stops from unexpected worker errors. Admission telemetry
covers one hour: remaining and required lifetime from launcher stderr,
execution queue and host from startup, exact slot identity and measured
slot-busy/execution seconds from the Condor event log. Repeated refusals are
grouped by slot and admission floor. Up to 4,000 new startup records are read
per run, with parsed evidence cached until it leaves the hour. Missing logs
remain unclassified; evidence coverage is reported.

The dashboard's slots/hour link opens `/panda/worker-starts/`, which combines
this evidence with a cached worker-start cohort and current payload-job and
worker counts. Recorded worker wall time includes startup and reporting
delay; Condor slot-busy and execution time are stated separately. The page
does not equate worker starts with useful payload capacity or diagnose a
node from admission declines.

Version 1.5 adds `queue_status`: each queue's `queueStatus` in the harvester queue configuration, the copy the queue registry's drift check compares (swf-epicprod QUEUE_REGISTRY.md).

Version 1.6 adds `queue_status.pilot_dispatch`, read from each queue's
actual `submitter.templateFile`: native pilots' exact bound image, known
legacy ALRB launchers' dynamic image selection, or an explicit unknown
with its reason. Each reading includes the template path and SHA-256.
The registry's admission check reads this fresh host evidence; unavailable,
unresolved or conflicting image bindings never imply compatibility.

Version 1.7 follows an image allocator named by the native template's
`EpicprodImagePolicyFile`. It advertises that policy's images only while
the allocator's matching active state is fresh. Missing or expired state
is unknown capability, so a stopped allocator cannot keep authorizing new
work for an image it no longer supplies. The authenticated
`/api/osg-image-demand/?memory_mb=4096&cores=1` read groups activated jobs
by exact image in one query for the two-minute allocator. Disk selection
remains the pilot dispatcher's actual-free-space check; a Condor disk
reservation is not that free-space limit.

Version 1.8 adds `health.volumes` for `/`, `/var`, `/data`, and `/tmp`:
total and unprivileged available bytes, percent used, and filesystem device
identity. The `host_disk_space` alarm reads these reports alongside the OSG
submit and PanDA server reporters. It warns at 90% used, naming the host,
filesystem and remaining space, before scheduler or log writes fail.
Aliases of the same filesystem are counted once. Missing or stale disk
readings raise a monitoring incident instead of implying recovery.
