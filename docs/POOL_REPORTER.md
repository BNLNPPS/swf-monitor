# Batch pool reporter

A PanDA queue's workers wait in somebody's batch pool, and the wait is
decided there rather than in PanDA. Two figures decide it: what share of
the pool is already claimed, and how many jobs are idle ahead of ours.
Neither is in any PanDA record. The pool's collector is the only source
and it answers the HTCondor protocol, so a reporter asks it and posts
the answer to the host-report ingest, on the pattern of the submit-host
and server-host reporters ([OSG_SUBMIT_REPORTER.md](OSG_SUBMIT_REPORTER.md),
[PANDA_SERVER_REPORTER.md](PANDA_SERVER_REPORTER.md)).

The occasion is a standing question: workers at the E1 queues have taken
twenty minutes and more to start, and the answer each time has been that
the shared pool was busy. The pool reading turns that from an answer
somebody gives into a number the page shows.

## What is read

`scripts/pool-reporter.py` queries a collector and delivers one record:

| Field | Meaning |
|---|---|
| `slots` | total, claimed, unclaimed, and the full state histogram |
| `cores` | cores in the pool and cores claimed |
| `claimed_fraction` | the fullness the queue pages show |
| `queue` | across every schedd in the pool: jobs running, idle and held, with the busiest schedds named |
| `machine_domains` | the domains of the pool's machines, bounded, which is how a queue is matched to its pool |

A query that fails is delivered as an error field rather than dropped.

The pool it reads by default is `bnl-scdf`, the SCDF shared pool
(collector `condorspool01.sdcc.bnl.gov:9665`), which the E1 and BNL
queues run in. The reporter takes `--pool` and `--collector`, so a
second pool needs no new code.

## Where it runs

On the monitor host, unlike the other reporters: the SCDF collector
answers pandaserver02 directly, so no foreign host is involved. It runs
in its own virtual environment, `/opt/swf-monitor/shared/pool-venv`,
because the HTCondor bindings are large and belong to this reporter
rather than to the web tier's dependencies. The web tier never speaks
the HTCondor protocol and never talks to a collector in a request path.

Delivery is `POST /api/host-reports/bnl-scdf/` with a reporter token,
the same ingest and the same cached-product store the other reporters
use, so the record carries its own freshness and an absent record is a
fact a page can state.

## Which pool a queue draws on

Nobody declares it, and a declared table would go stale. It is derived
from the queue's own finished jobs: a job record names the worker node
it ran on, the node's domain says whose pool it was in, and a queue
whose running jobs are overwhelmingly on one pool's domains draws on
that pool. The OSG queues are matched more directly still — the submit
host reports which queues it submits.

Two rules keep the derivation honest. Only jobs that started are
counted, because a job that never ran carries the harvester host rather
than a worker node, and those rows would put every queue in whatever
pool the harvester sits in. And a queue is attributed only on a strong
majority, so a stray node elsewhere does not carry it into the wrong
pool. A queue whose workers run in a pool we cannot see — NERSC, GREX,
the Google cloud queue — is attributed to nothing and its pool fields
are absent, which is the honest rendering.

The map is a cached product rebuilt every six hours
([CACHED_PRODUCTS.md](CACHED_PRODUCTS.md)); where a queue's workers run
changes over days, not minutes.

## Where it shows

- The ePIC queues page: the pool's fullness as a percentage and the
  idle jobs ahead, per queue, sortable.
- The queue detail page: a Batch pool card with the claimed share,
  slots and cores, the queue ahead across the pool's schedds, and the
  reading's age in the header. The attribution evidence stays in the
  record (the queue page showed it until 2026-09-20; it is the
  developer's provenance, not the operator's information).

Both read the stored record. Neither builds it.

## Pools we cannot read

A pool whose scheduler answers no collector of ours, NERSC Perlmutter
first, is read by its own jobs. The pilot launch production operations
publishes for the site (swf-epicprod docs/NERSC_PERLMUTTER.md, the pool
sample) runs `squeue` and `sinfo` on the worker node before the
container starts, once per worker, and writes `pool-sample.json`
(schema `pool-sample/1`): the machine's and the allocation's running and
pending jobs and nodes, the allocation's oldest pending age, and the
partition's node states. The payload carries the sample in its report
(EPICPROD_PAYLOAD.md item 14), the pilot ships the report as job
metadata, and the monitor takes the newest sample among the finished
jobs of the pool's queues in the last three days as the pool's reading,
a cached product (`pool_sample:v1:<pool>`, five minutes) so no page
reads the PanDA record. Such a pool is declared in `pools.py` with
`source: sample` and the queue-name prefix that identifies its queues;
attribution is by name, since nothing else runs there.

The reading renders in the same card and columns as a collector's:
claimed fraction and slots become allocated and total nodes of the
partition; the queue ahead is the machine's pending jobs and the nodes
they ask for; a row the collector pools do not have shows our own jobs
as the batch system counts them. The card names the sample's node and
the job that carried it, and its age is the sample's, not the store's.
Until a job of the pool's queues has carried a sample, the card says so.
The sample arrives with the jobs, so a pool that has stopped giving us
nodes stops reporting: the Workers card beside it is what says how long
we have been waiting.

## The queue's own workers

The pool reading says how full the pool is; the other half of the wait
question is what that costs at one queue, and that half is in a record
every queue has whether or not its pool is one we can see: the
harvester's worker table in the PanDA database. `monitor_app/workers.py`
reads it into a cached product per queue
(`harvester_workers:v1:<queue>`, two minutes) and the queue detail page
shows it as a Workers card beside the Batch pool card: the workers
submitted and not started, with the age of the oldest; the workers
running; the workers started in the last day and in the last hour; the
median and 90th-percentile wait from the harvester's submission to the
batch start over the workers that started in the last day; and the
workers that ended in the day by their final status.

At a pool we cannot read, NERSC Perlmutter above all, this card is the
whole reading: on 2026-09-19, 100 workers waiting, the oldest for 21
hours, a median wait of ten hours, which is the answer to "is something
wrong at Perlmutter" (nothing of ours: the machine is full).

## Install

1. The virtual environment with the HTCondor bindings.
2. The environment file at mode 600 naming `SWF_MONITOR_URL` and
   `SWF_REPORT_TOKEN`.
3. A cron entry on the reporting interval.

## Related

- [OSG_SUBMIT_REPORTER.md](OSG_SUBMIT_REPORTER.md) — the submit host's
  reporter, whose pool block covers the OSG side.
- [SNAPPER_PLATFORM.md](SNAPPER_PLATFORM.md) — the platform component
  the reading joins, so pool fullness is recorded history rather than
  only a current number.
- [CACHED_PRODUCTS.md](CACHED_PRODUCTS.md) — the store the record and
  the attribution map live in.
