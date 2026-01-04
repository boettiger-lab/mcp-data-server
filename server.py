import os
import re
import duckdb
import uvicorn
from contextlib import contextmanager
from mcp.server.fastmcp import FastMCP
from starlette.middleware.trustedhost import TrustedHostMiddleware

# Initialize MCP Server
mcp = FastMCP("DuckDB-S3-Geo-Isolated")

# -------------------------------------------------------------------------
# 1. LOAD CONFIG (Read-Only Global State)
# -------------------------------------------------------------------------
def load_text_file(filename):
    if not os.path.exists(filename): return ""
    with open(filename, 'r') as f: return f.read()

def parse_setup_sql(content):
    match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
    return match.group(1).strip() if match else ""

SETUP_RAW = load_text_file("query-setup.md")
SETUP_SQL = parse_setup_sql(SETUP_RAW)
CATALOG_RAW = load_text_file("datasets.md")

# -------------------------------------------------------------------------
# 2. ISOLATION ENGINE
# -------------------------------------------------------------------------
@contextmanager
def get_isolated_db():
    """
    Creates a FRESH DuckDB instance for every single request.
    This guarantees User A never sees User B's data/views.
    """
    conn = duckdb.connect(database=":memory:")
    try:
        # Fast setup (~5ms) - Secrets/Endpoints only
        if SETUP_SQL:
            conn.sql(SETUP_SQL)
        yield conn
    finally:
        conn.close() # Wipes memory instantly

# -------------------------------------------------------------------------
# 3. RESOURCES (Data Catalog)
# -------------------------------------------------------------------------
# (Catalog parsing logic omitted for brevity, same as previous version)
def parse_catalog_to_dict():
    catalog = {}
    if not CATALOG_RAW: return {}
    sections = re.split(r'(\*\*\d+\..*?\*\*)', CATALOG_RAW)
    catalog["_intro"] = sections[0].strip()
    for i in range(1, len(sections), 2):
        header = sections[i]
        body = sections[i+1]
        clean_key = re.sub(r'[\*\d\.]', '', header).strip().lower().split('(')[0].strip().replace(' ', '_')
        catalog[clean_key] = f"{header}\n{body.strip()}"
    return catalog

DATA_CATALOG = parse_catalog_to_dict()

@mcp.resource("catalog://list")
def list_datasets() -> str:
    output = [DATA_CATALOG.get("_intro", ""), "\n**Available Datasets:**"]
    for key in DATA_CATALOG.keys():
        if key == "_intro": continue
        output.append(f"- {key}")
    return "\n".join(output)

@mcp.resource("catalog://{name}")
def get_dataset_details(name: str) -> str:
    if name in DATA_CATALOG: return DATA_CATALOG[name]
    for key in DATA_CATALOG:
        if name in key: return DATA_CATALOG[key]
    return "Not found."

# -------------------------------------------------------------------------
# 4. TOOLS (Execution)
# -------------------------------------------------------------------------
@mcp.tool()
def query(sql_query: str) -> str:
    """Run SQL in an isolated, ephemeral environment."""
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
# 5. SERVER ENTRY POINT (Streamable HTTP)
# -------------------------------------------------------------------------
if __name__ == "__main__":
    # Streamable HTTP uses a single endpoint (default: /mcp)
    # It supports both GET (handshake) and POST (messages) on the same URL.
    app = mcp.streamable_http_app()
    
    # Disable host header validation for k8s ingress
    # The app has built-in host validation that needs to be disabled
    app.allowed_hosts = ["*"]
    
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        proxy_headers=True,  # Trust X-Forwarded-* headers from proxy
        forwarded_allow_ips="*"  # Allow any proxy IP (we're behind k8s ingress)
    )
