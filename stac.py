"""
STAC catalog integration — fetches dataset metadata at startup and exposes
it as MCP resources for client discovery.

The STAC catalog describes harmonized, cloud-native datasets that are
co-located with this MCP server on the same Kubernetes cluster, enabling
high-speed internal reads via the Ceph S3 endpoint.
"""

import os
import sys
import pystac


STAC_CATALOG_URL = os.environ.get(
    "STAC_CATALOG_URL",
    "https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json",
)


def _href_to_s3(href: str) -> str:
    """Convert an HTTPS S3 URL to an s3:// path for DuckDB read_parquet()."""
    if href.startswith("https://s3-west.nrp-nautilus.io/"):
        return href.replace("https://s3-west.nrp-nautilus.io/", "s3://")
    return href


def _format_columns(table_cols: list) -> list[str]:
    """Format a list of table:columns dicts into markdown lines."""
    if not table_cols:
        return []
    display_cols = [
        c for c in table_cols
        if c.get("name", "").lower() not in ("geometry", "geom", "bbox")
    ]
    h3_cols = [c for c in display_cols if c.get("name", "") in ("h0", "h8", "h9", "h10", "h11")]
    other_cols = [c for c in display_cols if c not in h3_cols][:20]

    lines = []
    if other_cols:
        for c in other_cols:
            desc = f" — {c['description']}" if c.get("description") else ""
            lines.append(f"    - `{c['name']}` ({c.get('type', '?')}){desc}")
    if h3_cols:
        lines.append(f"    - H3 index columns: {', '.join(c['name'] for c in h3_cols)}")
    return lines


def _extract_parquet_assets(col) -> list[str]:
    """Extract parquet/hex asset lines (with inline column schemas) from a collection's assets."""
    assets = []
    for asset_id, asset in (col.assets or {}).items():
        href = asset.href
        atype = asset.media_type or ""
        title = asset.title or asset_id

        if "parquet" in atype or href.endswith(".parquet") or href.endswith("/") or "/hex/" in href:
            # Skip PMTiles and COGs that might match loosely
            if "pmtiles" in atype or href.endswith(".pmtiles"):
                continue
            if "tif" in atype or href.endswith(".tif") or href.endswith(".tiff"):
                continue
            s3 = _href_to_s3(href)
            if s3.endswith("/"):
                s3 = s3.rstrip("/") + "/**"
            size = asset.extra_fields.get("file:size")
            size_note = f" ({size/1024**3:.2f} GiB)" if size and size > 1024**2 else ""
            assets.append(f"  - {title}{size_note}: `read_parquet('{s3}')`")
            # Inline per-asset column schema if present
            asset_cols = asset.extra_fields.get("table:columns", [])
            col_lines = _format_columns(asset_cols)
            if col_lines:
                assets.extend(col_lines)
    return assets


def _extract_columns(col) -> list[str]:
    """Extract table:columns as markdown lines from collection-level extra_fields."""
    table_cols = col.extra_fields.get("table:columns", [])
    if not table_cols:
        return []

    lines = ["\nKey columns:"]
    lines.extend(_format_columns(table_cols))
    return lines


def _format_collection(col) -> str:
    """Build a compact markdown summary of one STAC collection.

    If the collection has direct children (e.g. wyoming-wildlife-lands has
    per-species sub-collections), expand one level to find assets and
    column schemas that aren't on the parent. Does NOT recurse further.
    """
    lines = []
    lines.append(f"**{col.title or col.id}**")
    lines.append(f"Collection ID: `{col.id}`")
    if col.description:
        lines.append(col.description)

    # Collect parquet assets from this level
    parquet_assets = _extract_parquet_assets(col)

    # Column schema from this level
    col_lines = _extract_columns(col)

    # Check for sub-children — some collections (wyoming-wildlife-lands,
    # pad-us, census) group sub-datasets as child collections, each with
    # their own assets and column schemas.
    sub_children = list(col.get_children())
    if sub_children:
        lines.append(f"\n**Sub-datasets ({len(sub_children)}):**\n")
        for sc in sub_children:
            sc_title = sc.title or sc.id
            sc_parquet = _extract_parquet_assets(sc)
            if sc_parquet:
                lines.append(f"*{sc_title}* (`{sc.id}`):")
                lines.extend(sc_parquet)
            # Use sub-child columns if parent has none
            if not col_lines:
                col_lines = _extract_columns(sc)
    elif parquet_assets:
        lines.append("\nSQL data (use with `query` tool):")
        lines.extend(parquet_assets)

    if col_lines:
        lines.extend(col_lines)

    return "\n".join(lines)


