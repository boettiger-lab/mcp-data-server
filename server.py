import hmac
import os
import re
import socket
import duckdb
import uvicorn
import sys
import anyio
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.session import BaseSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from stac import STAC_DATASETS, STAC_LOAD_ERRORS, STAC_CATALOG_URL, list_datasets as _stac_list, get_dataset as _stac_get, get_collection as _stac_get_collection

# Workaround for https://github.com/boettiger-lab/mcp-data-server/issues/5
# send_notification crashes with ClosedResourceError when the client disconnects
# (e.g. after a ~60s client-side timeout) while a query is still running.
# The MCP library should catch this in send_notification; patch it until upstream fixes it.
_orig_send_notification = BaseSession.send_notification
async def _resilient_send_notification(self, notification, related_request_id=None):
    try:
        await _orig_send_notification(self, notification, related_request_id)
    except anyio.ClosedResourceError:
        pass
BaseSession.send_notification = _resilient_send_notification

# -------------------------------------------------------------------------
# 1. INITIALIZATION
# -------------------------------------------------------------------------
# App version + git SHA, baked at build time from the git tag (Dockerfile ARGs set
# by docker.yml). The tag is the single source of truth — no version string lives in
# source, so nothing can drift from what was actually tagged/built. Defaults mark a
# local/un-stamped run. See issue #221.
APP_VERSION = os.environ.get("APP_VERSION", "dev")
GIT_SHA = os.environ.get("GIT_SHA", "unknown")

