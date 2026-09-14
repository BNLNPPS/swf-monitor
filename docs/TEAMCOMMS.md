# TeamComms integration

TeamComms provides Entries, Dialog, session discovery and durable messaging through
the existing devcloud accounts and API tokens. Its Django applications use
the monitor's PostgreSQL database. The ASGI worker serves its HTTP, MCP and
finite event streams beside the monitor MCP service.

## Request path

The public prefix is https://epic-devcloud.org/prod/teamcomms/. swf-remote
forwards it through the existing SSH tunnel to /swf-monitor/teamcomms/.
Apache proxies that path to the existing worker at 127.0.0.1:8001.
The embedded adapter restores the public host, HTTPS scheme and prefix
before TC routing so redirects retain the external URL.

TC's lifespan runs with the monitor MCP lifespan. Streams reconnect within
25 seconds, preserve Last-Event-ID and revalidate authentication before
database reads and after an idle interval of at most five seconds. Apache
disables caching and compression for this route; swf-remote relays chunks
without buffering.

## Browser interface

When TC is enabled, the monitor's System menu links to its public browser
interface at `https://epic-devcloud.org/prod/teamcomms/`. The link uses
`SWF_TEAMCOMMS_PUBLIC_HOST`, including on the BNL face, so browser access uses
the existing devcloud account session. The UI provides the shared overview,
Entries list at `/entries` and editor at `/entries/<UUID>` beneath that prefix.

The application is constructed with
`browser_csrf_url="/prod/teamcomms/browser-csrf"`. Devcloud serves this
authenticated browser-only GET locally and returns a masked Django CSRF token
and its header name. The browser obtains the token from that endpoint and sends
it with same-origin cookie requests to the existing mutation APIs. The monitor
continues to verify devcloud's request-bound CSRF attestation; it does not issue
the browser's CSRF token. Tokens are not placed in URLs or rendered HTML.

UI links and asset URLs derive from the ASGI mount prefix. Packaged assets under
`/assets` travel through the same authenticated TC route; they need no Apache
static alias or Django `collectstatic` integration. The TC wheel contains the
licensed editor and rendering assets. The host installs the pinned package and
its Markdown, nh3 and pymdown-extensions dependencies before full deployment.
Devcloud preserves the UI response security headers through its relay.

`POST /api/entries/render` supplies sanitized preview through the same
authentication and CSRF guard. Existing entry operations retain their revision
preconditions. The shared package owns editor behavior, draft recovery and
conflict handling; the SWF integration supplies the mount, navigation, account
session and deployment. Host acceptance uses the authenticated public URL to
verify navigation, asset responses, browser CSRF and entry operations.

## SWF Pouch

The System menu's **SWF Pouch** link opens
`https://epic-devcloud.org/prod/teamcomms/pouch`, using the configured public
host and existing devcloud login. The same TC mount serves the Pouch page and
its authenticated APIs; no additional proxy route or credentials are required.

The Pouch is one canonical Entry bound to the installation's Team. Package
migrations create its schema in the monitor database. The host enables
`teamcomms.pouch.apps.PouchConfig` and applies Entries migrations
`0003_editplan` and `0004_editplan_integrity`, followed by Pouch migrations
`0001_initial` and `0002_binding_integrity`. Pouch's integrity migration depends
on Entries `0004`; the normal Django migration graph orders them.

An explicit authenticated `POST /api/pouch/initialize` with `{}` initializes
the empty document once under a team lock. Opening the page
or deploying the schema does not create content. Initialization imports no
TJAI material; existing `entries:read` and `entries:write` scopes govern access.
Database guards prohibit deleting or rebinding the canonical association or
changing its Entry's team or kind. Normal edits and restoration retain it.

All paths below are relative to `/prod/teamcomms`. `GET /api/pouch` reads the
canonical document, `/api/pouch/changes` supplies its revision change feed, and
`/api/pouch/export?revision=N` exports an attributed saved version. The browser
URL `/pouch?revision=N` opens a fixed revision read-only. These operations do not
broadcast Comms messages automatically.

