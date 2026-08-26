# PanDA server host reporter

A reporter agent on the PanDA server host (pandaserver01) delivers
server-side observations to swf-monitor for the platform-health
component and view (SNAPPER_PLATFORM.md). It complements what the PanDA
database shows from outside the host: request rates and outcomes at the
web tier, daemon liveness, and host resources. The agent is a
standalone script in the house pattern — Python standard library only,
local state file, periodic run, HTTPS delivery to a swf-monitor REST
ingest under a token — and it is read-only with respect to PanDA.

## Functions

Each run covers one 5-minute interval and posts one record.

| Function | Source on the host | Fields delivered |
|---|---|---|
| Web-tier request accounting | `/var/log/panda/panda_server_access_log` | requests per endpoint (updateJob, getJob, harvester, others); HTTP status split (2xx, 4xx, 5xx); request duration percentiles where logged |
| Web-tier error markers | `/var/log/panda/panda_server_error_log` | counts of worker-saturation markers, SSL read failures, WSGI errors, per interval |
| Daemon liveness and log freshness | `/var/log/panda/panda-*.log`, process table | for each PanDA daemon (Watcher, copyArchive, JobGenerator, JEDI daemons, MCP): process present, seconds since last log line, restart count since last run |
| Watcher activity | `/var/log/panda/panda-Watcher.log` | lost-heartbeat kills per interval, oldest heartbeat age seen |
| Service state | `systemctl status` (unprivileged view) | active/inactive per PanDA unit: panda_httpd, panda_daemon, panda_jedi, panda_mcp |
| Host resources | `/proc`, `df` | load average, memory, root and /var volume use, httpd and python process counts |
| Database reachability from the server host | TCP connect and a timed trivial query | connect latency, query latency, failure flag |
| Web-tier occupancy | Apache `mod_status` on localhost | busy and idle workers, scoreboard — not enabled today (see below) |
| System journal events | `journalctl` | daemon crashes, OOM kills, unit restarts — not readable today (see below) |

Every failure to read a source is delivered as a field, never dropped;
an unreachable swf-monitor buffers records locally and posts the
backlog on the next run. The component's freshness watch reports a
silent reporter.

## Access inventory

Verified on 2026-08-25 for the account that will run the reporter
(uid `wenauseic`, group `eic`):

Available without additional privilege:

- All PanDA logs: `/var/log/panda/` is world-writable with world-readable
  files, including the web-tier access and error logs and every daemon
  log.
- PanDA configuration: `/etc/panda/panda_server.cfg` and
  `panda_jedi.cfg` are readable.
- Process table, `/proc`, `df`, unprivileged `systemctl` views.
- Outbound HTTPS to swf-monitor (`pandaserver02.sdcc.bnl.gov`, verified)
  and TCP to the database host (`pandadb01.sdcc.bnl.gov:5432`, verified).
  `psql` is installed; the system `python3` (3.11) has no `psycopg2`, so
  the database check uses `psql` in a subprocess or a TCP connect only.
- Scheduling: cron is permitted for the account (`/etc/cron.deny` is
  empty; no `cron.allow`), so a cron entry runs the reporter today with
  no request. Unprivileged systemd user units exist but do not persist
  across logout without linger.

Privileged access: the account holds passwordless sudo
(`(root) NOPASSWD: ALL`, verified 2026-08-25). Not yet set, and set by
the reporter's install step under that access:

- Lingering user session (`Linger=no` today), so that a user systemd
  unit persists across logout; alternatively a system unit, as the
  swf-monitor bots on pandaserver02 are installed.
- System journal: the account is not in `systemd-journal`; daemon
  crash, OOM, and unit restart events are read once it is.
- Root-only logs: `/var/log/messages` (`/var/log/httpd/` is empty;
  PanDA's web tier logs under `/var/log/panda/`).
- Apache `mod_status`: not enabled (no listener on localhost:80).

Host note: the root volume is 12 GB at 64% use, /var is 32 GB at 40%;
the account's home is on the shared EIC NFS volume at 91% use. The
reporter's state and buffer files are kept small and rotated.

## Privileged setup at install

Each step is one root action, read-only in effect except the last:

1. **Persistent service** — a system unit under `/etc/systemd/system/`
   with `Restart=always`, or `loginctl enable-linger wenauseic` with a
   user unit. Either replaces the cron form.
2. **`systemd-journal` group membership** — adds daemon crash, OOM, and
   unit restart events to the report.
3. **Apache `mod_status` on localhost** — one httpd configuration
   fragment (`ExtendedStatus On`, `/server-status` allowed from
   127.0.0.1 only). Adds direct web-tier occupancy: busy and idle
   workers against `MaxRequestWorkers`, the saturation signal the logs
   only show indirectly.
4. **Service control** — `systemctl status|restart` on `panda_httpd`,
   `panda_daemon`, `panda_jedi`, `panda_mcp`, for the operations step
   beyond monitoring: the worker releaser (SNAPPER_PLATFORM.md, Worker
   release for stalled jobs) and daemon restarts on a silent-daemon
   detection. Operational control stays behind the platform's
   proposal and action-stream conventions; the access alone changes
   nothing.

Every install action on this host is announced and recorded in the
action stream before it is taken.

## Delivery

The reporter posts to a swf-monitor REST ingest endpoint authenticated
by a per-host token, in the arrangement the GPU worker host uses. The
ingest stores the record as the server-side fields of the platform
component; the component publication on the 5-minute refresh merges
them with the database-side fields. Token and endpoint live in the
reporter's environment file, mode 600.

## Related

- SNAPPER_PLATFORM.md — the platform-health component, view, and
  correlation functions this reporter feeds.
- [SNAPPER.md](SNAPPER.md) — Snapper operations in SWF.
- [ERROR_ATTRIBUTION.md](ERROR_ATTRIBUTION.md) — the error-label
  correction service that consumes the same observations.
