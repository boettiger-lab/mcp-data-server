import os
import re
import duckdb
import uvicorn
import sys
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# -------------------------------------------------------------------------
# 1. INITIALIZATION
# -------------------------------------------------------------------------
# Disable DNS rebinding protection for compatibility with K8s ingress
mcp = FastMCP(
    "DuckDB-S3-Geo-Isolated",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

# -------------------------------------------------------------------------
# 2. CONFIGURATION & FILE LOADING
# -------------------------------------------------------------------------
def load_text_file(filename):
    """Robust file loader that checks standard paths."""
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

# Load static configuration into memory
SETUP_RAW = load_text_file("query-setup.md")
SETUP_SQL = parse_setup_sql(SETUP_RAW)
CATALOG_RAW = load_text_file("datasets.md")
OPTIM_RAW = load_text_file("query-optimization.md")
H3_RAW = load_text_file("h3-guide.md")
ROLE_RAW = load_text_file("assistant-role.md")

# -------------------------------------------------------------------------
# 3. CONTEXT DEFINITIONS
# -------------------------------------------------------------------------
# Context to be injected into the Tool Description.
# This ensures clients that don't support explicit Prompts/Resources
# still receive the necessary schema and rules to function correctly.
TOOL_INJECTED_CONTEXT = f"""
---
### ⚠️ CRITICAL INSTRUCTIONS (READ BEFORE GENERATING SQL)
1. **AVAILABLE DATASETS (SCHEMA & S3 PATHS):**
{CATALOG_RAW}

2. **OPTIMIZATION RULES:**
{OPTIM_RAW}

3. **H3 SPATIAL MATH:**
{H3_RAW}
---
"""

# -------------------------------------------------------------------------
# 4. ISOLATION ENGINE
# -------------------------------------------------------------------------
@contextmanager
def get_isolated_db():
    """
    Creates a fresh, in-memory DuckDB connection for every request.
    This ensures complete isolation between tool calls.
    """
    conn = duckdb.connect(database=":memory:")
    try:
        if SETUP_SQL: conn.sql(SETUP_SQL)
        yield conn
    finally:
        conn.close()

# -------------------------------------------------------------------------
# 5. MCP RESOURCES (Schema Browsing)
# -------------------------------------------------------------------------
DATA_CATALOG = {}
if CATALOG_RAW:
    sections = re.split(r'(\*\*\d+\..*?\*\*)', CATALOG_RAW)
    DATA_CATALOG["_intro"] = sections[0].strip() if sections else ""
    for i in range(1, len(sections), 2):
        header = sections[i]
        body = sections[i+1]
        clean_key = re.sub(r'[\*\d\.]', '', header).strip().lower().split('(')[0].strip().replace(' ', '_')
        DATA_CATALOG[clean_key] = header + "\n" + body.strip()

@mcp.resource("catalog://list")
def list_datasets() -> str:
    return CATALOG_RAW

@mcp.resource("catalog://{name}")
def get_dataset_details(name: str) -> str:
    if name in DATA_CATALOG: return DATA_CATALOG[name]
    for key, val in DATA_CATALOG.items():
        if name in key: return val
    return "Dataset not found."

# -------------------------------------------------------------------------
# 6. MCP PROMPTS (Personas)
# -------------------------------------------------------------------------
@mcp.prompt("geospatial-analyst")
def analyst_persona() -> str:
    """Activates the Expert Analyst persona."""
    return f"""
    {ROLE_RAW}
    
    You have access to these datasets:
    {CATALOG_RAW}
    
    Follow these rules:
    {OPTIM_RAW}
    {H3_RAW}
    """

# -------------------------------------------------------------------------
# 7. TOOL DEFINITIONS
# -------------------------------------------------------------------------
def query(sql_query: str) -> str:
    """Placeholder docstring (overwritten during registration)."""
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

# -------------------------------------------------------------------------
# 8. REGISTRATION & STARTUP
# -------------------------------------------------------------------------
# Inject context into the docstring before registration.
query.__doc__ = f"""
Executes optimized DuckDB SQL queries on the geospatial lakehouse.

{TOOL_INJECTED_CONTEXT}
"""

# Manually register the tool with the modified docstring
mcp.tool()(query)

if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    
    print("🚀 Starting DuckDB MCP Server...", file=sys.stderr)
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
    