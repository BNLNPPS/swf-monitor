# Cached products — uniform long-build caching

A cached product is any expensive-to-build, read-often result: an
aggregation over a large table, a remote-service rollup, a rendered
fragment. This is the one pattern for serving them; new caching of a
long build uses this mechanism rather than adding another hand-rolled
variant.

## Contract

- **A request always serves the stored product immediately**, stamped
  with its build time. Nothing expensive builds in the request path —
  the generalization of the no-remote-calls-in-render rule.
- **Staleness rebuilds behind the response.** A request that finds the
  product older than its TTL returns it anyway and triggers one
  background rebuild; `building_since` on the store row is the
  cross-worker lock, so concurrent requests never stampede. A running
  build also holds a Postgres session advisory lock for its key, which
  the database releases when the building process's connection closes.
  A slot whose builder died, as a process recycle at deploy kills a
  background thread, is therefore abandoned as soon as no live session
  holds that lock (after a 30 s grace covering the thread's start), and
  every reader treats it as absent: the next request takes it over and
  rebuilds. A slot older than `BUILD_LOCK_TIMEOUT_SECONDS` (600 s) is
  abandoned whatever else is known. No product stays stale behind a dead
  lock.
- **Explicit update rebuilds synchronously.** The uniform Update button
  passes `refresh=1`; the user chose to wait, and gets fresh data back.
- **The first-ever fill builds synchronously** — there is nothing to
  serve; it happens once per key.
- **Failures surface.** A broken builder logs with its key and clears
  the lock; it never presents as silently stale data.
- **Only the deployed release stores** (Torre Wenaus, 2026-10-22). The
  store is the shared database, so a reader running from a development
  tree would otherwise rebuild a stale product with the code under edit
  and the production face would serve it; on 2026-10-22 a half-edited
  tree stored a comp snapshot without its tasks. A process whose code is
  not under the release root (`SWF_RELEASE_ROOT`, default
  `/opt/swf-monitor`, compared through symlinks) serves the stored
  product however old, never starts a rebuild, and builds only when
  nothing is stored or on an explicit refresh, returning that value
  without writing it. A builder that keeps state of its own in the
  store writes it only under the same rule (`cached_product.stores()`).

## Mechanism

- Store: the `CachedProduct` row (`swf_cached_product`) — key, JSON
  value, `built_at`, `build_seconds`, `building_since`.
- API: `monitor_app.cached_product.get_product(key, builder,
  ttl_seconds, refresh=False)` returning `{value, built_at,
  age_seconds, refreshing, built_now}`.
- Executor: pure-database builders run in a background thread here.
  Credentialed or very heavy builds belong on the prod-ops agent with
  an SSE completion push (`swf-epicprod/docs/EPICPROD_OPS_AGENT.md`) —
  the agent is the heavy half of this same serve-cached-always pattern.
- UI: DataTables pages get the freshness chip for free — return
  `create_response(..., extra={'product_built_at': ...,
  'product_age_seconds': ..., 'product_refreshing': ...})` and
  `_datatable_base.html` shows "Data as of HH:MM · Update". Non-table
  pages render the same fields from the `get_product` result.

## Products on this mechanism

| Key | Builder | TTL |
|---|---|---|
| `compute_efficiency:v1` | retained daily successful/ended core-hours (`monitor_app.compute_efficiency`), refreshed hourly with monthly full reconciliation | 3600 s |
| `panda_errors:v2:<days>:<user>:<site>:<source>` | PanDA error summary aggregation | 300 s |
| `panda_tasks_window:<days>` | PanDA tasks list full-window aggregation | 120 s |
| `prod_hub_corun_counts` | corun-ai assessment/narrative counts | 600 s |
| `evgen_convention:v2` | EVGEN paths implied by recorded produced outputs (`pcs.services.evgen_convention_paths_cached`) | 26 h; refreshed by the EVGEN sweep |
| `evgen_coverage:v2` | EVGEN registration worklist (`pcs.services.build_evgen_coverage`) | 26 h; refreshed by the EVGEN sweep |
| `harvester_workers:v1:<queue>` | the queue's harvester workers reading (`monitor_app/workers.py`), the Workers card of the queue detail page | 300 s |
| `task_resource_peaks:v1` | twelve-hour per-task job memory, scratch disk and file-count peaks (`monitor_app.task_resources`), on the production dashboard | 60 s; asynchronous first fill |
| `es_reading:v1:<queue>` | the node harness across the queue's Event Service jobs of the last 7 days, from the job reports (`monitor_app/es_reading.py`), the Event Service card of the queue detail page | 300 s |
| `allocation:v1:<harvester>:<worker>` | one batch allocation's pilot occupancy timeline (`workers.allocation_product` over `panda.queries.allocation_timeline`), the allocation page | 120 s while the slot runs; kept without rebuilding once it has ended, since PanDA drops the worker record after three months, and never replaced by a build that has lost it |
| `storage_ghosts:v1` | the storage record's ghost population with holders (`swf_epicprod.analytics.storage_listings.build_ghost_population`), behind the Storage exceptions page, the `epicprod_storage` tool and the REST listing | 90 min; refreshed by the storage sweep as its last step (`refresh_ghost_product`) |

A producer that changes a product's input refreshes the product as its
last step, synchronously, so the page serves the new product at once.
The EVGEN sweep (nightly and the Update button) rebuilds the two EVGEN
products above this way; the TTL is only the safety net for a missed
sweep. A builder that cannot see its own input raises rather than
returning an empty result, so an empty product is never stored in place
of a missing one.

## Migration targets

Hand-rolled predecessors that should fold onto this mechanism as they
are next touched: the catalog table fragment cache
(`pcs.views.rebuild_current_task_list_html_cache`), the campaign
progress snapshot refresh, and the per-request Rucio snapshot rollups.
The Rucio snapshot fetch itself stays on the prod-ops agent (it is
credentialed); its serve-side reads already follow the serve-cached
contract.
