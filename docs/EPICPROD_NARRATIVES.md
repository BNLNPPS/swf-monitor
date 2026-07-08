# epicprod Campaign Narratives

Narrative documents are the curated context that epicprod's AI assessors and
reporting load before reasoning about production state: what a campaign is
for, what should be running, what changed, and the standing facts of how
production operates. They are extracted once from raw material — meeting
presentations, planning discussions, release notes — so that no runtime
consumer ever re-derives context from slides or dialog.

Narratives are AI-authored **products** and live in corun-ai (created and
read through its REST interface), alongside assessments and reports. The
**rules** that govern them — this document, the naming grammar, the code
that enforces both — live in git. A narrative cites its governing
conventions; it never carries the master copy of them.

## Document classes

**General narrative** — the standing context: the dataset-request provenance
chain, resource landscape, priority and readiness semantics, naming
conventions, operational posture. Revised as a dated series; each revision
is a full re-baseline.

**Campaign narrative** — the evolving specification of one campaign:
purpose, software identity (container, geometry, reconstruction tags), the
dataset matrix with priorities and dispositions, timeline expectations,
retirements and replacements. Drafted before the campaign, living during
it, final revision frozen as its permanent record.

Campaign narratives cite the general narrative and never restate its
content. Provenance metadata records which general revision a campaign
revision was written against; consumers nevertheless always load the
current revision of each.

## Naming

```
campaign_general_YYYYMMDD          general series, dated
campaign_<campaign>[.revN]         campaign series, e.g. campaign_26.07.X.rev2
```

`<campaign>` is the campaign's canonical name as PCS records it (the bare
version, e.g. `26.07.X`) — never an approximation of it. `revN` is a
reserved terminal token following the `name_tokens.py` suffix grammar: the
bare name is the first revision, `.rev2` onward are supersessions, and
parsing strips the token from the right. Drafting churn never mints names;
only publication does.

## Lifecycle

Narrative revisions follow the PCS tag lifecycle: **draft → locked**.

- **Draft** — the AI–human iteration workspace. Modify in place, delete and
  recreate freely; nothing consumes drafts and no assessment cites them.
- **Locked (published)** — immutable, by the same one-way transition as
  tags. Publication is a human action. Any correction after publication is
  a new revision.

Published revisions are never edited: assessments cite narratives by name,
and the assessment corpus is only auditable if a cited name always resolves
to the same content.

## Content rules

- **Complete, not delta.** A consumer loads exactly one revision per class.
  A "changes since previous revision" section inside the document serves
  human readers; the rest of the document stands alone.
- **Sources are part of the document.** Every narrative carries a source
  list — meeting links, talk titles, dates — so any claim traces to raw
  material.
- **One resolver.** "Current revision" (highest date for the general
  series, highest rev per campaign) is implemented once, in the service
  layer, and shared by every consumer; it is not a convention each client
  re-implements.

## Relationship to the catalog

The campaign narrative's disposition content — what continues, holds,
retires, and what replaces what — is carried structurally by the dataset
`propagation` state and `replaced_by` reference (see
[PCS.md](PCS.md#datasets)). The narrative states the intent and the why;
the catalog fields make it computable. The procedurally generated campaign
summary (the per-family beam × Q² matrix with readiness and disposition)
accompanies the narrative as a generated artifact, not hand-authored
content.

## Related

- [PCS.md](PCS.md) — dataset identity, campaign editions, propagation.
- [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md) — the catalog the
  narratives describe.
- [EPICPROD_ACTION_STREAM.md](EPICPROD_ACTION_STREAM.md) — the action
  record assessors reason over, against narrative context.
- [EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md) — the corun-ai
  document store and REST interface.
