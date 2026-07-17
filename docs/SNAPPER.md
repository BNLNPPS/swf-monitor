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

Deployments use the standard `deploy-swf-monitor.sh branch main` path. That
freezes the generic `snapper-ai` package into the release virtual environment,
runs migrations, updates `/opt/swf-monitor/current`, and restarts the
`epicprod-ops-agent` worker.
