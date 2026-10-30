# What's new

The What's new page lists the developments in SWF that its users,
operators and developers should know about, newest first. It is the
second entry of the Help menu, after Documentation. Each item is an
entry in the monitor's entry store, so every edit is kept as a version.
Items are edited, commented on and voted on by anyone with authority to
act ([AUTHORITY.md](AUTHORITY.md)). LLM sessions propose items, and a
person approves them.

## The item

| Field | Content |
|---|---|
| Title | One line: what changed, in the reader's terms. |
| Summary | One to three lines: what it does and who it is for. |
| Details | Optional Markdown: how to use it, what it replaces, limits. |
| References | Optional Markdown: pages, documents, commits, pull requests. |
| Tags | Topical tags, at least one. |
| Importance | Exactly one of urgent, high, routine, minor. |
| Shipped | The date the development reached production. |
| Contact | The account of the principal developer, the person to ask. |
| Builds on | Earlier items this one develops further. |

An item is an `Entry` of kind `development` in the context `whats-new`.
The title is the entry's title and the details are its content; the
other fields are keys of its data. `data.entry_id` is a slug made from
the title at creation and never changed after, so an item's URL stays
fixed when its title is edited.

The entry's creation and modification times are kept and shown, but
the page orders by Shipped. Shipped defaults to the creation date and
is set to the production date for an item written after the fact, so a
backfill of past developments takes its true place in the list.

## Tags and importance

Tags form one flat set. The conventional tags name the primary
audience (`dev`, `ops`, `users`) or the area of the system (`testbed`,
`pcs`, `panda`, `ddm`, `alarms`, `snapper` and so on). New tags are
created by use; the set of tags in use is the vocabulary that the edit
form offers and that the MCP list tool reports, so tags are reused
rather than duplicated under other spellings.

Importance is required and single-valued: `urgent` (action needed by
readers), `high`, `routine`, `minor`. It is shown as a badge on each
item and filters like a tag.

## Links between items

An item lists the earlier items it builds on. The earlier item shows
the reverse, "Developed further in", computed by query from the later
items' lists, so the link is stored once.

## Comments and votes

A comment is an `Entry` of kind `comment` in the same context whose
parent is the item. Its content is Markdown, it is versioned like the
item, and its author may edit it. On the list, each item shows its
comment count; when the newest comment is by someone other than the
contact, the item is marked as awaiting a reply from the contact.

Each person has at most one vote per item, up or down, held in
`data.votes` as username to +1 or -1. Voting again replaces the vote,
and the same click withdraws it. The score is the sum. A vote does not
create a version: `votes` is excluded from versioning beside the other
operational keys in `signals.py`.

## Editing, history and authority

Reading needs a signed-in account. Creating or editing an item,
commenting and voting are writes, which the authority gate admits for
any person who may act; no further role is required. Every save records
the person who made it as the version's author. The item page lists
the versions, each viewable in full, and restores any of them as a new
version.

## The pages

| Page | URL |
|---|---|
| List | `/help/whats-new/` |
| Item | `/help/whats-new/<slug>/` |
| Version | `/help/whats-new/<slug>/versions/<n>/` |
| New item | `/help/whats-new/new/` |

The list shows one row per item: an expand arrow, the shipped date, the
importance badge, the title with the summary beneath it, the tags, the
contact, the score with up and down vote controls, and the comment
count. The arrow expands the details, references and links in place;
Open all and Close all at the top expand or collapse every row. The
title opens the item page, which carries the whole item, the comment
thread, the edit form and the version list.

Above the list, the inclusive filter ([INCLUSIVE_FILTER.md](INCLUSIVE_FILTER.md))
offers every tag and importance level with its count, and the search
box narrows the rows by text. Every element of the page state is in
the URL: `f` and `match` for the filter, `q` for the search,
`sort=score` for ordering by score instead of date, and `open=all` for
the expanded list.

Proposed items wait in a section at the top of the list, in the AI
treatment, each with its approve, edit and deny controls.

The pages live in the `help` application (`src/help/`), mounted at
`help/`, which also holds later Help pages. The external face proxies
the `help/` tree through one catch-all route in swf-remote.

## Writes

Writes are JSON calls under `/help/api/whats-new/`, authenticated by
the session on the internal face and by the forwarded `X-Remote-User`
identity on the external face, answering JSON and never a redirect
([EXTERNAL_ACCESS.md](EXTERNAL_ACCESS.md)). One service module,
`help/whats_new.py`, performs every write: the pages, the API, the MCP
tools and the AI proposal executor call it, and nothing else writes
the entries.

## LLM-proposed items

LLM sessions do not write items; they propose them through the AI
proposal subsystem ([AI_PROPOSALS.md](AI_PROPOSALS.md)), category
`whats_new`, reference prefix `wn`.

- **Subject**: a creation subject keyed on the proposed slug.
- **Payload**: every field of the item.
- **Precondition**: no item with that slug exists; a proposal whose
  slug has since been taken is stale.
- **Executor**: the item-create call of `help/whats_new.py`, the call
  the New item form makes.
- **Edit-then-approve**: the reviewer may change any field before
  approving; the changes are recorded under `amended` and the executor
  creates the amended item.
- **Review surfaces**: the proposed section of the What's new page and
  the proposals page.
- **Reversibility**: mitigable. An item approved in error is archived.

The watch that produces proposals is guidance to every LLM session
working on SWF, carried in the MCP server instructions: a change that
users, operators or developers should know about, once it is in
production, is proposed as a What's new item with its shipped date,
tags, importance and contact. The release procedure checks the
release's changes against the list and proposes the items that are
missing.

## MCP

| Tool | Purpose |
|---|---|
| `whats_new_list` | Recent items, newest first: filter by tags, importance, shipped date and text; each item's title, summary, tags, importance, shipped date, contact, score, comment count and URL; the tags in use with counts. |
| `whats_new_get` | One item in full, with its links, comments and version count. |
| `whats_new_propose` | Propose an item (the AI proposal path above). |
| `whats_new_edit` | Edit an existing item's fields, as the calling person. |

`whats_new_edit` changes the system and carries `@requires_authority`.
The list tool's description names the questions it answers, "what is
new", "recent developments", so that DISpatcher selects it for them
([PANDA_BOT.md](PANDA_BOT.md)).
