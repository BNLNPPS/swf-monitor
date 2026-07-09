# epicprod AI Proposals

AI proposals are how the LLM acts as a change agent in epicprod with a human
in the loop: a proposer emits concrete actions, a human reviews and decides,
and approval executes the change. Proposals make full use of the LLM for
automation — not only assessment — while keeping every mutation under
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
2. **Decide** — preconditions are re-checked against current state
   (revalidation on touch, the `precondition` anchor). A record that changed
   since the proposal saw it is marked stale and withdrawn, never silently
   re-interpreted.
3. **Execute** — the identical service call an operator makes by hand, with
   the approving human as `changed_by`. No LLM variance anywhere past
   checkpoint 1.

AI surfaces get only the *propose* verb; the decide path demands an
authenticated human. This is the credential-boundary pattern applied to
authority: the AI surface holds no mutation right, structurally.

## The AI proposal list: the Proposal table is canonical

The AI subsystem is its own Django app, `src/ai/` — a sibling of `pcs`,
owning the proposal model, services, pages, and API, plus the AI
assessment helpers and the corun-ai client (`ai/assessments.py`,
`ai/corun_client.py`; the legacy `AIContent` model remains in
`monitor_app` until its planned post-backfill retirement). Executors stay
in their domain apps; `ai` dispatches to them.

Every proposal is a `Proposal` row (`ai_proposal`): action, subject
(type + key, `counterpart_key` for relation subjects such as a
questionnaire–task match), frozen `payload`, required `comment`,
`confidence`, proposer identity + scan version, `batch_id`, **executor**
(`service` now; `agent_message` reserved for prod-ops-agent actions),
`precondition` (the deterministic decide-time guard), input hash, status,
and the decision record (`decided_by`, `decided_at`, optional `quality`,
executed action-stream log id).

Status vocabulary: `proposed → executed | denied | withdrawn | stale`, with
`approved_pending_execution` reserved for asynchronous executors. **Terminal
rows are retained** — the AI proposal list is where AI-proposed activity
and its decisions stay visible: the queue (pending), the decided history,
per-proposer track records, and the standing metric of what fraction of
production mutations originate with AI. Operators can delete list rows
(test or noise entries), logged with counts — a cleanup verb, never a
decision verb.

Records a proposal targets carry a **render projection** in their metadata
(`Dataset.metadata['proposal']`), written and cleared in the same
transactions by the same services — so the catalog badges, filters, and
compose detail render without joins while the table holds truth. Denial
memory is a proposal-list query (denied row with the same subject and input
hash), not record state.

## Review surfaces

Proposals are **visible but inert** on the open face: anyone can see a
recommendation; only an authenticated approver can act. Everything
AI-attributable renders in the universal AI presence scheme — purple ink
on lavender ground, defined once (`--ai-fg`, `--ai-bg`, `--ai-border`,
`.ai-attr` / `.ai-attr-text` / `.ai-fill`) in the base template. Used with
restraint: it marks AI material embedded in non-AI surfaces and the titles
of AI pages, so the color registers as "this is AI" without saturating.
The marker follows the AI's standing voice — proposals and assessments —
not authorship: human-adopted artifacts such as published narratives are
informational records and dress plain, whoever drafted them.

- **The catalog list**: an AI-proposed filter isolates recommended rows;
  each carries a badge with the proposed transition; Approve AI / Deny AI in
  the bulk bar act on the selection.
- **The compose detail**: the same proposal block with the same controls —
  the record is server-side, so list and detail cannot fork.
- **The proposals page** (`/ai/proposals/`, on the production home under
  AI assessments): the pending queue and the decided history, filterable by
  status, action, proposer, and batch, with bulk decide and per-proposer
  track records.

A decision optionally carries a **quality tag** from the shared review
vocabulary (`wrong | poor | ok | good` — the same scale as AI assessments).
`Deny — wrong` records a miscalibrated proposal and weighs against the
proposer's track record; plain deny records a sound proposal decided
against.

Execution events and history entries are **origin-stamped**
(`origin: ai_proposal`, proposer, scan version, batch, proposed-at)
alongside the approving `username`, so every mutation is classifiable as
human-originated or AI-proposed/human-approved.

## Lifecycle: the scan heartbeat

