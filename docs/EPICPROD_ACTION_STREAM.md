# epicprod Action Stream

The action stream is the structured record of everything epicprod does: one
record per production action — submissions, task operations, sweeps, imports,
configuration edits, assessments — capturing who did what to what, the
outcome, and the measured duration. It is the raw material for the live view,
for the coming digests and alarms, and for LLM assessment and reporting: the
corpus AI reasons over when it answers "what happened".

Companion docs: [EPICPROD_OPS_AGENT.md](EPICPROD_OPS_AGENT.md) (the agent
whose handlers emit most records), [EPICPROD_OPS.md](EPICPROD_OPS.md) (the
nightly catalog-sync runbook entry). The system-level description lives in the
[ePIC WFMS documentation](https://epic-wfms-docs.readthedocs.io).

## The stream

Records are `AppLog` rows with `app_name='epicprod'` — the action stream is a
namespace within the existing DB-backed swf logging, filterable everywhere
logs are filterable. `instance_name` is the component that performed the
action: `web`, `ops-agent`, `mcp`, `report` (as they come). Log `level`
keeps its universal meaning (INFO/ERROR) and is never repurposed.

Structured fields live in `extra_data`. Reserved keys:

| key | meaning |
|---|---|
| `action` | action identifier, e.g. `task_submit`, `rucio_sweep` |
| `subject_type` / `subject_key` | acted-on object (assessment subject types where applicable) |
| `username` | human or service account driving the action |
| `outcome` | `ok`, `error`, `timeout`, `skipped`, `unrecorded` |
| `duration_ms` | measured execution time — required in spirit for sweeps and timed operations |
| `sublevel` | declared verbosity class (below) |
| `live_default` | declared live-stream recommendation (below) |

Any additional keys are free counts and context (`rows_added=…`,
`jedi_task_id=…`, `summary=…`).

## Publication axes: sublevel and live

Two independent axes govern publication; neither touches log level.

**`sublevel`** — the event's verbosity class, declared at the call site,
AUTHORITATIVE: changing it means changing the event, in code, in git. It says
*which humans* an event reaches:

- `high` — everyone, including digest and email audiences (submissions,
  sweeps, failures, configuration changes)
- `normal` — live-page watchers (fetches, syncs, assessments)
- `low` — deliberately verbose viewers only (routine mechanics)

**`live`** — a special category: "interesting to some humans, now." Each
event declares a `live_default` recommendation; the effective decision is the
`epicprod_live_policy` override registry in SysConfig — the runtime attention
knob, flipped per action on the [live-policy page](#consuming-the-stream)
without a deploy. The two axes are genuinely independent: a low-sublevel
action can be temporarily fascinating (force it live while you watch), and a
high-sublevel bulk operation can be force-quieted while it floods through.

A **channel** is a verbosity setting applied to live events:
`live_stream_q(min_sublevel)` in `monitor_app/epicprod_logging.py` is the one
filter every channel uses. Current and planned channels:

| channel | filter | status |
|---|---|---|
| Logs page live view | live, all sublevels | operating |
| Mattermost #epic-live | live, `normal`+ | planned |
| Hourly/daily email digest | live, `high` (+ ERROR) | planned |
| RSS | live, `normal`+ | planned |

## Recording actions (developers)

Every state-changing or operationally significant action records exactly one
record per outcome path. The enqueue-vs-execute rule: log at *execution*, with
the requesting username carried in the message; the web tier logs only
enqueue failures.

In-process (web views, services, MCP tools):

```python
from monitor_app.epicprod_logging import log_epicprod_action

log_epicprod_action(
    'web', 'campaign_set_current',
    subject_type='campaign', subject_key=campaign.name,
    username=request.user.username,
    sublevel='high', live_default=True,
)
```

From the ops agent (out of process — REST twin with identical semantics):

```python
t0 = time.monotonic()
...run the doer...
self._log_action('rucio_sweep', t0, outcome='ok',
                 username=str(m.get('created_by') or ''),
                 sublevel='high', live_default=True,
                 datasets_updated=n)
```

Rules of the road:

1. Declare every new action in `ACTION_DEFAULTS`
   (`monitor_app/epicprod_logging.py`) — the greppable catalog the live-policy
   page reads. It MUST mirror the call sites.
2. Timed operations pass the start time; every sweep reports its execution
   time to the log.
3. Failed outcomes log at `level=logging.ERROR` — the no-silent-failures
   precept applies to the action stream first.
4. Requester identity travels in the message (`created_by`, `owner`,
   `requested_by`) and is recorded at execution. Anonymous open-face requests
   record an empty username, which is itself information.
5. The logging call never raises; a failed write is logged and the action
   proceeds.

## Consuming the stream

**Operators.** The Logs page (`/logs/`, Logs in the epicprod nav, pre-filtered
`?app_name=epicprod`) is the first live channel: the *Live stream* toggle
(`?live=1`) shows live events with 30-second auto-refresh. The
[live-policy page](../src/monitor_app/templates/monitor_app/live_policy.html)
at `/logs/live-policy/` lists every known action with its declared sublevel,
live default, current override, and effective state — overrides editable in
place when signed in, each save itself logged (`live_policy_edit`).

**LLMs and bots.** `epicprod_list_actions` is the purpose-built MCP tool —
prefer `summarize=True` (counts by action with ok/error split and duration
statistics) for reporting and assessment; filters on action, instance,
subject, username, outcome, and time window for drill-down. `swf_list_logs
(app_name='epicprod')` returns raw records. Tool ergonomics are sized for the
smallest consumer (the bot runs a small model): summarize-first, prescriptive
docstrings.

**Alarms and reports (next).** The stream is the data source for the
catalog-sync freshness alarm and the payload-fetch rate alarm, and the
what-happened section of the automated daily/weekly reports. The
born-vs-adopted task ratio (`created_by='association_sweep'` marks adopted
tasks) is the standing migration metric.

## Scheduled automation on the stream

The nightly `catalog_sync` (cron 02:15 → `enqueue-ops-message.py` → ops
agent) chains: csv catalog import → questionnaire import → association sweep
with auto-intake of direct group.EIC submissions → Rucio output snapshot →
EVGEN assimilation → questionnaire match → progress refresh. Each step logs
its own record with duration; the chain logs a summary record — the
catalog-freshness timestamp. Measured 2026-07-05: csv 8 s, association sweep
2.3 s (14-day window), Rucio snapshot 36 s, EVGEN 16 s, match 2 s, progress
5 s. Runbook: [EPICPROD_OPS.md](EPICPROD_OPS.md#nightly-catalog-sync).

## SysConfig

`SysConfig` (`swf_sys_config`) is the single-record JSON document of
operator-set configuration — live policy overrides, channel settings, sweep
knobs (`questionnaire_csv_url`) — viewable and editable at the bottom of the
System page. It is distinct from `PersistentState`, which is
machine-maintained state (counters, run numbers) and not for human editing.
All system configuration lives in the database and is adjustable through the
UI without deploys; SysConfig edits are themselves live actions in the stream.
