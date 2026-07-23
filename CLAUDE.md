# swf-monitor — Claude Code Guidelines

The common monitor, web, and database services of the swf platform: Django web
app, REST API, MCP server, and the platform machinery (action stream, SysConfig,
alarms, SSE relay), hosting the production applications installed from
`swf-epicprod` and running the ePIC production-operations agent. Part of the SWF
core (coordinated branches with `swf-testbed` and `swf-common-lib`).
Shared workspace rules — doc-first, git policy, environment — live in the
workspace `../CLAUDE.md`. This file is the repo's doc index and the one pattern
every designer/implementer should hold in mind. The official system-level
documentation for the whole WFMS is <https://epic-wfms-docs.readthedocs.io>
(workspace repo `epic-wfms-docs/`); this repo's docs carry the implementation
detail beneath it.

The workspace rule "Stay Within Scope Without Stopping" is mandatory here:
do not overreach beyond the requested change, and do not respond to a scope
correction by stopping or discarding the requested work. Keep the valid part,
remove only the unauthorized part, and continue inside the clarified boundary.

## Key pattern — credentialed async action with live browser push

We are building a live, automated, responsive production system. The standard way
to add a credentialed, slow, or hang-prone capability is the prod-ops agent
pattern — not a poller, not a blocking request, and never a credential in the web
tier:

> a new `_handle_<msg_type>` + a `run_in_background` doer (the agent holds the
> Rucio / PanDA / xrootd credentials) + a completion event on `/topic/epictopic` +
> an `EventSource` on the triggering page → the action fires server-side and the
> result is pushed to the browser the instant it is done — internally, and (via
> the swf-remote streaming proxy) to remote collaborators.

Worked examples: payload-log fetch and PanDA submit. Read before designing any
production-ops feature: **`swf-epicprod/docs/EPICPROD_OPS_AGENT.md`** (the
agent and the pattern) and **`docs/SSE_PUSH.md`** (the browser push).

## swf-remote tunnel flag

swf-monitor templates receive `is_tunnel` from
`monitor_app.middleware.tunnel_context`. It is true for localhost requests from
the swf-remote SSH tunnel and false for direct `pandaserver02` browser access.
Use this flag for conditional template behavior that must differ on the external
proxy face, such as disabling page-view POST controls that are only supported on
`pandaserver02`. Do not infer tunnel/proxy mode from URL strings or implement
HTML rewrites in `swf-remote` when `is_tunnel` can express the condition in the
swf-monitor template.

## corun-ai naming

The backing AI/document service is `corun-ai`. Do not write `CORUN` as a product
or system name in prose, UI text, reports, MCP descriptions, logs, or commit
messages. Existing compatibility names such as `CORUN_BASE_URL`,
`CORUN_API_TOKEN`, and `corun_page_group_ids` are config/schema identifiers only.

## Doc index (`docs/`)

The epicprod/PCS documentation set lives in `swf-epicprod/docs/` — see the
index there (README and `ARCHITECTURE_MAP.md`); permanent stubs remain at
the old paths here. This repo's docs cover the platform services:

- `ACTION_STREAM.md` — the action stream: structured action logging (sublevel/live axes, SysConfig live policy), live view, retrieval for LLMs (renamed from `EPICPROD_ACTION_STREAM.md`; the stream machinery is platform).
- `AI_PROPOSALS.md` — AI proposals: LLM proposes, human approves, deterministic execution; record-resident v1, origin-stamped events, the `.ai-attr` UI convention.
- `SSE_PUSH.md` — browser push of agent action completion (design).
- `SSE_RELAY.md` — the ActiveMQ → remote SSE relay this builds on.
- `EXTERNAL_ACCESS.md` — the swf-remote proxy contract, including write-action trigger rules.
- `SYSTEM_STATUS.md` — cached system status record and page.
- `CACHED_PRODUCTS.md` — uniform long-build caching: serve stored, rebuild behind, Update button; use this, never a new hand-rolled cache.
- `PRODUCTION_DEPLOYMENT.md` — deploying swf-monitor.
- `API_REFERENCE.md` — REST API surface.
- `MCP.md`, `MCP_TOOL_REFERENCE.md`, `MCP_CLIENTS.md`, `PANDA_BOT.md` — MCP server overview, tool catalog, client setup, and the DISpatcher Mattermost bot (MCP client).
- `alarms.md`, `SETUP_GUIDE.md`, `TEST_SYSTEM.md`, `POSTGRES_MCP.md` — alarms, setup, tests, Postgres MCP.
- `FASTMON_FILES.md`, `TFSLICE.md` — testbed fast-monitoring docs.

