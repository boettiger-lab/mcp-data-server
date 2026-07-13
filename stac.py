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
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pystac.stac_io import DefaultStacIO

import s3config


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
_STAC_FETCH_CONCURRENCY = int(os.environ.get("STAC_FETCH_CONCURRENCY", "16"))

# Child fetches that time out on the first pass are retried once, each with a
# longer per-child timeout. Rescues tail-latency failures without leaving
# otherwise-healthy collections out of the catalog. A persistent failure
# (both attempts time out) stays in STAC_LOAD_ERRORS.
_STAC_CHILD_RETRY_TIMEOUT = int(os.environ.get("STAC_CHILD_RETRY_TIMEOUT", "8"))

# Legacy alias retained so external callers (if any) that read the old name still work.
_STAC_TIMEOUT = _STAC_CHILD_TIMEOUT

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


def _child_identifier(href: str, title_hint: str = None, fetched_id: str = None) -> str:
    """Best-effort identifier for a STAC child — used for error reporting when the
    real collection `id` may not be available (fetch failed before JSON parse).

    Precedence:
    1. `fetched_id` if given — the real collection id
    2. The last path segment of the href (directory name for `.../dir/stac-collection.json`,
       or the file stem for `.../dir/name.json` / `.../dir/name-stac.json`)
    3. Optionally augmented with `title_hint` when present and non-redundant
    """
    if fetched_id:
        return fetched_id
    # Strip trailing slash, then take the last non-empty segment.
    path = href.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return title_hint or href
    tail = segments[-1]
    # If the tail is a generic collection-json filename, use the parent directory.
    if tail in ("stac-collection.json", "catalog.json"):
        tail = segments[-2] if len(segments) >= 2 else tail
    else:
        # Strip common suffixes so "foo-stac.json" / "foo.json" become "foo-stac" / "foo".
        for suffix in (".json",):
            if tail.endswith(suffix):
                tail = tail[: -len(suffix)]
                break
    if title_hint and title_hint.lower() not in tail.lower():
        return f"{tail} ({title_hint})"
    return tail


class _TimeoutStacIO(DefaultStacIO):
    def __init__(self, token: str = None, timeout: int = None):
        self._token = token
        self._timeout = timeout if timeout is not None else _STAC_CHILD_TIMEOUT

    def read_text_from_href(self, href: str) -> str:
        # Registry-driven reroute (s3config): e.g. NRP external SSL hrefs are
        # fetched via the in-cluster endpoint for speed.
        href = s3config.metadata_href(href)
        if href.startswith("http"):
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            resp = requests.get(href, timeout=self._timeout, headers=headers)
            resp.raise_for_status()
            return resp.text
        return super().read_text_from_href(href)


pystac.StacIO.set_default(_TimeoutStacIO)


def _href_to_s3(href: str) -> str:
    """Convert an HTTPS S3 URL to an s3:// path for DuckDB read_parquet().

    Registry-driven (s3config, #264): built-ins cover the primary NRP Ceph
    endpoint and the source.coop mirror (whose `data.source.coop` HTTPS gateway
    cannot list/glob — the AWS-bucket form can, see #260); deployments add
    sources via S3_SOURCES. Hrefs on unknown hosts pass through unchanged —
    the renderers surface a per-request routing hint for those instead
    (s3config.route_hint) rather than guessing a rewrite.
    """
    return s3config.rewrite_href(href)


def _format_columns(table_cols: list) -> list[str]:
    """Format a list of table:columns dicts into markdown lines."""
    if not table_cols:
        return []
    display_cols = [
        c for c in table_cols
        if c.get("name", "").lower() not in ("geometry", "geom", "bbox")
    ]

    lines = []
    for c in display_cols:
        desc = f" — {c['description']}" if c.get("description") else ""
        lines.append(f"    - `{c['name']}` ({c.get('type', '?')}){desc}")
        values = c.get("values")
        if values:
            lines.append(f"      values: {list(values)}")
    return lines


