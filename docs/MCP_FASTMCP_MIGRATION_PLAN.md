# MCP FastMCP Migration Plan

Related history:

- [MCP_STABILIZATION_PLAN.md](MCP_STABILIZATION_PLAN.md)
- [MCP_STABILIZATION_STATUS.md](MCP_STABILIZATION_STATUS.md)
- [MCP.md](MCP.md)

## Motivation

The first stabilization pass made local MCP usable on swf-testbed by enabling
`stateless` mode, bounding the uvicorn worker, simplifying the Apache proxy,
and adding a watchdog. It did not replace the underlying transport. From
`MCP_STABILIZATION_STATUS.md` §"Notes And Caveats":

> The current `django-mcp-server` adapter still creates and shuts down a
> `StreamableHTTPSessionManager` per request internally. Stateless mode removes
> server-side MCP session dependence and keeps requests short, but it is not a
> full replacement for a correct lifespan-managed ASGI MCP implementation.

The watchdog masks the residual lifecycle bug class; it does not fix it. This
plan replaces `django-mcp-server` with the official `mcp` Python SDK
(`FastMCP`) running as a dedicated, lifespan-managed ASGI application. After
the migration the watchdog becomes a transitional safety net rather than a
required component.

## Starting State

| Aspect | Today |
|---|---|
| MCP library | `django-mcp-server` |
| ASGI worker | `uvicorn` on `127.0.0.1:8001`, isolated from mod_wsgi Django |
| ASGI entrypoint | `swf_monitor_project.asgi:application` (full Django ASGI app) |
| Stateless flag | `DJANGO_MCP_GLOBAL_SERVER_CONFIG["stateless"] = True` |
| Session manager lifecycle | per-request `StreamableHTTPSessionManager` create/destroy inside the django-mcp-server adapter |
| Tool surface | ~50 tools across `monitor_app/mcp/` modules: `system.py`, `workflows.py`, `ai_memory.py`, `pandamon.py`, `pcs.py`, plus `swf_list_available_tools` registered in `__init__.py` |
| Implicit tool | `get_server_instructions` is registered automatically by django-mcp-server from `DJANGO_MCP_GLOBAL_SERVER_CONFIG["instructions"]`. There is no explicit tool definition for it in `monitor_app/mcp/`. Removing django-mcp-server without porting it deletes the tool silently. Existing client permission lists already reference `mcp__swf-monitor__get_server_instructions` (see `docs/MCP.md`). |
| Server name | `"swf-testbed"` (from `DJANGO_MCP_GLOBAL_SERVER_CONFIG["name"]`) — clients have this hard-coded in `.mcp.json` and Claude Code permission strings |
| URL prefix | `/swf-monitor/mcp/` mounted in `swf_monitor_project/urls.py` |
| Watchdog | `swf-monitor-mcp-watchdog.service` + `.timer`, restarts ASGI on failed probe |
| Auth | none on loopback; Auth0 OAuth scaffolding present-but-disabled (`monitor_app/auth0.py`, `MCPAuthMiddleware`, `.well-known/` route) |
| Live MCP clients | PanDA Mattermost bot, testbed bot, Claude Code, watchdog — all over loopback `http://127.0.0.1:8001/swf-monitor/mcp/` |

## Decisions And Defaults

1. **Loopback bearer auth — yes.** Apache also proxies `/swf-monitor/mcp/`
   externally. A single shared-secret bearer token gives parity with the
   transport-policy guarantees in the auth section of `MCP.md`. Operational
   clients (PanDA bot, testbed bot, watchdog) gain one env var each. Cheaper
   and safer than re-arming Auth0.
2. **Auth0 — remove.** `monitor_app/auth0.py`, `MCPAuthMiddleware`, the OAuth
   protected-resource metadata view, and the `.well-known/` route are dead
   code under the current operational stance ("the public Apache path still
   exists, but remote Claude.ai GET/SSE streaming is not an operational
   dependency"). Drop them in Phase 3.
3. **Watchdog — keep through bake-in, then retire.** Useful tripwire during
   the first stability window. Delete the unit/timer/script after a clean
   48–72h post-migration.
4. **URL prefix — unchanged.** `/swf-monitor/mcp/` stays. Two production bots
   and outside developer machines have it hard-coded.
5. **Server name — unchanged.** `serverInfo.name` must remain `"swf-testbed"`.
   Changing it breaks `.mcp.json` entries and Claude Code permission strings
   like `mcp__swf-monitor__*`.
6. **uvicorn worker count — drop to 2.** Current `--workers 4
   --limit-concurrency 32` was a defensive setting against the lifecycle
   bug. After migration, fewer workers reduce stuck-process and DB-connection
   failure modes without measurable throughput loss for this workload. Keep
   `--timeout-graceful-shutdown 15` and `TimeoutStopSec=30`.

## Out Of Scope

This migration changes the MCP transport (django-mcp-server → FastMCP) and
the ASGI guard around it. Nothing else. The following are explicitly not
touched and should not be folded into this work:

- `/swf-monitor/api/corun-callback/` and the rest of the Django REST API —
  stay on mod_wsgi.
- Django admin, the PCS web UI, and the SSE message stream — stay on
  mod_wsgi.
- `swf-remote` — does not talk to this MCP endpoint.
- The pandabot ↔ corun MCP integration — pandabot is a client of the
  separate corun MCP server; unrelated to swf-monitor's MCP transport.
- `DbLogHandler` async logging — already fixed during the stabilization
  pass; no further change required.
- The `swf-monitor-mcp-watchdog.service` / `scripts/mcp_watchdog.py` pair
  stays in place through Phase 2; retirement is a Phase 3 decision (step
  17) gated on bake-in, not on the transport swap.

## Phase 1 — Code (dev tree, no service change)

1. Add `mcp` (the official Python SDK) to `requirements.txt`. Leave
   `django-mcp-server` installed alongside until Phase 2 cuts over, so the
   running service is unaffected during development.

2. Move the instructions string out of `DJANGO_MCP_GLOBAL_SERVER_CONFIG` into
   a top-level `settings.py` constant so both the FastMCP constructor and the
   shim tool read from a single source of truth:

   ```python
   # swf_monitor_project/settings.py
   MCP_SERVER_NAME = "swf-testbed"
   MCP_SERVER_INSTRUCTIONS = """Streaming workflow orchestration testbed for the
   ePIC experiment at the Electron Ion Collider.
   ...verbatim, no edits to the existing string...
   """
   ```

3. Create the FastMCP instance and the compatibility tool. Because the tool
   modules already share a single `mcp` import, do this in
   `monitor_app/mcp/__init__.py`:

   ```python
   from django.conf import settings
   from mcp.server.fastmcp import FastMCP

   mcp = FastMCP(
       settings.MCP_SERVER_NAME,                # "swf-testbed"
       instructions=settings.MCP_SERVER_INSTRUCTIONS,
       stateless_http=True,
       json_response=True,
       streamable_http_path="/",
   )

   @mcp.tool()
   async def get_server_instructions() -> str:
       """Get the swf-monitor MCP server instructions.

       Compatibility tool for clients and permissions lists that previously used
       django-mcp-server's server-instruction helper.
       """
       return settings.MCP_SERVER_INSTRUCTIONS
   ```

4. In each tool module (`system.py`, `workflows.py`, `ai_memory.py`,
   `pandamon.py`, `pcs.py`), replace

   ```python
   from mcp_server import mcp_server as mcp
   ```

   with

   ```python
   from monitor_app.mcp import mcp
   ```

   No tool function bodies change.

5. Create `swf_monitor_project/mcp_asgi.py` — a dedicated ASGI app that
   replaces `swf_monitor_project.asgi:application` as the uvicorn entrypoint
   for the MCP service. It must:

   - call `django.setup()` after setting `DJANGO_SETTINGS_MODULE`
   - mount `mcp.streamable_http_app()` under a `Starlette` app with a lifespan
     that runs `mcp.session_manager.run()` for the application's lifetime
     (this is the fix for the per-request session-manager bug)
   - wrap the Starlette app in an ASGI guard that:
     - serves a `/health` endpoint with `{"status": "ok"}` (no auth)
     - rejects non-`POST` methods with HTTP 405
     - validates `Authorization: Bearer <token>` against
       `settings.MCP_BEARER_TOKEN`, returning 401/403/503 appropriately, and
       using `hmac.compare_digest` for the comparison
     - normalizes incoming paths so `/`, `/mcp[/...]`, and
       `/swf-monitor/mcp[/...]` all reach the mounted MCP app cleanly
       (handles whatever Apache `ProxyPass` strips or keeps)

   `MCP_BEARER_TOKEN` is a new setting read from `production.env`. Generate
   with `python -c "import secrets; print('swf_'+secrets.token_urlsafe(32))"`.

6. Remove `DJANGO_MCP_GLOBAL_SERVER_CONFIG` and `DJANGO_MCP_ENDPOINT` from
   `settings.py` once the new constants land. **Keep the MCP URL mount in
   `swf_monitor_project/urls.py` through Phase 1.** Removing it now would
   break the live django-mcp-server endpoint on the next service restart
   (deploys do restart, and uvicorn re-imports the URLconf). The mount
   comes out in Phase 2 in the same change that flips the systemd unit so
   the old endpoint never goes dark before its FastMCP replacement is in
   place. (Auth0 settings stay until Phase 3.)

7. Write `scripts/mcp_migration_smoke.py` — a parity probe that runs against
   two endpoints (the live django-mcp-server URL and a candidate URL) and
   exits non-zero if any check fails. See "Parity Checks" below.

   **Auth asymmetry during the parity window.** Live (post `af0292c`) still
   allows unauthenticated requests — `MCPAuthMiddleware` only enforces a
   token if one is sent. The candidate at `:8013` requires
   `Authorization: Bearer <MCP_BEARER_TOKEN>` on every non-`/health`
   request. The smoke script must therefore take per-endpoint auth config
   (e.g. `--live-token`, `--candidate-token`, either may be empty) rather
   than the candidate accepting a temporary "disable auth" env knob. A
   migration-only auth bypass on the candidate would have to be removed
   later and is exactly the kind of code we don't want shipped.

8. Stand the candidate ASGI up on a non-conflicting port (suggest `:8013`)
   pointed at the same Postgres + production env, run the smoke script
   against `8001` (live) and `8013` (candidate), and iterate until all parity
   checks pass.

## Phase 2 — Cutover

9. Update `swf-monitor-mcp-asgi.service`:

   - replace `swf_monitor_project.asgi:application` with
     `swf_monitor_project.mcp_asgi:application`
   - replace `--workers 4 --limit-concurrency 32` with `--workers 2`
   - keep `--host 127.0.0.1 --port 8001 --timeout-graceful-shutdown 15
     --proxy-headers --forwarded-allow-ips 127.0.0.1` and `TimeoutStopSec=30`

10. Remove the django-mcp-server URL mount. Drop

    ```python
    path("mcp/", include("mcp_server.urls")),
    ```

    from `swf_monitor_project/urls.py`. This deletion ships in the same
    commit that flips the systemd unit so the live django-mcp-server
    endpoint does not stop responding before its FastMCP replacement is in
    place.

11. Distribute `MCP_BEARER_TOKEN` to every client environment *before* the
    ASGI restart in step 12. The candidate has been serving authenticated
    traffic for parity tests, but the production cutover only succeeds if
    these clients hold the token at the moment uvicorn switches:

    - `production.env` for `swf-monitor-mcp-asgi.service` (already set in
      Phase 1 step 5)
    - `EnvironmentFile` (or inline `Environment=`) on
      `swf-panda-bot.service` and `swf-testbed-bot.service`; restart both
      bot units after the env edit so the new value is picked up
    - `swf-monitor-mcp-watchdog.service` and `scripts/mcp_watchdog.py` —
      script must read `MCP_BEARER_TOKEN` and send
      `Authorization: Bearer <token>` on its `initialize` and `tools/list`
      probes
    - local developer environments — add the token to `~/.env` on every
      machine running Claude Code against this MCP and reference it from
      `.mcp.json` as an `Authorization` header

    Validate with one direct authed `curl` per client host against the
    candidate `:8013` before proceeding.

12. Install the unit and reload (the deploy script does not install systemd
    units automatically):

    ```bash
    sudo install -o root -g root -m 644 \
      /opt/swf-monitor/current/swf-monitor-mcp-asgi.service \
      /etc/systemd/system/swf-monitor-mcp-asgi.service
    sudo systemctl daemon-reload
    sudo systemctl restart swf-monitor-mcp-asgi.service
    ```

13. Verify in this order:

    - `systemctl status swf-monitor-mcp-asgi.service` — active, no restarts
    - `curl -s http://127.0.0.1:8001/health` — `{"status": "ok"}`
    - `swf-monitor-mcp-watchdog.service` direct probe — `MCP watchdog OK: N
      tools` with N matching the candidate count from Phase 1
    - PanDA bot journal — `HTTP MCP: N tools`
    - testbed bot journal — `Discovered N tools via MCP`
    - one human-driven `tools/call` end-to-end via Claude Code

14. Update `docs/MCP.md`: Architecture, Transport, Settings, and "Adding New
    Tools" sections currently still describe django-mcp-server. Rewrite to
    describe the FastMCP+ASGI guard architecture and the bearer token. Add
    a one-line note that an HTTP 202 response from FastMCP is normal (it is
    the ack for client-to-server notification frames such as
    `notifications/initialized`) and is not a sign that SSE has returned.

## Phase 3 — Cleanup (after 48–72h clean operation)

15. Remove `django-mcp-server` from `requirements.txt`. Update the deploy
    script to uninstall it from the venv on first deploy after this change
    (single `pip uninstall -y django-mcp-server`).

16. Delete `monitor_app/auth0.py`, the OAuth portion of
    `monitor_app/middleware.py`, the protected-resource metadata view in
    `monitor_app/views.py`, and the `.well-known/` route in
    `swf_monitor_project/urls.py`. Drop the `AUTH0_*` settings from
    `settings.py`. Also remove the now-dead `LocationMatch
    "^/swf-monitor/\.well-known/"` block from `apache-swf-monitor.conf`
    (and any `ProxyPass`/`<Location>` entries that referenced the OAuth
    metadata URL) so Apache config and Django URL conf stay aligned.

17. Decide on the watchdog. If two clean weeks have elapsed with zero
    watchdog-induced restarts, delete `scripts/mcp_watchdog.py`,
    `swf-monitor-mcp-watchdog.service`, and
    `swf-monitor-mcp-watchdog.timer`, and disable the timer on the host.

## Parity Checks (must all pass before Phase 2 cutover)

The Phase 1 smoke script (`scripts/mcp_migration_smoke.py`) probes both the
live endpoint and the candidate, and asserts:

1. **Tool name set equality.** `set(tools/list against live)` ==
   `set(tools/list against candidate)`. The candidate must include every
   name the live server exposes, including django-mcp-server's implicit
   defaults. The known item is `get_server_instructions`. If any name
   appears live-only, either port it or document that it was unused; do not
   delete silently.

2. **Tool schema equivalence.** For each shared name, `inputSchema`
   parameter sets are equal and required-flags match. Description-text
   drift is allowed; structural drift fails the check.

3. **`initialize.serverInfo.name == "swf-testbed"`** on the candidate.
   Anything else breaks `.mcp.json` and `mcp__swf-monitor__*` permission
   strings on every client.

4. **`initialize.serverInfo.instructions == settings.MCP_SERVER_INSTRUCTIONS`**
   verbatim. The instructions string is what Claude Code surfaces as the
   server's system reminder; truncation or whitespace drift is a
   regression.

5. **`tools/call get_server_instructions` byte-equals
   `serverInfo.instructions`.** Same source of truth, two surfaces.

6. **Bearer auth behavior.** A request with no `Authorization` header
   returns 401; a wrong token returns 403; a missing server token returns
   503; a `GET` returns 405; an unauthenticated `GET /health` returns 200
   with `{"status": "ok"}`.

7. **`swf_list_available_tools` parity with `tools/list`.** The hardcoded
   list returned by `get_available_tools_list()` in
   `monitor_app/mcp/common.py` is what an LLM sees when it introspects;
   `tools/list` is what it gets when it asks. Drift between the two has
   bitten before — a tool callable via `tools/call` but missing from the
   self-describer is functionally invisible to clients that rely on the
   discovery helper. Assert: `set(get_available_tools_list())` ==
   `set(tools/list against candidate)`. Reconcile by editing the
   hardcoded list, not by silently shipping the gap.

8. **Bot startup.** After cutover, both PanDA bot and testbed bot journal
   lines show the expected tool counts as listed in Phase 2 step 13.

The smoke script must exit non-zero on any failure and print a clear diff
when a check fails.

## Code Templates

### `monitor_app/mcp/__init__.py` head

```python
"""MCP Tools for ePIC Streaming Workflow Testbed Monitor and PanDA Monitor."""
from django.conf import settings
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    settings.MCP_SERVER_NAME,
    instructions=settings.MCP_SERVER_INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


@mcp.tool()
async def get_server_instructions() -> str:
    """Get the swf-monitor MCP server instructions.

    Compatibility tool for clients and permissions lists that previously used
    django-mcp-server's server-instruction helper.
    """
    return settings.MCP_SERVER_INSTRUCTIONS


# (existing imports of tool modules follow, unchanged in shape)
```

### `swf_monitor_project/mcp_asgi.py` shape

```python
"""Standalone ASGI entrypoint for the swf-monitor MCP server."""
from __future__ import annotations

import contextlib
import hmac
import json
import os
from typing import Any

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")

import django
from django.conf import settings
from starlette.applications import Starlette
from starlette.routing import Mount

django.setup()

from monitor_app.mcp import mcp  # noqa: E402


def _json_body(value: dict[str, Any]) -> bytes:
    return json.dumps(value).encode("utf-8")


async def _send_json(send, status: int, value: dict[str, Any], headers=None) -> None:
    body = _json_body(value)
    response_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if headers:
        response_headers.extend(headers)
    await send({"type": "http.response.start", "status": status, "headers": response_headers})
    await send({"type": "http.response.body", "body": body})


class MCPRequestGuard:
    """Enforce auth and finite POST JSON-RPC before FastMCP sees a request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health":
            await _send_json(send, 200, {"status": "ok"})
            return

        scope = self._normalize_mcp_path(scope)
        method = scope.get("method", "").upper()
        if method != "POST":
            await _send_json(
                send, 405,
                {"error": "MCP endpoint accepts POST JSON-RPC only",
                 "allowed_methods": ["POST"]},
                headers=[(b"allow", b"POST")],
            )
            return

        headers = self._headers(scope)
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            await _send_json(send, 401, {"error": "Authorization required"})
            return

        expected = getattr(settings, "MCP_BEARER_TOKEN", None)
        if not expected:
            await _send_json(send, 503, {"error": "MCP token not configured"})
            return

        if not hmac.compare_digest(auth_header[7:], expected):
            await _send_json(send, 403, {"error": "Invalid token"})
            return

        await self.app(scope, receive, send)

    def _normalize_mcp_path(self, scope):
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        for prefix in ("/swf-monitor/mcp", "/mcp"):
            if path == prefix or path.startswith(prefix + "/"):
                scope = dict(scope)
                scope["root_path"] = root_path + prefix
                scope["path"] = path[len(prefix):] or "/"
                return scope
        return scope

    def _headers(self, scope) -> dict[str, str]:
        headers = {}
        for key, value in scope.get("headers", []):
            headers[key.decode("latin1").lower()] = value.decode("latin1")
        return headers


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield


_mcp_application = Starlette(
    routes=[Mount("/", app=mcp.streamable_http_app())],
    lifespan=lifespan,
)

application = MCPRequestGuard(_mcp_application)
```

### Updated systemd unit (Phase 2)

```ini
[Unit]
Description=SWF Monitor MCP endpoint on ASGI (uvicorn) worker
After=network.target postgresql.service

[Service]
Type=simple
User=wenauseic
Group=eic
WorkingDirectory=/opt/swf-monitor/current/src
EnvironmentFile=/opt/swf-monitor/config/env/production.env
Environment=DJANGO_SETTINGS_MODULE=swf_monitor_project.settings
ExecStart=/opt/swf-monitor/current/.venv/bin/uvicorn \
    swf_monitor_project.mcp_asgi:application \
    --host 127.0.0.1 \
    --port 8001 \
    --workers 2 \
    --timeout-graceful-shutdown 15 \
    --log-level info \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

## Rollback

If the new ASGI fails post-cutover, the immediate rollback is a one-line
unit revert:

```bash
sudo systemctl edit swf-monitor-mcp-asgi.service
# override ExecStart to point back at swf_monitor_project.asgi:application,
# restoring --workers 4 --limit-concurrency 32
sudo systemctl daemon-reload
sudo systemctl restart swf-monitor-mcp-asgi.service
```

The MCP URL mount in `swf_monitor_project/urls.py` (removed in Phase 2
step 10) must be restored before this rollback can serve traffic. Until
Phase 3 deletes `django-mcp-server` from requirements, the venv still has
the old library available.
