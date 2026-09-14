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
| subject | Immutable remote account ID, or ai: followed by that ID |
| username, name | Verified account lookup key and participant display name |
| kind | human or ai |
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