The shared package owns exact-target, atomic and bulk editing, revision checks,
attributed diffs and durable retry receipts. Monitor deployment installs the
reviewed package and applies its migrations before activating the ASGI worker.
Bulk plans freeze at most 20 explicitly selected Entries and their expected
revisions; applying the durable operation UUID is atomic and preserves the exact
outcome for retries.
The designated commissioning session initializes the Pouch once after rollout;
SWF host acceptance checks its public route and canonical document without
creating another document or modifying the operator's content.

## Authentication contract

swf-remote validates the browser session or existing API token and checks
Django CSRF protection on unsafe browser requests. It creates a 60-second
opaque reference bound to the request and the underlying authentication.
The caller's token remains in swf-remote. Caller-supplied proxy assertions
are discarded.

The designated loopback hop supplies X-TeamComms-Auth-Ref,
X-Forwarded-Host and X-Forwarded-Proto. Monitor accepts the configured public
host and HTTPS scheme only. For admission and every stream revalidation it
POSTs `{ "reference": "..." }` to the configured HTTPS introspection endpoint,
authenticated by a dedicated shared service credential. Redirects are refused
and authority failures deny access.

The response contains:

| Field | Meaning |
|---|---|
| subject | Immutable remote account ID for humans; ai:, program: or connector: followed by that ID for other kinds |
| username, name | Verified account lookup key and participant display name |
| kind | human, ai, program or connector |
| account_subject | For program or connector identities, the immutable remote account ID |
| operator | For AI, the human subject, username and name |
| auth_method | session or token |
| csrf_verified | Boolean result of the proxy's browser CSRF check |
| expires_at | Reference expiry as an ISO timestamp with timezone |
| method, path | Exact method and path suffix within TC, such as /api/whoami or /mcp/ |
| query_string | Exact raw query string |
| body_sha256 | SHA-256 of the bounded request body |

Devcloud checks the current session, token and account for each introspection.
Monitor verifies expiry and request binding, including the body for both
authentication methods. Invalid or revoked authentication returns 401,
denied access returns 403 and an unavailable authority returns 503. Active
streams end with the corresponding error event. An operation already admitted
may finish.

## Identity and permissions

The identity provider is swf-remote. Human subjects use the remote account's
immutable primary key. The existing token issuance page can explicitly bind
a token to an AI participant with subject ai:account-ID and a human operator.
Client headers and token display labels cannot select the actor. Human tokens
retain human attribution. TC membership and participant IDs persist across
token renewal and display-name changes.

Program and connector tokens use the existing devcloud issuance page's stored
`teamcomms_service_kind` selection, mutually exclusive with AI attribution.
Introspection supplies `kind`, `account_subject` and the exact subject
`program:account-ID` or `connector:account-ID`. Monitor accepts these identities
only with token authentication, a matching canonical decimal account ID and no
operator identity. Account activation and collaboration permissions follow the
verified username. Token renewal retains the participant; display labels do not
select identity. Connector-reported external provenance remains separate from
the authenticated author and cannot confer operator approval.

Authenticated accounts receive directory:read, entries:read, entries:write,
sessions:write, comms:read, comms:write, dialog:read and dialog:write.
Team participation is independent
of production-action rights. Monitor checks the verified username on each
request and revalidation: an explicitly inactive local account is denied,
and the operations role maps to TC admin membership. AI permissions use
the verified operator account. This mapping grants only TC permissions;
production-operation permissions remain governed by the monitor.

Migration monitor_app.0014 provisions the single ePIC Workflow Management
team, after the TC schema and integrity migrations. The normal privileged
migration helper applies these migrations; application processes keep the
existing swf_runtime role and its default grants. Embedded operation disables
TC credential provisioning.

## Dialog and fresh-session context

The Dialog application uses the same database, authentication and ASGI mount.
Its `0001_initial` and `0002_immutable` migrations run through the normal
privileged deployment helper. Capture
and bootstrap are explicit connector options; granting Dialog scopes does not
enable capture or inject history into existing sessions.