def _is_queryable_asset(href: str, atype: str) -> bool:
    """True for assets the `query` tool reads via read_parquet()."""
    if "pmtiles" in atype or href.endswith(".pmtiles"):
        return False
    if "tif" in atype or href.endswith(".tif") or href.endswith(".tiff"):
        return False
    return (
        "parquet" in atype or href.endswith(".parquet")
        or href.endswith("/") or "/hex/" in href
    )


def _asset_column_summary(table_cols: list) -> str:
    """One-line `` `name` (type) `` list of an asset's columns (geometry included).

    The per-asset delta view (#303): it makes each queryable asset's column set
    unambiguous — which asset carries the hex's `h0/h8/h10`, which one keeps
    `geometry` — without repeating every column's prose. The full descriptions
    and categorical `values` live once in the shared block appended by
    `_extract_parquet_assets`.
    """
    parts = []
    for c in table_cols:
        name = c.get("name")
        if not name:
            continue
        t = c.get("type")
        parts.append(f"`{name}` ({t})" if t else f"`{name}`")
    return ", ".join(parts)


def _any_queryable_asset_has_columns(col) -> bool:
    """True if any queryable asset carries its own `table:columns`.

    Gates the collection-level `Key columns` trailer (#303): when assets are
    self-describing, the shared per-asset block already covers every column, so
    the trailer would be a redundant copy. It stays only as the sole source for
    collections that describe columns solely at the collection level (e.g.
    census sub-datasets whose hex assets carry no `table:columns`).
    """
    for asset in (col.assets or {}).values():
        if not _is_queryable_asset(asset.href, asset.media_type or ""):
            continue
        if asset.extra_fields.get("table:columns"):
            return True
    return False


