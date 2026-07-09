# swf-monitor — Claude Code Guidelines

Django web app, REST API, MCP server, PCS (Physics Configuration System), and the
ePIC production-operations agent, for the ePIC streaming-workflow testbed. Part of
the SWF core (coordinated branches with `swf-testbed` and `swf-common-lib`).
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
production-ops feature: **`docs/EPICPROD_OPS_AGENT.md`** (the agent and the
pattern) and **`docs/SSE_PUSH.md`** (the browser push).

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

- `EPICPROD_OPS_AGENT.md` — the credentialed ops agent; capability model + the pattern above.
- `EPICPROD_ACTION_STREAM.md` — the epicprod action stream: structured action logging (sublevel/live axes, SysConfig live policy), live view, nightly catalog_sync, retrieval for LLMs.
- `EPICPROD_OPS.md` — ops runbook (submit, monitor, logs, systemd unit, cleaner-killer, nightly catalog sync).
- `EPICPROD_LLM_OPERATIONS.md` — corun-ai-backed LLM operations, artifacts, comments, and async completion.
- `SSE_PUSH.md` — browser push of agent action completion (design).
- `SSE_RELAY.md` — the ActiveMQ → remote SSE relay this builds on.
- `PCS.md`, `PCS_DATASET_REQUEST_WORKFLOW.md`, `EPICPROD_TASK_CATALOG.md` — Physics Configuration System and the production task catalog.
- `CAMPAIGN_CONTINUUM.md` — one curated catalog for every campaign; lifecycle as phase attribute; requests over families; instancing design.
- `EPICPROD_NARRATIVES.md` — campaign narrative documents: classes, naming, draft→locked lifecycle, corun-ai home.
- `AI_PROPOSALS.md` — AI proposals: LLM proposes, human approves, deterministic execution; record-resident v1, origin-stamped events, the `.ai-attr` UI convention.
- `EPICPROD_QUESTIONNAIRE.md` — ingest the PWG/DSC production-request Google Form into a PCS Questionnaire entity; public browser, request linkage.
- `EPICPROD_DATA_LINEAGE.md` — gather produced-dataset Rucio refs onto the catalog; reference + xrootd access.
- `EPICPROD_EVGEN_INPUTS.md` — assimilate JLab Rucio EVGEN inputs onto the catalog; the request↔Rucio input matcher and its (un)match gaps.
- `JEDI_INTEGRATION.md` — PCS → JEDI/PanDA submission design.
- `PRODUCTION_DEPLOYMENT.md` — deploying swf-monitor.
- `COMMISSIONING_RELAXATIONS.md` — alpha tag-lock/submission loosenings and exactly how to re-tighten each.
- `API_REFERENCE.md` — REST API surface.
- `MCP.md`, `MCP_TOOL_REFERENCE.md`, `MCP_CLIENTS.md`, `PANDA_BOT.md` — MCP server overview, tool catalog, client setup, and the DISpatcher Mattermost bot (MCP client).

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
by role. For editing large documents in-page, use the base-template
utility `swfFitEditor(el)` (or class `swf-fit-editor` on the textarea) so
the editor grows to the visible window.

## Deploy

`sudo bash deploy-swf-monitor.sh branch <current infra/baseline-vNN>` — pulls from
git, so commit + push first. Run it in the foreground.
