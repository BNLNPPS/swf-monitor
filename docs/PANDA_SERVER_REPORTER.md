# PanDA server host reporter

A reporter agent on the PanDA server host (pandaserver01) delivers
server-side observations to swf-monitor for the platform-health
component and view (SNAPPER_PLATFORM.md). It complements what the PanDA
database shows from outside the host: request rates and outcomes at the
web tier, daemon liveness, and host resources. The agent is a
standalone script in the house pattern — Python standard library only,
local state file, periodic run, HTTPS delivery to a swf-monitor REST
ingest under a token — and it is read-only with respect to PanDA. The
script is `scripts/panda-server-reporter.py`; it has run on the host
under cron, on a five-minute cadence, since 2026-09-07.

## Functions

Each run posts one record covering the interval since the previous
run. The access and error logs are read from the byte position the
previous run left, so a record counts exactly the lines appended since
it; a rotated log restarts from its beginning and the record says so;
the first run sets the positions and counts nothing.

| Function | Source on the host | Fields delivered |
|---|---|---|
| Web-tier request accounting | `/var/log/panda/panda_server_access_log` | requests in the interval by endpoint class (`update_job`, `acquire_jobs`, other pilot calls, harvester, the schedconfig cache, statistics, other) and by HTTP status class, the 5xx count, distinct clients and paths, the twenty most requested paths; the web tier's declared capacity (`MaxRequestWorkers`, `ServerLimit`, `ThreadsPerChild`, WSGI daemon processes) from the httpd configuration. The access log carries no request duration, so none is reported. |
| Web-tier error markers | `/var/log/panda/panda_server_error_log` | lines in the interval by Apache level and by named marker (worker saturation, WSGI response timeout, truncated WSGI response, SSL read failure, child exit signal, memory allocation failure); the last five lines above info level |
| Daemon log freshness | `/var/log/panda/*.log` | for every PanDA log: seconds since its last write and its size. A daemon is named by its log, and the age of the last write is what says whether it is working. |
| Service state | `systemctl show` (unprivileged view) | per PanDA unit (panda_httpd, panda_daemon, panda_jedi, panda_mcp): active state, sub-state, restart count since boot, seconds active |
| Processes | `/proc` | the httpd workers and the pandaserver python processes: count and resident memory |
| Host resources | `/proc` | load average, memory and swap, root and /var volume use, uptime |
| Database reachability from the server host | TCP connect | connect latency to the database host and port named in `/etc/panda/panda_server.cfg`; no credential is used, so this is reachability and the network's cost, not a query |
| Web-tier occupancy | Apache `mod_status` on localhost | busy and idle workers, scoreboard — not enabled today (see below) |
| System journal events | `journalctl` | daemon crashes, OOM kills, unit restarts — not readable today (see below) |

Lost-heartbeat kills are not counted here: they are the error-state
component's entries (SNAPPER_PLATFORM.md, one record per fact), and
the Watcher's log is reported among the daemon logs like every other.

Every failure to read a source is delivered as a field, never dropped;
an unreachable swf-monitor buffers records locally and posts the
backlog on the next run. The component's freshness watch reports a
silent reporter. A record is about 9 KB and takes under 0.1 s to
collect.

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

The reporter posts to `api/host-reports/pandaserver01/` on swf-monitor,
authenticated by the token of the Django user `pandaserver01-reporter`,
held in the reporter's environment file at mode 600. The ingest stores
the record whole in the cached-product store under a key naming the
host ([OSG_SUBMIT_REPORTER.md](OSG_SUBMIT_REPORTER.md), Delivery); the
platform component reads it at each 5-minute publication as its
`server_host` group and publishes `reporter_status` as fresh, stale, or
absent against the configured threshold.

## Install

1. The script at `~/.local/bin/panda-server-reporter.py` and its
   environment file `~/.swf-panda-server-reporter.env` (mode 600,
   `SWF_MONITOR_URL` and `SWF_REPORT_TOKEN`). The account's home
   directory is shared across the SCDF hosts, so both are placed from
   any of them; the reporter's state and buffer live beside them in
   `~/.swf-panda-server-reporter/`, and its one-line run log in
   `~/.swf-panda-server-reporter.log`.
2. The per-host token issued in swf-monitor.
3. A cron entry for the account on the host, every five minutes. The
   crontab is the one per-host part of the install.

The install is recorded in the action stream as
`host_reporter_install` before the cron entry is written. No change to
PanDA, its configuration, or its units is part of installing the
reporter.

## Related

- SNAPPER_PLATFORM.md — the platform-health component, view, and
  correlation functions this reporter feeds.
- [SNAPPER.md](SNAPPER.md) — Snapper operations in SWF.
- [ERROR_ATTRIBUTION.md](ERROR_ATTRIBUTION.md) — the error-label
  correction service that consumes the same observations.
