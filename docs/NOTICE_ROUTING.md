# Notice Routing

Notice routing extends the action stream ([ACTION_STREAM.md](ACTION_STREAM.md))
with subscriptions: named events matched to autonomously registered
subscribers and delivered per subscriber. The emitting code declares only the
event; it never names a recipient. Consumers — external systems like personal
feed aggregators, and delivery plugins like the Mattermost publisher —
register their own interest and receive matching events through their chosen
delivery mode.

The design separates three concerns that today are partially fused:

- **Events** — what happened, recorded once, named.
- **Subscriptions** — who wants what, registered by the consumer.
- **Delivery** — how a matched event reaches each subscriber.

## Events

The action stream is the event source. The `action` identifier is the event
name; the record's structured fields (`subject_type`, `subject_key`,
`outcome`, `namespace`, free keys) are its attributes. `ACTION_DEFAULTS` is
the event-name catalog. Emission sites change nothing: recording an action is
publishing an event.

Routing operates on the structured action space across logging namespaces
(`app_name`), independent of the `sublevel` and `live` axes — those govern
the built-in human channels (live page, `#epicprod-live`, digests), while
subscriptions are the extension mechanism for systems. A subscription may
filter on importance, but a low-importance mechanical event is fully
subscribable.

## Subscriptions

A `NoticeSubscription` row: subscriber name, event name (exact or trailing
wildcard), attribute filters (equality matches over the record's structured
fields), delivery mode, enabled flag, creator. Token-authed CRUD at
`/api/notices/subscriptions/`, so a third-party system registers and
maintains its own subscriptions without swf code changes. Subscription
changes are themselves logged actions.

## Delivery

Two modes, chosen per subscription:

- **Buffered pull** — the router writes one notice row tagged with the
  subscriber name; the subscriber drains its buffer over REST from its own
  side. This generalizes the existing Capcom store (`CapcomNotice` gains the
  subscriber tag; the existing drain endpoint and retention behavior carry
  over), and preserves the external-consumer boundary: swf buffers locally
  and holds no credential into any external system.
- **Push plugin** — the router hands the event to a named in-process plugin.
  The `#epicprod-live` Mattermost publisher becomes the first plugin; each
  plugin's settings live in SysConfig.

Notice composition from the event is deterministic: title from the action's
catalog description and subject, detail from `reason`/`summary`/`narration`
where present, URL to the log record, severity from the outcome, dedup key
from the record id. Composition belongs to the router, not to emitters or
subscribers.

The router is a stream tailer — the proven publisher pattern: a single
polling service that matches each new record against enabled subscriptions
and performs deliveries, with per-cycle caps and counted overflow, never
silent drops. Emission stays pure and never blocks or fails on delivery
problems.

## Workflow completion events

Workflow execution status changes arrive by plain REST update and record no
action today. The execution update path gains a terminal-transition event:
when status reaches `completed` or `failed`, it logs
`workflow_execution_completed` (namespace, execution id, status, elapsed,
and a `notice` attribute carried from the execution's parameters).
`testbed run --notice` (equivalently a `testbed.toml` key) stamps the run.

The first use is the nightly testbed heartbeat: the overnight cron run
carries the stamp, and a subscription (subscriber `capcom`, event
`workflow_execution_completed`, filter `notice=true`) delivers exactly one
notice per overnight run into the operator's feed — one notice for each
day the testbed ran, with warning severity on failure.

## Migration

The direct Capcom posters (ops-agent pause/resume terminal notices, the
assessment and delivery-daily notices) already emit matching stream events
carrying the needed fields; each becomes a subscription and its bespoke
posting code retires. The `/api/capcom/notices/ingest/` endpoint remains for
genuinely external posters. The Mattermost publisher's event selection
(live + importance threshold) is re-expressed as a subscription when it
becomes a plugin; its formatting is unchanged.

## Delivery sequence

1. The router service, the subscription model and REST, buffered-pull
   delivery on the generalized store, and the workflow-completion event —
   the nightly heartbeat working end to end.
2. Migration of the direct Capcom posters to subscriptions.
3. The Mattermost publisher as a push plugin.
