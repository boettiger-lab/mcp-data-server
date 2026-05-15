# Cross-pod state for hex tile builds

## Problem

Production `duckdb-mcp` runs with 6 replicas behind an HAProxy ingress using
`balance-algorithm: leastconn` and no session affinity. The async pyramid-build
machinery introduced in #143 / #144 tracks in-flight builds in a process-local
`_jobs` dict on each pod. A polling call (`get_hex_tile_status`) lands on the
build's owning pod only ~1/6 of the time; the other ~5/6 of the time it sees
no local job and no on-disk `metadata.json` yet, so it returns
`status: "unknown"`.

Observed from `open-llm-proxy` logs on 2026-05-14 (wyoming-public-demo, kimi):
five register/status round-trips against three different hashes, each followed
by an "unknown" poll within seconds. The build itself completes correctly
(metadata.json appears at ~100s), but the agent has by then either re-submitted
the same query (deterministic hash → harmless but wasted compute) or run out
of patience and given up.

## Goal

Make `register_hex_tiles` and `get_hex_tile_status` correct under N>1 replicas
by adding cluster-shared build state stored in the same S3 prefix as the tile
output. No changes to the MCP tool contract, no infra changes (no Redis, no
sticky sessions, no replica-count change), no changes to LLM-facing wait
budgets (separate concern).

## Non-goals

- Reducing the agent's polling-call budget. The current `wait_seconds=30`
  pattern requires multiple calls per ~100s build; bumping that cap is a
  separate decision that touches docstrings, agent system prompts, and
  rate-limiting tradeoffs.
- Browser-driven tile-completion (MapLibre retry without an agent poll).
  Would require a stats endpoint or default-style path; out of scope here.
- S3 conditional-write atomicity for the lock. Ceph S3 conditional-write
  support is variable; we accept a small dup-build race window instead.

## Mechanism

Three sibling files live in the per-hash output directory alongside the
existing `metadata.json`:

| file                       | written by         | read by             | meaning            |
|----------------------------|--------------------|---------------------|--------------------|
| `{output_uri}lock.json`    | build start        | register + status   | build in progress  |
| `{output_uri}failed.json`  | build exception    | register + status   | build raised       |
| `{output_uri}metadata.json`| build success      | register + status   | build complete (existing) |

`lock.json` contents:
```json
{"started_at": 1731600000.0, "pod_id": "duckdb-mcp-5855b4bfbf-95qnv"}
```

`failed.json` contents:
```json
{"error": "Out of memory during COPY", "failed_at": 1731600100.0}
```

A lock is **stale** if `now - started_at > _LOCK_STALE_SECONDS` (default
900s = 15 minutes). Stale locks are treated as absent, which handles pod
crashes mid-build.

## Modified `register_hex_tiles` flow

Additions in **bold**:

1. Plan the build (unchanged).
2. `metadata.json` exists → return `done` (unchanged).
3. **`failed.json` exists → return `failed` with stored error.**
4. **Fresh `lock.json` exists → return `running` with `elapsed_seconds` from
   the lock's `started_at`. Do not submit.**
5. **Write `lock.json` (best-effort; not conditional).** Submit the build to
   the local executor via a wrapper that:
   - On success: clears `lock.json`. (`metadata.json` is the success signal —
     written by the existing COPY in `build_hex_tiles`.)
   - On exception: writes `failed.json`, clears `lock.json`.
6. Wait `_BUILD_INLINE_WAIT_SECONDS` (unchanged, 5s). Return `done` /
   `failed` / `running` based on outcome (unchanged behavior).

## Modified `get_hex_tile_status` flow

Additions in **bold**:

1. `metadata.json` exists → return `done` (unchanged).
2. **`failed.json` exists → return `failed` with stored error.**
3. Local `_jobs[hash]` exists → existing long-poll logic on the local
   `Future` (unchanged).
4. **Fresh `lock.json` exists (build owned by a different pod): poll S3 for
   `metadata.json` / `failed.json` every 2s up to `wait_seconds`. Return
   `done` / `failed` when found. If `wait_seconds` expires, return `running`
   with `elapsed_seconds` computed from the lock's `started_at`.**
5. No lock, no failed, no metadata, no local job → return `unknown`
   (unchanged contract, narrower set of conditions).

The 2s S3 poll cadence is server-internal — a single `get_hex_tile_status`
call still corresponds to one LLM tool invocation.

## Race we knowingly accept

