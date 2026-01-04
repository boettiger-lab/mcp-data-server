import os
import re
import duckdb
import uvicorn
import sys
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# 1. INITIALIZE SERVER
# Disable DNS rebinding protection because we run behind a K8s ingress
mcp = FastMCP(
    "DuckDB-S3-Geo-Isolated",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

# -------------------------------------------------------------------------
# 2. CONFIG & FILE LOADING (Robust)
# -------------------------------------------------------------------------
def load_text_file(filename):
    """
    Attempts to load a file from multiple common locations (Dev vs Prod).
    """
    paths = [
        filename,                                     # Local dev
        os.path.join("/app", filename),               # Standard Container
        os.path.join(os.path.dirname(__file__), filename) # Relative to script
    ]
    
    for p in paths:
        if os.path.exists(p):
            # print(f"✅ Loaded {filename} from {p}", file=sys.stderr) # Debug
            with open(p, 'r') as f: return f.read()
            
    print(f"⚠️ Warning: Could not find {filename}. Checked: {paths}", file=sys.stderr)
    return ""

def parse_setup_sql(content):
    """Extracts SQL from the markdown block in query-setup.md"""
    match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
    return match.group(1).strip() if match else ""

# Load all configuration files into memory
SETUP_RAW = load_text_file("query-setup.md")
SETUP_SQL = parse_setup_sql(SETUP_RAW)

CATALOG_RAW = load_text_file("datasets.md")
OPTIM_RAW = load_text_file("query-optimization.md")
H3_RAW = load_text_file("h3-guide.md")

# -------------------------------------------------------------------------
# 3. CONTEXT INJECTION (For older clients that don't recognize resources)
# -------------------------------------------------------------------------
def get_catalog_summary():
    """Generates a clean bullet list of available datasets for the LLM."""
    if not CATALOG_RAW: return "No datasets found."
    summary = []
    # Regex extracts names like "**1. Vulnerable Carbon**"
    matches = re.findall(r'\*\*\d+\.\s(.*?)\*\*', CATALOG_RAW)
    for m in matches:
        summary.append(f"- {m}")
    return "\n".join(summary)

# This is the Master Context that will be injected into the Tool Description
# It forces the LLM to read this before it attempts to write any SQL.
SYSTEM_CONTEXT = f"""
---
### ⚠️ CRITICAL KNOWLEDGE FOR THIS TOOL
1. **AVAILABLE DATASETS:**
{get_catalog_summary()}

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
    This ensures no state leaks between users/queries.
    """
    conn = duckdb.connect(database=":memory:")
    try:
        # Initialize secrets and settings (takes ~5ms)
        if SETUP_SQL: conn.sql(SETUP_SQL)
        yield conn
    finally:
        conn.close()

# -------------------------------------------------------------------------
# 5. RESOURCES (For Deep Lookups)
# -------------------------------------------------------------------------
# We keep these for clients that support them, or for when the LLM
# wants to inspect specific table schema details.

DATA_CATALOG = {}
if CATALOG_RAW:
    # Split catalog into sections based on "**1. Name**" headers
    sections = re.split(r'(\*\*\d+\..*?\*\*)', CATALOG_RAW)
    DATA_CATALOG["_intro"] = sections[0].strip() if sections else ""
    for i in range(1, len(sections), 2):
        header = sections[i]
        body = sections[i+1]
        # Normalize key for lookup: "**1. Carbon**" -> "carbon"
        clean_key = re.sub(r'[\*\d\.]', '', header).strip().lower().split('(')[0].strip().replace(' ', '_')
        DATA_CATALOG[clean_key] = header + "\n" + body.strip()

@mcp.resource("catalog://list")
def list_datasets() -> str:
    """Returns the full raw catalog markdown."""
    return CATALOG_RAW

@mcp.resource("catalog://{name}")
def get_dataset_details(name: str) -> str:
    """Returns details for a specific dataset (fuzzy match)."""
    # Exact match
    if name in DATA_CATALOG: return DATA_CATALOG[name]
    # Fuzzy match
    for key, val in DATA_CATALOG.items():
        if name in key: return val
    return "Dataset not found."

# -------------------------------------------------------------------------
# 6. TOOLS (With Dynamic Context Injection)
# -------------------------------------------------------------------------
@mcp.tool()
def query(sql_query: str) -> str:
    """
    Executes optimized DuckDB SQL queries on the geospatial lakehouse.
    
    SYSTEM CONTEXT (READ BEFORE GENERATING SQL):
    {context}
    """
    # Log to stderr (visible in K8s logs)
    print(f"🔍 Executing: {sql_query}", file=sys.stderr)
    
    try:
        with get_isolated_db() as db:
            result = db.sql(sql_query)
            if result is None: return "Command executed successfully."
            
            # Limit rows to prevent blowing up the chat context
            df = result.limit(50).df()
            if df.empty: return "No results found."
            # Use tabulate for clean markdown tables
            return df.to_markdown(index=False)
    except Exception as e:
        return f"SQL Error: {str(e)}"

# 🔥 For DUMB MCP clients: Inject the context into the docstring dynamically
# This puts the dataset list & rules INSIDE the description the LLM reads.
query.__doc__ = query.__doc__.format(context=SYSTEM_CONTEXT)

# -------------------------------------------------------------------------
# 7. SERVER ENTRY POINT
# -------------------------------------------------------------------------
if __name__ == "__main__":
    # Create the ASGI app
    app = mcp.streamable_http_app()
    # Fix for some clients that mishandle trailing slashes
    app.router.redirect_slashes = False
    
    print("🚀 Starting DuckDB MCP Server on 0.0.0.0:8000...", file=sys.stderr)
    print(f"ℹ️  Injected {len(DATA_CATALOG)} datasets into tool context.", file=sys.stderr)
    
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        proxy_headers=True,      # Trust X-Forwarded-* (Ingress)
        forwarded_allow_ips="*"  # Allow all proxies
    )