mcp = FastMCP(
    "DuckDB-S3-Geo-Isolated",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

# Report APP_VERSION in the MCP `initialize` handshake (serverInfo.version) so clients
# learn it in-band, no extra request. FastMCP has no version kwarg, so set it on the
# wrapped low-level server. `mcp` is unpinned (the weekly cron re-resolves it), so guard
# against a future internal rename rather than crash startup — /version and /healthz
# still carry the version regardless.
try:
    mcp._mcp_server.version = APP_VERSION
except Exception:
    pass

# -------------------------------------------------------------------------
# 2. CONFIGURATION & FILE LOADING
# -------------------------------------------------------------------------
def load_text_file(filename):
    paths = [
        filename,
        os.path.join("/app", filename),
        os.path.join(os.path.dirname(__file__), filename)
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, 'r') as f: return f.read()
    print(f"⚠️ Warning: Could not find {filename}", file=sys.stderr)
    return ""

def parse_setup_sql(content):
    match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
    return match.group(1).strip() if match else ""

SETUP_RAW = load_text_file("query-setup.md")
SETUP_SQL = parse_setup_sql(SETUP_RAW)
OPTIM_RAW = load_text_file("query-optimization.md")
H3_RAW = load_text_file("h3-guide.md")
ROLE_RAW = load_text_file("assistant-role.md")

# -------------------------------------------------------------------------
# 3. CONTEXT INJECTION (PROMPT ENGINEERING)
# -------------------------------------------------------------------------
TOOL_INJECTED_CONTEXT = f"""
---
### ⚠️ CRITICAL SQL RULES (MUST FOLLOW)
1. **NO TABLES EXIST:** The database is empty. You CANNOT write `FROM table_name`.
2. **USE PARQUET PATHS:** You MUST use `FROM read_parquet('s3://...')` for ALL queries.
3. **DISCOVER PATHS — TRUST STAC PATHS EXACTLY:** Call `browse_stac_catalog` then `get_stac_details` to get exact S3 paths — then use them **verbatim**. NEVER guess, modify, or "fix" a path. Both path depth and glob pattern vary across datasets — there is no single convention. Examples:
   - `read_parquet('s3://public-wdpa/wdpa-december-2025/hex/h0=*/data_0.parquet')` — versioned collection, partition glob
   - `read_parquet('s3://public-padus/padus-4-1/fee/hex/h0=*/data_0.parquet')` — nested path, partition glob

   Both are correct. Copy-paste the path from get_stac_details. In SQL examples below, `<STAC_HEX_PATH>` means "insert the exact path from get_stac_details here."
4. **MASK BEFORE AGGREGATE (DPP rule).** When joining a small hex mask
   (e.g. state, district, county, protected-areas hexes) against a globally
   `h0`-partitioned hex dataset (land cover, climate, biomass — anything
   under `hex/h0=*/`), the `SEMI JOIN` against the mask MUST appear directly
   on the raw `read_parquet(...)` BEFORE `GROUP BY`. Aggregating the global
   side first scans every `h0` partition and will hit the 300-second MCP
   timeout. DuckDB dynamic partition pruning cannot push filters through
   `HASH_GROUP_BY`.
   ```sql
   SELECT a.h8, MODE(a.lc_class) AS dominant
   FROM read_parquet('<global_hex>', hive_partitioning = true) a
   SEMI JOIN <mask> m USING (h8, h0)
   WHERE a.lc_class IS NOT NULL
   GROUP BY a.h8;
   ```

### ⚡ OPTIMIZATION RULES
{OPTIM_RAW}

### 📐 H3 SPATIAL MATH
{H3_RAW}
---
"""

# -------------------------------------------------------------------------
# 4. ISOLATION ENGINE
# -------------------------------------------------------------------------
from s3config import (
    default_s3_secret_sql,
    infer_use_ssl,
    source_secret_sql,
    sql_quote as _sql_quote,
)

# Query engine seam (#227). Default "duckdb" reproduces the historical path
# byte-for-byte; QUERY_ENGINE can select an optional Polars/GPU backend. The
# DuckDB engine reuses get_isolated_db() below via a lazy import, so this stays
# import-cycle-free. See docs/architecture/gpu-query-engine.md.
from engines import select_engine, S3Request


@contextmanager
def get_isolated_db(s3_key: str = None, s3_secret: str = None, s3_endpoint: str = None, s3_scope: str = None):
    conn = duckdb.connect(database=":memory:")
    try:
        for stmt in (s.strip() for s in SETUP_SQL.split(";") if s.strip()):
            try:
                conn.sql(stmt)
            except Exception as e:
                print(f"⚠️ Setup statement skipped: {stmt!r}: {e}", file=sys.stderr)
        # Prefix-scoped secrets for every registry source (#264) — e.g. the
        # anonymous source.coop mirror. Scoped, so they coexist deterministically
        # with both the default `s3` secret and any client_s3 below (longest-
        # scope match wins); shared with the tile connection via s3config.
        for stmt in source_secret_sql():
            try:
                conn.sql(stmt)
            except Exception as e:
                print(f"⚠️ Source secret skipped: {e}", file=sys.stderr)
        # Bring-your-own-bucket. Two symmetric cases, both routed through one
        # per-request `client_s3` secret:
        #   - credentialed: both s3_key and s3_secret given (private data).
        #   - anonymous:    only s3_endpoint given, no creds (a public mirror,
        #     e.g. MinIO/source.coop during a Ceph outage — #264). We key off
        #     s3_endpoint here because an anonymous bucket has no other signal.
        # Pass s3_scope (e.g. 's3://public-') so this secret applies only to those
        # paths and everything else keeps the deployment default below.
        credentialed = bool(s3_key and s3_secret)
        has_client = credentialed or bool(s3_endpoint)
        if has_client:
            endpoint = s3_endpoint or "s3-west.nrp-nautilus.io"
            key = s3_key if credentialed else ""
            secret = s3_secret if credentialed else ""
            use_ssl = infer_use_ssl(endpoint)
            scope_clause = f", SCOPE '{_sql_quote(s3_scope)}'" if s3_scope else ""
            # Credentials (if any) injected here; intentionally not logged.
            conn.sql(
                f"CREATE OR REPLACE SECRET client_s3 ("
                f"TYPE S3, KEY_ID '{_sql_quote(key)}', SECRET '{_sql_quote(secret)}', "
                f"ENDPOINT '{_sql_quote(endpoint)}', URL_STYLE 'path', USE_SSL '{use_ssl}'"
                f"{scope_clause})"
            )
        # Default S3 endpoint — server-owned and per-deployment configurable (#268),
        # built by s3config (shared with the tile subsystem). Lets you deploy this
        # codebase as a data-access head pointed at any storage (Ceph / MinIO /
        # source.coop) purely via env, no code change.
        #
        # Skipped when client_s3 exists UNSCOPED: DuckDB's pick between two unscoped
        # secrets is an undocumented tie-break (empirically client_s3 captured every
        # path on duckdb 1.5.4, regardless of creation order — #271). Rather than
        # depend on that, make the de-facto semantic explicit and deterministic:
        # without s3_scope, the client's endpoint/creds own ALL s3:// paths for this
        # request; with s3_scope, the default serves everything outside the scope.
        if not (has_client and not s3_scope):
            try:
                conn.sql(default_s3_secret_sql())
            except Exception as e:
                print(f"⚠️ Default S3 secret setup skipped: {e}", file=sys.stderr)
        yield conn
    finally:
        conn.close()

# -------------------------------------------------------------------------
# 5. MCP RESOURCES (Schema Browsing)
# -------------------------------------------------------------------------
@mcp.resource("catalog://list")
def catalog_list() -> str:
    return _stac_list()

@mcp.resource("catalog://{dataset_id}")
def catalog_dataset(dataset_id: str) -> str:
    return _stac_get(dataset_id)

# -------------------------------------------------------------------------
# 6. MCP TOOLS — Dataset Discovery
# -------------------------------------------------------------------------
@mcp.tool()
def browse_stac_catalog(
    catalog_url: str = None,
    catalog_token: str = None,
    catalog: dict = None,
) -> str:
    """Browse the full public STAC catalog to discover datasets not already loaded in your app.
    Use when the user asks about data outside your pre-configured layers.
    Optionally provide catalog_url to use a custom STAC catalog instead of the server default.
    Optionally provide catalog_token (Bearer token) if the catalog requires authentication.
    Optionally provide catalog inline (a Catalog dict with nested `children: [<collection dict>, ...]`)
    to skip the HTTP fetch entirely — useful for OAuth-walled deployments where the
    client already has the catalog content cached."""
    return _stac_list(catalog_url, catalog_token, catalog=catalog)

@mcp.tool()
def get_stac_details(
    dataset_id: str,
    catalog_url: str = None,
    catalog_token: str = None,
    collection: dict = None,
) -> str:
    """Fetch metadata (parquet paths, column schemas) for any STAC collection by ID.
    Returns markdown formatted for LLM consumption (use get_collection for structured JSON).
    Optionally provide catalog_url and catalog_token if using a private STAC catalog.
    Optionally provide collection inline (a Collection dict, optionally with embedded
    `children: [<sub-collection dict>, ...]`) to skip the HTTP fetch entirely."""
    return _stac_get(dataset_id, catalog_url, catalog_token, collection=collection)

@mcp.tool()
def get_collection(
    collection_id: str,
    catalog_url: str = None,
    catalog_token: str = None,
    collection: dict = None,
) -> dict:
    """Return structured STAC collection metadata as JSON for programmatic use.

    Unlike get_stac_details (markdown for LLM consumption), this returns the
    raw collection dict with all assets (parquet, PMTiles, COG, GeoJSON),
    per-asset STAC extension fields (table:columns, raster:bands, vector:layers),
    full collection metadata, and nested child collections. S3 paths are pre-resolved.

    Optionally provide collection inline (a Collection dict, optionally with embedded
    `children: [<sub-collection dict>, ...]`) to skip the HTTP fetch — output round-trips
    back into the same parameter.

    Intended for app code that builds map layers and system prompts programmatically."""
    return _stac_get_collection(collection_id, catalog_url, catalog_token, collection=collection)

# -------------------------------------------------------------------------
# 7. MCP PROMPTS (Personas for Smart Clients)
# -------------------------------------------------------------------------
@mcp.prompt("geospatial-analyst")
def analyst_persona() -> str:
    return ROLE_RAW

# -------------------------------------------------------------------------
# 8. TOOL DEFINITION — SQL Query
# -------------------------------------------------------------------------
# The active query backend, chosen once at import from QUERY_ENGINE. Default is
# the DuckDB engine, whose run() reproduces the historical query() exactly.
_ENGINE = select_engine()


def query(sql_query: str, s3_key: str = None, s3_secret: str = None, s3_endpoint: str = None, s3_scope: str = None) -> str:
    """Placeholder (docstring set below). Delegates to the selected engine."""
    return _ENGINE.run(
        sql_query,
        S3Request(s3_key=s3_key, s3_secret=s3_secret, s3_endpoint=s3_endpoint, s3_scope=s3_scope),
    )

query.__doc__ = f"""
Executes optimized DuckDB SQL against S3 parquet files.

BEFORE writing any SQL:
1. Call `browse_stac_catalog` to see all available dataset IDs and titles.
2. Call `get_stac_details` with the relevant dataset ID to get exact S3 paths and column schemas.
3. Use ONLY paths returned by those tools — never guess or hardcode any S3 URLs.

For private data, pass s3_key, s3_secret, and optionally s3_endpoint and s3_scope alongside the SQL query.
For an anonymous public source (e.g. a read-only mirror), pass s3_endpoint alone (no key/secret) with s3_scope — useful to read a mirror like s3://public-* from a backup endpoint when the primary is unavailable.
Use s3_scope (e.g. 's3://private-wyoming' or 's3://public-') so DuckDB routes those paths to your endpoint rather than the server default; supply it whenever a query mixes sources.
WITHOUT s3_scope, your endpoint/credentials apply to EVERY s3:// path in the query and the server-default endpoint is disabled for this request — fine for a query touching only your bucket, wrong for a query mixing your bucket with catalog data. When mixing, always pass s3_scope.
Credentials, when given, are scoped to this request only and never persisted.

{TOOL_INJECTED_CONTEXT}
"""

# query runs a DuckDB scan up to 300s. FastMCP runs a *sync* tool inline on the
# uvicorn event loop, so a long query would freeze the whole pod: /healthz goes
# unanswered (readiness pulls the pod, a long-enough scan risks the liveness
# SIGKILL), tile GETs stall, and other MCP requests queue behind it (#176). #185
# offloaded the hex tools the same way; query was the remaining sync-on-loop tool.
# A CapacityLimiter bounds concurrent scans so a burst can't oversubscribe the
# pod's CPU/memory or starve the hex tools sharing anyio's default thread pool.
_QUERY_LIMITER = anyio.CapacityLimiter(int(os.environ.get("MCP_QUERY_CONCURRENCY", "8")))


async def _query_tool(
    sql_query: str,
    s3_key: str = None,
    s3_secret: str = None,
    s3_endpoint: str = None,
    s3_scope: str = None,
) -> str:
    # Mirrors query()'s signature so FastMCP derives the identical tool schema.
    return await anyio.to_thread.run_sync(
        query, sql_query, s3_key, s3_secret, s3_endpoint, s3_scope,
        limiter=_QUERY_LIMITER,
    )


# Register the async wrapper under the tool name, reusing the sync function's
# docstring as the LLM-facing description (mirroring the hex-tool pattern, #185).
mcp.tool(name="query", description=query.__doc__)(_query_tool)

# -------------------------------------------------------------------------
# 8b. TILE ENDPOINT — dynamic MVT for H3 hex visualization (see issue #4)
# -------------------------------------------------------------------------
import concurrent.futures
import threading
import time
from tiles.endpoint import serve_metadata, serve_tile
from tiles.db import build_tile_connection
from tiles.pyramid import (
    MVT_LAYER_NAME,
    prepare_hex_tiles,
    build_hex_tiles,
    cached_result_dict,
    render_recipe,
    lock_is_stale,
    read_existing_metadata,
    read_failed,
    read_lock,
    tile_paths_for_hash,
    write_failed,
    write_lock,
)


# Module-level persistent connection used for READS ONLY (tile-serve GETs +
# the fast prepare-phase probes for register_hex_tiles). Pyramid builds get
# their own connections via the executor below so a long-running COPY can't
# block tile-serve reads.
_tile_con = None


def _get_tile_con():
    global _tile_con
    if _tile_con is None:
        _tile_con = build_tile_connection()
    return _tile_con


# Pod identity for cross-pod attribution in lock.json. In k8s, HOSTNAME
# is the pod name; falling back to the OS hostname for local dev.
_POD_ID = os.environ.get("HOSTNAME") or socket.gethostname()

# Background pyramid builds. Each submitted job gets a fresh DuckDB
# connection so writes don't serialise behind each other or behind reads.
_BUILD_MAX_CONCURRENCY = int(os.environ.get("TILE_BUILD_MAX_CONCURRENCY", "2"))
_BUILD_INLINE_WAIT_SECONDS = float(os.environ.get("TILE_BUILD_INLINE_WAIT_SECONDS", "5"))
# DuckDB threads for the CPU-bound pyramid build. Default 48 leaves ~16 cores
# free under the 64-core limit, so the uvicorn event loop stays schedulable and
# /healthz keeps answering during a build. (#185 removed the on-loop polling
# that was the real event-loop starvation; a pure build at 48 has ample
# headroom on a properly-sized pod — #184.) Lower this via env only on
# under-provisioned / contended nodes where the pod can't actually get ~48
# cores. Reads use TILE_THREADS (also 48) — their queries are tiny.
_BUILD_THREADS = int(os.environ.get("TILE_BUILD_THREADS", "48"))
# While a build runs, the owning pod re-writes lock.json this often so a live
# (possibly slow, thread-capped) build never looks stale; when the pod stops,
# the lock ages out within _LOCK_STALE_SECONDS. Must stay well under it.
_LOCK_HEARTBEAT_SECONDS = float(os.environ.get("TILE_LOCK_HEARTBEAT_SECONDS", "30"))
_build_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_BUILD_MAX_CONCURRENCY,
    thread_name_prefix="tile-build",
)
_jobs_lock = threading.Lock()
# hash -> {"future": Future, "started_at": float}. Entries persist after
# completion so get_hex_tile_status can return "failed" with the error
# string; "done" status reads metadata.json directly so the job dict
# isn't authoritative for success.
_jobs: dict = {}


