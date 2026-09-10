# Authority — read, and act

Any GitHub account can sign in to `epic-devcloud.org` and reach swf-monitor
through swf-remote. As the monitor gains functions that change the system,
reading and acting separate:

- **Read** needs a signed-in account and nothing more, at every level.
  Collaborators outside the `eic` GitHub organisation, demonstrations, and
  non-member viewers keep the monitoring view they have today.
- **Act** needs authority: membership of the `eic` GitHub organisation, or
  rights granted here.

Every mechanism below is default-deny. An unknown account, an unwritten
field, and an undeclared capability all resolve to refused, so a function
whose protection was overlooked is closed rather than open.

Enforcement is upstream, in swf-monitor, at one point. It covers both faces:
external users arriving through the swf-remote tunnel and users reaching
`pandaserver02` directly. swf-remote does not enforce; it resolves
organisation membership and writes it here.

## The attribute

Authority is held per account as `UserPreference.prefs['authority']`
(`monitor_app/models.py`, table `user_preference`, keyed by username string).
The key matches the `X-Remote-User` identity the tunnel carries, and the
record needs no Django account: an account created later already carries what
was written for the name.

| Field | Values | Written by |
|---|---|---|
| `eic` | unset, true, false | swf-remote's sign-in sweep, and nothing else |
| `rights` | unset, `read`, `basic`, `ops` | a person, in the User admin page, and nothing else |
| `github` | the GitHub login the membership was observed for | the sign-in sweep, beside `eic` |

```
may act  =  rights != 'read'  and  (eic is true  or  rights in ('basic', 'ops'))
```

Provenance is carried by which field a value sits in, not by a set-by marker
inside one field. The consequences are the point of the split:

- The sweep maintains `eic` and nothing else, so a grant made here cannot be
  undone by a later sign-in.
- A person maintains `rights` and nothing else, so a member who leaves the
  organisation loses authority at their next sign-in, with no one acting.
- `rights: read` is an explicit veto that outranks `eic` — the way to stop a
  member of the organisation from acting.
- `rights: unset` is the ordinary state of a member, who draws authority from
  `eic` alone.
- An account with no GitHub identity holds `eic: unset` rather than false.
  Asserting a GitHub fact about an account that has no GitHub identity would
  be untrue, and it is what forced the two fields apart.

Each level names what the person may do rather than what they may not,
because they read it on their own account page: `read`, not "none". `basic`
rather than "user", which is already taken three times over by the Django
model, `UserPreference`, and `X-Remote-User`.

### The PAC role

| Field | Values | Written by |
|---|---|---|
| `pac` | unset, true | a person, in the User admin page, and nothing else |

A physics analysis coordinator (PAC) may set request priorities. The role
is a flag beside the rights ladder, not a rung on it, so an operations
account can hold it too and a PAC need not administer accounts:

```
may set priority  =  may act  and  (pac is true  or  rights == 'ops')
```

The physics coordinator's requirement (2026-09-10): with the pages open to
the collaboration, priority must be settable by coordinators only, since
anyone might otherwise raise their own request in good faith. Every
priority control renders only for accounts that may set it (the template
context variable `may_set_priority`), and the priority endpoints refuse a
person who may not with the `PRIORITY_REFUSAL` text, naming the User admin
page as the way to be granted the role. `may_set_priority`, `is_pac`,
`set_pac` and `PRIORITY_REFUSAL` live in `monitor_app/authority.py`; the
flag is written through the same endpoint as `rights`, `POST
/api/user-rights/ {"username": ..., "pac": true|false}`, so the sign-in
sweep's endpoint cannot touch it either.

`monitor_app/authority.py` is the whole namespace: `get_authority`,
`all_authority`, `may_act`, `is_ops`, and the two setters `set_eic` and
`set_rights`, each writing its own field only. `UserPreference.set_pref`
refuses the reserved `authority` key with a `ValueError`, so no general
preferences surface can become a path to privilege.

## The write endpoints

One endpoint per field. That is what makes the split structural rather than a
convention: the endpoint every sign-in goes through cannot write rights at
all, so no sweep bug and no malformed body can reach them.

