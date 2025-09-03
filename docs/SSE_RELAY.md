# Server-Sent Events (SSE) Relay via Channels + Redis

Date: 2025-08-26

## Overview

SWF Monitor forwards workflow messages it consumes from ActiveMQ to remote HTTP clients using Server-Sent Events (SSE). This provides a WAN/firewall-friendly, one-way push over HTTPS.

For user documentation on SSE streaming including client setup and usage examples, see [SSE Real-Time Streaming](../../swf-testbed/docs/sse-streaming.md) in the testbed repository.

Production runs multiple WSGI processes; the ActiveMQ listener runs in a separate process. To reliably fan out events to all SSE clients across processes, we use Django Channels with a Redis channel layer as an inter-process relay. WebSockets are not required or enabled for this feature.

Important: Redis/Channels is REQUIRED in any environment that must support remote ActiveMQ client recipients via SSE. The in-memory fallback is for single-process development only and is not suitable for production.

## Architecture

1. ActiveMQ listener (management command/process) consumes messages and persists them (WorkflowMessage, SystemAgent).
2. Listener publishes enriched message payloads to a Channels group (default: `workflow_events`).
3. Each web/WSGI process starts a small background subscriber that joins the group and forwards messages into the in-memory `SSEMessageBroadcaster`.
4. SSE clients connect to `/api/messages/stream/` and receive messages from per-client queues with heartbeats and optional filters.

Key files:
- `monitor_app/activemq_processor.py` — persists and publishes to Channels group (and in-process fallback)
- `monitor_app/sse_views.py` — SSE endpoints, broadcaster, and background subscriber loop
- `swf_monitor_project/settings.py` — `CHANNEL_LAYERS` (Redis if `REDIS_URL`), `SSE_CHANNEL_GROUP`

## Configuration

Environment variables (loaded via `.env`):
- `REDIS_URL` — e.g., `redis://localhost:6379/0`. Enables Redis-backed channel layer. If unset, falls back to in-memory (single-process only).
- `SSE_CHANNEL_GROUP` — Channels group name (default: `workflow_events`).

Django settings detect `REDIS_URL` and configure `CHANNEL_LAYERS` accordingly.

## Authentication and CORS

The SSE endpoint (`/api/messages/stream/`) implements manual token authentication (DRF removed to avoid content negotiation issues). Browser EventSource cannot send Authorization headers; use session authentication for same-origin access. For cross-origin browser use, configure CORS for credentialed requests (no wildcard origins) and ensure cookies are allowed.

Non-browser clients (headless) may pass tokens using standard HTTP clients; avoid placing tokens in query strings unless explicitly approved.

## Apache/WSGI Streaming

Ensure production is configured so streaming responses are not buffered or terminated prematurely:
- Use mod_wsgi daemon mode and appropriate timeouts to allow long-lived connections.
- Disable proxy buffering if present; `X-Accel-Buffering: no` is Nginx-specific and not used with Apache.

## Bash snippets

Activate environment and install requirements:

```bash
cd /eic/u/wenauseic/github/swf-testbed
source .venv/bin/activate
source ~/.env
pip install -r /eic/u/wenauseic/github/swf-monitor/requirements.txt
```

Set Redis configuration for the monitor (example):

```bash
export REDIS_URL=redis://localhost:6379/0
export SSE_CHANNEL_GROUP=workflow_events
```

Restart services (examples; adapt to your deployment):

```bash
# Restart the ActiveMQ listener and reload Apache
# supervisorctl restart swf-monitor-listener
# sudo systemctl reload httpd
```

## Filters and payload enrichment

Forwarded SSE payloads are enriched to support filters and diagnostics:
- `sender_agent`, `recipient_agent`
- `queue_name`
- `sent_at`

SSE clients can filter via query params: `msg_types`, `agents`, `run_ids`.

## Reliability & backpressure

SSE is best-effort real-time; there is no replay on reconnect. Each client has a bounded queue (drop-oldest on overflow). Heartbeats are emitted ~30s by default.

## Testing

The SSE functionality is comprehensively tested in `monitor_app/tests/test_sse_stream.py`:
- **Unit tests**: Core broadcasting logic, message filtering, client management
- **Integration tests**: Channel layer communication (when Redis is available)
- **HTTP endpoint tests**: Authentication, response format, status reporting

Run SSE-specific tests:
```bash
./run_tests.py src/monitor_app/tests/test_sse_stream.py
```

The tests use Django's test infrastructure rather than external HTTP connections, providing fast, reliable validation of SSE functionality without network dependencies.

## Operational notes

- If `REDIS_URL` is unset, cross-process fanout will not work; use only for single-process dev. For production and any deployment serving remote recipients, `REDIS_URL` must be configured and Redis must be running.
- Database write amplification from per-event stats is minimized in the hot path; consider batching if high-volume SSE usage is expected.
