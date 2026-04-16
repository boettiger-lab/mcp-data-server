import hmac
import os
import re
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

# Optional swappable query backend (e.g. GPU-accelerated).
# If query_backend.py is present alongside server.py it replaces the built-in DuckDB engine.
try:
    import query_backend as _query_backend
    _CUSTOM_BACKEND = True
except ImportError:
    _CUSTOM_BACKEND = False

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
   - `read_parquet('s3://public-wdpa/hex/**')` — bucket root, recursive glob
   - `read_parquet('s3://public-padus/padus-4-1/fee/hex/h0=*/data_0.parquet')` — nested path, partition glob

   Both are correct. Copy-paste the path from get_stac_details. In SQL examples below, `<STAC_HEX_PATH>` means "insert the exact path from get_stac_details here."

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
    if _CUSTOM_BACKEND:
        return _query_backend.execute(sql_query, s3_key=s3_key, s3_secret=s3_secret, s3_endpoint=s3_endpoint, s3_scope=s3_scope)
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
from tiles.endpoint import serve_tile
from tiles.db import build_tile_connection
from tiles.pyramid import register_hex_tiles as _register_hex_tiles


# Module-level persistent connection. Initialized lazily at first use OR
# at startup via the lifespan in mount_tiles().
_tile_con = None


def _get_tile_con():
    global _tile_con
    if _tile_con is None:
        _tile_con = build_tile_connection()
    return _tile_con


def register_hex_tiles(
    sql: str,
    finest_res: int,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = 4,
) -> dict:
    """Materialize a partitioned H3 hex pyramid to public object storage and return
    a MapLibre-compatible vector tile URL template.

    Use this tool for H3 hex datasets too large to return as markdown table —
    roughly >100k cells, or any case where the user wants to visualize hexes
    directly on the map (rather than color an existing polygon layer).

    Input SQL contract:
    - First column must be an H3 index at resolution `finest_res`.
    - For agg="COUNT": no other columns required. Output has a single `count`
      column (row count per hex). If the user SQL returns extra columns, they
      are ignored.
    - For agg in {AVG, SUM, MIN, MAX}: at least one numeric value column must
      follow the H3 index. Each is aggregated by `agg` at each coarser
      resolution down to `min_res`.

    Returns a dict with:
    - `tile_url_template`: MapLibre vector tile URL with {z}/{x}/{y} placeholders.
    - `value_columns`: list of output column names available as MVT feature
      properties. For agg="COUNT" this is ["count"]; otherwise the user's
      value columns.
    - `value_stats`: {<col>: {"by_res": {"<res>": {"min": <num>, "max": <num>}}}}.
      Per-resolution min/max for each value column — pass to the map client.
    - `layer_name`: the MVT source-layer name; use as `source-layer` in the client.
    - `hash`, `bounds`, `finest_res`, `min_res`, `zoom_offset`, `feature_count_finest`:
      tileset metadata.

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
    con = _get_tile_con()
    return _register_hex_tiles(
        con=con, sql=sql, finest_res=finest_res, min_res=min_res,
        agg=agg, zoom_offset=zoom_offset,
    )


# register_hex_tiles is not yet ready for production — see GitHub issue
# mcp.tool()(register_hex_tiles)


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
        auth = request.headers.get("Authorization", "")
        supplied = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), _MCP_AUTH_TOKEN.encode()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

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
