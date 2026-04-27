# MCP Stabilization Status

Date: 2026-04-27

Related plan: [MCP_STABILIZATION_PLAN.md](MCP_STABILIZATION_PLAN.md)

## Summary

The first stabilization pass has been implemented, committed, pushed, deployed,
and verified on swf-testbed.

Commit:

```text
ed87bd8 Stabilize local MCP service operation
```

The live PanDA bot and testbed bot now initialize MCP successfully through the
local loopback ASGI endpoint.

## Current Operating Model

MCP on swf-testbed is operated as local stateless POST request/response MCP:

- Local endpoint: `http://127.0.0.1:8001/swf-monitor/mcp/`
- Public Apache path remains present for routing, but long-lived GET/SSE
  streaming is not an operational dependency.
- `DJANGO_MCP_GLOBAL_SERVER_CONFIG["stateless"] = True`
- The ASGI service remains isolated from the main mod_wsgi Django site.

The useful supported surface is:

- `initialize`
- `tools/list`
- `tools/call`

## Implemented Changes

1. MCP stateless mode enabled.

   `src/swf_monitor_project/settings.py` now sets:

   ```python
   "stateless": True
   ```

2. ASGI worker bounded.

   `swf-monitor-mcp-asgi.service` now runs uvicorn with:

   ```text
   --workers 4
   --limit-concurrency 32
   --timeout-graceful-shutdown 15
   ```

   `TimeoutStopSec=30` is also set at the systemd layer.

3. Apache MCP proxy simplified.

   `apache-swf-monitor.conf` now treats MCP as bounded request/response
   traffic:

   - proxy timeout reduced from 3600s to 60s
   - streaming-specific `proxy-sendchunked` and `no-gzip` settings removed

4. Bots moved to local MCP by default.

   PanDA bot and testbed bot default to:

   ```text
   http://127.0.0.1:8001/swf-monitor/mcp/
   ```

5. Bot exception paths fixed.

   PanDA bot initializes tool metadata before MCP calls can fail, preventing
   the observed secondary `UnboundLocalError`. PanDA and testbed bots now log
   unexpected response-task exceptions and return a visible Mattermost error
   instead of failing silently.

6. Async DB logging fixed.

   `DbLogHandler` no longer writes Django ORM records directly from the caller
   context. It now queues log payloads and writes them from a background thread,
   avoiding Django async-context ORM errors.

7. MCP health endpoint added.

   New endpoint:

   ```text
   /swf-monitor/api/mcp-health/
   ```

   It verifies that Django can serve a request and reach the default database
   without invoking MCP transport/session code.

8. MCP watchdog added.

   New files:

   - `scripts/mcp_watchdog.py`
   - `swf-monitor-mcp-watchdog.service`
   - `swf-monitor-mcp-watchdog.timer`

   The watchdog checks:

   - MCP health endpoint
   - MCP `initialize`
   - MCP `tools/list`

   When run by the timer with `--restart`, it restarts
   `swf-monitor-mcp-asgi.service` after a failed probe.

9. Documentation updated.

   `docs/MCP.md` and `docs/PRODUCTION_DEPLOYMENT.md` now describe the local
   stateless request/response operating model and the watchdog.

## Deployment Performed

Deployment command used:

```bash
sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch infra/baseline-v35
```

Deployment result:

- release: `branch-infra-baseline-v35`
- deployed commit: `ed87bd8`
- Apache health check: passed
- Apache configuration synced from repository canonical
- ASGI worker restarted by deploy script
- PanDA bot restarted by deploy script
- testbed bot restarted by deploy script

Additional manual systemd unit sync was required because the deploy script
does not install service unit definitions:

```bash
sudo install -o root -g root -m 644 /opt/swf-monitor/current/swf-monitor-mcp-asgi.service /etc/systemd/system/swf-monitor-mcp-asgi.service
sudo install -o root -g root -m 644 /opt/swf-monitor/current/swf-monitor-mcp-watchdog.service /etc/systemd/system/swf-monitor-mcp-watchdog.service
sudo install -o root -g root -m 644 /opt/swf-monitor/current/swf-monitor-mcp-watchdog.timer /etc/systemd/system/swf-monitor-mcp-watchdog.timer
sudo systemctl daemon-reload
sudo systemctl restart swf-monitor-mcp-asgi.service
sudo systemctl enable --now swf-monitor-mcp-watchdog.timer
```

## Live Verification

Verified after deployment:

- `swf-monitor-mcp-asgi.service`: active
- `swf-panda-bot.service`: active
- `swf-testbed-bot.service`: active
- `swf-monitor-mcp-watchdog.timer`: active
- MCP health endpoint returned:

  ```json
  {
    "ok": true,
    "service": "swf-monitor-mcp-asgi",
    "database": "ok",
    "mcp_stateless": true
  }
  ```

- Watchdog direct probe returned:

  ```text
  MCP watchdog OK: 45 tools
  ```

- PanDA bot journal showed:

  ```text
  Listening on #pandabot ... MCP: http://127.0.0.1:8001/swf-monitor/mcp/
  HTTP MCP: 13 tools
  ```

- Testbed bot journal showed:

  ```text
  Listening on #swf-testbed-bot + DMs (MCP: http://127.0.0.1:8001/swf-monitor/mcp/)
  Discovered 44 tools via MCP
  ```

User confirmed the bot MCP path is working after deployment.

## Notes And Caveats

The current `django-mcp-server` adapter still creates and shuts down a
`StreamableHTTPSessionManager` per request internally. Stateless mode removes
server-side MCP session dependence and keeps requests short, but it is not a
full replacement for a correct lifespan-managed ASGI MCP implementation.

One watchdog run failed during the exact ASGI restart window and restarted the
ASGI service. A subsequent manual watchdog run succeeded. This is expected for
the initial enable/restart sequence.

The PanDA bot performs model/cache initialization on startup and may show high
CPU briefly after restart. This is separate from the MCP ASGI worker lockup
problem.

## Remaining Follow-Up

1. Monitor journals and process CPU after sustained use.

   Watch:

   ```bash
   sudo journalctl -u swf-monitor-mcp-asgi.service -f
   sudo journalctl -u swf-monitor-mcp-watchdog.service -f
   sudo journalctl -u swf-panda-bot.service -f
   ```

2. Consider updating the deploy script to install changed systemd unit files
   automatically, with validation before reload.

3. Consider optimizing deployment venv handling. The current deploy script
   copies the virtual environment every deploy because releases are
   self-contained; in principle the venv only needs to change when dependency
   inputs change.

4. If remote MCP or GET/SSE streaming becomes a real requirement, implement it
   as a dedicated ASGI app with an application-lifetime
   `StreamableHTTPSessionManager` and load-test it before advertising support.

5. Add request timing metrics around MCP calls if the endpoint still shows
   unexplained stalls under normal local bot usage.