Two pods receive `register_hex_tiles` for the same hash within the small
window before either writes a `lock.json` → both proceed to submit a build.
Because the hash is deterministic over (sql, agg, ...), both builds write to
the same `output_uri`. DuckDB COPY is not concurrency-safe, but the last
writer wins on the same key; on completion both clear their (now-merged)
lock. The visible failure mode is wasted compute on one pod, not data
corruption.

This is strictly better than today's behavior (N parallel builds, no shared
state at all). A future hardening could use Ceph S3 conditional-write headers
to make lock acquisition atomic, but is deferred until we see this race
manifest in practice.

## Stale-lock TTL

`_LOCK_STALE_SECONDS = int(os.environ.get("TILE_LOCK_STALE_SECONDS", "900"))`.

Calibration: observed builds complete in 100–120s on prod. 900s gives ~8×
headroom for slow days and accounts for queue time on busy pods (build
executor has `_BUILD_MAX_CONCURRENCY = 2`). Configurable for ops without a
code change.

When a stale lock is encountered, the reader treats it as no-lock. The next
`register_hex_tiles` overwrites it. We do not actively delete stale locks
(simpler, idempotent under concurrent readers).

## Code changes

### `tiles/pyramid.py`

New helpers (all operate via the same DuckDB connection / COPY mechanism
that already writes `metadata.json`, so S3 credentials and endpoint config
are reused):

- `write_lock(con, output_uri, pod_id) -> None`
- `read_lock(con, output_uri) -> dict | None`
- `lock_is_stale(lock, now=None) -> bool`
- `clear_lock(con, output_uri) -> None`
- `write_failed(con, output_uri, error: str) -> None`
- `read_failed(con, output_uri) -> dict | None`

S3 deletion via DuckDB doesn't have a direct primitive; use a small
"`COPY (SELECT 1 WHERE 1=0) TO '...'`" approach to overwrite with empty
content, or — better — call the `httpfs` extension's `S3 DELETE` via the
boto-like `aws` secret already configured. Implementation detail to settle
during the plan phase.

### `server.py`

- `_submit_build`: wrap `_do_build` to manage lock/failed lifecycle.
- `register_hex_tiles`: insert steps 3-4 (failed/lock short-circuits)
  before `_submit_build`; insert step 5 (write lock) just before submitting.
- `get_hex_tile_status`: insert step 2 (failed check) and step 4 (cross-pod
  long-poll branch). Keep the existing `_jobs`-based long-poll for the local
  case — it's strictly faster than S3 polling and remains correct.

### `tests/`

Add to existing files (no new files):

- `tests/test_tile_pyramid.py`: unit tests for the six new helpers using
  local-filesystem `output_uri` (same approach as the existing pyramid
  tests). Cover: lock round-trip, stale-lock detection, failed round-trip,
  clear semantics.
- `tests/test_server.py`: integration-style tests of the new flows. Simulate
  cross-pod by clearing `_jobs` between calls and verifying:
  - register sees fresh lock → returns running without rebuild
  - register sees failed.json → returns failed
  - get_hex_tile_status with cleared `_jobs` but fresh lock → long-polls S3
    and returns done when metadata appears (use a thread to drop
    `metadata.json` mid-poll)
  - build exception → `failed.json` written, lock cleared, subsequent
    register/status see failed
  - stale lock → treated as absent

## Operational notes

- No deployment.yaml change. `TILE_LOCK_STALE_SECONDS` is optional; default
  is reasonable.
- One bump to the version tag when merging (v0.6.5) per existing convention,
  so `k8s/deployment.yaml`'s `git clone --branch` picks up the change at the
  next rollout.
- Backwards compatible: pods running the old code that encounter
  `lock.json` / `failed.json` files (if a mixed-version rollout briefly
  exists) will ignore them, falling back to today's unknown-prone behavior
  for that subset of polls. No data corruption, just transient regression
  to current-state during rollout.

## Open implementation questions to resolve during planning

1. Exact S3-delete primitive available through the DuckDB `httpfs` /
   `aws` secret stack. May need a small `boto3`-style helper if DuckDB
   doesn't expose `DELETE`. Fallback: write a zero-byte file with the same
   key (read_lock checks both presence and content validity).
2. Whether the lock-clearing wrapper in `_submit_build` should also handle
   the case where `metadata.json` is written but the lock-clear fails (S3
   blip). Acceptable: the lock will be marked stale 15 minutes later. No
   correctness loss.