```
POST /api/user-authority/
     {"username": "...", "authority": {"eic": true, "github": "..."}}
  → {"username": "...", "authority": {"eic": true, "rights": null,
                                      "github": "...", "eic_at": "..."}}

POST /api/user-rights/
     {"username": "...", "rights": "basic"}
  → {"username": "...", "authority": {...}, "may_act": true}
```

`github` travels nested inside `authority`, which is the contract; the field
is also accepted beside it. `eic: null` and `rights: null` clear their fields
to unset. Each endpoint refuses the other's field with 400. JSON in, JSON
out, never a redirect, so both survive the swf-remote hop
([EXTERNAL_ACCESS.md](EXTERNAL_ACCESS.md)). Through Apache the paths carry
the `/swf-monitor/` prefix.

`eic_at` is stamped by every membership write. The sweep runs at sign-in, so
it is the moment the person last signed in — the only sign-in time this side
can know, since a proxied request never opens a session here.

The backfill writes both fields and so uses both endpoints: membership for
the GitHub-linked accounts through the first, `rights: basic` for the
grandfathered non-members and for the accounts with no GitHub identity
through the second.

Writing authority is not a user capability. The membership endpoint requires
the swf-remote authority writer — the service identity `swf-remote-authority`
presented as `X-Remote-User` over the localhost tunnel — or an authenticated
superuser on this side. A request carrying a signed-in person's identity is
refused with 403 whatever that person's authority, so the endpoint stays
closed even if it is ever reachable through the proxy. The rights endpoint
additionally accepts the people who may use the User admin page: staff, and
accounts holding `ops`.

That the identity is a service name rather than a person is what has to hold,
and it rests on three properties of the swf-remote side: the sign-in sweep
presents that identity, the proxy sets `X-Remote-User` itself and never
forwards a client-supplied value, and `swf-remote-authority` cannot be
claimed as an account name there. The third was a real opening: sign-up
adopts the GitHub login as the username, so a collaborator registering that
name on GitHub would have arrived wearing the service identity. It is closed
by an account-name blacklist covering the service names.

There is at present no route to either endpoint through the proxy — its
catch-alls cover only `panda/` and `pcs/`, and no explicit entry names them.
The identity check is what stays true if an `api/` catch-all is ever added,
which would otherwise turn every signed-in user into someone who can write
their own privilege.

## The two pages

Both live here, beside the data, and so serve the internal face as well as
the external one.

**A person's own account page** states their standing in plain language:
whether they may act, which GitHub login was checked, and, when they may not,
the joining procedure. What the person reads is what drove the naming of the
levels, so the page is descriptive rather than punitive.

**User admin**, under the System menu, visible to staff and to accounts with
`rights == 'ops'` — the first live use of `ops`. It lists accounts with their
GitHub login, origin, `eic`, `rights`, the PAC role, and last sign-in, and it
is where `rights` and the PAC role are set. `eic` is display-only there: it
is observed from GitHub, and a hand edit would be overwritten at the next
sign-in.

### The refusal

Enforcement is on this side, so the refusal a non-member sees is rendered
here. GitHub has no self-service join: an organisation owner has to issue the
invitation, so a bare 403 leaves the person with nowhere to go. The refusal
names the GitHub login that failed the check, states that reading is
unaffected, and links the joining procedure:

> Your GitHub account `<login>` is not a member of the `eic` organization.
> ePIC production monitoring requires membership for actions against the
> production system; reading monitoring information does not. The joining
> procedure is on the ePIC Software & Computing Get Started page,
> https://eic.github.io/documentation/getstarted.html — see "Join GitHub".
> Once you have been added, sign in again and your account will work.

The procedure is linked, not restated. The published sources disagree on the
contact address — the Get Started page gives one list address and the `eic`
organisation profile another — and a copy here would be a third that goes
stale independently.

## Enforcement

One rule: **a person who writes must hold authority.** It is applied at one
point on each of the two surfaces a person can write through, and it is
keyed on the expression above, never on `eic` alone.

