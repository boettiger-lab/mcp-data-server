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
from stac import STAC_DATASETS, STAC_CATALOG_URL, list_datasets as _stac_list, get_dataset as _stac_get

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

DATA_CATALOG = STAC_DATASETS

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
3. **DISCOVER PATHS:** Call `browse_stac_catalog` then `get_stac_details` to get exact S3 paths and schemas — NEVER guess or hardcode paths.

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
def browse_stac_catalog(catalog_url: str = None, catalog_token: str = None) -> str:
    """Browse the full public STAC catalog to discover datasets not already loaded in your app.
    Use when the user asks about data outside your pre-configured layers.
    Optionally provide catalog_url to use a custom STAC catalog instead of the server default.
    Optionally provide catalog_token (Bearer token) if the catalog requires authentication."""
    return _stac_list(catalog_url, catalog_token)

@mcp.tool()
def get_stac_details(dataset_id: str, catalog_url: str = None, catalog_token: str = None) -> str:
    """Fetch metadata (parquet paths, column schemas) for any STAC collection by ID.
    Use for datasets outside your pre-loaded app catalog — for datasets already in your app,
    use the local get_dataset_details tool instead.
    Optionally provide catalog_url and catalog_token if using a private STAC catalog."""
    return _stac_get(dataset_id, catalog_url, catalog_token)

def get_dataset_details(dataset_id: str) -> str:
    return _stac_get(dataset_id)

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
# 9. SERVER START
# -------------------------------------------------------------------------
if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False

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