def _start_lock_heartbeat(output_uri: str):
    """Spawn a daemon thread that refreshes lock.json's heartbeat every
    _LOCK_HEARTBEAT_SECONDS until stopped, so a long (thread-capped) build never
    looks stale to status polls. Uses its own tiny connection — the build's
    connection is busy running the multi-minute COPY. Preserves the original
    started_at (read from the lock register wrote) so reported elapsed grows.
    Returns a stop() callable; call it when the build ends (the pod dying just
    stops the heartbeat, letting the lock age out within _LOCK_STALE_SECONDS)."""
    stop = threading.Event()
    hb_con = build_tile_connection(threads=1)
    existing = read_lock(hb_con, output_uri)
    started_at = (existing or {}).get("started_at")

    def _beat():
        try:
            while not stop.wait(_LOCK_HEARTBEAT_SECONDS):
                try:
                    write_lock(hb_con, output_uri, pod_id=_POD_ID, started_at=started_at)
                except Exception:
                    pass  # transient S3 blip; next beat retries
        finally:
            hb_con.close()

    threading.Thread(target=_beat, name="tile-lock-heartbeat", daemon=True).start()
    return stop.set


def _submit_build(plan: dict) -> concurrent.futures.Future:
    """Submit (or join) a background pyramid build for this plan. Dedups
    within-process: if a job for the same hash is already in flight, returns
    that future instead of starting a duplicate build."""
    h = plan["hash"]
    with _jobs_lock:
        existing = _jobs.get(h)
        if existing is not None and not existing["future"].done():
            return existing["future"]

        def _do_build():
            build_con = build_tile_connection(threads=_BUILD_THREADS)
            print(f"[tile-build] hash={h} START pod={_POD_ID} threads={_BUILD_THREADS}",
                  file=sys.stderr)
            t0 = time.perf_counter()
            stop_heartbeat = _start_lock_heartbeat(plan["output_uri"])
            try:
                return build_hex_tiles(build_con, plan)
            except Exception as exc:
                print(
                    f"[tile-build] hash={h} FAILED after "
                    f"{time.perf_counter() - t0:.1f}s: {exc}",
                    file=sys.stderr,
                )
                # Persist failure so other pods (and this pod after _jobs
                # eviction) can return status=failed instead of "unknown".
                try:
                    write_failed(build_con, plan["output_uri"], error=str(exc))
                except Exception:
                    # Marker write failed (S3 blip); preserve original raise.
                    pass
                raise
            finally:
                stop_heartbeat()
                build_con.close()

        future = _build_executor.submit(_do_build)
        _jobs[h] = {"future": future, "started_at": time.time()}
        return future


