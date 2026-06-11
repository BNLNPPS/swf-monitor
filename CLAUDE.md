# swf-monitor — Claude Code Guidelines

Django web app, REST API, MCP server, PCS (Physics Configuration System), and the
ePIC production-operations agent, for the ePIC streaming-workflow testbed. Part of
the SWF core (coordinated branches with `swf-testbed` and `swf-common-lib`).
Shared workspace rules — doc-first, git policy, environment — live in the
workspace `../CLAUDE.md`. This file is the repo's doc index and the one pattern
every designer/implementer should hold in mind.

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

## Doc index (`docs/`)

- `EPICPROD_OPS_AGENT.md` — the credentialed ops agent; capability model + the pattern above.
- `EPICPROD_OPS.md` — ops runbook (submit, monitor, logs, systemd unit, cleaner-killer).
- `SSE_PUSH.md` — browser push of agent action completion (design).
- `SSE_RELAY.md` — the ActiveMQ → remote SSE relay this builds on.
- `PCS.md`, `PCS_DATASET_REQUEST_WORKFLOW.md`, `EPICPROD_TASK_CATALOG.md` — Physics Configuration System and the production task catalog.
- `EPICPROD_QUESTIONNAIRE.md` — ingest the PWG/DSC production-request Google Form into a PCS Questionnaire entity; public browser, request linkage.
- `EPICPROD_DATA_LINEAGE.md` — gather produced-dataset Rucio refs onto the catalog; reference + xrootd access.
- `JEDI_INTEGRATION.md` — PCS → JEDI/PanDA submission design.
- `PRODUCTION_DEPLOYMENT.md` — deploying swf-monitor.
- `COMMISSIONING_RELAXATIONS.md` — alpha tag-lock/submission loosenings and exactly how to re-tighten each.
- `API_REFERENCE.md`, `MCP.md` — REST and MCP surfaces.

## Deploy

`sudo bash deploy-swf-monitor.sh branch <current infra/baseline-vNN>` — pulls from
git, so commit + push first. Run it in the foreground.
