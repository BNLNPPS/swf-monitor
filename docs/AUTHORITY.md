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
GitHub login, origin, `eic`, `rights`, and last sign-in, and it is where
`rights` is set. `eic` is display-only there: it is observed from GitHub, and
a hand edit would be overwritten at the next sign-in.

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

Two rules, both keyed on the authority the requester holds, computed from the
expression above and never from `eic` alone.

**Methods.** `POST`, `PATCH`, `PUT` and `DELETE` require authority. `GET`,
`HEAD` and `OPTIONS` pass on sign-in alone. The safe-method set is the
complement, so a verb that changes state is covered by construction.

**The operations agent.** Reaching `epicprod_ops_agent` requires authority
whatever verb triggered it, because one of the two supported external-safe
trigger shapes is a `GET` page view that drops a message as a side effect
([EPICPROD_OPS_AGENT.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_OPS_AGENT.md)
§ *Building a new capability*). The method gate alone would let that shape
through.

Authority is declared per `msg_type` against the agent's `KNOWN_TYPES`:

- **Open to any signed-in account**: `fetch_payload_log`,
  `sync_epicprod_inventory`. Both are bounded per-object retrievals that
  serve the requesting viewer's own page.
- **Internal only**: `health_ping`, `shutdown`, refused to any externally
  originated request. `shutdown` stops production; organisation membership is
  not the right test for it.
- **Requires authority**: every other type.

The publish path takes the authority basis as a required argument, so a call
site that does not state its basis raises instead of publishing, and a test
asserts that every name in `KNOWN_TYPES` resolves to a declared authority, so
the catalog and the policy cannot drift apart.

Internal callers are unaffected by construction. Cron, the `catalog_sync`
chain steps, and other agents publish inside the perimeter, in process or
through `enqueue-ops-message.py`, without crossing the web tier. What is
gated is an externally originated request, not the `msg_type` itself; gating
the type would stop the nightly chain and would put an organisation
requirement on functions used outside the monitor.

Two mechanisms that look like candidates for the gate cannot carry it.
`log_epicprod_action` runs at or after execution and its contract is that it
never raises, so a gate there would fail open. `ACTION_DEFAULTS` is not
authoritative either: `epicprod_logging.py` computes the known actions as the
catalog plus any action observed in the log, which means the code expects
call sites that were never declared — exactly the forgotten action a gate
must catch.

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
