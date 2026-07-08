# epicprod AI Proposals

AI proposals are how the LLM acts as a change agent in epicprod with a human
in the loop: the AI proposes concrete actions, a human reviews and approves
them, and approval executes the change. Proposals make full use of the LLM
for automation — not only assessment — while keeping every mutation under
deterministic human control.

## The invariant: deterministic after authoring

A proposal is an **executable payload**, not prose: the action identifier and
the exact validated arguments of the service call it wants (for the pilot,
`dataset_propagation_set` — state, `replaced_by`, comment). The LLM is
consulted exactly once, at authoring time. From the moment the proposal
exists it is frozen data plus existing code, passing three deterministic
checkpoints:

1. **Propose** — the payload is validated by the same validators the service
   uses. An unexecutable proposal is refused at birth; nothing reaches review
   that cannot run.
2. **Approve** — preconditions are re-checked against current state
   (revalidation on touch). A record that changed since the proposal saw it
   is skipped and counted, never silently re-interpreted.
3. **Execute** — the identical service call an operator makes by hand, with
   the approving human as `changed_by`. No LLM variance anywhere past
   checkpoint 1.

AI surfaces get only the *propose* verb; the execute path demands an
authenticated human. This is the credential-boundary pattern applied to
authority: the AI surface holds no mutation right, structurally.

## v1: record-resident proposals on the catalog

The first consumers are catalog dataset actions (the propagation disposition
pass). A proposal rides on the record it targets, in the extensible JSON:

```json
Dataset.metadata['proposal'] = {
  "action": "propagation",
  "payload": {"state": "final", "replaced_by": ""},
  "comment": "retire with 26.07: legacy pythia8 superseded by pythia8.316-1.0 — per S. Rahman, PSC 2026-07-08",
  "proposer": "claude-fable-5",
  "scan_version": 1,
  "batch_id": "26.07-dispositions",
  "prev_state": "continue",
  "proposed_at": "<iso8601>"
}
```

Record-residence gives the review surfaces for free: the catalog list and
the compose detail render the same server-side record, so moving between
list and detail cannot lose or fork review state. `prev_state` is the
staleness anchor — approval refuses when the record no longer matches.
`batch_id` ties a family-sized set together; bulk approval of a batch is one
service call and one action-stream event, never one per dataset.

A general proposal table (multi-subject actions beyond the catalog) is the
designed growth path once a second consumer appears; the metadata schema
lifts into it unchanged.

## Review

Proposals are **visible but inert** on the open face: anyone can see the
recommendation (badge, filter); only an authenticated approver can act. In
the catalog:

- an **AI proposed** filter isolates recommended rows; each row carries an
  AI-styled badge showing the proposed transition (`continue → final`);
- **Approve** and **Deny** in the bulk bar act on the selection (tick-all
  included, confirmation included), so review is the same filter → select →
  act motion as every other bulk operation;
- the compose detail page shows the same proposal with the same controls.

Approval executes through the service and clears the proposal; the executed
action's stream event and the object's history entry both carry the origin
stamp — `origin: ai_proposal` plus proposer, scan version, batch id, and
proposed-at — alongside the approving `username`. Every mutation is thereby
classifiable as human-originated or AI-proposed/human-approved; the fraction
is a standing metric of earned automation.

Denial records the proposal's input hash in the record's denial memory
(`metadata['proposal_denied']`): a denied proposal is not re-proposed until
the proposer's inputs change.

## Lifecycle: the nightly heartbeat, not timers

Recurring proposers (nightly scans) refresh rather than expire: each scan
withdraws its unacted proposals and re-derives from current inputs. What
still holds returns freshly validated; what no longer holds does not return;
new findings appear. Pending staleness is bounded by the scan cadence, and
nothing important evaporates unlooked-at — it is re-proposed until decided.
Denial is the only stop. Withdrawals are logged and counted
(`dataset_proposal_expired`), never silent.

One-shot proposal sets (a hand-run pass) persist until decided; revalidation
on touch covers their staleness. Per-action-type proposing is switchable off
in SysConfig. The graduation path — an action type earning `auto+notify`
after sustained approval — is a future policy knob on the same registry
pattern as the live policy, not a code change.

## Action-stream records

| action | when |
|---|---|
| `dataset_proposal_created` | one per propose call, with count and batch |
| `dataset_proposal_denied` | one per deny decision, with count |
| `dataset_proposal_expired` | one per heartbeat withdrawal, with count |
| (execution) | the executed action's own event, origin-stamped |

## AI presence — the UI convention

Material attributable to the AI renders in the universal AI scheme: dark
purple ink on light purple ground, defined once as CSS custom properties
(`--ai-fg`, `--ai-bg`, `--ai-border`) with the `.ai-attr` class in the base
template. Proposal badges, the AI-proposed filter, proposer lines, and
future AI-authored blocks all inherit it; retuning the system-wide AI look
is a one-place edit.

## Related

- [PCS.md](PCS.md) — the propagation disposition model the pilot proposes on.
- [EPICPROD_ACTION_STREAM.md](EPICPROD_ACTION_STREAM.md) — the event record.
- [EPICPROD_NARRATIVES.md](EPICPROD_NARRATIVES.md) — the context proposers
  reason against.
- [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md) — the review surface.
