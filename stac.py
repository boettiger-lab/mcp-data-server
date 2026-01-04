import pystac
from mcp.server.fastmcp import FastMCP

# ------------------------------------------------------------------
# 1. DYNAMIC DATASETS (STAC Integration)
# ------------------------------------------------------------------
STAC_API_URL = "https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json"

# Create MCP instance for decorators (will be used when imported by server.py)
mcp = FastMCP("STAC-Catalog")

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
            # Get providers info
            providers = child.providers or []
            producer = next((p.name for p in providers if 'producer' in (p.roles or [])), 'Unknown')
            
            # Get available formats
            formats = child.summaries.get_list('formats') if child.summaries else []
            formats_str = ', '.join(formats) if formats else 'N/A'
            
            # Get documentation links
            docs = [link for link in child.links if link.rel == 'documentation']
            docs_str = docs[0].href if docs else 'N/A'
            
            # Get license
            license_info = child.license or 'N/A'
            
            # Get assets (data URLs)
            assets_info = []
            if child.assets:
                for asset_key, asset in child.assets.items():
                    # Convert HTTPS URLs to S3 URLs for partitioned parquet (directories)
                    href = asset.href
                    # Check if it's a directory path (doesn't end in a file extension) and not PMTiles/COG
                    if (href.startswith('https://s3-west.nrp-nautilus.io/') and 
                        not href.endswith('.parquet') and 
                        not href.endswith('.pmtiles') and
                        not href.endswith('.tif') and
                        not href.endswith('.tiff')):
                        # Convert https://s3-west.nrp-nautilus.io/bucket/path/ to s3://bucket/path/
                        href = href.replace('https://s3-west.nrp-nautilus.io/', 's3://')
                    
                    asset_desc = f"  • {asset.title}: {href}"
                    if hasattr(asset, 'description') and asset.description:
                        asset_desc += f"\n    ({asset.description})"
                    assets_info.append(asset_desc)
            
            assets_str = '\n'.join(assets_info) if assets_info else '  (No direct assets)'
            
            # Extract useful metadata for the LLM
            info = f"""**Dataset:** {child.title}
**ID:** {child.id}
**Description:** {child.description}
**Producer:** {producer}
**Formats:** {formats_str}
**License:** {license_info}
**Documentation:** {docs_str}

**Available Assets:**
{assets_str}
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