| Interface under the TC prefix | Operation |
|---|---|
| POST /api/dialog/events | record_dialog: capture an event for the caller's registered session |
| GET /api/dialog | get_dialog: retrieve bounded history by host, participant, session, topic or time |
| POST /api/dialog/bootstrap | session_bootstrap: assemble bounded history and explicitly selected guidance entries |

Captured events retain source identity, sequence and time, role, phase and
content. Stable source identifiers make recorder replay idempotent. A human
transcript role records the source's reported authorship; it does not confer
operator approval. Canonical peer-message links must refer to messages delivered
to the recording session, and retrieval retains their Comms access restrictions.
Historical instructions remain context subject to the current session's policy.

The initial SWF bootstrap policy is opt-in for a selected connector configuration:

| Setting | Initial SWF selection |
|---|---|
| Service | https://epic-devcloud.org/prod/teamcomms |
| Host filter | swf-testbed |
| History window | Previous 24 hours |
| Record limit | 40 events |
| Content budget | 16,000 characters shared by history and guidance |
| Guidance | Explicit SWF team Entry IDs; record the revisions returned |
| Local state | Private state directory retained across recorder and receiver restarts |
| Automatic greeting | Disabled during bounded acceptance |

Add these fields to the selected private connector configuration to enable
capture and bootstrap. Both are disabled by default (`dialog_capture=false`,
`bootstrap=null`):

```json
{
  "dialog_capture": true,
  "bootstrap": {
    "host": "swf-testbed",
    "hours": 24,
    "limit": 40,
    "max_chars": 16000,
    "guidance_entry_ids": []
  }
}
```

Select up to ten guidance Entry IDs before the guidance acceptance check.
The bootstrap response includes `context`, `chars`, `truncated`,
`next_before_id`, `selected_event_ids`, `coverage` and `guidance_revisions`.
Dialog retrieval supports `host`, `participant_id`, `session_id`, `topic`,
`since`, `before`, `before_id` and `event_id` filters. Its record limit is
1–100 and content budget is 1,000–30,000 characters; bootstrap additionally
accepts a history window of 1–720 hours.

Returned context retains timestamps, source references, coverage gaps
and continuation information. Relevant local project guidance remains available
through the checkout's documentation. Guidance Entries are selected explicitly;
personal TJAI history and guidance are not imported automatically.

The recorder captures visible human messages, assistant progress updates and final
answers, and peer provenance. Reasoning and tool payloads are outside this SWF
capture selection. Bootstrap injections must not become new human Dialog events
when the native transcript is captured. The dedicated recorder can attach to an
existing supported session, retaining its JSONL cursor and retries without a
native client restart.

For an existing Codex session, run the recorder with its registered TC session,
native session ID and transcript path:

```sh
teamcomms-connect --config CONFIG record \
  --session-id TC_UUID --native-id NATIVE_UUID \
  --client codex --transcript PATH
```

The first attachment starts at the transcript's end and records a coverage gap.
Restart with the same configuration and state directory to resume the saved
cursor. `--from-start` explicitly requests historical transcript capture; it is
not part of the initial SWF setup. `teamcomms-connect --config CONFIG reload`
prints fresh context. Its `--native-id`, `--client`, `--socket` and `--pid`
options select an existing native runtime for explicit context injection.

Acceptance uses one selected fresh SWF session to recover recent captured work
and selected guidance within the configured budget. Verify source attribution,
visible coverage limits and continuation, then recorder restart recovery without
duplicate records. Enablement and package installation are coordinated separately
from the already completed Comms acceptance.

## Configuration and deployment

### One observed watcher event

The initial event source is the existing buffered notice stream. Its active
`capcom` subscription selects `workflow_execution_completed` with `notice=true`,
including the nightly testbed heartbeat. The adapter observes that result; it
does not launch a workflow or alter the notice router, publisher or subscription.

