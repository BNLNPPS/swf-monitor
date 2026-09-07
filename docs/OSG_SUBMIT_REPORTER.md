# OSG submit host reporter

A reporter on the OSG submit host (osgsub01) delivers the submission
side of ePIC's OSG production to swf-monitor: what the pilots are
allowed to land on, and what the pool currently offers them. It exists
because that state is only knowable on that host. The submit
description is the operative control over where pilots may run, and
swf-monitor cannot read it: ssh to the submit host is refused except
through the facility gateway with agent forwarding, so nothing on the
monitor host can poll it. The reporter pushes instead.

Until it runs, the ePIC queues page renders the exclusions from a list
declared in `swf_epicprod.osg_exclusions` and reconciled by hand with
`swf-epicprod scripts/check-osg-exclusions.py`. That page labels those
sections as declared rather than live, because a copy can be wrong.
Replacing that label is the reporter's first purpose.

The agent is a standalone script in the house pattern — standard
library only, local state file, periodic run, HTTPS delivery to a
swf-monitor REST ingest under a per-host token — and it is read-only
with respect to harvester, HTCondor, and the submit descriptions.

## Functions

Each run posts one record.

| Function | Source on the host | Fields delivered |
|---|---|---|
| Exclusions in force | the submit description of each OSG queue | per queue: the excluded sites, the excluded site-and-node pairs parsed from the `Requirements` clause, the file's modification time |
| Submission shape | the same files | requirements, resource requests, job duration category, pilot wrapper and its version |
| Queue-to-file mapping | `/opt/harvester/etc/panda/panda_queueconfig.json` | which queue uses which submit description, worker limits, push or pull |
| Pool admission | `condor_status` against the pool collectors | total slots; slots the queue's requirements admit; slots the exclusions remove, by site and node |
| Pool composition | the same | slots per site, operating system, user-namespace and CVMFS availability |
| Submission health | `condor_q` on the local schedd | workers idle, running and held per queue, with held reasons |

Every failure to read a source is delivered as a field, never dropped;
an unreachable swf-monitor buffers records locally and posts the
backlog on the next run. A silent reporter is visible as the record's
age, which the page shows beside the exclusions.

## Why the exclusions are worth reporting rather than declaring

A node exclusion names a site and a node together, because node names
recur across sites: the pool advertises bare names such as `compute05`,
`n358` and `fc20621` alongside fully qualified ones, and a name alone
would ban a healthy machine elsewhere. That pairing is expressed in a
`Requirements` clause, which is easy to write wrongly and impossible to
verify from the monitor. Reporting it back closes the loop: the page
shows what the submit host is actually enforcing, and disagreement with
what anyone believes is enforced becomes visible rather than latent.

The pool-admission fields make the same point quantitatively. The
exclusions are only meaningful against what the pool offers, and that
changes daily; a count of slots removed is the measure of what an
exclusion is doing today.

## Access inventory

Verified on 2026-09-07 for the account that will run the reporter
(uid `wenauseic`, group `eic`):

Available without additional privilege:

- Submit descriptions: `/var/data/atlpan/harvester_common/*.sdf`,
  world-readable, owned by `atlpan`.
- Harvester queue configuration:
  `/opt/harvester/etc/panda/panda_queueconfig.json`, world-readable.
- HTCondor client tools (`condor_q`, `condor_status`, version 25.0.12),
  giving the local schedd and the pool collectors.
- Outbound HTTPS to swf-monitor on `pandaserver02.sdcc.bnl.gov`,
  verified at 13 ms.
- Scheduling: cron is permitted for the account (`/etc/cron.deny` is
  empty, no `cron.allow`, no crontab today).
- `/var` has 180 GB free; the root volume is 12 GB at 71 per cent, so
  state and buffer files live under `/var`.

Constraint worth stating because it is not obvious: the host's
`python3` is 3.6.8. The reporter is written to that version and to the
standard library alone, so it neither needs nor gets a virtual
environment.

Privileged access: the account holds passwordless sudo. Nothing in the
reporter's read path requires it. It is used at install only, to place
a systemd unit if the cron form is replaced later.

## Delivery

The reporter posts to `api/host-reports/<host>/` on swf-monitor,
authenticated by a per-host token held in the reporter's environment
file at mode 600. The ingest stores the record in the cached-product
store under a key naming the host, so the record needs no table of its
own: the store already holds a keyed JSON value with the time it was
built, which is exactly a reporter's record and its freshness.

The ePIC queues page reads that record when it is present and falls
back to the declared list when it is not, stating which of the two it
is showing and how old it is.

## Install

1. The script and its environment file on the submit host, mode 600 on
   the environment file.
2. A cron entry for the account, on the reporting interval.
3. The per-host token issued in swf-monitor.

No change to harvester, to HTCondor, or to any submit description is
part of installing the reporter. Every install action on that host is
announced and recorded in the action stream before it is taken.

## Related

- [PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md) — the reporter
  on the PanDA server host, whose pattern this follows.
- `swf-epicprod` `docs/OSG_SUBMISSION.md` — the submission path, the
  levers, and the exclusions this reports.
- [CACHED_PRODUCTS.md](CACHED_PRODUCTS.md) — the store the record
  lands in.
