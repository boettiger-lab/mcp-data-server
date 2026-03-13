import os
import re
import duckdb
import uvicorn
import sys
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from stac import STAC_DATASETS, STAC_CATALOG_URL, list_datasets as _stac_list, get_dataset as _stac_get

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
GUIDE_RAW = load_text_file("datasets.md")
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
3. **DISCOVER PATHS:** Use `list_datasets` and `get_dataset` tools to find S3 paths and column schemas.

### 📂 SQL DATA GUIDE
{GUIDE_RAW}

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
def get_isolated_db():
    conn = duckdb.connect(database=":memory:")
    try:
        if SETUP_SQL: conn.sql(SETUP_SQL)
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
def list_datasets() -> str:
    """List all available datasets with their collection IDs and titles.
    Call this first to discover what data is available before writing SQL queries."""
    return _stac_list()

@mcp.tool()
def get_dataset(dataset_id: str) -> str:
    """Get detailed metadata for a dataset: S3 parquet paths, column schemas, and descriptions.
    Use the collection ID from list_datasets."""
    return _stac_get(dataset_id)

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
def query(sql_query: str) -> str:
    """Placeholder (overwritten below)."""
    print(f"🔍 Executing: {sql_query}", file=sys.stderr)
    try:
        with get_isolated_db() as db:
            result = db.sql(sql_query)
            if result is None: return "Command executed successfully."

            df = result.limit(50).df()
            if df.empty: return "No results found."
            return df.to_markdown(index=False)

    except Exception as e:
        return f"SQL Error: {str(e)}"

query.__doc__ = f"""
Executes optimized DuckDB SQL.
STRICTLY FOLLOW THE RULES BELOW.

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