# Deliberate API design: only `sql` and `agg` are documented for the LLM.
# `finest_res`, `min_res`, `zoom_offset` are kept in the Python signature as
# optional kwargs for tests / REPL overrides, but NOT mentioned in the
# docstring — the MCP framework derives the LLM-facing tool schema from the
# docstring, so they stay invisible to the agent. Auto-detection (in
# tiles.pyramid.register_hex_tiles) reads the H column's resolution to set
# finest_res; min_res=2 is the coarsest level worth materializing. zoom_offset is
# now an adaptive *bias* knob: the tile endpoint picks the H3 res per zoom from
# the data's extent + cell count to hold each tile near a cell budget (#188), so
# bounded data (CA) renders finer at mid-zoom than global data does, instead of a
# single linear offset that fit neither. zoom_offset=2 is neutral (no bias);
# smaller nudges one res finer per step (legacy direction). The pyramid still
# builds all res levels, so this is a serve-time choice — no rebuild on retune.
# Adding `finest_res` etc. to the docstring is almost certainly a mistake — see
# #125 for the trigger-tightening rationale and the discussion about param surface.
def register_hex_tiles(
    sql: str,
    agg: str = "COUNT",
    finest_res: int | None = None,
    min_res: int = 2,
    zoom_offset: int = 2,
    color_scale: str = "linear",
    layer_style: str = "fill",
) -> dict:
    """Aggregate an H3 hex visualization to public object storage and return a
    paste-ready MapLibre render recipe.

    The server picks the rendering automatically — a single GeoJSON file for
    small/bounded results, a vector tile pyramid for large ones — and hands back
    a ready `source` and `layer`. You render those verbatim; you never choose.

    WHEN TO USE — only when the user explicitly asks for an aggregate
    density / heatmap / hex-grid visualization over a region. Trigger phrases:
    "hex map", "density map", "heatmap", "show density of X", "hex grid",
    "aggregate X by hex", "visualize density of X", "map the count of X per
    area".

    Call this ONLY to display a value your SQL COMPUTES that is not already a
    servable field anywhere — not a column in a layer's PMTiles, and not a
    raster field served by a COG. If the value already lives somewhere
    renderable, render that instead. If intent is ambiguous, ask first.

    Parameters:
    - `sql`: a SELECT whose first column is an H3 index. The tool reads that
      column's H3 resolution and uses it as the pyramid's finest level. To get
      a coarser tileset, project upstream in the SQL (e.g.
      `SELECT h3_cell_to_parent(h10, 6) AS h6, ...`).
    - `agg`: aggregation applied at each coarser pyramid level.
        - "COUNT" (default): SQL needs only the H3 column; output property
          is `count` (row count per hex).
        - "AVG" / "SUM" / "MIN" / "MAX": SQL must return at least one
          numeric value column after the H3 index; each is aggregated by
          `agg` at every coarser level.
    - `color_scale`: "linear" (default) or "log". Controls how the viridis
      ramp is spread across the data domain in the returned recipe; the tiles
      themselves are identical either way. Use "log" for right-skewed data
      (counts, populations) where a few hot cells otherwise wash everything
      else out. `value_stats[<col>].suggested_scale` (see below) is a free
      hint telling you when "log" is worth re-requesting — re-call with the
      same SQL and `color_scale="log"` for a cache hit that just re-styles.
    - `layer_style`: "fill" (default, flat 2D) or "fill-extrusion" (3D — hex
      height also encodes the value). Like color_scale this only restyles the
      recipe, not the tiles. For 3D the map client must set pitch > 0 to see
      the extrusions; the returned `layer` is a `fill-extrusion` layer.

    Returns a dict with `status` ∈ {"done", "running", "failed"}:
    - status="done" (cache hit or fast build): includes the render recipe —
      `source` (pass to map.addSource) and `layer` (pass to map.addLayer),
      both ready to use as-is with a default viridis color ramp. Also
      `value_columns` and `value_stats` ({<col>: {"by_res": {"<res>":
      {"min","max","mean"}}, "suggested_scale": "linear"|"log"}}) if you want
      to customize the palette or honor the log-scale hint, plus `hash`,
      `bounds`, `feature_count_finest`.
    - status="running": being built in the background. You get `hash` and
      `tile_url_template`. Call `get_hex_tile_status(hash, wait_seconds=30)`
      to poll — it long-polls server-side, so one call returns either the
      final recipe or a single "still running" response. Do NOT retry
      register_hex_tiles with different parameters; the original build still
      finishes, and re-submitting only queues more work.
    - status="failed": build raised an error inline. `error` has the message.
      Safe to re-submit with adjusted parameters.

    MapLibre usage (identical regardless of format — render what you got):
        map.addSource(id, result.source);
        map.addLayer({id: ..., source: id, ...result.layer});

    SQL patterns — pick the one matching the ask; paste exact paths from
    get_stac_details. `<H>` is the H3 resolution, usually 8.

    1. Density (count features per hex):

       SELECT h<H> FROM read_parquet('<hex_path>') WHERE <filter>

       Call agg="COUNT". Works for pre-indexed points (GBIF) or polygons
       (PAD-US). For raw points with lat/lng:
       `h3_latlng_to_cell(lat, lng, <H>) AS h<H>`.

    2. Masked aggregate (value dataset inside a geographic mask):

       SELECT a.h<H>, AVG(a.value) AS value         -- or MODE(class) / SUM / MAX
       FROM read_parquet('<values_hex>', hive_partitioning = true) a
       SEMI JOIN read_parquet('<mask_hex>', hive_partitioning = true) b
                 USING (h<H>, h0)
       WHERE a.value IS NOT NULL
       GROUP BY a.h<H>;

       Call agg="AVG" (or the matching op). The SEMI JOIN must sit on the
       raw read_parquet(), upstream of GROUP BY — see h3-guide.md Problem 2.

    Always pass hive_partitioning = true so the planner can prune h0=* files.
    """
    # Per-call cursor (not the bare shared connection): this function now runs
    # inside a worker thread via the async tool wrapper, and several may be in
    # flight at once. A cursor gives each its own thread-isolated handle on the
    # shared :memory: connection (the tile endpoint uses the same pattern).
    read_con = _get_tile_con().cursor()
    plan = prepare_hex_tiles(
        con=read_con, sql=sql, agg=agg,
        finest_res=finest_res, min_res=min_res, zoom_offset=zoom_offset,
    )
    if plan["cached"] is not None:
        result = cached_result_dict(plan, plan["cached"])
        result["status"] = "done"
        return _apply_render_opts(result, color_scale, layer_style)

    failed = read_failed(read_con, plan["output_uri"])
    if failed is not None:
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "failed",
            "error": failed.get("error", ""),
        }

    existing_lock = read_lock(read_con, plan["output_uri"])
    if existing_lock is not None and not lock_is_stale(existing_lock):
        # Another pod owns this build. Don't submit a duplicate.
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "running",
            "elapsed_seconds": round(time.time() - existing_lock["started_at"], 1),
        }

    try:
        write_lock(read_con, plan["output_uri"], pod_id=_POD_ID)
    except Exception:
        # S3 blip writing lock; proceed anyway. Worst case is a duplicate
        # build elsewhere — see spec "Race we knowingly accept".
        pass

    future = _submit_build(plan)
    try:
        result = future.result(timeout=_BUILD_INLINE_WAIT_SECONDS)
        result["status"] = "done"
        return _apply_render_opts(result, color_scale, layer_style)
    except concurrent.futures.TimeoutError:
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "running",
        }
    except Exception as e:
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "failed",
            "error": str(e),
        }