Prepare exactly one existing AppLog event through its buffered notice:

```sh
python src/manage.py prepare_teamcomms_notice \
  --event-id 3456168 --subscriber capcom \
  --audience '{"topics":["swf-observed-events"]}' --topic swf-observed-events \
  --output /path/to/private/observed-event.json
```

The command requires one exact `event:<AppLog-ID>:<subscriber>` match and creates
a mode-0600 file without overwriting an existing payload. The JSON supplies
`source`, `event_id`, `content`, `audience`, `observed_at` and `topic` for the TC
event publisher. The destination must be explicit and pass TC audience
validation. The installation
namespace defaults to `swf-monitor:epic-devcloud.org/prod`; its value and the
`applog:<ID>` event identifier remain stable across retries. `observed_at` is the
original AppLog timestamp, not the preparation time. The notice's buffer
timestamp is labeled separately.

Publication uses the package's event publisher and durable outbox after an
explicit destination is configured. Preserve the prepared payload and reuse its
source and event identifier on retry. The first acceptance sends only the selected
observed event. Replies must retain the resulting canonical TC message reference.

```sh
teamcomms-connect --config /path/to/private/program-config.json \
  publish-event /path/to/private/observed-event.json
```

The package derives the canonical message UUID from `source` and `event_id`,
persists the exact body before publication, and rejects a changed body on retry.

Existing Mattermost configuration is in
`/opt/swf-monitor/config/env/production.env`: `MATTERMOST_URL`,
`MATTERMOST_TEAM`, `MATTERMOST_CHANNEL` and the private `MATTERMOST_TOKEN`.
The separate `mattermost-live` notice plugin prefers `EPICPROD_LIVE_TOKEN`
and reads its channel from SysConfig `epicprod_live_channel`. These settings
describe existing publishers; the TC connector's destination is selected
explicitly. Its account-bound program/connector token authenticates to the
existing public TC endpoint. The introspection service credential is not a
publication credential.

The commissioned live-feed destination is `epicprod-live` on
`chat.epic-eic.org`, team `main` (`cxdw3uij5irc8xhk95pk9mnq3h`), channel
`578q7d98h7gnprya6qt8we9fta`. The existing live-feed bot is `epicprod`.
Keep its credential on the owning SWF host; configure the TC bridge with the
explicit channel ID. Resolving this destination does not post or join a channel.

The bridge's private files live in
`/data/wenauseic/.config/teamcomms/swf-events/`. `program.json` and
`connector.json` use the public TC URL, host `swf-testbed`, separate private
state directories and `greeting=false`. Their `token_file` values name
`program-token` and `connector-token`, respectively, issued by devcloud with
the corresponding service kind. `live-bot-token` is a local private copy of
`EPICPROD_LIVE_TOKEN`; no Mattermost token is transferred to another host.

`mattermost.json` selects the live-feed route:

```json
{
  "url": "https://chat.epic-eic.org",
  "token_file": "/data/wenauseic/.config/teamcomms/swf-events/live-bot-token",
  "routes": [{
    "channel_id": "578q7d98h7gnprya6qt8we9fta",
    "name": "epicprod-live",
    "topics": ["swf-observed-events"],
    "inbound_audience": {"topics": ["swf-observed-events"]}
  }]
}
```

Selected AI sessions subscribe explicitly to `swf-observed-events`. The bridge
registers one TC session for its channel and suppresses its own bot posts and
source-channel reflections. Mattermost provenance is connector-reported and
does not replace the authenticated TC connector author.

`swf-teamcomms-mattermost.service` runs the bridge as `wenauseic`, using the
deployed release's Python and the private connector configuration. Install and
start the unit after the package and devcloud service tokens are ready. Retain
its state directory through restart for replay and publication recovery. The
existing SWF notice publisher and bot remain separately managed.

### Backend environment

Set these values in the production environment:

