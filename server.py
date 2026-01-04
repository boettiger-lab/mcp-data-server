import os
import re
import duckdb
import pandas as pd
from mcp.server.fastmcp import FastMCP

# Initialize the MCP Server
mcp = FastMCP("DuckDB-S3-Geo")

# -------------------------------------------------------------------------
# 1. DATABASE INITIALIZATION (The Engine)
# -------------------------------------------------------------------------
# We create a persistent connection that stays open as long as the script runs.
con = duckdb.connect(database=":memory:")

def boot_database():
    """
    On startup, read query-setup.md and run the configuration SQL.
    This ensures S3 and H3 are ready before the user asks a question.
    """
    setup_file = "query-setup.md"
    if not os.path.exists(setup_file):
        print(f"❌ CRITICAL: {setup_file} not found. Database not configured.")
        return

    try:
        with open(setup_file, 'r') as f:
            content = f.read()
        
        # Regex to grab content between ```sql and ```
        match = re.search(r"```sql\n(.*?)\n```", content, re.DOTALL)
        if match:
            setup_sql = match.group(1).strip()
            # EXECUTE IMMEDIATELY
            con.sql(setup_sql)
            print("✅ Database configured (S3/H3 ready).")
        else:
            print("⚠️ No SQL block found in setup file.")
    except Exception as e:
        print(f"❌ Boot Error: {e}")

# Run boot sequence immediately on script load
boot_database()

# -------------------------------------------------------------------------
# 2. CATALOG PARSING (The Map)
# -------------------------------------------------------------------------
# We parse datasets.md once at startup into a dictionary.

def parse_catalog():
    dataset_file = "datasets.md"
    if not os.path.exists(dataset_file): return {}

    with open(dataset_file, 'r') as f:
        content = f.read()

    catalog = {}
    
    # Split by bold numbered headers (e.g., "**1. Global Lakes...")
    # This captures the header + the body text following it
    sections = re.split(r'(\*\*\d+\..*?\*\*)', content)
    
    # The first section is usually the intro/preamble
    catalog["_intro"] = sections[0].strip()

    # Loop through the regex matches (Header, Body, Header, Body...)
    for i in range(1, len(sections), 2):
        header = sections[i]
        body = sections[i+1]
        
        # specific logic to parse your markdown structure
        full_text = f"{header}\n{body.strip()}"
        
        # Create a search key: "vulnerable_carbon"
        # Remove bold markers, numbers, dots, and convert to snake_case
        clean_key = re.sub(r'[\*\d\.]', '', header).strip().lower().replace(' ', '_')
        clean_key = clean_key.split('(')[0].strip().replace(' ', '_') # Handle "(GLWD)"
        
        catalog[clean_key] = full_text
        
    return catalog

# Load catalog into memory immediately
DATA_CATALOG = parse_catalog()

# -------------------------------------------------------------------------
# 3. RESOURCES (The Interface to the Map)
# -------------------------------------------------------------------------

@mcp.resource("catalog://list")
def list_datasets() -> str:
    """
    Returns the list of available datasets and global instructions (H3, etc).
    The LLM reads this first to know what data is available.
    """
    if not DATA_CATALOG:
        return "No datasets available."
    
    # Start with the intro text (H3 instructions)
    output = [DATA_CATALOG.get("_intro", ""), "\n**Available Datasets:**"]
    
    # List the keys
    for key in DATA_CATALOG.keys():
        if key == "_intro": continue
        output.append(f"- {key}")
        
    output.append("\nTo see columns/schema, read: catalog://{dataset_name}")
    return "\n".join(output)

@mcp.resource("catalog://{name}")
def get_dataset_details(name: str) -> str:
    """
    Returns the specific schema, S3 path, and notes for a dataset.
    """
    # Exact match
    if name in DATA_CATALOG:
        return DATA_CATALOG[name]
    
    # Fuzzy match (if LLM guesses "carbon" instead of "vulnerable_carbon")
    for key in DATA_CATALOG:
        if name in key:
            return DATA_CATALOG[key]
            
    return f"Dataset '{name}' not found. Check catalog://list."

# -------------------------------------------------------------------------
# 4. TOOLS (The Interface to the Engine)
# -------------------------------------------------------------------------

@mcp.tool()
def query(sql_query: str) -> str:
    """
    Executes a SQL query.
    - AUTOMATICALLY configured for S3 and H3 (no setup needed).
    - Returns results as a Markdown table.
    - LIMIT output to 50 rows.
    """
    try:
        # Run the query
        # We explicitly cast to DataFrame to format as Markdown
        # This handles both SELECT (returns data) and other commands gracefully
        res = con.sql(sql_query)
        
        if res is None:
            return "Command executed successfully."
            
        # Convert to DF and limit to prevent overflowing context
        df = res.limit(50).df()
        
        if df.empty:
            return "Query ran successfully but returned no results."
            
        return df.to_markdown(index=False)
        
    except Exception as e:
        return f"SQL Error: {str(e)}"

if __name__ == "__main__":
    mcp.run()