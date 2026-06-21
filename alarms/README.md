# swf-alarms

Standalone polling alarm engine for the swf ecosystem (PanDA, streaming
workflow). Zero Django coupling: pulls PanDA data via swf-monitor REST,
persists state in swf-monitor's Postgres, sends email via AWS SES.

Full system overview: see `../docs/alarms.md`. This README is the
engine-developer entry point.

## Why standalone

- Runs on the monitor host without Django bootstrap, project PYTHONPATH,
  or a management command.
- Lightweight deps (`httpx`, `boto3`, `psycopg`) mean a small, portable
  venv.
- The Django side renders dashboards and writes alarm/team configuration
  edits; the standalone engine writes event and engine-run state.

## Install

```bash
cd /opt/swf-monitor/current/alarms
bash deploy/install.sh
```

Creates `/opt/swf-monitor/shared/alarms-venv`, copies
`config.toml.example` to `/opt/swf-monitor/config/alarms/config.toml`
if absent.

Edit `config.toml` (SES region, from address, DB DSN) before the first
live run.

## Run

Dry-run (writes state, suppresses email):

```bash
/opt/swf-monitor/shared/alarms-venv/bin/swf-alarms-run --config /opt/swf-monitor/config/alarms/config.toml --dry-run -v
```

For real:

```bash
/opt/swf-monitor/shared/alarms-venv/bin/swf-alarms-run --config /opt/swf-monitor/config/alarms/config.toml -v
```

## Schedule

See `deploy/crontab.example`. Every 5 minutes is the default cadence.

## Data source

The engine hits swf-monitor's `/api/panda/*` endpoints using the
`engine.service_base_url` from `config.toml`. Adding new panels of
PanDA data (queues, jobs, errors) is a question of swf-monitor exposing
another REST endpoint. No engine topology change is required.

## Adding a new alarm

See `../docs/alarms.md` "Adding a new alarm" for the full mechanism.
Summary:

1. Drop `swf_alarms/alarms/<name>.py` exposing a `PARAMS` dict and
   `def detect(client, params)`, yielding `Detection(...)` objects.
2. Share math via `swf_alarms/common/*`; there is no central registry.
3. Create an `Entry` row (kind='alarm', context='swf-alarms',
   data.entry_id matching the module name) via data migration or
   Django shell.
4. Next cron tick picks it up automatically.

The contract: `detect` must not email, must not raise on transient
fetch failures (log + yield nothing), and must set a stable
`dedupe_key` per entity so state-based dedup works.

## Adding a new channel

Add `send_<channel>(alarm, **cfg) -> bool` in `notify.py`. Wire into
`run.py` behind a `channels = [...]` config knob. Failures must return
False (not raise) so one stuck channel can't cascade.

## "Disabled" (per-alarm) semantics

Each alarm's `data.enabled` flag controls **only the email side**. When
False:

- The algorithm still runs every tick.
- Event rows are still created, and active/clear still ticks.
- The dashboard still shows everything.
- **No SES call is made.** `last_notified` is not updated.

When True, the engine additionally sends email on new detections and on
renotification. "Stop the algorithm entirely" is `archived=True`, not
`enabled=False`. There is no global email switch — per-alarm is the
only control.

## Dedup and renotification

- **State-based dedup.** One active `event` row per `(alarm, entity)`.
  While active, the engine bumps `data.last_seen` without re-emailing.
- **Auto-clear.** On a successful tick where the entity is no longer
  in the detection set, the event's `data.clear_time` is set to now.
  A transient fetch failure does NOT auto-clear — last-known state is
  preserved.
- **One email per alarm per tick.** Every detection that would warrant
  a send this tick (new events, plus events whose renotification
  window has elapsed, plus events created while emails were off) is
  bundled into a single SES email. No more one-email-per-task.
- **Renotification window.** Per-alarm `data.renotification_window_hours`.
  Governs when a still-firing event is eligible to be re-included in
  the next bundle. 0 / missing = one email per event lifecycle (the
  event is bundled once when new, never renotified until it clears and
  re-fires).

## Dashboard

Served by swf-monitor Django at `/swf-monitor/alarms/`. Reads from the same
Postgres `entry` table the engine writes. See
`../src/monitor_app/alarm_views.py`.