```
SWF_TEAMCOMMS_ENABLED=True
SWF_TEAMCOMMS_PUBLIC_HOST=epic-devcloud.org
SWF_TEAMCOMMS_INTROSPECTION_URL=https://epic-devcloud.org/prod/teamcomms-auth/introspect/
SWF_TEAMCOMMS_SERVICE_TOKEN_FILE=/path/to/private/service-token
```

The credential is transferred through SSH and stored outside Git. User
credentials are managed on the existing devcloud account page. Missing
configuration denies TC access. The route defaults to disabled.

The dependency is pinned to a reviewed teamcomms-ai Git revision and installed
non-editable in the shared development virtualenv. A full monitor deployment
copies that environment, applies migrations and installs the Apache route.
Requirements changes must be installed in the shared environment before
deployment. Coordinate the environment update and full deployment with other
SWF sessions because the environment is shared. The existing deployment script
also freezes local sibling package trees.

Verification of the SWF installation covers the public cookie/token paths,
CSRF and request binding, authentication revocation during streams, reconnect
replay, and selected live Claude/Codex sessions. TC's package-level suite has
separate approval requirements in teamcomms-ai/AGENTS.md. Package fixture results
do not establish live connector acceptance.

## SWF Inflight

The System menu groups TeamComms, SWF Pouch and **SWF Inflight**. Inflight opens
`https://epic-devcloud.org/prod/teamcomms/inflight`, using the configured public
host and existing devcloud login. The complete subtree is already proxied; no
new Apache path, authentication reference format or browser credential is needed.

Enable `teamcomms.inflight.apps.InflightConfig`. The pinned package's
`0001_initial` and `0002_guards` migrations create work, dependency and immutable
retry-receipt records in the monitor database, ordered after Entries integrity,
Comms and service identity migrations. Deployment creates schema only.

Authenticated collaboration members receive `inflight:read` and `inflight:write`
alongside the existing component scopes. These permissions remain independent
of production-action rights. The package additionally checks owner/executor or
administrator authority for each mutation. A work item retains a stable
participant owner across session disconnection. A named successor must accept
an ownership handoff; executor assignment is separate. Revisions and ownership
generations reject stale updates. Completion requires an outcome and evidence;
closed work needs explicit reopening. Referenced resources are associations,
not enforced reservations or permission to mutate production systems.

Under the existing public TC prefix, `/api/inflight` creates and lists work,
`/api/inflight/read` reads current or fixed revisions, `/api/inflight/mutate`
handles explicit state/ownership changes, and `/api/inflight/changes` supplies
bounded attributed history. The corresponding connector tools are `create_work`,
`list_work`, `get_work`, `mutate_work` and `get_work_changes`. Mutations carry a
stable operation UUID; retries keep the exact request and return its original
outcome. Work uses the shared editor, source rendering and local draft recovery.
Pouch's **Create work from this revision** preserves its saved source reference.

Full deployment installs the pinned package, applies both Inflight migrations
and activates the existing ASGI worker. Bounded acceptance checks the rendered
System links, one explicitly created commissioning work item, source revision,
completion evidence and saved history. No automatic TJAI task import, work
execution, connector restart or additional native commissioning is required.

## Claims, custody and guarded deployment

Inflight migrations `0003`–`0005` add explicit offers, atomic multi-resource
claims, retained custodians, immutable retry receipts and guarded-run records.
The existing application and `inflight:read/write` scopes cover these APIs.
No new authority, credential issuer, Apache route or automatic execution is added.

An eligible authenticated participant claims offered work and its complete
resource set. The accountable owner is retained. Renewal and completion check
ownership generations; completion requires evidence and releases the set.
Expiry shows lost contact and keeps resources held. A holder must stop and
release its work, or the owner must record stopped-work evidence, before a new
claim can acquire those resources. Custody stays assigned while idle and can
change only through an accepted transfer. Work descriptions may associate
resources without reserving them; explicit claims establish reservations.

`scripts/teamcomms-guarded-deploy.py` is an optional protected entrypoint:

