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
  cross-worker lock, so concurrent requests never stampede.
- **Explicit update rebuilds synchronously.** The uniform Update button
  passes `refresh=1`; the user chose to wait, and gets fresh data back.
- **The first-ever fill builds synchronously** — there is nothing to
  serve; it happens once per key.
- **Failures surface.** A broken builder logs with its key and clears
  the lock; it never presents as silently stale data.

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
| `panda_errors:<days>:<user>:<site>:<source>` | PanDA error summary aggregation | 300 s |
| `prod_hub_corun_counts` | corun-ai assessment/narrative counts | 600 s |

## Migration targets

Hand-rolled predecessors that should fold onto this mechanism as they
are next touched: the catalog table fragment cache
(`pcs.views.rebuild_current_task_list_html_cache`), the campaign
progress snapshot refresh, and the per-request Rucio snapshot rollups.
The Rucio snapshot fetch itself stays on the prod-ops agent (it is
credentialed); its serve-side reads already follow the serve-cached
contract.
