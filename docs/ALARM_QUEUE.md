# Alarm Queue

The alarm system is the operations queue for ePIC production: the set of
raised conditions that need a response, each with a lifecycle from raise to
clear, a per-subscriber feed with read state for the people and agents who
follow it, and an automated responder that acts on selected alarm classes.
This document specifies those three extensions of the alarm system described
in [alarms.md](alarms.md). They build on the action stream
([ACTION_STREAM.md](ACTION_STREAM.md)) and notice routing
([NOTICE_ROUTING.md](NOTICE_ROUTING.md)).

## Roles of the three systems

- **The alarm system** holds the queue. A raised condition is an alarm event;
  the event carries the condition's lifecycle and its response record. The
  dashboard at `/alarms/` is the shared view of every raised condition.
- **The action stream** is evidence and record. Detect modules read it (with
  PanDA and Rucio) to decide whether a condition is raised, and every
  lifecycle transition and every response action is logged on it as an
  incident, so Mattermost channels, external feeds, and reports receive alarm
  incidents by subscription.
- **Notice routing** delivers. The responder is a push plugin; announcements
  reach channels through subscriptions.

Admission to the queue is a detect module with a threshold, in code, in git.
A condition enters the queue only by being detected. The responder never
creates queue items.

## Lifecycle

An alarm event is one condition on one subject (`dedupe_key`). Its lifecycle
is a sequence of transitions, recorded in an `AlarmTransition` table (event,
kind, time, actor, detail). The engine writes the detection transitions; the
web tier, REST, and the responder write the rest.

| Transition | Written by | Meaning |
|---|---|---|
| `raised` | engine | condition detected; the event is created |
| `renotified` | engine | condition persists past the renotify interval |
| `acknowledged` | person or agent | someone has taken the item |
| `assigned` | person | the item is handed to a named subscriber |
| `response_proposed` | responder | a diagnosis and proposed action are attached |
| `response_taken` | ops agent, via the responder or a person | the action was executed |
| `verified` | responder | the action's effect was checked |
| `escalated` | engine | raised and unacknowledged past the escalation interval |
| `cleared` | engine | condition no longer detected |

The event row keeps its current state as a summary of the latest transitions
(`acknowledged_by`, `assigned_to`, `escalated_at`, `clear_time`), and the
transitions are its history. Acknowledgement and assignment are actions on the
dashboard and through REST; the response transitions carry a response record:
the diagnosis, the proposed action, who approved it, what was done, and what
the verification found.

Every transition is also logged on the action stream as an incident
(`alarm_raised`, `alarm_acknowledged`, `alarm_response_taken`, `alarm_cleared`,
and so on) with the alarm name, the subject, the severity, and the dashboard
URL as attributes. The engine logs through the REST twin of
`log_epicprod_action` that the ops agent uses, since the engine does not boot
Django. Email notification is unchanged.

## Feeds

A feed is a subscriber's view of the queue with read state. A subscriber is a
person or an agent, identified by account. Everyone sees every raised
condition by default; a subscription filter is available and is used for
agents, where it is an authorization boundary (a responder subscribes to the
alarm classes it may act on and sees nothing else).

Read state is one row per (subscriber, event) holding the id of the last
transition that subscriber has read. The event is unread for a subscriber
when its latest transition is newer than that pointer, so every transition
(a renotify, a response, a clear) makes the item unread again and moves it
to the top of the feed, ordered by latest transition. The event's history is
the transition list, expandable in place. The unread count is a count over
the join.

- **People** use a feed page in swf-monitor: reverse-time by latest
  transition, unread rendering, mark-read and mark-unread per item, mark-all,
  unread-only and severity filters carried in the URL, and the same
  keyboard navigation as the Logs page. Marking an item read updates only
  that subscriber's pointer.
- **Agents** use two MCP tools on their own service account: list unread
  (never marks anything) and mark read. For an agent, unread is its work
  queue: it lists, handles, and marks read when handled; an item it did not
  mark stays unread and is handled again on the next pass.

Read state is private to the subscriber. The dashboard shows the shared state
of the queue and no read state.

## Response

The responder is an LLM operation dispatched on `raised` for the alarm
classes it subscribes to. It runs where the platform already runs bounded LLM
work: a notice-routing push plugin, `alarm-responder`, hands the incident to a
corun-ai wrangle work item (EPICPROD_LLM_OPERATIONS.md, the comment-reply
pattern), and the worker runs with the swf-monitor MCP tools. It never holds a
production credential; a privileged action is a request to the production
ops agent (`panda_task_operations`), which executes it, verifies the result,
and records the verified outcome as it does for any operator request.

The responder has three modes, set per alarm class in SysConfig and visible
on the dashboard:

| Mode | Behaviour |
|---|---|
| `listen` | the responder is not dispatched |
| `advise` | the responder diagnoses and attaches `response_proposed` (diagnosis, proposed action, reasoning); a person approves on the dashboard or through the bot, and the approval executes the action |
| `act` | for reversible actions only: the responder requests the action, waits for the ops agent's verified outcome, attaches `response_taken` and `verified`, and the transitions notify subscribers after the fact |

Reversible means the action has an inverse and a false positive costs delay
rather than data: pausing tasks (resume is the inverse) is the first such
action. Kill, retry, and anything touching data are never taken in `act`
mode.

Guardrails apply in every mode: the tool surface exposed to the responder is
the authorization boundary (pause and resume, nothing else, in the first
version); a per-hour cap on responder-initiated actions per alarm class; every
proposal, approval, action, and verification is a transition and an
action-stream incident; and the dispatch is skipped when the event already
carries a response transition, so a renotify does not restart the analysis.

## Escalation

An event raised and unacknowledged past the alarm's escalation interval gets
an `escalated` transition, which re-flags every subscriber and is an incident
subscribable by a Mattermost channel or an email digest. The interval is a
per-alarm parameter alongside the existing renotify interval.

## Detect modules

The queue is only as useful as its detect modules. The first three:

- **Black hole** — a site or queue on which jobs fail at a high rate with
  short wall time over a window; the subject is the site, and the response is
  to pause the tasks routed to it.
- **Catalog-sync freshness** — the nightly `catalog_sync` summary record
  missing or old on the action stream.
- **Payload-fetch rate** — `fetch_payload_log` error rate over a window on
  the action stream.

Each is one `detect(client, params)` module and one alarm entry, per
[alarms.md](alarms.md).

## Data

- `AlarmTransition` — event, kind, time, actor, detail (JSON). Written by the
  engine through psycopg for detection transitions and by Django for the
  rest.
- `AlarmFeedState` — subscriber, event, last read transition. Unique per
  (subscriber, event).
- Alarm entry parameters gain `escalation_interval` and `responder_mode`.
- SysConfig gains the responder's per-class caps.

## Delivery sequence

1. Transitions table and detection transitions from the engine; every
   transition logged as an action-stream incident; acknowledgement and
   assignment on the dashboard. Existing alarms and email unchanged.
2. Feed state, the feed page, and the two MCP tools.
3. The black-hole detect module.
4. The responder in `advise` mode on the black-hole class, with the
   dashboard approval path executing through the ops agent.
5. `act` mode for pause, after the advise record shows the proposals are
   what an operator would have done.

## Status

Design. Nothing in this document is implemented.
