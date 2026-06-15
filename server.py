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
mcp = FastMCP(
    "DuckDB-S3-Geo-Isolated",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

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
@contextmanager
def get_isolated_db(s3_key: str = None, s3_secret: str = None, s3_endpoint: str = None, s3_scope: str = None):
    conn = duckdb.connect(database=":memory:")
    try:
        for stmt in (s.strip() for s in SETUP_SQL.split(";") if s.strip()):
            try:
                conn.sql(stmt)
            except Exception as e:
                print(f"⚠️ Setup statement skipped: {stmt!r}: {e}", file=sys.stderr)
        if s3_key and s3_secret:
            endpoint = s3_endpoint or "s3-west.nrp-nautilus.io"
            use_ssl = "false" if endpoint.startswith("rook") else "true"
            scope_clause = f", SCOPE '{s3_scope}'" if s3_scope else ""
            # Credentials injected here; intentionally not logged
            conn.sql(
                f"CREATE OR REPLACE SECRET client_s3 ("
                f"TYPE S3, KEY_ID '{s3_key}', SECRET '{s3_secret}', "
                f"ENDPOINT '{endpoint}', URL_STYLE 'path', USE_SSL '{use_ssl}'"
                f"{scope_clause})"
            )
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
def query(sql_query: str, s3_key: str = None, s3_secret: str = None, s3_endpoint: str = None, s3_scope: str = None) -> str:
    """Placeholder (overwritten below)."""
    print(f"🔍 Executing: {sql_query}", file=sys.stderr)
    try:
        with get_isolated_db(s3_key=s3_key, s3_secret=s3_secret, s3_endpoint=s3_endpoint, s3_scope=s3_scope) as db:
            result = db.sql(sql_query)
            if result is None: return "Command executed successfully."

            # Drop geometry columns — GEOMETRY('OGC:CRS84') crashes pandas conversion
            # (DuckDB issue: unsupported NumPy type). Geometry is not useful in tabular output.
            geom_cols = [c for c, t in zip(result.columns, result.dtypes) if "GEOMETRY" in str(t).upper()]
            if geom_cols:
                keep = [f'"{c}"' for c in result.columns if c not in geom_cols]
                result = result.select(", ".join(keep))

            df = result.limit(50).df()
            if df.empty: return "No results found."
            return df.to_markdown(index=False)

    except Exception as e:
        return f"SQL Error: {str(e)}"

query.__doc__ = f"""
Executes optimized DuckDB SQL against S3 parquet files.

BEFORE writing any SQL:
1. Call `browse_stac_catalog` to see all available dataset IDs and titles.
2. Call `get_stac_details` with the relevant dataset ID to get exact S3 paths and column schemas.
3. Use ONLY paths returned by those tools — never guess or hardcode any S3 URLs.

For private data, pass s3_key, s3_secret, and optionally s3_endpoint and s3_scope alongside the SQL query.
Use s3_scope (e.g. 's3://private-wyoming') when the query mixes private and public S3 paths so DuckDB routes each to the correct endpoint.
Credentials are scoped to this request only and never persisted.

{TOOL_INJECTED_CONTEXT}
"""

mcp.tool()(query)

# -------------------------------------------------------------------------
# 8b. TILE ENDPOINT — dynamic MVT for H3 hex visualization (see issue #4)
# -------------------------------------------------------------------------
import concurrent.futures
import threading
import time
from tiles.endpoint import serve_tile
from tiles.db import build_tile_connection
from tiles.pyramid import (
    MVT_LAYER_NAME,
    prepare_hex_tiles,
    build_hex_tiles,
    cached_result_dict,
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
            build_con = build_tile_connection()
            print(f"[tile-build] hash={h} START pod={_POD_ID}", file=sys.stderr)
            t0 = time.perf_counter()
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
# finest_res; min_res=2 is the coarsest level worth materializing. zoom_offset=2
# maps map-zoom z to H3 res z-2, keeping each tile to ~100-2000 hexes (a healthy
# MVT feature count) while preserving detail. The previous default (-1, i.e. res
# z+1) put 40k-130k hexes in every CA-scale tile — fat .pbf payloads and slow
# ST_AsMVT, for sub-pixel hexes MapLibre couldn't distinguish anyway (#178).
# Adding `finest_res` etc. to the docstring is almost certainly a mistake — see
# #125 for the trigger-tightening rationale and the discussion about param surface.
def register_hex_tiles(
    sql: str,
    agg: str = "COUNT",
    finest_res: int | None = None,
    min_res: int = 2,
    zoom_offset: int = 2,
) -> dict:
    """Materialize a partitioned H3 hex pyramid to public object storage and return
    a MapLibre-compatible vector tile URL template.

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

    Returns a dict with `status` ∈ {"done", "running", "failed"}:
    - status="done" (cache hit or fast build): full metadata is included —
      `tile_url_template` (MapLibre vector tile URL with {z}/{x}/{y}),
      `value_columns` (MVT feature properties: ["count"] for agg="COUNT",
      otherwise your value columns), `value_stats` ({<col>: {"by_res":
      {"<res>": {"min", "max"}}}} for client-side palette domain),
      `layer_name` (use as `source-layer`), plus `hash`, `bounds`,
      `finest_res`, `feature_count_finest`.
    - status="running": pyramid is being built in the background. You get
      `hash` and `tile_url_template` only. Call
      `get_hex_tile_status(hash, wait_seconds=30)` to poll — that long-polls
      server-side, so one call returns either the final result or a single
      "still running" response. Do NOT retry register_hex_tiles with
      different parameters; the original build will still finish, and
      re-submitting queues more work without cancelling the first one.
    - status="failed": build raised an error inline. `error` field has the
      exception message. Safe to re-submit with adjusted parameters.

    MapLibre usage:
        map.addSource(id, {type: 'vector', tiles: [tile_url_template], minzoom: 0, maxzoom: 14});
        map.addLayer({..., 'source-layer': layer_name, paint: {...}});

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
    read_con = _get_tile_con()
    plan = prepare_hex_tiles(
        con=read_con, sql=sql, agg=agg,
        finest_res=finest_res, min_res=min_res, zoom_offset=zoom_offset,
    )
    if plan["cached"] is not None:
        result = cached_result_dict(plan, plan["cached"])
        result["status"] = "done"
        return result

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
        return result
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


mcp.tool()(register_hex_tiles)


_STATUS_POLL_MAX_WAIT_SECONDS = 60


def _done_response(base: dict, source: dict) -> dict:
    """Build a status='done' response from either an S3 metadata dict or
    a build_hex_tiles return value — both have the same shape."""
    return {
        **base,
        "status": "done",
        "bounds": source["bounds"],
        "finest_res": source["finest_res"],
        "min_res": source["min_res"],
        "zoom_offset": source["zoom_offset"],
        "value_columns": source["value_columns"],
        "value_stats": source["value_stats"],
        "layer_name": source.get("layer_name", MVT_LAYER_NAME),
        "feature_count_finest": source["feature_count_finest"],
    }


def get_hex_tile_status(hash: str, wait_seconds: int = 0) -> dict:
    """Poll the status of a pyramid build started by register_hex_tiles.

    Call this when register_hex_tiles returned {status: "running"}.

    Long-poll: pass `wait_seconds` (clamped to [0, 60]) to let the server
    block until either the build finishes OR the wait expires. Use
    wait_seconds=30 as a default — you get the final result if it finishes
    in that window, otherwise one "still running" response so you know to
    call again. Do NOT poll faster than this; rapid polling wastes turns
    without giving the build time to make progress. wait_seconds=0 returns
    immediately (the legacy non-blocking poll).

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
    wait_seconds = max(0, min(int(wait_seconds or 0), _STATUS_POLL_MAX_WAIT_SECONDS))
    paths = tile_paths_for_hash(hash)
    base = {"hash": hash, "tile_url_template": paths["tile_url_template"]}

    cached = read_existing_metadata(_get_tile_con(), paths["output_uri"])
    if cached is not None and "bounds" in cached and "feature_count_finest" in cached:
        return _done_response(base, cached)

    failed = read_failed(_get_tile_con(), paths["output_uri"])
    if failed is not None:
        return {**base, "status": "failed", "error": failed.get("error", "")}

    with _jobs_lock:
        job = _jobs.get(hash)

    if job is None:
        # No local job — but another pod may own this build. Consult lock.json.
        lock = read_lock(_get_tile_con(), paths["output_uri"])
        if lock is None or lock_is_stale(lock):
            return {**base, "status": "unknown"}

        # Fresh lock from another pod. Long-poll S3 for metadata.json /
        # failed.json appearance up to wait_seconds. 2s granularity is fine —
        # this is server-internal, the LLM sees one tool call.
        deadline = time.time() + wait_seconds
        while True:
            cached = read_existing_metadata(_get_tile_con(), paths["output_uri"])
            if cached is not None and "bounds" in cached and "feature_count_finest" in cached:
                return _done_response(base, cached)
            failed_now = read_failed(_get_tile_con(), paths["output_uri"])
            if failed_now is not None:
                return {**base, "status": "failed", "error": failed_now.get("error", "")}
            if time.time() >= deadline:
                break
            time.sleep(min(2.0, max(0.1, deadline - time.time())))

        # Wait expired without resolution. Report running with elapsed from lock.
        return {
            **base,
            "status": "running",
            "elapsed_seconds": round(time.time() - lock["started_at"], 1),
        }

    future = job["future"]

    if not future.done() and wait_seconds > 0:
        try:
            result = future.result(timeout=wait_seconds)
            return _done_response(base, result)
        except concurrent.futures.TimeoutError:
            pass  # still running — fall through to the standard branch below
        except Exception as e:
            return {**base, "status": "failed", "error": str(e)}

    if future.done():
        exc = future.exception()
        if exc is not None:
            return {**base, "status": "failed", "error": str(exc)}
        return _done_response(base, future.result())

    return {**base, "status": "running",
            "elapsed_seconds": round(time.time() - job["started_at"], 1)}


mcp.tool()(get_hex_tile_status)


def mount_tiles(app):
    """Mount the /tiles route onto the Starlette app and ensure tile con is ready."""
    # Pre-initialize the connection so first tile request is fast.
    con = _get_tile_con()
    app.state.tile_con = con
    app.add_route("/tiles/{namespace}/{name}/{z:int}/{x:int}/{y:int}.pbf", serve_tile)

# -------------------------------------------------------------------------
# 9. OPTIONAL BEARER TOKEN AUTH
# -------------------------------------------------------------------------
_MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # /healthz must remain reachable to the kubelet probe even with auth on.
        if request.url.path == "/healthz":
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
    return JSONResponse({"ok": True})

# -------------------------------------------------------------------------
# 10. SERVER START
# -------------------------------------------------------------------------
if __name__ == "__main__":
    # If the STAC root catalog was unreachable at startup, serving would give
    # clients a useless empty catalog. Exit non-zero so Kubernetes restarts the
    # pod and gets a fresh attempt against whatever S3 looks like now. Child
    # failures (partial catalog) are fine — the resilience design serves what
    # loaded and records the rest in STAC_LOAD_ERRORS for list_datasets's footer.
    if "__root__" in STAC_LOAD_ERRORS:
        print(
            "💀 STAC root catalog unreachable at startup — exiting so k8s can "
            f"restart and retry. Reason: {STAC_LOAD_ERRORS['__root__']}",
            file=sys.stderr,
        )
        sys.exit(1)

    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    mount_tiles(app)
    app.add_route("/healthz", _healthz, methods=["GET"])

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
