# Snapper operations in SWF

Snapper is installed as a generic Django application in the SWF runtime and
uses the existing `swfdb` PostgreSQL database. SWF owns the domain adapters,
capture process, configuration, monitoring, and presentation.

The deployment follows the same infrastructure boundary as corun-ai: Apache
serves Django views backed by database rows, while an independently supervised
worker performs asynchronous work. The `epicprod-ops-agent` systemd service is
Snapper's initial worker. It invokes standalone doers and never captures state
inside an Apache request.

## Capture scheduler

The ops agent evaluates both `testbed` and `epicprod` through:

```text
scripts/capture-system-snap.py --scope all
```

The agent polls every 10 seconds by default. The doer reads each scope's
effective policy from `SysConfig` on every invocation, aligns the opportunity,
and lets the PostgreSQL capture cursor make duplicate or quiet evaluations
cheap. The database transaction serializes each scope and locks its registered
components in stable name order. The agent stamps each invocation before
starting the Django doer, so process-start latency cannot move an evaluation
into a later boundary or manufacture a coverage gap.

The first read seeds these operator-visible `SysConfig` keys:

| Key | Commissioning default |
|---|---:|
| `snapper_opportunity_seconds_testbed` | `10` |
| `snapper_baseline_every_testbed` | `10` |
| `snapper_capture_policy_testbed` | `testbed-v1` |
| `snapper_opportunity_seconds_epicprod` | `10` |
| `snapper_baseline_every_epicprod` | `10` |
| `snapper_capture_policy_epicprod` | `epicprod-v1` |
| `snapper_lock_timeout_ms` | `5000` |

Opportunity intervals below 10 seconds are rejected. A lock timeout or another
capture failure is recorded in the scope cursor; the next successful boundary
creates a recovery snap. Periodic quiet and duplicate results remain in the
bounded cursor and service journal. Material captures and failures enter the
epicprod action stream.

## Manual capture

An authorized operations publisher sends `capture_system_snap` to
`/queue/epicprod.ops`, using the existing `prodops` namespace boundary:

```json
{
  "msg_type": "capture_system_snap",
  "namespace": "prodops",
  "scope": "testbed",
  "created_by": "operator-name"
}
```

`scope` may be `testbed`, `epicprod`, or `all`. The doer targets the next
aligned boundary when the current boundary has already been evaluated. Results
are published as `snapper_capture_ready` on `/topic/epictopic` and recorded in
the action stream with the requesting identity.

## Operational status

System Status exposes one check per scope:

- `snapper-testbed-scheduler`
- `snapper-epicprod-scheduler`

The checks read PostgreSQL cursor state only. They report stale heartbeats,
consecutive failures, open coverage gaps, invalid SysConfig values, and the
latest scheduler outcome. The heartbeat threshold is derived from the scope's
opportunity interval, with a 60-second minimum.

## Web presentation

The monitor mounts Snapper beneath the global System navigation. Its primary
view state is represented by server routes rather than client-side tab state:

```text
/snapper/<scope>/report/
/snapper/<scope>/report/<snap-uuid>/
/snapper/<scope>/system/
```

`scope` is `testbed` or `epicprod`. The Report tab presents one selected full
snap and the latest 100 history rows in a sortable table. Component envelopes
use a generic registration-driven presentation; SWF-owned component presenters
may add domain-specific rendering while retaining the complete component JSON.

The System tab presents the effective scope policy, scheduler cursor and
health, and the complete component registration catalog. It is read-only;
operator configuration remains in the existing System configuration editor.
Tab, scope, and selected-snap links are durable URL state, so reload, browser
history, bookmarks, and external proxy links preserve the selected view.

Component titles are descriptive human labels. Internal component names,
publisher identities, policy names, field paths, and resolver names use plain
text, not HTML code styling: the active theme renders code text red, which
signals an error. When an internal name is useful, present it explicitly as
“Internal name: …” beside the descriptive title.

During an active coordinated baseline, deployments use the repository's
standard script from the current infra/baseline-vNN branch:

```bash
sudo bash deploy-swf-monitor.sh branch infra/baseline-vNN
```

The script creates the normal isolated release copy under
/opt/swf-monitor/releases, freezes the generic snapper-ai package and other
editable local packages into the release virtual environment, runs migrations,
updates /opt/swf-monitor/current, restarts the required workers, and performs
the HTTP health check. Check every local package tree that the script freezes
for uncommitted work before running it.

## Component maintainers

The full five-minute System status refresh also maintains three Snapper
component families before the independent capture scheduler observes them:

- **System health** in both scopes (internal name: health), from the bounded
  System status registry;
- **PanDA activity** in epicprod (internal name: panda), from the curated PanDA
  activity adapter; and
- **Workflow activity** in testbed (internal name: workflow), from the
  workflow-execution records and the STF prompt-processing PanDA tasks.

The PanDA adapter publishes trailing 24-hour job and task counts, current counts
for every in-flight job state and nonterminal task state, running cores, and
bounded target-site and task-type maps. It excludes users and individual
records. Selected
`refresh-system-status.py --only` runs skip the PanDA query; full manual and
periodic refreshes publish it. A publication error causes the refresh doer to
fail visibly while preserving the last accepted component state.

The workflow adapter publishes running and trailing-24-hour workflow execution
counts (by workflow name) and STF processing task counts (processingtype
stfprocessing) split by target site and status — the prompt-processing decision
box's site assignment as recorded state. On the testbed Time history these
render as curves (workflow executions running; STF tasks by site · status)
alongside the datataking lanes.

The datataking component's RunState source advances from the run lifecycle
messages themselves: the ActiveMQ processor applies each stamped transition
(`monitor_app/run_state_transitions.py`) and republishes the component, so
lanes track the E0-E1 state machine in real time. The
`repair_stuck_run_states` management command (dry-run default) terminalizes
rows from before this write side existed.