def _extract_parquet_assets(col, compact: bool = False) -> list[str]:
    """Extract parquet/hex asset lines from a collection's assets.

    Column *descriptions* are deduplicated (#303): rather than reprinting every
    column's prose once per asset — 2-3× on the common flat-GeoParquet + hex
    dataset, which pushed the largest schemas past geo-agent's 16k tool-result
    cap and truncated them — each asset line lists only its own column
    names/types, and every column's description + categorical `values` is
    rendered exactly once in a shared block appended after the asset lines.

    *compact* (#305): emit only each asset's ``read_parquet(...)`` path, routing
    hint, description, and extension fields — NO per-asset column names and NO
    shared description block. Used by the parent/sub-dataset index, where the
    columns would re-dump every child's schema (20-50k parent payloads over the
    16k cap) and duplicate what the per-child ``get_stac_details`` call returns.
    The caller appends an explicit per-child "call get_stac_details(<id>)"
    pointer instead.
    """
    assets = []
    shared_cols: dict = {}  # name -> col dict, in first-seen order (union of all assets)
    for asset_id, asset in (col.assets or {}).items():
        href = asset.href
        atype = asset.media_type or ""
        title = asset.title or asset_id

        if _is_queryable_asset(href, atype):
            s3 = _href_to_s3(href)
            # Unknown host (no registry rewrite): render the derived s3:// form —
            # the only form that can glob — with the per-request routing params
            # the caller must pass to `query` (see s3config.route_hint, #264).
            hint = s3config.route_hint(s3) if s3.startswith("http") else None
            if hint:
                s3 = hint["path"]
            if s3.endswith("/"):
                s3 = s3.rstrip("/") + "/**"
            size = asset.extra_fields.get("file:size")
            size_note = f" ({size/1024**3:.2f} GiB)" if size and size > 1024**2 else ""
            assets.append(f"  - {title}{size_note}: `read_parquet('{s3}')`")
            if hint:
                assets.append(
                    f"    - ⚠️ non-default endpoint (derived from `{href}` assuming "
                    f"path-style S3): call `query` with s3_endpoint='{hint['endpoint']}', "
                    f"s3_scope='{hint['scope']}' (anonymous; add s3_key/s3_secret if private)"
                )
            # Asset-level description — carries per-asset facts (e.g. "this file has
            # duplicate rows per feature; dedup by <id>") that the model needs at
            # column-choice time. Without this line they never reach the prompt.
            if asset.description:
                assets.append(f"    - {asset.description}")
            # Asset-level STAC extension fields (h3, raster, vector)
            for ext_key in (
                "h3:native_resolution",
                "h3:parent_resolutions",
                "raster:bands",
                "vector:layers",
            ):
                ext_val = asset.extra_fields.get(ext_key)
                if ext_val is not None:
                    assets.append(f"    - {ext_key}: {ext_val}")
            # Per-asset columns are omitted in compact mode (parent index, #305):
            # the caller adds a per-child get_stac_details pointer instead.
            if compact:
                continue
            # Per-asset column *set* only (names/types); descriptions are shared below.
            asset_cols = asset.extra_fields.get("table:columns", [])
            summary = _asset_column_summary(asset_cols)
            if summary:
                assets.append(f"    - columns: {summary}")
            # Accumulate the union for the single shared description block. First
            # asset to define a name wins; later assets backfill any field it was
            # missing (description/values/type) so the block is maximally complete.
            for c in asset_cols:
                name = c.get("name")
                if not name:
                    continue
                if name not in shared_cols:
                    shared_cols[name] = dict(c)
                else:
                    existing = shared_cols[name]
                    for k in ("description", "values", "type"):
                        if not existing.get(k) and c.get(k):
                            existing[k] = c[k]

    if not compact:
        block = _format_columns(list(shared_cols.values()))
        if block:
            assets.append("\nColumn descriptions (shared across the assets above):")
            assets.extend(block)
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
    self_href = col.get_self_href()
    if self_href:
        lines.append(f"collection_url: `{self_href}`")
    if col.description:
        lines.append(col.description)

    # Collect parquet assets from this level
    parquet_assets = _extract_parquet_assets(col)

    # Collection-level "Key columns" trailer, kept ONLY as a fallback (#303):
    # when queryable assets are self-describing, `_extract_parquet_assets` already
    # renders every column once, so this would be a redundant 2nd/3rd copy. It
    # survives solely for collections that describe columns at the collection
    # level and nowhere else (e.g. census sub-datasets).
    col_lines = [] if _any_queryable_asset_has_columns(col) else _extract_columns(col)

    # Check for sub-children — some collections (wyoming-wildlife-lands,
    # pad-us, census) group sub-datasets as child collections, each with
    # their own assets and column schemas.
    if sub_children is None:
        sub_children = list(col.get_children())
    if sub_children:
        lines.append(f"\n**Sub-datasets ({len(sub_children)}):**\n")
        for sc in sub_children:
            sc_title = sc.title or sc.id
            # Compact render (#305): paths + extensions only, no columns. The
            # parent index is a chooser — inlining every child's schema re-dumped
            # the whole bucket (20-50k payloads over the 16k cap) and duplicated
            # what the per-child get_stac_details call returns anyway.
            sc_parquet = _extract_parquet_assets(sc, compact=True)
            if sc_parquet:
                lines.append(f"*{sc_title}* (`{sc.id}`):")
                lines.extend(sc_parquet)
                lines.append(f"  - columns: call get_stac_details('{sc.id}')")
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

    # External links (exclude navigational rel types). Use get_target_str()
    # rather than lnk.href: the latter calls get_root(), which resolves the root
    # link over the network — so rendering an inline collection whose root points
    # at an unreachable catalog (e.g. the source.coop mirror path during a Ceph
    # outage, #260) would crash. get_target_str() returns the raw href, no I/O.
    result["links"] = [
        {k: v for k, v in {
            "rel": lnk.rel,
            "href": lnk.get_target_str(),
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
        # Queryable asset on a host outside the registry: keep the original href
        # (still fetchable over HTTPS by map clients) and attach the derived
        # per-request routing advisory — s3_href + the s3_endpoint/s3_scope to
        # pass to `query` (s3config.route_hint, #264). Additive only, so inline
        # round-trips and non-DuckDB consumers are unaffected.
        if a["href"].startswith("http") and _is_queryable_asset(asset.href, asset.media_type or ""):
            hint = s3config.route_hint(a["href"])
            if hint:
                a["s3_href"] = hint["path"]
                a["s3_endpoint"] = hint["endpoint"]
                a["s3_scope"] = hint["scope"]
        # Merge extra_fields (table:columns, raster:bands, vector:layers, file:size, …)
        a.update(asset.extra_fields)
        assets[asset_id] = {k: v for k, v in a.items() if v is not None}
    result["assets"] = assets

    # Collection-level STAC extension fields
    for ext_key in ("table:columns", "vector:layers", "raster:bands"):
        val = col.extra_fields.get(ext_key)
        if val is not None:
            result[ext_key] = val

    # Child collections — only populated when sub_children are provided. Emitted
    # as a list of nested dicts (not just IDs) so the output round-trips back
    # into the inline `collection=` parameter; see issue #105.
    if sub_children is not None:
        result["children"] = [_collection_to_dict(sc) for sc in sub_children]

    return {k: v for k, v in result.items() if v is not None}


def _fetch_parent(href: str, title: str | None, token: str | None, timeout: int = None):
    """Thread-worker: fetch one top-level child Collection.

    Returns a 3-tuple (col, subchild_hrefs, error):
    - col: the parsed pystac.Collection on success, else None
    - subchild_hrefs: list of sub-child hrefs to enqueue next (empty on failure
      OR when the collection has no children — both cases are valid)
    - error: None on success, or {identifier: reason} on failure

    Rendering (to markdown and dict) happens in the caller after all fetches
    complete, so that parents can be rendered with their successfully-fetched
    sub-children in hand.

    This function must NEVER raise — all exceptions are caught and translated
    to the error dict.

    `timeout` overrides the default child timeout; used by the retry pass.
    """
    child_io = _TimeoutStacIO(
        token=token,
        timeout=timeout if timeout is not None else _STAC_CHILD_TIMEOUT,
    )
    try:
        col = pystac.Collection.from_file(href, stac_io=child_io)
        subchild_hrefs = [l.href for l in (col.links or []) if l.rel == "child"]
        return col, subchild_hrefs, None
    except Exception as e:
        ident = _child_identifier(href, title_hint=title)
        reason = f"{type(e).__name__}: {e}"
        return None, [], {ident: reason}


def _fetch_subchild(href: str, parent_id: str, token: str | None, timeout: int = None):
    """Thread-worker: fetch one sub-child Collection (a leaf of a parent).

    Returns a 2-tuple (col, error):
    - col: the parsed pystac.Collection on success, else None
    - error: None on success, or {identifier: reason} on failure

    Never raises — all exceptions caught.

    `timeout` overrides the default child timeout; used by the retry pass.
    """
    child_io = _TimeoutStacIO(
        token=token,
        timeout=timeout if timeout is not None else _STAC_CHILD_TIMEOUT,
    )
    try:
        col = pystac.Collection.from_file(href, stac_io=child_io)
        return col, None
    except Exception as e:
        ident = _child_identifier(href, title_hint=None)
        reason = f"{type(e).__name__}: {e}"
        return None, {ident: reason}


# Parallel cache of structured dicts, populated alongside STAC_DATASETS at startup.
_STAC_RAW: dict[str, dict] = {}

# Populated by fetch_stac_catalog on the default-catalog path; keys are the best
# available identifier (real collection id when parse succeeded, else href tail),
# values are short reason strings. Cleared on each successful default-catalog load.
STAC_LOAD_ERRORS: dict[str, str] = {}


def fetch_stac_catalog(catalog_url: str = None, catalog_token: str = None) -> dict[str, str]:
    """Fetch the STAC catalog and return {collection_id: markdown_summary}.

    Resilient to slow / partially-failing S3:
    - Root fetch uses _STAC_ROOT_TIMEOUT (generous); failure returns {} + records __root__ error.
    - Parent and sub-child fetches run in a bounded ThreadPoolExecutor using _STAC_CHILD_TIMEOUT
      (tight); individual failures are isolated and recorded in STAC_LOAD_ERRORS rather than
      aborting the whole walk.
    - For the default catalog (no catalog_url), module-level state (STAC_DATASETS, _STAC_RAW,
      STAC_LOAD_ERRORS) is replaced after the pool drains.
    """
    url = catalog_url or STAC_CATALOG_URL

    # --- Phase 1: root fetch (must succeed) ---
    root_io = _TimeoutStacIO(token=catalog_token, timeout=_STAC_ROOT_TIMEOUT)
    try:
        cat = pystac.Catalog.from_file(url, stac_io=root_io)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        print(f"⚠️ Failed to load STAC root catalog: {reason}", file=sys.stderr)
        if not catalog_url:
            STAC_LOAD_ERRORS.clear()
            STAC_LOAD_ERRORS["__root__"] = reason
        return {}

    # Enumerate parent child-links directly from the parsed root (no HTTP).
    parent_links = [
        (l.href, getattr(l, "title", None))
        for l in (cat.links or [])
        if l.rel == "child"
    ]

    # --- Phase 2: dynamic parallel fetch ---
    # parent_cols[id] = pystac.Collection (only successfully-fetched parents)
    # subchild_cols_by_parent[parent_col_id] = {subchild_id: pystac.Collection}
    # errors[identifier] = reason
    parent_cols: dict = {}
    subchild_cols_by_parent: dict = {}
    errors: dict = {}

    # Failed items recorded during the initial pool drain so we can retry them once
    # below with a longer per-child timeout. Rescues tail-latency failures without
    # leaving healthy-but-slow collections out of the catalog.
    failed_parents: list[tuple[str, str | None]] = []      # (href, title)
    failed_subchildren: list[tuple[str, str]] = []         # (href, parent_col_id)

    with ThreadPoolExecutor(max_workers=_STAC_FETCH_CONCURRENCY) as pool:
        # future -> ("parent", href, title) OR ("subchild", href, parent_col_id)
        pending: dict = {}

        for href, title in parent_links:
            fut = pool.submit(_fetch_parent, href, title, catalog_token)
            pending[fut] = ("parent", href, title)

        while pending:
            done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                entry = pending.pop(fut)
                kind = entry[0]
                if kind == "parent":
                    _, href, title = entry
                    col, subchild_hrefs, error = fut.result()
                    if col is not None:
                        parent_cols[col.id] = col
                        if subchild_hrefs:
                            subchild_cols_by_parent[col.id] = {}
                        for sub_href in subchild_hrefs:
                            sub_fut = pool.submit(
                                _fetch_subchild, sub_href, col.id, catalog_token,
                            )
                            pending[sub_fut] = ("subchild", sub_href, col.id)
                    if error:
                        errors.update(error)
                        failed_parents.append((href, title))
                else:  # subchild
                    _, sub_href, parent_col_id = entry
                    col, error = fut.result()
                    if col is not None:
                        subchild_cols_by_parent.setdefault(parent_col_id, {})[col.id] = col
                    if error:
                        errors.update(error)
                        failed_subchildren.append((sub_href, parent_col_id))

    # --- Phase 2.5: retry failed children once with a longer timeout ---
    # Rescues tail-latency failures (borderline-slow S3 responses). A retry that
    # also fails leaves the original error in STAC_LOAD_ERRORS. When a previously-
    # failed parent succeeds on retry, its sub-children ARE now enqueued to the
    # same retry pool — otherwise a rescued parent would be in the catalog without
    # any of its sub-datasets indexed (observed on dev: us-census retry rescued
    # the parent but left its 6 census-year collections absent).
    if failed_parents or failed_subchildren:
        with ThreadPoolExecutor(max_workers=_STAC_FETCH_CONCURRENCY) as retry_pool:
            retry_pending: dict = {}
            for href, title in failed_parents:
                fut = retry_pool.submit(
                    _fetch_parent, href, title, catalog_token, _STAC_CHILD_RETRY_TIMEOUT,
                )
                retry_pending[fut] = ("parent", href, title)
            for sub_href, parent_col_id in failed_subchildren:
                fut = retry_pool.submit(
                    _fetch_subchild, sub_href, parent_col_id, catalog_token, _STAC_CHILD_RETRY_TIMEOUT,
                )
                retry_pending[fut] = ("subchild", sub_href, parent_col_id)
            # Dynamic drain — mirrors the main pool's pattern so that sub-children of
            # retry-rescued parents can be enqueued mid-loop.
            while retry_pending:
                done, _ = wait(retry_pending.keys(), return_when=FIRST_COMPLETED)
                for fut in done:
                    entry = retry_pending.pop(fut)
                    kind = entry[0]
                    if kind == "parent":
                        _, href, title = entry
                        col, subchild_hrefs, error = fut.result()
                        if col is not None:
                            parent_cols[col.id] = col
                            # Clear the first-pass error for this parent since retry succeeded
                            errors.pop(_child_identifier(href, title_hint=title), None)
                            print(
                                f"🔁 Retry succeeded for parent: {col.id}",
                                file=sys.stderr,
                            )
                            # Enqueue sub-children of the newly-rescued parent. These are
                            # first-time fetches, not retries — they use the retry timeout
                            # so they have the same generous budget as the rest of this pool.
                            if subchild_hrefs:
                                subchild_cols_by_parent.setdefault(col.id, {})
                            for sub_href in subchild_hrefs:
                                sub_fut = retry_pool.submit(
                                    _fetch_subchild, sub_href, col.id, catalog_token, _STAC_CHILD_RETRY_TIMEOUT,
                                )
                                retry_pending[sub_fut] = ("subchild", sub_href, col.id)
                        # If retry also failed, `error` carries the same identifier key as
                        # the first-pass error; updating leaves STAC_LOAD_ERRORS pointing at
                        # the most-recent (retry) reason.
                        if error:
                            errors.update(error)
                    else:  # subchild
                        _, sub_href, parent_col_id = entry
                        col, error = fut.result()
                        if col is not None:
                            subchild_cols_by_parent.setdefault(parent_col_id, {})[col.id] = col
                            errors.pop(_child_identifier(sub_href, title_hint=None), None)
                            print(
                                f"🔁 Retry succeeded for sub-child: {col.id}",
                                file=sys.stderr,
                            )
                        if error:
                            errors.update(error)

    # --- Phase 3: render markdown / dicts; swap module state ---
    datasets: dict = {}
    raw: dict = {}
    for parent_id, col in parent_cols.items():
        sub_cols = list(subchild_cols_by_parent.get(parent_id, {}).values())
        datasets[parent_id] = _format_collection(col, sub_children=sub_cols)
        raw[parent_id] = _collection_to_dict(
            col, sub_children=sub_cols if sub_cols else None,
        )
        for sub_id, sub_col in subchild_cols_by_parent.get(parent_id, {}).items():
            # Explicit empty list so _format_collection doesn't fire another HTTP call
            # trying to discover (absent) grandchildren.
            datasets[sub_id] = _format_collection(sub_col, sub_children=[])
            raw[sub_id] = _collection_to_dict(sub_col, sub_children=None)

    print(
        f"📂 Loaded {len(datasets)} collections "
        f"({len(errors)} failed) from STAC: {url}",
        file=sys.stderr,
    )
    for ident, reason in errors.items():
        print(f"⚠️ Child fetch failed: {ident} — {reason}", file=sys.stderr)

    if not catalog_url:
        # Only replace successful-load state (STAC_DATASETS / _STAC_RAW) when the new
        # load produced something. If every child failed, keep the previous snapshot
        # rather than wiping a working cache. Errors are always refreshed so operators
        # and the list_datasets footer see the current failure state.
        if datasets:
            STAC_DATASETS.clear()
            STAC_DATASETS.update(datasets)
            _STAC_RAW.clear()
            _STAC_RAW.update(raw)
        STAC_LOAD_ERRORS.clear()
        STAC_LOAD_ERRORS.update(errors)

    return datasets


# Module-level caches — declared before the startup load so the loader's
# clear()/update() pattern works on first call.
STAC_DATASETS: dict[str, str] = {}

# Kick off the initial load at import. Populates STAC_DATASETS, _STAC_RAW,
# STAC_LOAD_ERRORS in place.
fetch_stac_catalog()


def _render_inline_catalog(catalog: dict) -> dict[str, str]:
    """Build a {collection_id: markdown} dict from an inline catalog dict.

    Mirrors the parent-then-sub-children indexing of `fetch_stac_catalog`:
    each parent contributes one entry, each embedded sub-child contributes
    its own entry. Goes one level deep — matches the existing renderer.
    """
    datasets: dict[str, str] = {}
    for child_dict in catalog.get("children", []) or []:
        col = _coerce_inline_collection(child_dict)
        sub_dicts = child_dict.get("children") or []
        sub_cols = [_coerce_inline_collection(s) for s in sub_dicts]
        datasets[col.id] = _format_collection(col, sub_children=sub_cols)
        for sub_dict, sub_col in zip(sub_dicts, sub_cols):
            datasets[sub_col.id] = _format_collection(sub_col, sub_children=[])
    return datasets


def list_datasets(
    catalog_url: str = None,
    catalog_token: str = None,
    catalog: dict = None,
) -> str:
    """List all available datasets from the STAC catalog.

    Appends a warning footer when `STAC_LOAD_ERRORS` is non-empty, so callers
    can distinguish "not in catalog" from "failed to load this time."

    Resolution order: `catalog` (inline, no fetch) → `catalog_url` (fetch) →
    server default. See issue #105.
    """
    if catalog is not None:
        datasets = _render_inline_catalog(catalog)
        url = catalog.get("id") or "<inline>"
        footer_errors: dict = {}
        lines = [f"# Available Datasets ({len(datasets)} collections)\n"]
        lines.append(f"STAC catalog: `{url}` (provided inline)\n")
        for cid, summary in datasets.items():
            first_line = summary.split("\n")[0]
            lines.append(f"- **{cid}**: {first_line}")
        return "\n".join(lines)

    if catalog_url:
        datasets = fetch_stac_catalog(catalog_url, catalog_token=catalog_token)
        url = catalog_url
        # Errors for custom catalogs are not tracked in module state; caller
        # can detect failure via returned dict being empty or partial.
        footer_errors: dict = {}
    else:
        datasets = STAC_DATASETS
        url = STAC_CATALOG_URL
        footer_errors = STAC_LOAD_ERRORS
    if not datasets and not footer_errors:
        return f"No datasets loaded. STAC catalog: {url}"
    lines = [f"# Available Datasets ({len(datasets)} collections)\n"]
    lines.append(f"STAC catalog: `{url}`\n")
    for cid, summary in datasets.items():
        first_line = summary.split("\n")[0]
        lines.append(f"- **{cid}**: {first_line}")
    if footer_errors:
        lines.append("")
        err_pairs = ", ".join(f"{k} ({v.split(':', 1)[0]})" for k, v in footer_errors.items())
        lines.append(
            f"⚠️ {len(footer_errors)} collection"
            f"{'s' if len(footer_errors) != 1 else ''} could not be loaded: {err_pairs}"
        )
    return "\n".join(lines)


def get_dataset(
    dataset_id: str,
    catalog_url: str = None,
    catalog_token: str = None,
    collection: dict = None,
) -> str:
    """Get detailed metadata for a specific dataset.

    Resolution order: `collection` (inline, no fetch) → `catalog_url` (fetch) →
    server default. Embedded `children: [<dict>, ...]` in the inline dict are
    rendered as sub-datasets. See issue #105.
    """
    if collection is not None:
        col = _coerce_inline_collection(collection)
        child_dicts = collection.get("children") or []
        sub_children = [_coerce_inline_collection(c) for c in child_dicts]
        return _format_collection(col, sub_children=sub_children)

    if catalog_url:
        datasets = fetch_stac_catalog(catalog_url, catalog_token=catalog_token)
    else:
        datasets = STAC_DATASETS

    result = _fuzzy_lookup(datasets, dataset_id)
    if result is not None:
        return result

    # Cache miss (default catalog only): re-fetch in case datasets were added since startup
    if not catalog_url:
        fetch_stac_catalog()  # populates STAC_DATASETS in place if successful
        result = _fuzzy_lookup(STAC_DATASETS, dataset_id)
        if result is not None:
            return result
    return f"Dataset '{dataset_id}' not found. Use list_datasets to see available datasets."


def _coerce_inline_collection(d: dict):
    """Parse an inline STAC collection dict into a pystac.Collection.

    Tolerates dicts that omit STAC envelope fields (`type`, `stac_version`,
    `links`, `description`) so that both `_collection_to_dict` output AND
    hand-built/client-cached dicts (#105) can pass through the inline
    `collection=` parameter. The `children` key is consumed by the caller and
    must be stripped before pystac sees the dict.
    """
    payload = {k: v for k, v in d.items() if k != "children"}
    payload.setdefault("type", "Collection")
    payload.setdefault("stac_version", "1.0.0")
    # STAC marks `license` as required, but `_collection_to_dict` strips None
    # values — so a license-less collection round-tripped through this path
    # would crash pystac's parser. "various" is the STAC-recommended placeholder.
    payload.setdefault("license", "various")
    # pystac raises a bare KeyError for these two (unlike `extent`, which it
    # defaults with a warning) — a hand-built dict shouldn't need them (#278).
    payload.setdefault("links", [])
    payload.setdefault("description", "")
    try:
        return pystac.Collection.from_dict(payload)
    except Exception as e:
        # Surface remaining parse failures (e.g. a missing `id`) as a readable
        # message instead of pystac's bare KeyError reaching the tool response.
        raise ValueError(f"Invalid inline collection: {type(e).__name__}: {e}") from e


def get_collection(
    collection_id: str,
    catalog_url: str = None,
    catalog_token: str = None,
    collection: dict = None,
) -> dict:
    """Return structured STAC collection metadata for programmatic use.

    Unlike get_stac_details (which returns markdown for LLM consumption),
    this returns the raw collection dict with:
    - All assets (parquet, PMTiles, COG, GeoJSON) with hrefs converted to s3://
    - Per-asset STAC extension fields (table:columns, raster:bands, vector:layers)
    - Full collection metadata (providers, extent, license, keywords, links)
    - Child collections (nested dicts) for parent containers

    Resolution order:
      1. If `collection` is provided, render directly (no fetch). Embedded
         `children: [<dict>, ...]` are surfaced as nested child dicts.
      2. Else if `catalog_url` is provided, walk that catalog as today.
      3. Else use the server's default STAC_CATALOG_URL.

    Intended for app code (e.g. geo-agent) that builds map layers and system
    prompts programmatically from structured data.
    """
    if collection is not None:
        col = _coerce_inline_collection(collection)
        child_dicts = collection.get("children") or []
        sub_children = [_coerce_inline_collection(c) for c in child_dicts] or None
        return _collection_to_dict(col, sub_children=sub_children)

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
