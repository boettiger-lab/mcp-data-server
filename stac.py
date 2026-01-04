import pystac
from mcp.server.fastmcp import FastMCP

# ------------------------------------------------------------------
# 1. DYNAMIC DATASETS (STAC Integration)
# ------------------------------------------------------------------
STAC_API_URL = "https://your-stac-catalog.com/catalog.json"

def fetch_stac_collections():
    """
    Connects to the STAC catalog and returns a simplified dict of datasets.
    This replaces the static 'datasets.md' parsing.
    """
    try:
        # Load the Catalog (Root)
        cat = pystac.Catalog.from_file(STAC_API_URL)
        
        datasets = {}
        # Iterate over child collections
        for child in cat.get_children():
            # Extract useful metadata for the LLM
            info = f"""
            **Dataset:** {child.title}
            **ID:** {child.id}
            **Description:** {child.description}
            **S3 Path:** {child.extra_fields.get('s3:path', 'Not specified')}
            **Keywords:** {', '.join(child.keywords or [])}
            """
            datasets[child.id] = info
            
        return datasets
    except Exception as e:
        return {"error": f"Failed to load STAC: {e}"}

# Load once at startup
DATA_CATALOG = fetch_stac_collections()

@mcp.resource("catalog://list")
def list_datasets() -> str:
    """Lists all datasets found in the STAC catalog."""
    return "\n".join([f"- {k}" for k in DATA_CATALOG.keys()])

@mcp.resource("catalog://{dataset_id}")
def get_dataset_schema(dataset_id: str) -> str:
    """Returns the STAC metadata for a specific dataset."""
    return DATA_CATALOG.get(dataset_id, "Dataset not found.")