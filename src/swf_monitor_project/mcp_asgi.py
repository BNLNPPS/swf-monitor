"""Standalone ASGI entrypoint for the swf-monitor ASGI worker.

Serves two things from the uvicorn process on 127.0.0.1:8001:

- The MCP server: a lifespan-managed FastMCP service replacing the
  Django ASGI app for /swf-monitor/mcp/ traffic — the fix for the
  per-request StreamableHTTPSessionManager lifecycle that
  django-mcp-server's adapter has.
- The SSE message stream (/api/messages/stream/): the full Django ASGI
  application for exactly this path, so long-held EventSource
  connections live on the async event loop instead of pinning sync
  mod_wsgi workers (SSE_RELAY.md). Apache proxies the path here.

MCPRequestGuard wraps the MCP Starlette app and enforces:
- /health returns {"status": "ok"} with no auth, for the watchdog
- GET on the endpoint root returns the setup page for people and their
  assistants (how to connect on this face, and the external page for
  those outside BNL); every other non-POST is 405 — no server-pushed SSE
- Authorization: Bearer <settings.MCP_BEARER_TOKEN> on every non-health
  request (401 missing, 403 wrong, 503 not configured), OR the proxied
  identity: a request from the localhost hop (the swf-remote SSH tunnel,
  as Apache reports the client) carrying X-Remote-User is accepted as
  that user without a token, the same trust the REST tier gives the
  tunnel (EXTERNAL_ACCESS.md). The identity is published to the tools
  through ``monitor_app.mcp.common.CALLER`` so a tool that takes a
  username defaults to it.
- Path normalization so /swf-monitor/mcp[/...], /mcp[/...], and / all
  reach the FastMCP app cleanly, regardless of what Apache strips

See docs/MCP_FASTMCP_MIGRATION_PLAN.md.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
from typing import Any

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")

import django
from starlette.applications import Starlette
from starlette.routing import Mount

django.setup()

from django.conf import settings  # noqa: E402

from monitor_app.mcp import mcp  # noqa: E402
from monitor_app.mcp.common import CALLER  # noqa: E402

_LOCALHOST = ("127.0.0.1", "::1")


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
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": response_headers,
    })
    await send({"type": "http.response.body", "body": body})


_SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>swf-monitor MCP</title>
<style>
body { font: 17px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #222; background: #fff; max-width: 60em; margin: 2em auto; padding: 0 1em; }
h1 { font-size: 1.6em; } h2 { font-size: 1.2em; margin-top: 1.6em; }
code, pre { font-size: 0.95em; background: #f3f3f3; border-radius: 4px; }
code { padding: 1px 5px; } pre { padding: 10px 14px; overflow-x: auto; }
</style></head><body>
<h1>swf-monitor MCP</h1>
<p>This endpoint serves the ePIC production monitor's MCP toolset: PanDA
tasks and jobs, queues, the production catalog, canary probes, alarms and
pings, AI proposals, Rucio, and the testbed. An AI assistant connected to
it can answer questions about the system and propose actions for a person
to accept.</p>
<h2>Outside BNL</h2>
<p>Use the external face: <a href="https://epic-devcloud.org/prod/mcp/">https://epic-devcloud.org/prod/mcp/</a>,
which explains how to sign in, create a token, and connect. The tools run
there as the signed-in person.</p>
<h2>On the BNL network</h2>
<p>Connect to this endpoint with the shared bearer token, kept in the
monitor's environment as <code>SWF_MONITOR_MCP_TOKEN</code>:</p>
<pre>claude mcp add --transport http swf-testbed https://pandaserver02.sdcc.bnl.gov/swf-monitor/mcp/ \\
  --header "Authorization: Bearer $SWF_MONITOR_MCP_TOKEN"</pre>
<p>Tools that take a <code>username</code> need it on this face; through the
external face it is filled in from the sign-in. Details: the repository's
<code>docs/MCP_CLIENTS.md</code> and <code>docs/MCP.md</code>.</p>
</body></html>
"""


async def _send_setup_page(send) -> None:
    body = _SETUP_PAGE.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
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
        if method == "GET" and scope.get("path", "/") in ("", "/"):
            await _send_setup_page(send)
            return
        if method != "POST":
            await _send_json(
                send,
                405,
                {
                    "error": "MCP endpoint accepts POST JSON-RPC only",
                    "allowed_methods": ["POST"],
                },
                headers=[(b"allow", b"POST")],
            )
            return

        headers = self._headers(scope)
        auth_header = headers.get("authorization", "")
        client_host = (scope.get("client") or ("", 0))[0]
        remote_user = headers.get("x-remote-user", "").strip()
        if not auth_header.startswith("Bearer "):
            # The swf-remote tunnel arrives from localhost with the signed-in
            # user's name; uvicorn takes the client address from Apache's
            # X-Forwarded-For (--proxy-headers), so a direct client is never
            # seen as localhost.
            if client_host in _LOCALHOST and remote_user:
                token = CALLER.set(remote_user)
                try:
                    await self.app(scope, receive, send)
                finally:
                    CALLER.reset(token)
                return
            await _send_json(send, 401, {"error": "Authorization required"})
            return

        expected = getattr(settings, "MCP_BEARER_TOKEN", "") or ""
        if not expected:
            await _send_json(send, 503, {"error": "MCP token not configured"})
            return

        if not hmac.compare_digest(auth_header[7:], expected):
            await _send_json(send, 403, {"error": "Invalid token"})
            return

        token = CALLER.set("")
        try:
            await self.app(scope, receive, send)
        finally:
            CALLER.reset(token)

    def _normalize_mcp_path(self, scope):
        """Accept common proxy forms: /, /mcp[/...], or /swf-monitor/mcp[/...]."""
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

from django.core.asgi import get_asgi_application  # noqa: E402

_django_application = get_asgi_application()

# The SSE stream path, in the forms Apache and dev servers present it.
# FORCE_SCRIPT_NAME strips the subpath inside Django's ASGI handler.
_SSE_STREAM_PATHS = (
    "/swf-monitor/api/messages/stream/",
    "/api/messages/stream/",
)


class StreamRouter:
    """Route the SSE stream to the Django ASGI app; everything else —
    including lifespan, which FastMCP owns — to the MCP app."""

    def __init__(self, django_app, mcp_app):
        self.django_app = django_app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "") in _SSE_STREAM_PATHS:
            await self.django_app(scope, receive, send)
            return
        await self.mcp_app(scope, receive, send)


application = StreamRouter(_django_application, MCPRequestGuard(_mcp_application))