```sh
sudo /opt/swf-monitor/current/.venv/bin/python \
  /usr/local/libexec/swf-teamcomms-guarded-deploy.py \
  --claim-id UUID --generation N --check
# For an authorized deployment, replace --check with branch infra/baseline-v43.
```

It runs the TC guard as root, takes all configured canonical resource locks,
validates and renews the caller's existing claim, and invokes the committed
release's normal deployment script. It records a durable command admission and
confirmed stop; the caller then records completion or stopped/released work.
`--check` uses the same admission path for one read-only checkout revision read.
An unconfirmed prior run or failed renewal denies/stops protected work.
A TC service outage therefore interrupts guarded work; recovery must inspect
that work before recording stopped-work evidence and claiming again.

Explicit host setup uses `provision_work_resource` to register the monitor
project, verified checkout aliases and deployment service, with a named active
custodian and `local_flock` protection on `swf-testbed`. Schema migration alone
does not provision resources. The host command requires trusted database
administration access and records the selected custodian as the audit identity;
it does not grant that participant remote administration privileges.

Install the reviewed committed wrapper after deployment with `sudo install -o
root -g root -m 0755 scripts/teamcomms-guarded-deploy.py
/usr/local/libexec/swf-teamcomms-guarded-deploy.py`. This explicit host setup keeps
the privileged entrypoint outside release ownership changes.

Root-private files live in `/etc/swf-teamcomms` (0700):
`connector.json`, `guard.json` and `program-token` (0600). The token is a local
copy of the existing account-bound SWF program token, issued by devcloud. Keep
both copies synchronized on rotation. It is never a service-introspection key.
Connector fields are `url`, `token_file`, `host=swf-testbed`, `state_dir` and
`greeting=false`. Guard fields are `host=swf-testbed`,
`lock_dir=/var/lib/swf-teamcomms/guards` and the explicit
`resource_ids` allowlist, covering the checkout and deployment service.
Every cooperating invocation uses the same root-owned lock directory and IDs.
These paths are outside the release tree, whose ownership the normal deployment
script resets to the application account.

Coverage is exclusion between cooperating foreground invocations of this
entrypoint on this host. Direct deploy scripts, raw shell commands, escaped
children and unrelated entrypoints remain outside it. Reservations do not confer
production rights, and the wrapper does not replace commit/push, host coordination
or the standard frozen-tree checks. Do not run privileged children beneath an
unprivileged guard: it could not stop their process group on authority loss.

## Topical Capcom

The System menu opens `/prod/teamcomms/capcom` within the existing authenticated
subtree. Add `teamcomms.capcom.apps.CapcomConfig` and grant ordinary members
`capcom:read` and `capcom:write`, independently of production rights. The package's
Capcom `0001_initial`, `0002_integrity` and `0003_conversationread` migrations
create topics, notices,
explicit decisions, personal read/follow state, immutable mutation receipts and
per-session presentation policies. Existing Inflight and component apps remain
installed; migration creates schema only.

Topics reference current Inflight work, fixed document versions, selected Dialog
records and existing exact-topic Comms conversations. Private message access stays
with its original audience. Events and sampled state carry distinct labels and
observation times; old blockers cannot replace current task state. Reading, marking
a topic read, native receipt, model consideration and decision resolution are
separate operations. There is no automatic source import, watcher subscription,
Mattermost post or production action.

Receiver attention controls require explicit `attention_controls=true` in the
selected connector configuration and a receiver advertising `attention-v1`.
Session-owned policies choose immediate, recorded-only or batched machine routine
notifications, with optional quiet deadlines. Deferred and coalesced dispositions
remain visible beside independent transport receipts. Alarms, human instructions,
conversations and offers bypass routine filtering. Existing connectors keep their
current delivery behavior until explicitly enabled; runtime/TJAI settings and the
live-feed bridge are preserved.

Package installation, mandatory host checks and full deployment precede bounded
public navigation/topic/reference checks. A commissioning pull session may exercise
publication and attention bookkeeping without a model or native client launch.
