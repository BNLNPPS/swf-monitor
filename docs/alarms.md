# swf-monitor alarms

Alarms are now owned by `swf-monitor` on `pandaserver02`. The old
`swf-remote` alarm code and database remain available for rollback/reference,
but the live alarm dashboard, editor, runtime state, and cron runner are in
this repository.

## Runtime

- Dashboard: `/swf-monitor/alarms/`
- External face: `/prod/alarms/` through `swf-remote`, proxied to monitor
- Engine code: `alarms/swf_alarms/`
- Engine install: `/opt/swf-monitor/shared/alarms-venv`
- Engine config: `/opt/swf-monitor/config/alarms/config.toml`
- Engine logs: `/opt/swf-monitor/shared/logs/swf-alarms/`
- Cadence: every 5 minutes via cron

The alarm engine is standalone. It does not boot Django. It reads alarm
configuration and writes event/run state directly through psycopg against the
monitor Postgres `entry`, `entry_context`, and `entry_version` tables.

## Email

The alarm engine sends through AWS SES using `boto3`. `notify.py` is the send
hook. This is intentionally isolated so the delivery channel can be replaced
with a BNL-supported SMTP relay or mail API without changing alarm detection.

## Data Model

Rows use a tjai-style generic entry model.

| Context | Kind | Meaning |
|---|---|---|
| `swf-alarms` | `alarm` | One configured alarm. `data.entry_id` names the Python module. `data.enabled` gates email only. |
| `swf-alarms` | `event` | One firing instance. `data.clear_time` null means active. |
| `swf-alarms` | `engine_run` | One engine tick, with aggregate and per-alarm counters. |
| `teams` | `team` | Recipient aliases such as `@prodops`. |

The imported cutover state from `swf-remote` contained 2 contexts, 17,554
entries, and 38 versions: 2 alarm configs, 67 events, 17,484 engine runs, and
1 team.

## Detection Flow

1. Load all non-archived `kind='alarm'` entries.
2. Import `swf_alarms.alarms.<name>` from `data.entry_id`.
3. Call `detect(client, params)`.
4. Create or update event rows using stable `dedupe_key` values.
5. Clear events that are no longer detected on a successful tick.
6. If email is enabled for that alarm and the run is not `--dry-run`, bundle
   new/renotified detections into one email for that alarm.

`data.enabled=False` means "silent": detection still runs, event rows still
update, and the dashboard remains truthful. To stop an alarm algorithm entirely,
archive the alarm row.

## Adding a New Alarm

1. Add `alarms/swf_alarms/alarms/<name>.py`.
2. Expose a `PARAMS` dict and `detect(client, params)`.
3. Yield `Detection(...)` objects from `swf_alarms.common`.
4. Share helper code under `swf_alarms/common/`.
5. Add a corresponding `Entry(kind='alarm', context='swf-alarms')` with
   `data.entry_id='alarm_<name>'`.

The engine dispatches by module name. There is no central registry.

## swf-remote Boundary

`swf-remote` no longer owns alarm runtime or copied alarm navigation. It
preserves monitor-rendered production navigation and replaces only the local
auth block. `/prod/alarms/...` is a proxy to monitor.

Old `swf-remote` alarm files are intentionally retained for rollback/reference.
Do not delete them as part of routine monitor-side alarm work.
