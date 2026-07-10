# Browser Push for Agent Action Completion (SSE)

When an ops-agent action finishes — a payload-log fetch, a PanDA submission, and
later the campaign-provenance sweep — the result should appear in the browser on
its own, with no manual refresh and no polling loop. This design delivers that by
reusing the existing SSE relay ([SSE_RELAY.md](SSE_RELAY.md)) with the in-app
browser as a new consumer class.

The relay today is an outbound, read-only firehose of ActiveMQ workflow messages
to remote headless agents (e.g. a monitor in Japan), over WAN/firewall-friendly
HTTPS. This adds nothing to that pipe. It adds a new *emitter* — the ops agent,
publishing a small completion event when a credentialed action succeeds — and a
new *consumer* — a browser page holding an `EventSource` that updates the DOM the
instant the event arrives. See [EPICPROD_OPS_AGENT.md](EPICPROD_OPS_AGENT.md) for
the agent and its `run_in_background` capability.

## Completion events

On confirmed success, the agent's worker publishes to `/topic/epictopic` — the
topic the monitor's listener consumes and the relay broadcasts — using
`BaseAgent.send_message` (thread-safe from a worker via the send lock):

| Action | Event | Payload |
|---|---|---|
| `_do_fetch_payload_log` (after `.done`) | `payload_log_ready` | `pandaid`, `jeditaskid` |
| `_do_submit_task` / `_do_submit_evgen_task` (after record-submission OK) | `prodtask_submitted` | `task_name`, `jedi_task_id` |
| Submit failure before a JEDI id is recorded | `prodtask_submit_failed` | `task_name`, `reason` |
| Submit succeeded but PCS record update failed | `prodtask_submit_unrecorded` | `task_name`, `jedi_task_id`, `reason` |
| Existing PanDA task operation finished | `panda_task_operation_done` | `task_name`, `jedi_task_id`, `operation`, `ok`, `summary` or `error` |

These ride the existing workflow topic rather than a dedicated channel: zero new
relay infrastructure, and the events become a useful ops audit trail as enriched
`WorkflowMessage`s. The event carries the identifying field (`pandaid` /
`task_name`) so a waiting page can recognize its own result.

corun-ai-backed LLM operations use the same browser notification mechanism with a
different server-side source. corun-ai posts completion callbacks to swf-monitor,
and swf-monitor emits the corresponding SSE event. The browser-side rule is the
same short-lived `EventSource` pattern described below. See
[EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md).

## Relay — unchanged

Listener consumes `/topic/epictopic` → enriches + persists → publishes to the
Channels group (Redis in prod) → `SSEMessageBroadcaster` → per-client queues →
`/api/messages/stream/`, filtered by `msg_type`. No change here; see
[SSE_RELAY.md](SSE_RELAY.md).

## Browser consumer

A page that has triggered an action opens an `EventSource` filtered to the event
it awaits, e.g. `…/api/messages/stream/?msg_types=payload_log_ready`. On each
event it matches its own `pandaid` / `task_name` in the payload (server-side
filters are by `msg_type`, not per-entity, so the last-mile match is done in JS),
then loads the log or drops in the "PanDA Task N" link.

Button-triggered actions must use short-lived streams. A button that queues an
operation opens the `EventSource` only after the button click and closes it on
the matching event, on timeout, and on page unload. It must not create or reuse a
page-scoped stream for the whole compose session; leaked compose-page streams
consume server workers.

**Same template serves both faces.** The `EventSource` URL is written with the
monitor's own `/swf-monitor/` prefix; swf-remote's existing body rewrite turns it
into `/prod/api/messages/stream/` for devcloud automatically. Only the external
proxy *route* is new (below) — the page is identical.

For campaign task submission, `prodtask_submitted` is the live completion path
and a bounded poll remains as a recording backstop.

### Reliability backstop — required

SSE is best-effort with no replay on reconnect, and the agent can finish before
the browser's `EventSource` has connected. An action that needs a browser update
therefore must:

1. open the `EventSource` before or immediately after queueing the action,
2. perform an immediate status check where the result is also stored in the
   database,
3. keep a bounded fallback poll where the page can independently observe the
   result,
4. close the stream on match, timeout, or page unload.

SSE is the live, sub-second path; the immediate check and slow poll exist only so
a missed event cannot strand the user. Heartbeats (~30 s) keep the connection
alive through proxies; `EventSource` reconnects on its own.

## External face — swf-remote streaming proxy (new infrastructure)

`pandaserver02` is inside the BNL perimeter and unreachable by a remote browser,
so the devcloud face goes through the swf-remote proxy on ec2dev. The browser's
`EventSource` is therefore **same-origin to epic-devcloud.org** — there is no
browser CORS. The cross-network hop is swf-remote → monitor over the SSH tunnel.

The existing `monitor_client.proxy()` cannot carry SSE: it reads the full
response body (`httpx.get`, 30 s timeout) and byte-rewrites it, which an infinite
`text/event-stream` would break. A **dedicated streaming view** is required:

- `httpx.stream('GET', f'{base}/api/messages/stream/', params=…, headers=…, timeout=None)`
  → `StreamingHttpResponse(content_type='text/event-stream')`, yielding chunks
  with **no buffering, no body rewrite, no timeout cap**.
- Route: `/prod/api/messages/stream/`.
- Devcloud's Apache must not buffer this response (build-time verification).

This view is the only new piece for the external face, and it is deployed on
ec2dev (swf-remote is solo-maintained, direct-to-main).

## Authentication — no browser CORS anywhere

- **Internal browser → monitor:** session (CILogon). The SSE endpoint already
  accepts session auth.
- **devcloud browser → swf-remote:** Django session, same-origin, gated by login.
- **swf-remote → monitor (the stream hop):** a **service `Token`** on the
  upstream request. The SSE endpoint already honors `Authorization: Token`; it
  does not currently honor `X-Remote-User`, so the service token is the path of
  least change. The devcloud user is still gated by swf-remote's login; the
  token authenticates only the trusted proxy hop.

## View copy

While an action is in flight, the triggering view renders "log / task ID will
appear shortly" instead of asking the user to refresh.

## Verification order

1. **Internal pipe:** publish a `payload_log_ready` to `/topic/epictopic`,
   confirm a token SSE subscriber receives it (shell round-trip).
2. **Internal browser:** a logged-in monitor page receives the event and updates
   live.
3. **External browser:** through the new swf-remote streaming proxy — confirm
   prompt delivery (no buffering) and survival past 30 s via heartbeats.

## Build order

1. This design doc.
2. Shared substrate (swf-testbed): agent emits the two completion events; relay
   already broadcasts. Internal page gets the `EventSource` + backstop + the view
   copy change.
3. External face (ec2dev): the swf-remote streaming proxy view + route, Apache
   no-buffer. Authored in the swf-remote clone here, deployed on ec2dev.

The substrate is shared; the devcloud delta is just the streaming proxy.

**Status:** implemented. The prod-ops agent publishes the completion events
(`agents/epicprod_ops_agent.py`: `payload_log_ready`, `prodtask_submitted`,
`prodtask_submit_failed`, `prodtask_submit_unrecorded`,
`panda_task_operation_done`); the internal browser pages hold the `EventSource`
consumers
(`src/monitor_app/viewdir/pandamon.py`, `src/pcs/templates/pcs/prod_task_compose.html`);
and the swf-remote streaming proxy relays the stream on the external face
(`../swf-remote/src/remote_app/monitor_client.py`, `StreamingHttpResponse`).
