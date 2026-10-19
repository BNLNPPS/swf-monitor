# Wrangler Agent

The wrangler agent is the platform's always-on executor for bounded work that
is neither a privileged production action nor an interactive dialog: LLM
judgments over production state, and the scripts that prepare and record them.
It is built on `BaseAgent` from swf-common-lib, so it has the identity,
liveness, and monitor visibility of every other agent, and on
[wrangle-ai](https://github.com/BNLNPPS/wrangle-ai), whose `Wrangler` is its
work loop: a durable work queue, a wake signal in place of polling, bounded
concurrency, dedup of in-flight work, and recovery of work a dead process left
behind. Its first handler is the alarm responder specified in
[ALARM_QUEUE.md](ALARM_QUEUE.md).

## Role beside the production ops agent

The production ops agent
([EPICPROD_OPS_AGENT.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_OPS_AGENT.md))
is the credentialed executor: it holds the PanDA token and the Rucio proxy, and
every privileged action passes through it. wrangle-ai is that agent's pattern
rebuilt as a generic core, and the wrangler agent is its first use in the
platform. The two agents divide by the nature of the work, not by trigger:

| | Production ops agent | Wrangler agent |
|---|---|---|
| Work | Deterministic doers under production credentials | LLM doers and their deterministic preparation and recording |
| Queue | Bus messages, consumed once; the action stream is the record | Durable worker rows in the bullpen, claimed, retried, and reclaimed; the action stream is the record |
| Wake | Bus delivery | Postgres `NOTIFY` from any producer, in any process or OS account |
| Privileged actions | Executes and verifies them | Requests them from the ops agent through the task-operation queue |

The ops agent is not extended with LLM work. An LLM doer is the class of
work most able to exhaust a host in time and memory, and it belongs in a
process whose loss costs a diagnosis rather than a submission. No migration of
ops-agent handlers onto the wrangler is planned; the pattern for one, should a
handler come to need durable rows or scheduled execution, is the migration
section of wrangle-ai's `docs/scheduler.md`.

## Construction

- **BaseAgent.** The agent runs under a fixed namespace with the standard
  control messages (`health_ping`, `shutdown`) on its own queue, so it appears
  in the agent list, answers the cleaner-killer's liveness ping, and stops
  deliberately with the same exit convention as the ops agent. Work does not
  arrive on the bus.
- **Bullpen.** `PgBullpen` from `wrangle_ai.postgres` over a `wrangle_workers`
  table in the monitor database. The table is created by a Django migration
  whose DDL is a frozen copy of the package schema, and read by an unmanaged
  model for the UI. A subclass hooks `mark_failed` to record an action-stream
  incident (`wrangler_worker_failed`, level ERROR, with the worker type, id,
  and error) so every failure surfaces uniformly, and `mark_done` to record
  `wrangler_worker_done` with the measured duration. The agent does not boot
  Django; the bullpen is psycopg, and the records go through the REST twin of
  `log_epicprod_action`, as the ops agent's do.
- **Bell.** `PgBell` on the monitor database. A producer inserts a worker row
  and rings with `pg_notify`; the web tier does this through its ordinary
  Django connection. A monitor-side helper, `enqueue_worker(type, payload)`,
  is the single producer entry point.
- **Claim restriction.** The agent claims only the worker types it has
  registered, so a second agent with a different handler set can share the
  table.
- **Pulse.** Once per loop pass the agent writes `wrangler_heartbeat` to
  SysConfig with the in-flight count. The loop never executes work, so a
  stale pulse means a stopped process, and the System page reads it as such.

## Handlers and doers

A handler is registered with a worker type, a timeout, and a dedup key
function. It runs a doer, a subprocess with a hard timeout, and records the
outcome; the handler is deterministic and the doer is the only place an LLM
runs. The LLM doer is `claude -p` under subscription authentication with the
API key removed from its environment, per the platform rule that unattended
LLM operations never use metered access, and with the MCP configuration of the
production checkout, which carries the swf-monitor MCP server. The doer
returns a structured result; the handler parses it and writes the record. The
model writes nothing to production state.

### `alarm_respond`

The first handler. The notice router's `alarm-responder` push plugin enqueues
one worker per matching `alarm_raised` or `alarm_renotified` incident whose
event carries no response transition, with the event id, alarm name, and
transition id as payload and `alarm:<event id>` as the dedup key, so a
renotify while an analysis is in flight is a recorded skip. The doer reads the
event, its transitions, and the evidence the alarm class names (the action
stream, PanDA, Rucio) through MCP, and returns a diagnosis, a proposed action
from the alarm class's allowed set, and its reasoning. The handler posts a
`response_proposed` transition. In `act` mode, for reversible actions only, the
handler additionally queues the action through the existing task-operation
path under source `alarm-responder`, subject to the per-class hourly cap in
SysConfig, and enqueues a follow-up worker that waits for the operation's
verified outcome and posts `response_taken` and `verified`. Modes, caps, and
the transition vocabulary are specified in ALARM_QUEUE.md.

## Trust boundary

The agent runs as the operating account, the same account as the ops agent, so
the boundary between them is not an OS boundary: an LLM doer under that
account can read what the account can read. The effective boundary is the
tool surface the doer is given and the rule that a production action is a
request to the ops agent, recorded as a task operation with a verified
outcome, never a direct call. A dedicated service account for the wrangler,
with its own subscription login, would add an OS boundary and is a deployment
choice, not a design requirement.

## Data

- `wrangle_workers` (frozen migration, unmanaged model): worker id, type,
  payload, status, result, error, attempts, claiming agent and process, doer
  process, and timestamps, as defined by `wrangle_ai.postgres`.
- SysConfig: `wrangler_heartbeat`; the responder caps per alarm class.
- Action stream: `wrangler_worker_done`, `wrangler_worker_failed`, declared in
  `ACTION_DEFAULTS`.

## Operation

The agent is a systemd unit, `swf-wrangler-agent.service`, modeled on the ops
agent's: the operating account, `Restart=always` with a burst cap, and the
deliberate-stop exit code that systemd does not restart. The deploy script
restarts it with the ops agent and the canary agent, so a deploy picks up new
handler code; in-flight doers survive a restart when they follow the detached
self-completing convention, and the bullpen's liveness-checked reclaim leaves
them alone. wrangle-ai enters the swf-monitor requirements as a floor. Worker
rows are listed on a monitor page with status, type, timing, and error, the
work history the action stream does not carry.

## Delivery sequence

1. The package, the migration, and the agent with its control messages, pulse,
   unit, and deploy restart; a no-op handler proves the loop end to end.
2. `alarm_respond` in `advise` mode, with the router plugin as producer
   (ALARM_QUEUE.md step 4).
3. `act` mode and the verification follow-up (ALARM_QUEUE.md step 5).
4. Scheduled work through wrangle-ai's Foreman and Roster when a first
   scheduled candidate exists.

## Status

Design. Nothing in this document is implemented.