async def _register_hex_tiles_tool(
    sql: str,
    agg: str = "COUNT",
    finest_res: int | None = None,
    min_res: int = 2,
    zoom_offset: int = 2,
    color_scale: str = "linear",
    layer_style: str = "fill",
) -> dict:
    # Served MCP tool. register_hex_tiles is sync (and the directly-tested core);
    # FastMCP would run a sync tool inline on the uvicorn event loop, where its
    # S3 marker I/O + bounded inline build-wait block /healthz and every other
    # request (#176). Offload it to a worker thread so the loop stays free.
    return await anyio.to_thread.run_sync(
        register_hex_tiles, sql, agg, finest_res, min_res, zoom_offset, color_scale, layer_style
    )


# Register the async wrapper under the tool name, reusing the sync function's
# docstring as the LLM-facing description (the wrapper mirrors its signature).
mcp.tool(name="register_hex_tiles", description=register_hex_tiles.__doc__)(
    _register_hex_tiles_tool
)


_STATUS_POLL_MAX_WAIT_SECONDS = 60


def _apply_render_opts(result: dict, color_scale: str, layer_style: str) -> dict:
    """Re-render the recipe with non-default render options. color_scale and
    layer_style are pure render choices — they never change the tiles — so they
    are applied here at the tool boundary by rebuilding {source, layer} from the
    already-populated stats in `result`, rather than threaded through the content
    hash or build. No-op unless status="done" and at least one option is
    non-default."""
    cs = (color_scale or "linear").lower()
    ls = (layer_style or "fill").lower()
    if result.get("status") == "done" and (cs == "log" or ls == "fill-extrusion"):
        result.update(render_recipe(
            result, result["tile_url_template"], color_scale=cs, layer_style=ls,
        ))
    return result