def fetch_stac_catalog() -> dict[str, str]:
    """Fetch the STAC catalog and return {collection_id: markdown_summary}."""
    try:
        cat = pystac.Catalog.from_file(STAC_CATALOG_URL)
        datasets = {}
        for child in cat.get_children():
            datasets[child.id] = _format_collection(child)
        print(f"📂 Loaded {len(datasets)} collections from STAC: {STAC_CATALOG_URL}", file=sys.stderr)
        return datasets
    except Exception as e:
        print(f"⚠️ Failed to load STAC catalog: {e}", file=sys.stderr)
        return {}


# Load once at startup
STAC_DATASETS = fetch_stac_catalog()


def fetch_stac_collections(catalog_url: str = None) -> dict[str, str]:
    """Fetch STAC catalog and return {collection_id: metadata_string}.

    Returns {collection_id: formatted_string} on success, or
    {"error": "Failed to load STAC catalog: <message>"} on failure.
    """
    url = catalog_url or STAC_CATALOG_URL
    try:
        cat = pystac.Catalog.from_file(url)
        datasets = {}
        for col in cat.get_children():
            lines = []
            lines.append(f"**{col.title or col.id}**")
            lines.append(f"Collection ID: {col.id}")
            if col.description:
                lines.append(col.description)

            # Producer from providers
            producer = "Unknown"
            for p in (col.providers or []):
                if hasattr(p, "roles") and p.roles and "producer" in p.roles:
                    producer = p.name
                    break
            lines.append(f"Producer: {producer}")

            # Formats from summaries
            formats = "N/A"
            if col.summaries:
                fmt_list = col.summaries.get_list("platform")
                if fmt_list:
                    formats = ", ".join(fmt_list)
            lines.append(f"Formats: {formats}")

            # License
            license_str = getattr(col, "license", None) or "N/A"
            lines.append(f"License: {license_str}")

            # Documentation links
            doc_links = [lnk.href for lnk in (col.links or []) if lnk.rel == "documentation"]
            lines.append(f"Documentation: {', '.join(doc_links) if doc_links else 'N/A'}")

            # Assets
            for asset_id, asset in (col.assets or {}).items():
                href = asset.href
                title = asset.title or asset_id
                if href.endswith("/"):
                    href = _href_to_s3(href)
                desc = asset.description
                line = f"{title}: {href}"
                if desc:
                    line += f"\n{desc}"
                lines.append(line)

            datasets[col.id] = "\n".join(lines)
        return datasets
    except Exception as e:
        return {"error": f"Failed to load STAC catalog: {e}"}


# Load once at startup (simple format for external consumers)
DATA_CATALOG = fetch_stac_collections()


def list_datasets() -> str:
    """List all available datasets from the STAC catalog."""
    if not STAC_DATASETS:
        return f"No datasets loaded. STAC catalog: {STAC_CATALOG_URL}"
    lines = [f"# Available Datasets ({len(STAC_DATASETS)} collections)\n"]
    lines.append(f"STAC catalog: `{STAC_CATALOG_URL}`\n")
    for cid, summary in STAC_DATASETS.items():
        first_line = summary.split("\n")[0]
        lines.append(f"- **{cid}**: {first_line}")
    return "\n".join(lines)


def get_dataset(dataset_id: str) -> str:
    """Get detailed metadata for a specific dataset."""
    if dataset_id in STAC_DATASETS:
        return STAC_DATASETS[dataset_id]
    # Fuzzy match
    for key, val in STAC_DATASETS.items():
        if dataset_id.lower() in key.lower():
            return val
    return f"Dataset '{dataset_id}' not found. Use list_datasets to see available datasets."