**The REST and page surface.** `AuthorityGateMiddleware`
(`monitor_app/middleware.py`) runs after the tunnel middleware, so a request
that carries a person — a browser session, or the identity swf-remote
forwards as `X-Remote-User` — is authenticated by then. On `POST`, `PATCH`,
`PUT` or `DELETE` that person must `may_act`; otherwise the request is
refused with the joining procedure, as JSON on the API paths and as a page
elsewhere. `GET`, `HEAD` and `OPTIONS` pass on sign-in alone. Signing in and
out, changing a password, and the two authority endpoints — which admit only
the swf-remote service identity or an administrator — are exempt.

A request carrying **no person** — machinery on a service token, or nothing
— is left to the view's own authentication, exactly as before. The gate adds
protection for people and removes nothing else. This is what keeps the
testbed running: the agents' log posts (some ten thousand a day, open by
design), the heartbeats, the host reporters, the episode builder, the
production-record ingest and the registrar all authenticate by token and
carry no person. The `02:47 catalog_sync` chain publishes in process and
never crosses the web tier at all.

**The MCP surface.** MCP is a separate service calling the service layer in
process, so a Django middleware never sees it. Each of the fourteen tools
that change the system carries `@requires_authority` under its
`@mcp.tool()` (`monitor_app/mcp/common.py`): a tunnel caller is a person
and must `may_act`; a bearer-token caller carries no person and passes, as
on the REST face. `AUTHORITY_GUARDED_TOOLS` declares the fourteen, and
`tests/test_authority.py` asserts that the guarded set and the declared set
are identical, so a write tool cannot be added without being declared and a
declared name cannot go stale.

**Observe, then enforce.** The SysConfig knob `authority_enforce` (false
until set) decides whether the gates refuse or only observe. While false,
every refusal a gate would have made is logged as a warning naming the
method, path and person, and the request proceeds; the rule is proven
against live traffic before it bites. Setting it true on the System page
turns refusal on with no deploy.

### The tunnel identity fallback

`TunnelAuthentication` used to answer a localhost request carrying no
`X-Remote-User` with a generic `swf-remote-proxy` user, before any token was
read. A valid token, a garbage token and no credential at all were all that
user. The hourly production-record writer, whose default endpoint is the
localhost face, ran under it: 65 PCS rows and, in one week, 24 production
action records including task submissions were attributed to it. The
fallback is removed; a localhost request without the header falls through to
session or token authentication. swf-remote names its service identity
explicitly on the calls that need one, all of them reads.

### What cannot carry the gate

`log_epicprod_action` runs at or after execution and its contract is that it
never raises, so a gate there would fail open. `ACTION_DEFAULTS` is not
authoritative either: `epicprod_logging.py` computes the known actions as the
catalog plus any action observed in the log, which means the code expects
call sites that were never declared — exactly the forgotten action a gate
must catch. And a gate keyed on the service token would exempt six people
who hold one and, since the agents authenticate with a person's token, would
either exempt that person or stop every agent.

### Tokens

Six people and four services hold API tokens. A person's token is that
person: on the external face the proxy forwards `Authorization` and sets
`X-Remote-User` from the session, so the person is present and gated; on the
internal face a bare token carries no person and is governed by the view's
authentication, as any API token is. Token issuance is the control there.
The agents authenticate with a person's token; giving the machinery its own
identity is a separate cleanup.

## Rollout

The order matters: enforcement before backfill denies every write for
everyone.

1. **Store, inert** (swf-monitor). The namespace, the two setters, the
   reserved-key guard, the endpoint, and the two pages. Nothing enforces.
2. **Resolve and backfill** (swf-remote). `read:org` added to the GitHub
   scope, membership resolved at sign-in and rewritten on every sign-in, and
   every existing account populated: `eic` for the GitHub-linked accounts,
   `rights: basic` for those with no GitHub identity, whose access is
   established inside the BNL perimeter through the account sync.
3. **Enforce** (swf-monitor). The method gate and the agent gate, after every
   account carries its field.