def _done_response(base: dict, meta: dict) -> dict:
    """Build a status='done' response from either an S3 metadata dict or
    a build_hex_tiles return value — both have the same shape. Includes the
    paste-ready render recipe (source + layer) so the agent renders
    the result without further branching."""
    return {
        **base,
        "status": "done",
        "bounds": meta["bounds"],
        "finest_res": meta["finest_res"],
        "min_res": meta["min_res"],
        "zoom_offset": meta["zoom_offset"],
        "value_columns": meta["value_columns"],
        "value_stats": meta["value_stats"],
        "layer_name": meta.get("layer_name", MVT_LAYER_NAME),
        "feature_count_finest": meta["feature_count_finest"],
        **render_recipe(meta, base["tile_url_template"]),
    }


def _status_check_once(hash: str, con=None):
    """One non-blocking status probe. Returns (kind, payload) where kind is
    "done" | "failed" | "running" | "unknown" and payload is the response dict.
    "running" means the caller should keep waiting (a local build is in flight,
    or another pod holds a fresh lock); the others are terminal for this poll.
    No sleeping here — the looping/waiting lives in the callers."""
    if con is None:
        con = _get_tile_con().cursor()
    paths = tile_paths_for_hash(hash)
    base = {"hash": hash, "tile_url_template": paths["tile_url_template"]}

    cached = read_existing_metadata(con, paths["output_uri"])
    if cached is not None and "bounds" in cached and "feature_count_finest" in cached:
        return ("done", _done_response(base, cached))

    failed = read_failed(con, paths["output_uri"])
    if failed is not None:
        return ("failed", {**base, "status": "failed", "error": failed.get("error", "")})

    with _jobs_lock:
        job = _jobs.get(hash)

    if job is None:
        # No local job — another pod may own this build. Consult lock.json.
        lock = read_lock(con, paths["output_uri"])
        if lock is None or lock_is_stale(lock):
            return ("unknown", {**base, "status": "unknown"})
        return ("running", {
            **base, "status": "running",
            "elapsed_seconds": round(time.time() - lock["started_at"], 1),
        })

    future = job["future"]
    if future.done():
        exc = future.exception()
        if exc is not None:
            return ("failed", {**base, "status": "failed", "error": str(exc)})
        return ("done", _done_response(base, future.result()))
    return ("running", {
        **base, "status": "running",
        "elapsed_seconds": round(time.time() - job["started_at"], 1),
    })


