# System Ticks

A system tick is the recorded global state of a scope — the testbed, the
epicprod production system — at an aligned timestamp, written on a fixed
cadence. The tick series is the discrete-time complement to the event
streams the platform already records (messages, the action stream, status
history): events carry exact transition times, ticks carry the uniform
movie. Any instant in the past is answerable as a single-row read; any two
ticks diff into "what changed"; every subsystem joins at a common
timestamp. This realizes, on the platform, the sampled history of the
global state defined in the
[E0-E1 state machine](https://github.com/BNLNPPS/swf-testbed/blob/infra/baseline-v39/docs/e0-e1-state-machine.md):
a vertical cut through the system's concurrent components, made a record.

Ticks sample asynchronous services; state changes between ticks are
carried by the event streams, and the tick is the truth assembled at the
boundary. The design follows the System status boundary rule
([SYSTEM_STATUS.md](SYSTEM_STATUS.md)): an agent-driven writer produces
rows, the web tier and every other consumer read rows, and nothing probes
services in a request path.

## The tick record

Extensibility is the first requirement: the system's state grows
component by component, and the tick record must grow with it without
migrations and without breaking a single consumer. The tick data is one
JSON document:

```json
{
  "v": 1,
  "scope": "testbed",
  "tick_time": "2026-07-16T18:30:00Z",
  "components": {
    "datataking": {"state": "run", "substate": "physics", "run_number": 100123},
    "workflows": {"by_status": {"running": 2, "completed_24h": 14, "failed_24h": 1}},
    "agents": {"alive": 6, "by_type": {"daqsim": 1, "data": 1, "processing": 1, "fastmon": 2}},
    "data": {"stf_files": 15234, "tf_slices": 88210, "slices_by_status": {"queued": 40, "processing": 12, "completed": 88158}},
    "messages": {"sent_1h": 412}
  }
}
```

```json
{
  "v": 1,
  "scope": "epicprod",
  "tick_time": "2026-07-16T18:30:00Z",
  "components": {
    "campaigns": {"current": "26.06", "tasks_by_status": {"draft": 3, "ready": 1, "submitted": 4, "completed": 121, "partial": 2, "failed": 1}},
    "panda": {"as_of": "2026-07-16T18:29:41Z", "jobs_by_state": {"running": 250, "activated": 40, "finished_24h": 3100, "failed_24h": 45}, "tasks_active": 9},
    "alarms": {"active": 0},
    "assessments": {"daily_age_hours": 6.2, "weekly_age_hours": 60.5},
    "ops": {"agent_alive": true, "actions_1h": 12}
  }
}
```

The component field lists above are the starting set, expected to be
revised in review and to grow in use.

### Evolution rules

- `components` is an open map. Adding a component, or a field within one,
  is always safe and is the normal way the tick grows. Consumers must
  ignore unknown keys.
- Within a schema version (`v`), keys are never renamed or removed and
  meanings never change. A breaking change bumps `v`; during a transition
  a writer may emit both versions' fields.
- Every count-by-category is an open map keyed by the domain value
  (`by_status`, `by_state`, `by_type`), so new statuses appear in the
  record without any schema change.
- Counters are cumulative where the source is cumulative; rates are
  derived by differencing adjacent ticks, not stored.
- A component whose data has independent freshness (a cached view of a
  remote system, such as PanDA) carries its own `as_of`.
- One component = one registered collector = one key. A collector owns
  its section's internal structure.

## Data model

New table `swf_system_tick` (`monitor_app.SystemTick`):

| Field | Type | Notes |
|---|---|---|
| `scope` | char, indexed | `testbed`, `epicprod`; unique with `tick_time` |
| `tick_time` | timestamptz, indexed | aligned: wall clock truncated to the tick interval |
| `state` | JSONB | the tick record above |
| `created_at` | timestamptz auto | actual write time; lag against `tick_time` is an assembly-health telltale |

Index `(scope, tick_time)` descending serves "latest" and range scans.
JSONB GIN indexing is deferred until a query pattern needs it.

## Collectors and writer

Tick collectors follow the System status collector pattern: registered
functions keyed by component name, defined per scope in
`monitor_app/system_ticks.py`. Collectors read the local database only —
cached rows, monitor tables, SysConfig. No remote calls in the tick path;
remote-derived state enters through the existing cached views and is
stamped with its `as_of`.

The writer is a standalone doer beside the status refresher:

```bash
scripts/record-system-tick.py --scope all --source ops_agent_periodic
```

The epicprod ops agent runs it on a periodic loop and handles
`msg_type=record_system_tick` for manual triggers, exactly as it does for
`refresh_system_status`. Both scopes read the same database, so one doer
covers both.

## Cadence, retention, configuration

SysConfig keys, present at their defaults:

| Key | Default | Meaning |
|---|---|---|
| `tick_interval_seconds_testbed` | 30 | testbed tick cadence |
| `tick_interval_seconds_epicprod` | 30 | epicprod tick cadence |
| `tick_retention_days_full` | 30 | full-resolution retention |
| `tick_thin_keep_every` | 10 | past the full window, keep every Nth tick |
| `tick_retention_days_thinned` | 365 | thinned retention |

Two scopes at 30 s is under 6k rows/day at a few KB each — negligible for
swfdb; the thinning pass runs in the same doer. Intervals are per scope so
either can be retuned from observed information content without touching
the other.

A missing tick is itself a signal: a System status collector watching the
age of the newest tick per scope turns the System indicator red if the
writer stops, the same stale rule the status cache uses.

## Consumers

- **Pages**: read rows only. A tick history view — the system movie, a
  time slider over the record — becomes possible once the series exists.
- **MCP**: `swf_get_system_tick(scope, at=None)` returning the tick at or
  nearest a time (latest by default), and tick-range retrieval for
  trending. Tool additions follow the standard MCP checklist.
- **AI**: assessments and daily reports diff ticks instead of re-deriving
  state; anomaly detection runs on the uniform series.
- **Incident review**: the tick at the incident time is the system-wide
  context, joined with the event streams for exact sequences.

## Deferred

- Per-namespace tick scopes (the scope field admits them when wanted).
- Downsampling/rollup aggregates beyond simple thinning.
- Event-tick correlation views.