Recurring proposers refresh their pending proposals: each scan withdraws
its unacted proposals and re-derives from current inputs — what still holds
returns freshly validated, what no longer holds does not return, new
findings appear. Pending staleness is bounded by the scan cadence, an
unreviewed proposal returns at each scan until decided, and denial is the
only stop (a denied input hash is never re-proposed until the proposer's
inputs change).
Withdrawals are logged and counted, never silent. One-shot proposal sets
persist until decided; revalidation on touch covers their staleness.
Per-action-type proposing is switchable off in SysConfig.

## Proposers

The subsystem is **proposer-agnostic**: rule-based detectors, LLMs, and
hybrids are all legitimate proposers — a deterministic rule ("this family's
EVGEN inputs vanished from Rucio → propose hold") emits through the same
surface as an LLM scan.

**Comments are templated, not freeform.** Each proposer's harness defines a
per-action comment template: code fills the facts (what changes, counts,
replacement target, source and date), the model fills only the judgment
fragment — some proposers fill nothing. Facts in comments therefore cannot
be hallucinated, and the human reviews a uniform comment shape per action
type. The questionnaire automatch is the worked hybrid example: structured
frame plus the model's one-line reason.

**Weakest-consumer design.** The propose surface is one flat call with
deterministic validation, so a small-model proposer's worst case is a
denied proposal. The proposal list's
per-proposer approval and wrong-rates make proposal rights earnable and
revocable by data: a proposer whose wrong-rate climbs gets its proposing
switched off. LLM *readers* of proposal state (bots, assessors) get a
retrieval tool sized for the smallest model: summarize-first, prescriptive
docstring.

## Ceremony proportional to stakes

Review friction matches irreversibility, in both directions:

- **Undoable** actions (catalog state: propagation, tags, matches) approve
  in one tap, and the system can offer **Undo** — a computed compensating
  action with full provenance (`origin: undo-of`), never erasure; history
  stays append-only. The undo offer expires when the record moves on, by
  the same precondition machinery.
- **Mitigable** actions (external effects: a submitted PanDA task can be
  killed or superseded by `.tryN`, not unsubmitted) offer the named
  mitigation, labeled honestly: it does not restore the prior world.
- **Irreversible-by-design** actions (locking a tag, publishing a
  narrative revision) are labeled as such — their immutability is a
  guarantee other records depend on — and get ceremony: dry-run preview
  before approve.

Every approve control states its undo story. Reversibility class is
declared per action type in the same registry family as sublevel and live
defaults.

**Dry run** is the trust instrument for consequential and composite
actions: execute the frozen payload with writes off and show the concrete
before→after; approve then executes what was just shown. Dry runs write
nothing, so they are open to all readers, not only approvers. Composite
executors (campaign creation) must be
dry-runnable; a composite proposal's review surface is its dry run, diffed
against the preview frozen at propose time — staleness made visible at a
scale a single `prev_state` anchor cannot guard.

## Growth path

Designed-for extensions, reserved now, built when their consumer arrives:

- **Asynchronous executors** (`executor: agent_message`): approval queues
  the payload to the prod-ops agent; the proposal holds
  `approved_pending_execution` until the completion event closes it.
- **Recordless subjects**: system-scoped proposals ("re-run catalog_sync")
  carry `subject_type: system` and no render projection — the queue page is
  their only surface.
- **Creation subjects**: proposals that create records ("compose a draft
  task from request #202") anchor on idempotency keys rather than
  `prev_state`; the intake surface already provides them.
- **Second consumer, first adaptation**: questionnaire–task matching — the
  automatch's `suggested` matches land as proposals with confidence
  (relation subjects, `counterpart_key`), high/medium confidence keeps
  auto-accepting with origin stamps: the graduation ladder
  (`manual → auto+notify`) already in production.
- **Beyond epicprod**: the subsystem is system-wide — testbed and
  streaming actions are expected consumers. Proposal events currently log
  to the epicprod action stream; where non-epicprod proposals log is
  decided when the first such consumer arrives.

## Action-stream records

| action | when |
|---|---|
| `proposal_created` | one per propose call, with count and batch |
| `proposal_denied` | one per deny decision, with count and quality |
| `proposal_expired` | one per heartbeat withdrawal, with count |
| (execution) | the executed action's own event, origin-stamped |

## Related

- [PCS.md](PCS.md) — the propagation disposition model the pilot proposes on.
- [EPICPROD_ACTION_STREAM.md](EPICPROD_ACTION_STREAM.md) — the event record.
- [EPICPROD_NARRATIVES.md](EPICPROD_NARRATIVES.md) — the context proposers
  reason against.
- [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md) — the review surface.
- [EPICPROD_OPS.md](EPICPROD_OPS.md) — the shared review-quality vocabulary.
