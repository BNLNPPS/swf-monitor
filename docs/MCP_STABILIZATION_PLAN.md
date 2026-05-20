# MCP Stabilization Plan

Date: 2026-04-27

## Context

The swf-monitor MCP endpoint has repeatedly locked up on swf-testbed. The
current production service runs uvicorn with 20 worker processes for
`/swf-monitor/mcp/`, fronted by Apache. Increasing the worker count did not fix
the failure mode: on 2026-04-27 the service became nonresponsive, Apache
returned MCP backend 502s, the PanDA bot timed out waiting for MCP
`initialize`, and systemd had to SIGKILL all uvicorn workers after graceful
shutdown failed.

This is not primarily a concurrency shortage. It is an MCP transport and
lifecycle problem.

## Appraisal

The installed `django-mcp-server` adapter creates, runs, and shuts down a fresh
`StreamableHTTPSessionManager` per Django request via `async_to_sync`. The MCP
SDK documents `StreamableHTTPSessionManager` as an application-lifetime object
that should be created once and run once. The current adapter may work for short
JSON POST requests, but it is a poor fit for long-lived streamable HTTP/SSE
sessions.

The deployment also claims streaming HTTP support, but GET/SSE behavior is not
healthy in practice. Direct GET requests to `/swf-monitor/mcp/` return 400 or
406 depending on headers, and logs show clients attempting GET and receiving
406. The system is paying the operational complexity cost of streamable HTTP
without delivering robust streaming semantics.

On swf-testbed, swf-monitor MCP is used locally. There is no current operational
requirement for remote MCP clients to maintain long-lived streaming sessions.
The useful surface is request/response tool invocation: `initialize`,
`tools/list`, and `tools/call`.

## Streaming Clarification

The proposed stabilization does not replace MCP with arbitrary REST and does
not break the MCP JSON-RPC tool surface. The endpoint should remain MCP over
HTTP for clients that use POST request/response calls.

The change is to stop depending on long-lived GET/SSE streaming and server-side
MCP session state until there is a proven use case and a correct ASGI
implementation.

Client implications:

- PanDA bot, testbed bot, and local scripts that use POST-only JSON-RPC should
  continue to work.
- Because swf-monitor MCP is only used locally here, there is no known external
  user depending on GET/SSE streaming.
- A generic MCP client that insists on stateful streamable HTTP sessions or
  working GET/SSE streams could fail, but that behavior is already unreliable
  today.
- If streaming becomes valuable later, reimplement it deliberately with an
  ASGI app that owns a lifespan-managed `StreamableHTTPSessionManager`, rather
  than the current Django APIView bridge.

## Repair Plan

1. Make MCP stateless request/response first.

   Set `DJANGO_MCP_GLOBAL_SERVER_CONFIG["stateless"] = True`, stop issuing
   Django-backed MCP session IDs, and reduce uvicorn from 20 workers to a small
   count such as 2 or 4. The current tools are database/API primitives and do
   not require server-side MCP session state.

   Authentication implication: low risk. Current MCP authentication is request
   middleware, not MCP session state. Bearer token validation still happens per
   request. Local unauthenticated POSTs continue to pass unless that policy is
   changed explicitly.

2. Stop advertising streaming as an operational dependency.

   Document the local MCP deployment as POST request/response MCP. Do not
   promise GET/SSE support until it is deliberately reimplemented and tested.

3. Prefer local REST-style HTTP for bots over Python service coupling.

   Short term, point local bot MCP traffic at
   `http://127.0.0.1:8001/swf-monitor/mcp/` to bypass Apache/HTTPS for
   same-host calls.

   Better follow-up: add local REST endpoints for bot memory and common
   SWF/PanDA queries so bots can remain mobile without importing Django service
   functions directly. Keep MCP as an AI integration surface, not mandatory
   loopback plumbing for every local bot operation.

4. Add guardrails and observability.

   Add uvicorn concurrency limits, shorter graceful shutdown, a cheap
   non-MCP health endpoint, request start/end timing logs, and a watchdog that
   restarts `swf-monitor-mcp-asgi.service` if `initialize` or `tools/list`
   exceeds a small threshold.

5. Fix async logging.

   `DbLogHandler` currently writes to `AppLog` through synchronous Django ORM
   calls even when invoked from async contexts, producing repeated
   `You cannot call this from an async context` errors. Replace it with an
   async-safe queue/background-writer design, or disable DB logging in async
   uvicorn/bot processes until the queue writer exists. Logging failures must
   be visible; silent failure is not acceptable.

6. Fix bot exception paths.

   Initialize PanDA bot metadata variables before any MCP call can fail. On MCP
   failure, return a clear Mattermost-visible error and log the exception. Do
   the same review for testbed bot paths. A failed MCP request must not produce
   hidden task exceptions such as `UnboundLocalError`.

## Verification

Before deployment:

- Direct local MCP POST tests: `initialize`, `tools/list`, representative
  `swf_*`, `panda_*`, and `pcs_*` calls.
- Apache-proxied MCP POST tests.
- Bot smoke tests against the local endpoint.
- Confirm GET/SSE behavior is intentionally unsupported/documented, or fully
  implemented later.
- Confirm `systemctl restart swf-monitor-mcp-asgi.service` exits cleanly
  without SIGKILL.
- Confirm async logging no longer emits Django async-context errors.

## Execution Order

Phase 1 should be a stabilization patch: stateless MCP, reduced worker count,
local bot endpoint, async logging fix, and bot exception-path fixes.

Phase 2 should add broader observability: health endpoint, watchdog, timing
logs, and documentation cleanup.
