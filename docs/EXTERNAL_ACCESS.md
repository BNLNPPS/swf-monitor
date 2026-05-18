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
