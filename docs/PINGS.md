# Pings

A ping is a dated obligation: an action to take on a subject by a due
date, with an owner and a lead time. The alarm system carries pings at
their own severity, `ping`: a ping raises when its due date comes within
its lead time, repeats while open, becomes an alarm when its due date
passes unfulfilled, and clears when the obligation is fulfilled. Pings are
entered by a person on the alarm dashboard or proposed by the AI through
the proposal mechanism ([AI_PROPOSALS.md](AI_PROPOSALS.md)) and accepted
there by a person. This document specifies the record, its lifecycle, the
two ways a ping enters, and its delivery. It builds on the alarm engine
([alarms.md](alarms.md)) and the alarm queue ([ALARM_QUEUE.md](ALARM_QUEUE.md)).

## The record

A ping is one entry of kind `ping` in the `swf-alarms` context, beside
the alarm configurations, events, and teams the dashboard already edits:

| Field | Meaning |
|---|---|
| `title` | the obligation, as a sentence: "Renew the osgsub01 host certificate" |
| `due` | the due date |
| `lead_days` | how far ahead the ping raises; default 7 |
| `owner` | the person or team responsible, a recipient token as on alarm configurations |
| `note` | what to do and where; the alarm event body |
| `url` | where to look or act, optional |
| `status` | `open` or `fulfilled` |
| `origin` | `manual`, or the proposal ref that created it |
| `fulfilled_by`, `fulfilled_at` | the fulfilment record |

The entry's content holds the note; the fields live in its `data`. A
fulfilled ping is retained; it is the record that the obligation was met
and when.

## Lifecycle

One detect module, `pings`, and one alarm entry, `alarm_pings`, carry
every ping through the engine's ordinary tick:

- **Raised.** An open ping whose due date is within `lead_days` yields a
  detection keyed on the ping, at severity `ping`. The event is created,
  the email bundle goes to the alarm's subscribers and the ping's owner,
  and the incident reaches Capcom.
- **Repeated.** The alarm entry's renotify window, seven days by default,
  re-sends the open ping and, under the alarm queue design, writes its
  `renotified` transition.
- **Overdue.** Once the due date has passed, the detection carries
  severity `alarm`: the event's severity is updated on the tick, the
  dashboard and the feed show it as an alarm, and the next bundle says so.
  This is the alarm queue's `escalated` transition with the due date as
  the escalation point.
- **Fulfilled.** A person marks the ping fulfilled on the dashboard, or
  accepts an AI proposal to that effect. The module no longer yields it,
  and the engine clears the event on the next tick, the same auto-clear
  every alarm has. Nothing observes fulfilment on its own: a renewed
  certificate does not close a ping, a person does, and the next AI look
  at the certificate proposes the closure if it is still open.

Severity is carried on the event (`data.severity`) and on the incident
the transition logs, so subscribers filter on it: a Capcom feed at
`ping`, an email digest at `alarm` and above.

## Entering a ping

Two ways in, side by side on the alarm dashboard's Pings section:

- **By hand.** A form with the record's fields; the write is
  login-gated like every dashboard write. Fulfilment is one control on
  the ping's row.
- **By proposal.** The AI proposes a ping through the proposal mechanism:
  category `ping`, ref prefix `pg`, a creation subject keyed on the
  obligation and due date, executor `ping_create` in the alarm domain,
  the identical call the form makes. The reviewer may change the due
  date before accepting, recorded on the payload as an amendment, as the
  campaign-plan category does. A proposal is a proposal-created incident
  and so reaches Capcom on creation; acceptance creates the ping with
  `origin` set to the proposal ref, and the ping then raises on its own
  schedule. Fulfilment is proposable the same way, category `ping_fulfil`
  on the ping's entry. A denied proposal is denial memory: the same
  obligation is not re-proposed until its inputs change.

The proposer is whatever AI is looking: a session's assistant reading a
certificate or a credential, or, later, the alarm responder
([WRANGLER_AGENT.md](WRANGLER_AGENT.md)). The propose surface is the
existing proposal REST endpoint and an MCP propose tool sized for the
smallest model: title, due date, owner, note, and a comment.

A proposer need not be a model. The proposal design admits rule-based
proposers, and the expiry checks move into the ping system that way:

- **Credentials.** The nightly credential expiry check (EPICPROD_OPS.md)
  reads the expiry of the PanDA token and the two x509 proxies. As a
  proposer it proposes, for each credential with no open ping on its
  expiry, a ping due at that expiry with a seven-day lead, keyed on the
  credential and the date so the proposal is made once; when a credential
  is renewed it proposes fulfilment of the open ping and a ping on the new
  date. Its action-stream warning line retires; the pings are the record.
- **Certificates.** The same shape over the host certificates of the
  PanDA and OSG service hosts, read from the served chain: a ping on
  each expiry, and a ping on a certificate served without its
  intermediate, since browsers without the grid CA bundle cannot verify
  it.

Both proposers run where their reads are possible: the credential
proposer on the production ops agent, the certificate proposer in the
alarm engine's tick or beside it. Neither writes a ping; a person accepts
each one, with the date editable.

## Delivery

- **Email.** The `alarm_pings` entry's recipients are the subscribers;
  the ping's owner is added to the bundle that carries it. The engine's
  one-email-per-alarm-per-tick rule stands.
- **Capcom.** The `alarm_raised` and `alarm_escalated` incidents at
  severity `ping` and `alarm` reach the operator's feed through the
  standing notice-routing subscription; `ping` joins the Capcom severity
  vocabulary on the tjai side, so a reminder is not dressed as a
  warning.
- **The feed.** Under the alarm queue's per-subscriber feeds, an open
  ping is an item like any other: unread on raise, on repeat, on
  escalation, and on clear.

## Dashboard

The alarm dashboard gains a Pings section: open pings with due date,
days left, owner, origin, and the fulfil control, ordered by due date;
pending AI-proposed pings beside them in the AI treatment with accept,
amend-date, and deny; fulfilled pings in a collapsed history. The event
history of a ping is the alarm event history it already has.

## Data

- Entry kind `ping` in `swf-alarms`, fields as above.
- Alarm entry `alarm_pings`: recipients (the subscribers),
  `renotification_window_hours` 168, `severity` `ping`.
- Event `data.severity` refreshed on each tick from the detection, so an
  overdue ping's event reads `alarm`.
- Proposal categories `ping` (ref `pg`) and `ping_fulfil` (ref `pf`),
  with executors `ping_create` and `ping_fulfil`, creation-keyed
  preconditions, and their `ACTION_REF_PREFIXES` entries.
- Action-stream records: `ping_create`, `ping_fulfil`, both
  origin-stamped, at importance `normal`, live.

## Delivery sequence

1. The ping entry, the `pings` detect module and its alarm entry, the
   dashboard section with entry and fulfil, event severity refresh, the
   owner added to the bundle. Pings work by hand end to end.
2. The `ping` and `ping_fulfil` proposal categories, the MCP propose
   tool, and the proposal rows in the Pings section.
3. The credential and certificate proposers; the nightly check's warning
   line retired.
4. `ping` as a Capcom severity on the tjai side.

## Status

Step 1 was completed 2026-09-04: the ping entry, the `pings` module and
its alarm entry, the dashboard section with entry and fulfil, event
severity refresh, and owner recipients are in production, and the first
two pings raised on the engine's tick. Steps 2 to 4 are not started.
