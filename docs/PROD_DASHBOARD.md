# The production dashboard (plan)

A web page for collaborators that shows the state of ePIC production at a
glance and opens onto the pages behind it. The computing-centre room in
the etaverse dome is its proving ground: what serves its operator there is
what the dashboard shows (Torre Wenaus, 2026-10-23: "every feed you send to
the floor is a feed you will send to our prod dash"). This document is the
plan; it becomes the page's reference once the page exists.

## Form: the floor's, at a glance

The room is graphical and so is the dashboard. It is the room's
information design carried onto a flat page, not a copy of its 3D
objects: dense, read at a glance, the whole of production on one laptop
screen that does not scroll. Where the room stands a pillar, the page
draws a compact histogram of the same quantity over the last 12 hours, so
one small figure gives the level now and how it got there. The room's
trend panels and boards carry over nearly as they are, since they are
already flat. Its visual language carries over too: the colours that mean
running, queued, finished, failed and paused, number over word, and a
level mark rather than a sentence. Space follows relevance at the moment:
what is wrong or busy is large, and an idle or closed queue or a paused
task is a mark and a number. Designs are judged against images of the
room before they are shown.

## What it answers

Read top to bottom, the page answers, in order:

1. Is production healthy right now, and if not, what is wrong? The report
   items, worst first, each with its link.
2. Where is work running, and is each place keeping up? The sites: running
   and queued jobs, cores in use against capacity, the last hour's finished
   and failed jobs, health and its parts.
3. What is waiting to run and why? The pressure front: tasks ready to
   release, released in the last hour, the front's decision per queue.
4. Which tasks are in production, and how far along? The task board, and
   the paused tasks beside it.
5. Is the data arriving? JLab and BNL Rucio storage: files registered and
   bytes in the last hour, registration health.
6. What happened recently? The live screen's warnings and errors, and the
   daily assessment's verdict.

Each number is a link: a queue to its Snapper history at the room's 12-hour
scope, a task to its page, a failure line to the error summary, an alarm to
its event.

## The feed

The page reads only the comp snapshot (`comp_snapshot:v1`,
[COMP_SNAPSHOT.md](COMP_SNAPSHOT.md)), the room's record, through its REST
face `panda/comp/snapshot.json`. It adds no query of its own. Anything the
page needs and the record lacks is added to the record, shaped for both
readers (counts, levels, plain-text detail, links), so the room gains it
too. The page refreshes from the record once a minute while attended
(`swfPollGate`), as the room does.

A live session's reading of an item (the readings layer swf-1 is planning:
an explanation with author, evidence and expiry attached to a room item)
shows on the page exactly as in the room, marked as an AI reading.

## The page

A swf-monitor page on the production face, `/prod/panda/dashboard/`
(name to settle), in the user view's nav. House conventions throughout:
state colours by `state_class`, relative times, sortable tables, short
names through the one filter, levels as the room draws them (green, amber,
red, grey). It is read-only and needs no login beyond what the production
face already asks.

The layout follows the room's (Form, above); the mockups settle it.

## How it is built: fast mockups, then the page

Design moves in many fast iterations before any page code:

1. Mockups are private Artifact pages built from a captured snapshot (the
   live record saved as JSON at the time), so each iteration is an edit and
   a publish in seconds, with real numbers and no deploy. Torre comments on
   the mockup; the next version follows.
2. When the design settles, the page is built in swf-monitor from the
   mockup's markup and the live feed, deployed with the lightweight deploy,
   and checked on the external face.
3. Later changes to the design go through the mockup first when they are
   more than a detail.

## Open questions

- The page's name and place in the nav.
- Whether collaborators outside the production team see everything, or a
  reduced view (`user_view`).
- Phone layout: whether the page must read on a phone, or a desk screen
  is enough.
