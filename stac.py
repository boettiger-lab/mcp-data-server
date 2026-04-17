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
import requests
from pystac.stac_io import DefaultStacIO


STAC_CATALOG_URL = os.environ.get(
    "STAC_CATALOG_URL",
    "https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json",
)

# Backwards-compatible: STAC_TIMEOUT alone still works as a single knob; the two
# new vars override it when set. Root is a hard prerequisite (generous timeout);
# children are individually skippable (tight timeout). See
# docs/superpowers/specs/2026-04-16-stac-catalog-resilience-design.md.
_STAC_ROOT_TIMEOUT = int(
    os.environ.get("STAC_ROOT_TIMEOUT", os.environ.get("STAC_TIMEOUT", "15"))
)
_STAC_CHILD_TIMEOUT = int(
    os.environ.get("STAC_CHILD_TIMEOUT", os.environ.get("STAC_TIMEOUT", "5"))
)
_STAC_FETCH_CONCURRENCY = int(os.environ.get("STAC_FETCH_CONCURRENCY", "8"))

# Legacy alias retained so external callers (if any) that read the old name still work.
_STAC_TIMEOUT = _STAC_CHILD_TIMEOUT

_S3_PUBLIC = "https://s3-west.nrp-nautilus.io/"
_S3_INTERNAL = (
    os.environ.get("S3_ENDPOINT_URL", "http://rook-ceph-rgw-nautiluss3.rook").rstrip("/") + "/"
)

# Navigational STAC link rel types excluded from _collection_to_dict output.
_NAV_RELS = {"root", "parent", "self", "child", "item"}


def _fuzzy_lookup(mapping: dict, key: str):
    """Look up *key* in *mapping*: exact → prefix → substring. Returns None if no match."""
    if key in mapping:
        return mapping[key]
    low = key.lower()
    # Prefix match (e.g. "census" matches "census-2024-state")
    for k, v in mapping.items():
        if k.lower().startswith(low):
            return v
    # Substring match
    for k, v in mapping.items():
        if low in k.lower():
            return v
    return None


class _TimeoutStacIO(DefaultStacIO):
    def __init__(self, token: str = None, timeout: int = None):
        self._token = token
        self._timeout = timeout if timeout is not None else _STAC_CHILD_TIMEOUT

    def read_text_from_href(self, href: str) -> str:
        if href.startswith(_S3_PUBLIC):
            href = _S3_INTERNAL + href[len(_S3_PUBLIC):]
        if href.startswith("http"):
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            resp = requests.get(href, timeout=self._timeout, headers=headers)
            resp.raise_for_status()
            return resp.text
        return super().read_text_from_href(href)


pystac.StacIO.set_default(_TimeoutStacIO())


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


