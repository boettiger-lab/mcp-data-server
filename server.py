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
mcp = FastMCP(
    "DuckDB-S3-Geo-Isolated",
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
CATALOG_RAW = load_text_file("datasets.md")
OPTIM_RAW = load_text_file("query-optimization.md")
H3_RAW = load_text_file("h3-guide.md")
ROLE_RAW = load_text_file("assistant-role.md")

# -------------------------------------------------------------------------
# 3. CONTEXT INJECTION (PROMPT ENGINEERING)
# -------------------------------------------------------------------------
# We frame this as a "Strict Syntax Guide" rather than just "Context".
# This forces the model to abandon its training on standard "SELECT * FROM table".
TOOL_INJECTED_CONTEXT = f"""
---
### ⚠️ CRITICAL SQL RULES (MUST FOLLOW)
1. **NO TABLES EXIST:** The database is empty. You CANNOT write `FROM table_name`.
2. **USE PARQUET PATHS:** You MUST use `FROM read_parquet('s3://...')` for ALL queries.
3. **COPY PATHS EXACTLY:** Use the S3 paths listed in the Catalog below.

### 📂 DATA CATALOG (Source of Truth)
{CATALOG_RAW}

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
# 5. MCP RESOURCES (Schema Browsing for Smart Clients)
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
# 6. MCP PROMPTS (Personas for Smart Clients)
# -------------------------------------------------------------------------
@mcp.prompt("geospatial-analyst")
def analyst_persona() -> str:
    return f"""
    {ROLE_RAW}
    DATASETS:
    {CATALOG_RAW}
    RULES:
    {OPTIM_RAW}
    """

# -------------------------------------------------------------------------
# 7. TOOL DEFINITION & MANUAL REGISTRATION
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

# 💉 INJECTION: Force the strict rules into the tool description
query.__doc__ = f"""
Executes optimized DuckDB SQL. 
STRICTLY FOLLOW THE RULES BELOW.

{TOOL_INJECTED_CONTEXT}
"""

# ®️ REGISTER: Manually register the tool with the injected prompt
mcp.tool()(query)

# -------------------------------------------------------------------------
# 8. SERVER START
# -------------------------------------------------------------------------
if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    
    print("🚀 Starting DuckDB MCP Server (Strict Mode)...", file=sys.stderr)
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )