# Harvester host reporter

A reporter on each harvester host we can reach (pandaharvester01, osgsub01) delivers to swf-monitor
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

pandaharvester02 runs a third harvester instance, operated by PanDA
operations; the reporter runs there once its database read access is
granted.

Verified on 2026-10-02 for the account (`wenauseic`, group `eic`): the
harvester logs are world-readable, cron is permitted, the host's `python3`
is 3.6.8, and the monitor answers over HTTPS. No change to the harvester,
its configuration or its submit descriptions is part of installing the
reporter.

## Related

- [PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md),
  [OSG_SUBMIT_REPORTER.md](OSG_SUBMIT_REPORTER.md) — the other host reporters.
- [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) § SSL/TLS — the
  2026-10-02 certificate outage.
