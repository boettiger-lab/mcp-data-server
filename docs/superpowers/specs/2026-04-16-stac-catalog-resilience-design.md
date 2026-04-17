# STAC Catalog Fetch Resilience — Design

Addresses [mcp-data-server#65](https://github.com/boettiger-lab/mcp-data-server/issues/65). Partial progress already landed in [PR #66](https://github.com/boettiger-lab/mcp-data-server/pull/66) (dropped the duplicate startup walk). This design covers the remaining work.

## Problem

`fetch_stac_catalog()` in `stac.py` runs synchronously at module import to populate `STAC_DATASETS` / `_STAC_RAW`. It walks a pystac `Catalog`, iterating children and sub-children serially. Under normal S3 conditions this finishes in a few seconds. Under observed Ceph RGW incidents (queue depth 600–800, ~1 in 5 serial GETs stalling 6–20s) it takes minutes — during which the pod accepts no connections, fails its readiness probe, and gets killed. Every restart re-pays the cost, compounding the incident.

Three structural weaknesses produce this behavior:

1. **One slow child blocks the whole walk.** pystac's `get_children()` is a serial generator; every child is fetched in sequence before the walk completes.
2. **One module-wide timeout (`STAC_TIMEOUT=15s`) is used for every GET.** The root catalog (a hard prerequisite) and each individual child (independently skippable) are treated identically — both worst-case stall 15s.
3. **Any exception fails the whole walk.** The loader is wrapped in a single top-level `try/except` that returns `{}` on any failure. One bad child → zero datasets loaded.

## Scope

This design addresses three items from issue #65:

- **Per-child timeout** independent of the root timeout.
- **Bounded parallelism** of the catalog walk.
- **Partial-result fallback** — one bad child does not kill the whole load, and the caller is told which children failed.

**Explicitly out of scope:**

- **Non-blocking / background startup.** The original issue proposed deferring the load to a background task so the server can accept requests before the catalog is ready. With the three items above, the catalog-load wall clock becomes bounded (~20s typical, ~40s worst case against pathological S3), which fits within the existing readiness probe budget (`initialDelaySeconds: 15 + 5 × periodSeconds × failureThreshold = 40s`). Keeping sync startup avoids the state-machine complexity of "server is up but catalog isn't ready yet" and the associated test surface.
- **Short-TTL cache refresh.** Rejected in prior conversation. Catalog updates are deploy-triggered and handled via Kubernetes rollouts.
- **Parent-with-heterogeneous-children rendering.** Previously a server-side concern; now being addressed upstream in [data-workflows#122](https://github.com/boettiger-lab/data-workflows/issues/122) by cleaning the catalog (four collections with both own-assets and children get restructured). With the catalog clean, the server never sees a parent that carries its own data.
- **Redundant per-asset column listings.** A separate optimization worth doing (for svi-class wide datasets), but independent of this design.

## Design decisions

| Question | Decision |
|---|---|
| Timeout strategy | Two timeouts: `STAC_ROOT_TIMEOUT=15s`, `STAC_CHILD_TIMEOUT=5s`. Root and children are asymmetric — root is a hard prereq, children are individually skippable. |
| Back-compat for `STAC_TIMEOUT` | If set and the two new vars are unset, `STAC_TIMEOUT` applies to both. |
| Parallelism | `ThreadPoolExecutor`, 8 workers by default, env-overridable as `STAC_FETCH_CONCURRENCY`. |
| Walk shape | **Dynamic enqueue (A2):** parents submitted up-front; as each parent's JSON arrives, its sub-child links are submitted to the same pool. No static wave boundary. Rationale: under slow S3, a static "all parents, then all sub-children" scheme serializes on the slowest parent before any sub-child can start. Dynamic enqueue keeps the pool saturated without increasing S3 pressure (same 8-worker cap). |
| Partial-result contract | Silent skip per failing child + side-channel `STAC_LOAD_ERRORS` dict + footer appended to `list_datasets()` output. Main `STAC_DATASETS` dict stays clean of error entries. |
| Concurrency safety | No lock on module state. Two racing loaders result in ~2× S3 load briefly; both eventually do `dict.clear(); dict.update(...)`, and the states diverge only on which transient failures each happened to hit. YAGNI for a lock until this causes real problems. |

## Architecture

One file touched: `stac.py`. The shape of `fetch_stac_catalog()` changes from a serial pystac walk wrapped in a single `try/except` to a phased parallel fetch with per-item error isolation.

- **Phase 1:** fetch root catalog with root timeout. If it fails → record root error, log, return `{}`. Nothing else can proceed.
- **Phase 2 (parallel, dynamic):** enumerate parent child-links from the parsed root. Submit one `_fetch_parent` task per parent link to a shared `ThreadPoolExecutor`. As each parent future completes, the main thread enumerates its sub-child links (from the parsed parent JSON) and submits `_fetch_subchild` tasks. Workers never recurse themselves — each worker does exactly one HTTP GET, bounded by the child timeout. Main thread drains both parent and sub-child futures together.
- **Phase 3:** swap module state from the main thread (`clear` + `update` on `STAC_DATASETS`, `_STAC_RAW`, `STAC_LOAD_ERRORS`). Not lock-guarded — see Error Handling for the race analysis.

No changes to `server.py`, to the MCP tool signatures, or to the cache-miss refetch paths in `get_dataset` / `get_collection` — those inherit the new behavior through the same function.

## Components

### New module-level state

```python
STAC_LOAD_ERRORS: dict[str, str] = {}  # id-or-href-tail → reason
```

Cleared and repopulated on each default-catalog load.

### New config env vars

```python
_STAC_ROOT_TIMEOUT = int(os.environ.get("STAC_ROOT_TIMEOUT", os.environ.get("STAC_TIMEOUT", "15")))
_STAC_CHILD_TIMEOUT = int(os.environ.get("STAC_CHILD_TIMEOUT", os.environ.get("STAC_TIMEOUT", "5")))
_STAC_FETCH_CONCURRENCY = int(os.environ.get("STAC_FETCH_CONCURRENCY", "8"))
```

Note the default for child drops from 15 to 5; this is the intended tightening.

### Modified: `_TimeoutStacIO`

Accepts an optional `timeout` kwarg on construction; uses it on the per-request `requests.get(...)` call. Two instances are built per `fetch_stac_catalog()` call:

- `root_io = _TimeoutStacIO(token=token, timeout=_STAC_ROOT_TIMEOUT)`
- `child_io = _TimeoutStacIO(token=token, timeout=_STAC_CHILD_TIMEOUT)`

Shared across workers; thread-safe because only state is the immutable token.

### New private helpers

**`_child_identifier(href, title_hint, fetched_id=None) -> str`** — returns the best-available identifier for a child. Prefers `fetched_id` (only available if JSON parse succeeded), falls back to the last path segment of the href, optionally augmented with `title_hint`.

**`_fetch_parent(href, title, token) -> ParentResult`** — thread-worker for top-level children. One pystac `Collection.from_file(...)` call, returns a typed result with (a) the parent's dict/markdown entries, (b) the list of sub-child links parsed out of the result's `.links`, (c) any errors. All exceptions caught.

**`_fetch_subchild(href, parent_id, token) -> SubchildResult`** — thread-worker for sub-children. One pystac fetch, returns (a) the sub-child's dict/markdown entries, (b) any errors. All exceptions caught.

### Rewritten: `fetch_stac_catalog(catalog_url=None, catalog_token=None)`

Phase-1 root fetch + phase-2 dynamic parallel walk + phase-3 state swap. Signature unchanged. Returns `dict[str, str]` (markdown summaries) just like today.

### Modified: `list_datasets()`

After building the existing markdown, append a footer when `STAC_LOAD_ERRORS` is non-empty:

```
⚠️ 2 collections could not be loaded: public-wyoming (timeout), public-census (500)
```

### Unchanged

`_format_collection`, `_collection_to_dict`, `_extract_parquet_assets`, `_extract_columns`, `_format_columns`, `_fuzzy_lookup`, `_href_to_s3`, `get_dataset`, `get_collection`. These inherit the more-resilient cache transparently.

## Data flow

1. Build `root_io` and `child_io` with their respective timeouts.
2. `pystac.Catalog.from_file(root_url, stac_io=root_io)` — up to 15s. On failure: record `STAC_LOAD_ERRORS["__root__"]`, log, return `{}`.
3. Enumerate parent child-links from the parsed root (no HTTP).
4. Open `ThreadPoolExecutor(max_workers=_STAC_FETCH_CONCURRENCY)`. Submit one `_fetch_parent` per link; track futures in `parent_futures`. Initialize `subchild_futures = set()`.
5. Loop on `concurrent.futures.wait(parent_futures | subchild_futures, return_when=FIRST_COMPLETED)`:
   - For each completed parent future: merge its entries and errors into local dicts. If the parent fetched successfully, submit `_fetch_subchild` for each of its sub-child links.
   - For each completed sub-child future: merge its entries and errors.
   - Terminate when both sets are empty.
6. For the default catalog (no `catalog_url` arg): swap module state — `STAC_DATASETS.clear(); STAC_DATASETS.update(datasets); _STAC_RAW.clear(); _STAC_RAW.update(raw); STAC_LOAD_ERRORS.clear(); STAC_LOAD_ERRORS.update(errors)`. A concurrent reader during the brief window between `clear()` and `update()` sees an empty dict; see Error Handling. Custom catalog: don't touch module state; return the datasets dict.
7. Log summary: `📂 Loaded N collections (M failed) from <url>`.

**Wall-clock bound:**

- Root: 15s worst case
- Pool work: 30 parents + ~33 sub-children = 63 fetches. With 8 workers and 5s per-fetch ceiling, the critical path is ~one parent's 5s + sub-children of that parent filling the remaining budget → ~30–35s realistic worst case, ~20s typical.
- Total: ~40–50s worst, ~20s typical.

Readiness probe tolerates 40s today. If we approach the ceiling, the first knob is bumping `failureThreshold` on the probe, not restructuring the loader.

## Error handling

| Failure mode | Outcome |
|---|---|
| Root fetch fails | `STAC_LOAD_ERRORS["__root__"]` set, log, return `{}`. Module state untouched (keeps last successful load if any). |
| Parent fetch fails | Worker returns error; parent + all its sub-children are silently skipped. Identifier falls back to href tail. |
| Sub-child fetch fails | Worker returns error; parent's entry stays in the dict; only that sub-child is missing. |
| Asset parsing raises | Caught by worker's try/except; treated as a child failure. |
| `catalog_url` path (custom catalog) | Same error handling, but module state is not touched. Caller gets whatever succeeded. |
| Concurrent `fetch_stac_catalog()` calls | No lock. Two loaders race; last one to finish wins. States diverge only on transient failures. |

Logging:

- One line per child failure: `⚠️ Child fetch failed: <id> — <reason>`
- One summary line at end: `📂 Loaded N collections (M failed) from <url>`
- No stack traces in normal operation; failures include only class name + message.

## Testing

Existing tests stay green. New test class `TestFetchResilience` added to `tests/test_stac.py`:

| Test | What it verifies |
|---|---|
| Root-fetch fails | `fetch_stac_catalog()` returns `{}`; `STAC_LOAD_ERRORS["__root__"]` is set |
| One parent of three fails | Other two parents' entries present; failing parent's identifier in errors; return has 2 entries |
| All parents fail | Datasets dict empty; errors dict has all parent identifiers; root error absent |
| One sub-child of three fails | Parent entry present; 2 sub-child entries present; 1 sub-child error recorded |
| Footer appears on `list_datasets()` when errors exist | Footer text present with each error ID; absent when errors dict empty |
| Concurrency env var honored | `STAC_FETCH_CONCURRENCY=2` → executor built with `max_workers=2` |
| Root/child timeout env vars reach StacIO | Each `_TimeoutStacIO` instance receives the expected `timeout` value |
| Back-compat: `STAC_TIMEOUT` alone | With only `STAC_TIMEOUT=10`, both root and child timeouts become 10 |
| Wall-clock sanity (optional) | 3 slow-child mocks, 2 workers, total elapsed < 10s (would be 12s if serial) |

**Not tested:** exact log ordering (racy), dynamic-enqueue ordering (only final state matters), real network I/O.

All mocks patch `pystac.Catalog.from_file` and `pystac.Collection.from_file`, or the underlying `requests.get` where per-request timeout assertions are needed. No new test files.

## Non-goals / deferred

- Retry on individual child failures. YAGNI: the dominant failure mode is incident-wide slowness, where a retry just costs another 5s to (maybe) save one child. Deterministic skip + log is clearer.
- Moving the catalog load to a background task or lifespan startup. Covered in scope section above.
- Any changes to `get_collection` / `get_dataset` cache-miss logic.
- Structured JSON logging. Current stderr lines are fine for the operator audience.

## Rollout

- Tests run in CI on PR.
- Merge to main → dev deployment picks up via `git clone` at pod restart.
- Monitor the next S3-tail-latency incident: compare pod-startup wall-clock, count of collections loaded, whether footer appears when expected.
- Tag and promote to prod if dev behavior is clean.
