# External Access — swf-remote Proxy

swf-monitor lives inside the BNL authentication perimeter on
`pandaserver02.sdcc.bnl.gov` and is not reachable from the open
internet. External users (collaborators outside BNL, LLM tools) access
swf-monitor through **swf-remote**, a separate Django app deployed on
`ec2dev` and served at `epic-devcloud.org` / `etaverse.com`.

swf-remote is a thin proxy: it forwards rendered HTML pages and REST
responses from swf-monitor over an SSH tunnel, preserving the user
identity via `X-Remote-User`. Static assets are proxied too, so CSS
and JS stay in sync without redeployment on the swf-remote side.

**Repo**: `/data/wenauseic/github/swf-remote/` (cloned read-only on
swf-testbed; the deployed copy lives on ec2dev). Git policy:
solo-maintained, commits direct to `main`, no PRs.

## URL forwarding model

swf-remote enumerates every proxied path explicitly in
`src/remote_app/urls.py`. Each entry maps a path to a generic proxy
view (`views.pcs_proxy` for `/pcs/...`, `views.panda_proxy` for
`/panda/...`, etc.) which reads `request.path_info` and forwards to
swf-monitor's matching path.

Generic forwarding *mechanism*, explicit forwarding *enumeration*. A
URL that exists on swf-monitor but lacks an entry in swf-remote
`urls.py` returns 404 to external users, regardless of whether the
page itself works on swf-monitor.

## Contract: adding a new swf-monitor URL intended for external access

When you add a new URL to swf-monitor (typically in `src/pcs/urls.py`
or `src/monitor_app/urls.py`) and you want external users to reach
it through `epic-devcloud.org`, you must **also** add a sibling entry
in `swf-remote/src/remote_app/urls.py` pointing at the appropriate
proxy view. Without this, the page is BNL-internal only.

Pattern:

```python
# swf-remote/src/remote_app/urls.py
path('pcs/<your-path>/', views.pcs_proxy, name='<your-name>'),
```

The `name=` should mirror the swf-monitor URL name where practical, so
existing `{% url %}` template references resolve correctly when
templates are rendered server-side on swf-monitor and proxied through.

REST API endpoints under `/pcs/api/` are already covered by a
catch-all (`pcs/api/<path:path>`) — those do not need per-endpoint
entries.

## Write actions and triggers (POST/PATCH/DELETE) — read this before adding a button

The proxy carries write requests, but under a strict contract set by
`remote_app/monitor_client.py:proxy`. A browser-triggered action that ignores
the contract **works on the internal face (`pandaserver02...`) and silently
fails on the external face (`epic-devcloud.org`)** — the face nearly every
collaborator uses. Build and verify write actions on the external face; the
internal face hides every constraint below.

What the proxy actually does (verified in `monitor_client.py`):

- Forwards method + query + **body** + the user identity as **`X-Remote-User`**.
- Sends only `Host`, `X-Remote-User`, `Content-Type` upstream — **every other
  request header is dropped** (no `X-Requested-With`, no `X-CSRFToken`, no
  cookies). swf-monitor therefore sees no Django session and no CSRF token.
- **Cannot relay a redirect**: any upstream 3xx is turned into a `502` page. A
  page-view POST that ends in `redirect()` (the POST-redirect-GET pattern)
  returns 502 through the proxy.

So the recipe for an externally-working browser-triggered (or agent-backed)
action:

1. **Trigger through `/pcs/api/`**, not a page-view URL. `/pcs/api/<path>` is
   already proxied by the csrf-exempt `pcs_api_proxy` — no swf-remote entry
   needed. A page-view POST is not viable here: it relies on session+CSRF (which
   the proxy does not carry) and usually redirects (which the proxy cannot
   relay).
2. **Authenticate by `X-Remote-User`**, set by the proxy from the logged-in
   user — not Django session/CSRF.
3. **Return JSON, never a redirect** (e.g. `202 {"status": "queued"}`). A 3xx
   becomes a 502 at the proxy boundary.
4. **Do not branch on dropped headers.** `X-Requested-With`, `X-CSRFToken`, and
   cookies do not survive the hop; the endpoint must behave correctly given only
   `X-Remote-User` + body.
5. **Completion returns over SSE.** The relay (`/api/messages/stream/`) is
   already proxied (`sse_proxy`); the page holds an `EventSource` and updates
   when the agent emits an event or swf-monitor relays a corun-ai completion
   callback. See [SSE_PUSH.md](SSE_PUSH.md) and
   [EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md).
6. **Verify on `epic-devcloud.org`, not internally.** The internal face
   satisfies session+CSRF and relays redirects, so it passes while the external
   face fails. Test where the users are.

The supported write path is the `/pcs/api/` surface (per *Authentication*
below); page-view POSTs through the proxy are not.

## Conditional template behavior on the proxy

swf-monitor exposes the template context flag `is_tunnel` for requests arriving
through the swf-remote SSH tunnel. It is set by
`monitor_app.middleware.tunnel_context`, which checks whether the upstream
request reaches swf-monitor from localhost. Direct browser access on
`pandaserver02` gets `is_tunnel = False`; proxied external access through
swf-remote gets `is_tunnel = True`.

Use `is_tunnel` in swf-monitor templates when a control or message must differ
between the internal and external faces. Existing examples include hiding or
disabling page-view write controls that are only supported on `pandaserver02`.
Do not implement this class of behavior by matching URL strings or rewriting
proxied HTML in swf-remote.

## Static assets

CSS, JS, and images at `/swf-monitor/static/...` proxy through
`views.static_proxy`. New static files committed to swf-monitor reach
external users automatically on the next swf-monitor deploy; no
swf-remote change is required.

## Authentication

External users authenticate against swf-remote (OIDC via CILogon, same
identity provider as swf-monitor's Apache layer). swf-remote forwards
the resolved username to swf-monitor as `X-Remote-User` for both
HTML-page and API requests.

## Why explicit enumeration

The historical reason is auditability: external exposure is opt-in
per-path, not blanket. Internal-only pages (admin, debug, raw
management endpoints) stay internal even after they're added to
swf-monitor.