## Editing discipline (AI sessions)

- Templates and HTML are edited with precision edits (the Edit tool), never
  stream editors (`sed`/`awk`) — a regex that clips one attribute quote
  renders as a silently truncated page.
- Before every commit: `bash scripts/pre-commit-checks.sh` — compiles
  changed `.py`, one Django system check, lints changed templates; under
  ten seconds, and it encodes the manage.py/venv invocation so no session
  rediscovers it.
- After any deploy that touched templates: smoke-check the affected page's
  content (fetch and confirm a marker below the changed region), not just
  the deploy's HTTP 200 health check.
- Any new or edited doc gets a separate re-read at doc voice before the
  commit — dialog phrasings leak into docs under momentum.

## Button roles

One role → one treatment, everywhere (all `btn-sm`, solid variants):

| Role | Class | Examples |
|---|---|---|
| Constructive: create / save / new / import / match | `btn-primary` | Save, New Task, Import |
| Object verbs on existing items: edit / copy | `btn-dark-green` | Edit, Copy |
| Consequential commits: submit / lock / publish / approve-and-execute | `btn-success` | Submit to PanDA, Lock, Publish, Approve |
| Suggestion adoption (yellow suggestion bars only) | `btn-warning btn-apply` | Apply |
| Destructive: delete | `btn-danger` | Delete |
| Neutral dismiss / utility: cancel / clear / open-close all | `btn-secondary` | Cancel, Clear |
| Stateful toggles ONLY (fill = on/off) | `btn-outline-*` | Catalog/Progress switch |

Outline is never the default reflex — anything that acts is solid, colored
by role.

## House UI helpers (base template)

Shared page utilities live at the bottom of `src/templates/base.html` and
are available on every page. Use these — never reimplement them in a
page template:

- **Relative time**: give any element
  `data-relative-time="<ISO timestamp>"` and its text renders and ticks
  every second as `just now` / `Ns ago` / `Nm ago` / `Nh ago` / `Nd ago`
  (`window.swfFormatElapsed` is the formatter).
- **Editor fit**: `swfFitEditor(el)` (or class `swf-fit-editor` on the
  textarea) grows an in-page document editor to the visible window.
- **AI attribution**: `.ai-attr` / `.ai-attr-text` (purple-on-lavender
  chip) and `.ai-fill` (lavender container) mark AI-origin content; the
  classes are defined in the base template's style block.
- **Page tabs**: `.swf-page-tabs` renders first-class page views as an
  underlined tab row. Every tab is an ordinary server-routed link whose URL
  contains the selected view state; do not use hidden Bootstrap panels,
  client-only state, or a remembered preference for these tabs.
- **Sortable tables**: DataTables is loaded globally in `base.html`
  and is the house table widget. Big list pages use the
  `_datatable_base` / `_datatable_dynamic_base` templates (ajax);
  static server-rendered tables get class `swf-sortable`, which the
  base template initializes as a chrome-free DataTable (sort headers
  only, no paging/search). Table columns are sortable by default
  everywhere; a non-sortable table is the special case and needs
  Torre's say-so. Static tables use the house table classes
  (`table table-striped table-bordered table-sm align-middle w-auto`).
- **Status cells**: state values render with the BigMon fill classes
  via `{% load swf_fmt %}` and
  `<td class="{{ value|state_class }}">{{ value }}</td>` (or
  `task_badge`/`job_badge` for badge form). Never a bare status cell.
- **Names and fields**: do not wrap human-facing names, identifiers, field
  paths, policies, publishers, or resolvers in `<code>`. The active theme
  renders code text red, which signals an error. Use a descriptive title and,
  when needed, a plain muted label such as “Internal name: health”.

## Deploy

`sudo bash deploy-swf-monitor.sh branch <current infra/baseline-vNN>` — pulls from
git, so commit + push first. Run it in the foreground.

The standard script creates an isolated release copy; “cloning” in its output
does not mean it is changing the shared development checkout. Before running
it, inspect every local package tree it freezes into the release, including
swf-epicprod, snapper-ai, site-canary, and swf-common-lib, so another session's
uncommitted work is not shipped.

For UI/template/view-only or MCP-tool-only changes, prefer the fast in-place
sync: `sudo ./deploy-lightweight-ui-mcp.sh --ui` and/or `--mcp` (add
`--static` when assets changed). Use the full deploy whenever migrations,
requirements, Apache config, systemd units, ops-agent code, or bot code are
involved. Commit first either way.
