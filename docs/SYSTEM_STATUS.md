# System Status

The System status page is the monitor's cached health view for production
infrastructure. It is intentionally broader than PanDA, but it is routed under
the working production path so it is visible on both the internal monitor and
the devcloud proxy.

| Surface | URL |
|---|---|
| Internal | `https://pandaserver02.sdcc.bnl.gov/swf-monitor/panda/system/` |
| External | `https://epic-devcloud.org/prod/panda/system/` |
| Internal JSON | `https://pandaserver02.sdcc.bnl.gov/swf-monitor/panda/system/status.json` |
| External JSON | `https://epic-devcloud.org/prod/panda/system/status.json` |

## Design rule

The web tier does **not** probe services on page load. The page and the nav read
the cached database state. The production ops agent is responsible for keeping
that state fresh.

This preserves the same boundary used elsewhere in epicprod:

- **Ops agent** performs active checks and writes results.
- **Django web tier** reads cached rows and renders them.
- **Browser nav** polls the cached JSON endpoint, not the underlying services.

## Data model

Current status lives in `monitor_app.SystemStatus`
(`swf_system_status`). Historical observations live in
`monitor_app.SystemStatusHistory` (`swf_system_status_history`).

Both tables include a JSON `data` field so collectors can add structured
evidence without another schema migration. Current rows are keyed by stable
collector name; history rows are append-only observations used for later
incident review.

`SystemStatus` core fields:

- `name`: stable collector key, e.g. `epicprod-ops-agent`
- `category`: display group, e.g. `agents`, `services`, `external`
- `status`: `ok`, `warning`, `error`, or `unknown`
- `summary`: short operator-facing explanation
- `data`: JSON evidence from the collector
- `checked_at`: when the collector produced this observation

## Collectors

The initial collector set is defined in `monitor_app/system_status.py`:

| Collector | Category | Meaning |
|---|---|---|
| `epicprod-ops-agent` | `agents` | systemd state plus monitor heartbeat row |
| `swf-panda-bot` | `agents` | systemd state plus monitor heartbeat row |
| `swf-monitor-mcp-asgi` | `services` | systemd state |
| `httpd` | `services` | systemd state |
| `epic-devcloud-prod` | `external` | HTTP check of the external face `/prod/` |
| `epic-devcloud-doc` | `external` | HTTP check of the external face `/doc/` |
| `github-actions` | `ci` | Latest completed GitHub Actions run of every workflow in the core repos (`GITHUB_REPOS` in `system_status.py`), on `main` and `infra/baseline-*` branches only; a failing workflow is a warning (development CI does not redden the collaboration-facing indicator), with the failing run linked in the summary |
| `bot-usage` | `agents` | Informational (always ok): bot user turns over the last 7 and 30 days, channel vs DM, from the recorded exchanges. Aggregate counts only — no per-user detail on this open surface |
| `campaign-assessments` | `agents` | Scheduled campaign-assessment slots actually filled: ages the newest registered daily/weekly assessment per target campaign against SysConfig thresholds (`assessment_daily_stale_hours`, `assessment_weekly_stale_hours`). A run lost anywhere upstream of registration — trigger, corun run, callback, enforcement — goes red here. Policy in `swf_epicprod.assessment.freshness` |

The `external` category is rendered as **Public Web Services** in the UI and
`ci` as **Continuous Integration**. The external endpoint URLs derive from the
external-face configuration point (`external_face_base_url`). The GitHub
collector uses the public API unauthenticated; `GITHUB_TOKEN` or `GH_TOKEN` in
the agent environment raises the rate limit if ever needed.

## Refresh mechanism

The ops agent handles `msg_type=refresh_system_status` on
`/queue/epicprod.ops`. It delegates to the standalone doer:

```bash
scripts/refresh-system-status.py --source ops_agent_periodic
```

This is deliberately **not** a Django management command. The same doer is used
for manual refreshes and periodic refreshes.

The agent starts a periodic refresh loop:

- `EPICPROD_SYSTEM_STATUS_INTERVAL`, default `300` seconds
- `EPICPROD_SYSTEM_STATUS_INITIAL_DELAY`, default `30` seconds
- `EPICPROD_SYSTEM_STATUS_TIMEOUT`, default `60` seconds

Manual refresh from the page posts to `panda/system/refresh/`, which queues the
same `refresh_system_status` message to the ops agent. It does not run checks in
the Apache request.

## Overall status

`status_summary()` derives the aggregate state from cached current rows:

- `error` if any current row is `error`
- `error` if the latest cached check is older than 15 minutes
- `warning` if any row is `warning` or `unknown`
- `ok` when all current checks are OK and fresh
- `unknown` before any rows exist

The stale rule is important: if the ops agent stops refreshing, the System menu
must eventually turn red even if the last individual checks were green.

The stale threshold is the `STATUS_STALE_AFTER` constant in
`monitor_app/system_status.py`, currently 15 minutes. That is three missed
cycles at the default 5-minute ops-agent refresh interval. Tune this constant if
the nav produces false stale alarms or reacts too slowly to a dead refresher.

## Navigation indicator

The production nav `System` item is red when the aggregate status is red.

Initial page render gets aggregate state through the global context processor
`monitor_app.context_processors.system_status_nav`. While a browser page remains
open, base-template JavaScript polls:

```text
panda/system/status.json
```

once per minute. The endpoint reads only cached database state. On devcloud the
same reversed URL is served as:

```text
/prod/panda/system/status.json
```

The browser also applies the 15-minute stale rule locally between JSON polls.

## UI conventions

- Tables size to content instead of stretching across the full window.
- Status cells use the existing BigMon-style filled state classes
  (`ok_fill`, `warning_fill`, `error_fill`, `unknown_fill`).
- URLs in summaries and JSON evidence are clickable.
- The header shows dynamic time since the latest cached check.

## Operational checks

Quick health checks after deploy:

```bash
curl -sS https://epic-devcloud.org/prod/panda/system/status.json | python3 -m json.tool
curl -sS -H 'Host: pandaserver02.sdcc.bnl.gov' \
  http://127.0.0.1/swf-monitor/panda/system/status.json | python3 -m json.tool
systemctl is-active epicprod-ops-agent swf-panda-bot swf-monitor-mcp-asgi httpd
```

Expected healthy JSON shape:

```json
{
  "overall_status": "ok",
  "overall_reason": "All current checks are OK.",
  "latest_checked_at": "2026-06-17T22:37:27.720575+00:00",
  "counts": {
    "ok": 6,
    "warning": 0,
    "error": 0,
    "unknown": 0,
    "total": 6
  }
}
```