def get_hex_tile_status(
    hash: str, wait_seconds: int = 0, color_scale: str = "linear", layer_style: str = "fill",
) -> dict:
    """Poll the status of a pyramid build started by register_hex_tiles.

    Call this when register_hex_tiles returned {status: "running"}.

    Long-poll: pass `wait_seconds` (clamped to [0, 60]) to let the server
    block until either the build finishes OR the wait expires. Use
    wait_seconds=30 as a default — you get the final result if it finishes
    in that window, otherwise one "still running" response so you know to
    call again. Do NOT poll faster than this; rapid polling wastes turns
    without giving the build time to make progress. wait_seconds=0 returns
    immediately (the legacy non-blocking poll).

    `color_scale` ("linear" default, or "log") and `layer_style` ("fill"
    default, or "fill-extrusion") restyle the recipe returned on completion,
    exactly as in register_hex_tiles — pass them here so a build you polled
    comes back already styled, without a second register call.

    Returns one of:
    - {hash, tile_url_template, status: "done", bounds, value_stats, ...} —
      pyramid is on disk and renderable. Use bounds + value_stats with the
      map client (fit_bounds, palette domain).
    - {hash, tile_url_template, status: "running", elapsed_seconds} —
      still building. Call again with wait_seconds=30.
    - {hash, tile_url_template, status: "failed", error} — build raised.
      You may re-submit register_hex_tiles with adjusted parameters.
    - {hash, tile_url_template, status: "unknown"} — no record of this hash
      in the current server process and no completed tileset on disk. The
      hash may be for a different server, or the build may never have
      started. Re-submit register_hex_tiles to (re-)start it.

    Idempotent — safe to call repeatedly. Hash is the value returned by
    register_hex_tiles.
    """
    # Sync core, kept for direct unit testing. Loops over the one-shot check
    # with time.sleep; the served tool is the async wrapper below.
    wait_seconds = max(0, min(int(wait_seconds or 0), _STATUS_POLL_MAX_WAIT_SECONDS))
    con = _get_tile_con().cursor()
    deadline = time.time() + wait_seconds
    while True:
        kind, payload = _status_check_once(hash, con)
        if kind != "running" or time.time() >= deadline:
            return _apply_render_opts(payload, color_scale, layer_style)
        time.sleep(min(2.0, max(0.1, deadline - time.time())))