def _format_collection(col, sub_children: list = None) -> str:
    """Build a compact markdown summary of one STAC collection.

    If the collection has direct children (e.g. wyoming-wildlife-lands has
    per-species sub-collections), expand one level to find assets and
    column schemas that aren't on the parent. Does NOT recurse further.

    *sub_children* may be passed to avoid a duplicate ``get_children()``
    network call when the caller already fetched them.
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
    if sub_children is None:
        sub_children = list(col.get_children())
    if sub_children:
        lines.append(f"\n**Sub-datasets ({len(sub_children)}):**\n")
        for sc in sub_children:
            sc_title = sc.title or sc.id
            sc_parquet = _extract_parquet_assets(sc)
            if sc_parquet:
                lines.append(f"*{sc_title}* (`{sc.id}`):")
                lines.extend(sc_parquet)
        lines.append(f"\nCall get_stac_details with a sub-dataset ID (e.g. `{sub_children[0].id}`) for column schemas and details.")
    elif parquet_assets:
        lines.append("\nSQL data (use with `query` tool):")
        lines.extend(parquet_assets)
        if col_lines:
            lines.extend(col_lines)

    return "\n".join(lines)


def _collection_to_dict(col, sub_children=None) -> dict:
    """Convert a pystac Collection to a structured dict for programmatic use.

    All asset hrefs are converted from HTTPS to s3:// form.
    Pass sub_children when already fetched to avoid extra network calls.
    """
    result: dict = {
        "id": col.id,
        "title": col.title,
        "description": col.description,
        "license": getattr(col, "license", None),
        "keywords": list(getattr(col, "keywords", None) or []),
    }

    # Providers
    result["providers"] = [
        {k: v for k, v in {
            "name": p.name,
            "description": getattr(p, "description", None),
            "roles": list(p.roles) if getattr(p, "roles", None) else None,
            "url": getattr(p, "url", None),
        }.items() if v is not None}
        for p in (col.providers or [])
    ]

    # Extent
    ext = col.extent
    result["extent"] = {
        "spatial": {"bbox": ext.spatial.bboxes if ext and ext.spatial else []},
        "temporal": {
            "interval": [
                [dt.isoformat() if dt else None for dt in interval]
                for interval in (ext.temporal.intervals if ext and ext.temporal else [])
            ]
        },
    }

    # External links (exclude navigational rel types)
    result["links"] = [
        {k: v for k, v in {
            "rel": lnk.rel,
            "href": lnk.href,
            "title": getattr(lnk, "title", None),
        }.items() if v is not None}
        for lnk in (col.links or [])
        if lnk.rel not in _NAV_RELS
    ]

    # Summaries
    if col.summaries:
        try:
            result["summaries"] = col.summaries.to_dict()
        except Exception:
            result["summaries"] = {}
    else:
        result["summaries"] = {}

    # All assets — every media type, with S3 path conversion
    assets = {}
    for asset_id, asset in (col.assets or {}).items():
        a: dict = {
            "href": _href_to_s3(asset.href),
            "type": asset.media_type,
            "title": asset.title,
            "description": asset.description,
        }
        # Merge extra_fields (table:columns, raster:bands, vector:layers, file:size, …)
        a.update(asset.extra_fields)
        assets[asset_id] = {k: v for k, v in a.items() if v is not None}
    result["assets"] = assets

    # Collection-level STAC extension fields
    for ext_key in ("table:columns", "vector:layers", "raster:bands"):
        val = col.extra_fields.get(ext_key)
        if val is not None:
            result[ext_key] = val

    # Child collection IDs — only populated when sub_children are provided
    if sub_children is not None:
        result["children"] = [sc.id for sc in sub_children]

    return {k: v for k, v in result.items() if v is not None}


# Parallel cache of structured dicts, populated alongside STAC_DATASETS at startup.
_STAC_RAW: dict[str, dict] = {}

# Populated by fetch_stac_catalog on the default-catalog path; keys are the best
# available identifier (real collection id when parse succeeded, else href tail),
# values are short reason strings. Cleared on each successful default-catalog load.
STAC_LOAD_ERRORS: dict[str, str] = {}


def fetch_stac_catalog(catalog_url: str = None, catalog_token: str = None) -> dict[str, str]:
    """Fetch the STAC catalog and return {collection_id: markdown_summary}."""
    url = catalog_url or STAC_CATALOG_URL
    try:
        stac_io = _TimeoutStacIO(token=catalog_token)
        cat = pystac.Catalog.from_file(url, stac_io=stac_io)
        datasets = {}
        raw: dict[str, dict] = {}
        for child in cat.get_children():
            sub_children = list(child.get_children())
            datasets[child.id] = _format_collection(child, sub_children=sub_children)
            raw[child.id] = _collection_to_dict(child, sub_children=sub_children)
            # Index child collections so get_dataset / get_collection work with child IDs
            for sub_child in sub_children:
                datasets[sub_child.id] = _format_collection(sub_child)
                raw[sub_child.id] = _collection_to_dict(sub_child)
        print(f"📂 Loaded {len(datasets)} collections from STAC: {url}", file=sys.stderr)
        # Update module-level structured cache only for the default catalog
        if not catalog_url:
            _STAC_RAW.update(raw)
        return datasets
    except Exception as e:
        print(f"⚠️ Failed to load STAC catalog: {e}", file=sys.stderr)
        return {}


# Load once at startup
STAC_DATASETS = fetch_stac_catalog()


def list_datasets(catalog_url: str = None, catalog_token: str = None) -> str:
    """List all available datasets from the STAC catalog."""
    if catalog_url:
        datasets = fetch_stac_catalog(catalog_url, catalog_token=catalog_token)
        url = catalog_url
    else:
        datasets = STAC_DATASETS
        url = STAC_CATALOG_URL
    if not datasets:
        return f"No datasets loaded. STAC catalog: {url}"
    lines = [f"# Available Datasets ({len(datasets)} collections)\n"]
    lines.append(f"STAC catalog: `{url}`\n")
    for cid, summary in datasets.items():
        first_line = summary.split("\n")[0]
        lines.append(f"- **{cid}**: {first_line}")
    return "\n".join(lines)


def get_dataset(dataset_id: str, catalog_url: str = None, catalog_token: str = None) -> str:
    """Get detailed metadata for a specific dataset."""
    if catalog_url:
        datasets = fetch_stac_catalog(catalog_url, catalog_token=catalog_token)
    else:
        datasets = STAC_DATASETS

    result = _fuzzy_lookup(datasets, dataset_id)
    if result is not None:
        return result

    # Cache miss (default catalog only): re-fetch in case datasets were added since startup
    if not catalog_url:
        refreshed = fetch_stac_catalog()
        if refreshed:
            STAC_DATASETS.update(refreshed)
            result = _fuzzy_lookup(STAC_DATASETS, dataset_id)
            if result is not None:
                return result
    return f"Dataset '{dataset_id}' not found. Use list_datasets to see available datasets."


def get_collection(collection_id: str, catalog_url: str = None, catalog_token: str = None) -> dict:
    """Return structured STAC collection metadata for programmatic use.

    Unlike get_stac_details (which returns markdown for LLM consumption),
    this returns the raw collection dict with:
    - All assets (parquet, PMTiles, COG, GeoJSON) with hrefs converted to s3://
    - Per-asset STAC extension fields (table:columns, raster:bands, vector:layers)
    - Full collection metadata (providers, extent, license, keywords, links)
    - Child collection IDs for parent containers

    Intended for app code (e.g. geo-agent) that builds map layers and system
    prompts programmatically from structured data.
    """
    if catalog_url:
        # On-demand fetch for non-default catalogs — avoid full-catalog iteration.
        stac_io = _TimeoutStacIO(token=catalog_token)
        try:
            cat = pystac.Catalog.from_file(catalog_url, stac_io=stac_io)
            # Direct top-level lookup (stops at first match).
            child = cat.get_child(collection_id)
            if child is not None:
                sub_children = list(child.get_children())
                return _collection_to_dict(child, sub_children=sub_children)
            # Not top-level — scan lazily for sub-children and fuzzy matches.
            low = collection_id.lower()
            prefix_hit = None
            substring_hit = None
            for top in cat.get_children():
                sub_children = list(top.get_children())
                for sc in sub_children:
                    if sc.id == collection_id:
                        return _collection_to_dict(sc)
                # Track best fuzzy match while iterating
                for candidate_id, candidate in [(top.id, top)] + [(sc.id, sc) for sc in sub_children]:
                    cl = candidate_id.lower()
                    if prefix_hit is None and cl.startswith(low):
                        sc_children = sub_children if candidate is top else []
                        prefix_hit = _collection_to_dict(candidate, sub_children=sc_children or None)
                    elif substring_hit is None and low in cl:
                        sc_children = sub_children if candidate is top else []
                        substring_hit = _collection_to_dict(candidate, sub_children=sc_children or None)
            if prefix_hit is not None:
                return prefix_hit
            if substring_hit is not None:
                return substring_hit
        except Exception as e:
            return {"error": f"Failed to fetch catalog: {e}"}
        return {"error": f"Collection '{collection_id}' not found. Use browse_stac_catalog to list available collections."}

    # Default catalog: look up from pre-populated cache.
    result = _fuzzy_lookup(_STAC_RAW, collection_id)
    if result is not None:
        return result

    # Cache miss: re-fetch.
    # fetch_stac_catalog() updates _STAC_RAW when catalog_url is None.
    fetch_stac_catalog()
    result = _fuzzy_lookup(_STAC_RAW, collection_id)
    if result is not None:
        return result

    return {"error": f"Collection '{collection_id}' not found. Use browse_stac_catalog to list available collections."}