async def _get_hex_tile_status_tool(
    hash: str, wait_seconds: int = 0, color_scale: str = "linear", layer_style: str = "fill",
) -> dict:
    # Served MCP tool. Truly async long-poll: `await anyio.sleep` frees the
    # event loop (vs the old time.sleep-on-the-loop that blocked /healthz and
    # every other request for up to 60s per poll — #176), and each one-shot
    # S3 check is offloaded to a worker thread.
    wait_seconds = max(0, min(int(wait_seconds or 0), _STATUS_POLL_MAX_WAIT_SECONDS))
    con = _get_tile_con().cursor()
    deadline = time.time() + wait_seconds
    while True:
        kind, payload = await anyio.to_thread.run_sync(_status_check_once, hash, con)
        if kind != "running" or time.time() >= deadline:
            return _apply_render_opts(payload, color_scale, layer_style)
        await anyio.sleep(min(2.0, max(0.1, deadline - time.time())))


mcp.tool(name="get_hex_tile_status", description=get_hex_tile_status.__doc__)(
    _get_hex_tile_status_tool
)


def mount_tiles(app):
    """Mount the /tiles route onto the Starlette app and ensure tile con is ready."""
    # Pre-initialize the connection so first tile request is fast.
    con = _get_tile_con()
    app.state.tile_con = con
    app.add_route("/tiles/{namespace}/{name}/{z:int}/{x:int}/{y:int}.pbf", serve_tile)
    # Same-origin metadata sidecar so clients can read value_stats/bounds by
    # hash instead of routing the large JSON through an LLM's tool-call args.
    app.add_route("/tiles/{namespace}/{name}/metadata.json", serve_metadata, methods=["GET"])

# -------------------------------------------------------------------------
# 9. OPTIONAL BEARER TOKEN AUTH
# -------------------------------------------------------------------------
_MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # /healthz must remain reachable to the kubelet probe even with auth on;
        # /version is deliberately public too (version discovery is the whole point).
        if request.url.path in ("/healthz", "/version"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        supplied = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), _MCP_AUTH_TOKEN.encode()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


async def _healthz(_request):
    # Async, no executor work — fails fast if uvicorn's event loop is starved
    # (e.g. a runaway DuckDB query saturating CPU/memory on the pod). That's the
    # signal we want: a wedged pod becomes NotReady within ~15s and HAProxy
    # stops routing to it. See issue #157.
    return JSONResponse({"ok": True, "version": APP_VERSION})


async def _version(_request):
    # App version + git SHA of the running image (issue #221). Lets consumers and
    # ops confirm what's deployed without cluster access.
    return JSONResponse({"version": APP_VERSION, "git_sha": GIT_SHA})

# -------------------------------------------------------------------------
# 10. SERVER START
# -------------------------------------------------------------------------
def _enforce_stac_startup_gate() -> bool:
    """Decide whether to boot when the STAC root catalog failed to load.

    Normally, an unreachable root catalog means serving would give clients a
    useless empty catalog, so we exit non-zero and let Kubernetes restart the
    pod for a fresh attempt against whatever S3 looks like now. (Child failures —
    a partial catalog — are fine: the resilience design serves what loaded and
    records the rest in STAC_LOAD_ERRORS for list_datasets's footer.)

    But during a sustained Ceph outage that fail-fast prevents the server from
    booting at all — which defeats the source.coop mirror fallback (#260): a
    running process can still serve known datasets and resolve source.coop STAC
    entries directly. STAC_ALLOW_DEGRADED_START=true opts into booting anyway
    with an empty catalog. Returns True if starting degraded, False if the root
    loaded fine. Default behavior (flag unset) is unchanged: exit(1).
    """
    if "__root__" not in STAC_LOAD_ERRORS:
        return False
    reason = STAC_LOAD_ERRORS["__root__"]
    allow_degraded = os.environ.get("STAC_ALLOW_DEGRADED_START", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if allow_degraded:
        print(
            "⚠️ STAC root catalog unreachable at startup — booting in DEGRADED mode "
            "(STAC_ALLOW_DEGRADED_START set). Discovery (browse_stac_catalog / "
            "list_datasets) will be empty until the catalog is reachable; clients can "
            "still query known datasets and pass source.coop STAC entries directly. "
            f"Reason: {reason}",
            file=sys.stderr,
        )
        return True
    print(
        "💀 STAC root catalog unreachable at startup — exiting so k8s can restart "
        "and retry. Set STAC_ALLOW_DEGRADED_START=true to boot anyway (e.g. to serve "
        f"the source.coop mirror during a Ceph outage). Reason: {reason}",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    _enforce_stac_startup_gate()

    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    mount_tiles(app)
    app.add_route("/healthz", _healthz, methods=["GET"])
    app.add_route("/version", _version, methods=["GET"])

    if _MCP_AUTH_TOKEN:
        app.add_middleware(_BearerAuthMiddleware)
        print("🔒 Auth enabled (MCP_AUTH_TOKEN is set)", file=sys.stderr)
    else:
        print("🔓 Auth disabled (MCP_AUTH_TOKEN not set)", file=sys.stderr)

    print("🚀 Starting DuckDB MCP Server...", file=sys.stderr)
    print(f"📂 STAC catalog: {STAC_CATALOG_URL}", file=sys.stderr)
    print(f"📊 Datasets loaded: {len(STAC_DATASETS)}", file=sys.stderr)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
