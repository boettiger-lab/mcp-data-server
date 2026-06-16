"""Pyramid SQL generation and registration.

register_hex_tiles() materializes a partitioned parquet pyramid to object storage.
Tile requests read directly from the pyramid — no coordination needed.
"""
import json
import os
import sys
import time
from decimal import Decimal
from typing import List

import duckdb

from tiles.tile_math import content_hash

# DuckDB's ST_AsMVT emits a single layer named "layer" (see probe in
# tests/test_tile_pyramid.py). Clients reference this via MapLibre's
# `source-layer` option.
MVT_LAYER_NAME = "layer"

# Builds are tracked across pods via lock.json. A lock older than this
# is treated as abandoned (the owning pod likely crashed mid-build).
# Configurable for ops; observed builds complete in 100-120s so 900s
# gives ~8x headroom.
_LOCK_STALE_SECONDS = int(os.environ.get("TILE_LOCK_STALE_SECONDS", "900"))

# Hex sets at or below this many finest-res cells are served as a single
# GeoJSON FeatureCollection (streamed straight to object storage, fetched
# client-side by MapLibre from the public gateway) instead of building an MVT
# tile pyramid. Larger sets fall back to the pyramid. Env-overridable. See #178.
_GEOJSON_MAX_FEATURES = int(os.environ.get("TILE_GEOJSON_MAX_FEATURES", "100000"))


def build_pyramid_statements(
    user_sql: str,
    finest_res: int,
    min_res: int,
    agg: str,
    value_columns: List[str],
    h3_column: str,
    output_uri: str,
) -> List[str]:
    """Return the ordered list of COPY statements that build a partitioned pyramid.

    Two-phase design:
      Phase 1 (first statement): one COPY reads user_sql, aggregates by (h, h0),
        and writes only res=finest_res partitioned by (res, h0).
      Phase 2 (remaining statements): for res = finest_res - 1 down to min_res,
        each COPY reads the previously written res+1 partition (already aggregated,
        small) and writes res. Working set per Phase 2 step is bounded by the
        cardinality of the previous res, which shrinks ~7x per step.

    AVG mode stores an internal `__pyramid_weight` column alongside the aggregate
    so parent rollups produce correctly weighted averages instead of an
    unweighted mean-of-means.
    """
    _VALID_AGG = {"AVG", "SUM", "MIN", "MAX", "COUNT"}
    agg_upper = agg.upper()
    if agg_upper not in _VALID_AGG:
        raise ValueError(f"agg must be one of {_VALID_AGG}, got {agg!r}")
    if agg_upper != "COUNT" and not value_columns:
        raise ValueError(
            "user SQL must return at least one value column after the H3 index "
            "(or use agg='COUNT')"
        )

    qh = f'"{h3_column}"'

    # Per-agg expressions at finest (Phase 1, aggregating raw source rows)
    # and at parent levels (Phase 2, rolling up the previous resolution).
    if agg_upper == "COUNT":
        phase1_values = "COUNT(*) AS count"
        phase2_values = "SUM(count) AS count"
    elif agg_upper == "SUM":
        phase1_values = ", ".join(f'SUM("{c}") AS "{c}"' for c in value_columns)
        phase2_values = ", ".join(f'SUM("{c}") AS "{c}"' for c in value_columns)
    elif agg_upper == "MIN":
        phase1_values = ", ".join(f'MIN("{c}") AS "{c}"' for c in value_columns)
        phase2_values = ", ".join(f'MIN("{c}") AS "{c}"' for c in value_columns)
    elif agg_upper == "MAX":
        phase1_values = ", ".join(f'MAX("{c}") AS "{c}"' for c in value_columns)
        phase2_values = ", ".join(f'MAX("{c}") AS "{c}"' for c in value_columns)
    else:  # AVG
        # Phase 1: average raw source rows + carry COUNT for weighted parents.
        phase1_values = (
            ", ".join(f'AVG("{c}") AS "{c}"' for c in value_columns)
            + ", COUNT(*) AS __pyramid_weight"
        )
        # Phase 2: weighted average = SUM(v*weight) / SUM(weight). Propagate weight.
        phase2_values = (
            ", ".join(
                f'SUM("{c}" * __pyramid_weight) / SUM(__pyramid_weight) AS "{c}"' for c in value_columns
            )
            + ", SUM(__pyramid_weight) AS __pyramid_weight"
        )

    # Phase 1: scan user_sql, derive h0 once in the src CTE, aggregate at finest.
    phase_1 = (
        "COPY (\n"
        f"  WITH src AS (\n"
        f"    SELECT *, CAST(h3_cell_to_parent({qh}, 0) AS BIGINT) AS h0\n"
        f"    FROM (\n{user_sql}\n    )\n"
        f"  )\n"
        f"  SELECT {qh} AS h,\n"
        f"         h0,\n"
        f"         {phase1_values},\n"
        f"         {finest_res} AS res\n"
        f"  FROM src\n"
        f"  GROUP BY 1, 2\n"
        f") TO '{output_uri}' "
        f"(FORMAT PARQUET, PARTITION_BY (res, h0), OVERWRITE_OR_IGNORE)"
    )

    statements = [phase_1]

    # Phase 2: each parent res reads from the previously written res+1.
    for res in range(finest_res - 1, min_res - 1, -1):
        src_uri = f"{output_uri}res={res + 1}/**/*.parquet"
        stmt = (
            "COPY (\n"
            f"  SELECT h3_cell_to_parent(h, {res}) AS h,\n"
            f"         h0,\n"
            f"         {phase2_values},\n"
            f"         {res} AS res\n"
            f"  FROM read_parquet('{src_uri}', hive_partitioning=true)\n"
            f"  GROUP BY 1, 2\n"
            f") TO '{output_uri}' "
            f"(FORMAT PARQUET, PARTITION_BY (res, h0), OVERWRITE_OR_IGNORE)"
        )
        statements.append(stmt)

    return statements


def _bucket_base() -> str:
    return os.environ.get("TILE_BUCKET_BASE", "s3://public-output").rstrip("/")


def _public_base_url() -> str:
    return os.environ.get("MCP_PUBLIC_BASE_URL", "https://duckdb-mcp.nrp-nautilus.io").rstrip("/")


def _s3_public_http_base() -> str:
    """Public HTTPS gateway for the object store (CORS-enabled, GET-only). A
    browser fetches GeoJSON directly from here — see stac.py, same gateway."""
    return os.environ.get("S3_PUBLIC_HTTP_BASE", "https://s3-west.nrp-nautilus.io").rstrip("/")


def _to_public_http_url(uri: str) -> str:
    """Map an s3://bucket/key write URI to the public HTTPS URL MapLibre can
    fetch client-side. Non-s3 paths (local test dirs) are returned unchanged."""
    if uri.startswith("s3://"):
        return f"{_s3_public_http_base()}/{uri[len('s3://'):]}"
    return uri


def build_geojson_statement(finest_uri: str, geojson_uri: str, value_columns: List[str]) -> str:
    """COPY that writes the finest-res hexes as one valid GeoJSON FeatureCollection.

    Uses DuckDB's native JSON export (FORMAT JSON) plus ST_AsGeoJSON — a spatial
    scalar, NOT the GDAL-backed ST_Write — so it streams to s3:// over httpfs.
    A single output row becomes one line that is the whole FeatureCollection
    object; json_group_array nests each cell's GeoJSON geometry and properties.
    """
    props = ", ".join(f"'{c}', \"{c}\"" for c in value_columns)
    return (
        "COPY (\n"
        "  SELECT 'FeatureCollection' AS type,\n"
        "         coalesce(json_group_array(json_object(\n"
        "           'type', 'Feature',\n"
        "           'geometry', CAST(ST_AsGeoJSON(h3_cell_to_boundary_wkt(h)::GEOMETRY) AS JSON),\n"
        f"           'properties', json_object({props})\n"
        "         )), '[]'::JSON) AS features\n"
        f"  FROM read_parquet('{finest_uri}')\n"
        f") TO '{geojson_uri}' (FORMAT JSON)"
    )


def render_recipe(meta: dict, tile_url_template: str) -> dict:
    """Return {format, source, layer}: a paste-ready MapLibre source + fill
    layer with a default linear color ramp over the first value column.

    The agent renders these verbatim and never branches on format. `format` is
    read from meta ('geojson' or 'vector'; defaults to 'vector' for pre-#178
    metadata). GeoJSON sources point at the public object-store URL; vector
    sources at the tile endpoint and carry `source-layer`.
    """
    fmt = meta.get("format") or "vector"
    col = meta["value_columns"][0]
    by_res = meta.get("value_stats", {}).get(col, {}).get("by_res", {})
    stats = by_res.get(str(meta["finest_res"])) or (next(iter(by_res.values())) if by_res else None)
    vmin = stats["min"] if stats and stats.get("min") is not None else 0
    vmax = stats["max"] if stats and stats.get("max") is not None else vmin
    if vmax == vmin:
        vmax = vmin + 1  # degenerate domain — keep the interpolate stops distinct
    paint = {
        "fill-color": ["interpolate", ["linear"], ["get", col], vmin, "#fee5d9", vmax, "#a50f15"],
        "fill-opacity": 0.7,
    }
    if fmt == "geojson":
        source = {"type": "geojson", "data": meta.get("geojson_url")}
        layer = {"type": "fill", "paint": paint}
    else:
        source = {"type": "vector", "tiles": [tile_url_template], "minzoom": 0, "maxzoom": 14}
        layer = {"type": "fill", "source-layer": meta.get("layer_name", MVT_LAYER_NAME), "paint": paint}
    return {"format": fmt, "source": source, "layer": layer}


def _json_dumps_escaped(obj) -> str:
    # DuckDB's COPY ... (FORMAT CSV, QUOTE '') writes the raw string. We must
    # escape any single quotes in the JSON so they don't break the SQL literal.
    return json.dumps(obj).replace("'", "''")


def _inspect_user_sql(con: duckdb.DuckDBPyConnection, user_sql: str):
    """Run user SQL with LIMIT 0 to extract column names without materializing data.

    Returns (h3_column, value_columns). value_columns may be empty — the caller
    is responsible for validating that an empty list is acceptable for the
    chosen aggregation (only agg="COUNT" supports it).
    """
    columns = con.sql(f"SELECT * FROM ({user_sql}) LIMIT 0").columns
    if not columns:
        raise ValueError("user SQL returned no columns")
    return columns[0], list(columns[1:])


def _read_existing_metadata(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Return the cached metadata dict if metadata.json exists at output_uri,
    else None. Mirrors endpoint._read_metadata for s3 vs local paths.
    """
    return _read_json_marker(con, f"{output_uri}metadata.json")


def _read_json_marker(con: duckdb.DuckDBPyConnection, uri: str):
    """Return parsed JSON dict at uri, or None if absent/unreadable.
    Mirrors _read_existing_metadata's local-vs-remote handling."""
    try:
        if not uri.startswith("s3://") and not uri.startswith("http"):
            if not os.path.exists(uri):
                return None
            with open(uri, "r") as f:
                return json.loads(f.read().strip())
        row = con.sql(f"SELECT content FROM read_text('{uri}')").fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _write_json_marker(con: duckdb.DuckDBPyConnection, uri: str, payload: dict) -> None:
    """Write payload as a single-row CSV-as-JSON file at uri. Uses the
    same COPY pattern as the existing metadata.json write. For local-fs
    paths, ensures the parent dir exists (DuckDB COPY does not auto-mkdir,
    and these markers may fire before build_hex_tiles' own makedirs)."""
    if not uri.startswith("s3://") and not uri.startswith("http"):
        parent = os.path.dirname(uri)
        if parent:
            os.makedirs(parent, exist_ok=True)
    sql = (
        f"COPY (SELECT '{_json_dumps_escaped(payload)}' AS j) "
        f"TO '{uri}' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(sql)


def write_lock(con: duckdb.DuckDBPyConnection, output_uri: str, pod_id: str) -> None:
    """Write {output_uri}lock.json announcing this pod owns the in-progress
    build for this hash. Overwrites any prior lock at the same path."""
    payload = {"started_at": time.time(), "pod_id": pod_id}
    _write_json_marker(con, f"{output_uri}lock.json", payload)


def read_lock(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Return the lock dict {started_at, pod_id} or None if no lock.json."""
    return _read_json_marker(con, f"{output_uri}lock.json")


def lock_is_stale(lock: dict | None, now: float | None = None) -> bool:
    """A missing lock is 'stale' (treated the same as absent). A lock older
    than _LOCK_STALE_SECONDS is considered abandoned."""
    if lock is None:
        return True
    started = lock.get("started_at")
    if not isinstance(started, (int, float)):
        return True
    if now is None:
        now = time.time()
    return (now - started) > _LOCK_STALE_SECONDS


def write_failed(con: duckdb.DuckDBPyConnection, output_uri: str, error: str) -> None:
    """Write {output_uri}failed.json recording a build exception. Readers
    treat this as terminal-failed for the hash until a new register_hex_tiles
    overwrites it."""
    payload = {"error": str(error), "failed_at": time.time()}
    _write_json_marker(con, f"{output_uri}failed.json", payload)


def read_failed(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Return the failed dict {error, failed_at} or None if no failed.json."""
    return _read_json_marker(con, f"{output_uri}failed.json")


def tile_paths_for_hash(h: str) -> dict:
    """Return {output_uri, tile_url_template} for a content hash."""
    return {
        "output_uri": f"{_bucket_base()}/hex/{h}/",
        "tile_url_template": f"{_public_base_url()}/tiles/hex/{h}/{{z}}/{{x}}/{{y}}.pbf",
    }


def read_existing_metadata(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Public alias for _read_existing_metadata — server.py reads this directly."""
    return _read_existing_metadata(con, output_uri)


def cached_result_dict(plan: dict, cached: dict) -> dict:
    result = {
        "tile_url_template": plan["tile_url_template"],
        "hash": plan["hash"],
        "bounds": cached["bounds"],
        "finest_res": cached["finest_res"],
        "min_res": cached["min_res"],
        "zoom_offset": cached["zoom_offset"],
        "value_columns": cached["value_columns"],
        "value_stats": cached["value_stats"],
        "layer_name": cached.get("layer_name", MVT_LAYER_NAME),
        "feature_count_finest": cached["feature_count_finest"],
        "geojson_url": cached.get("geojson_url"),
        "cache_hit": True,
    }
    result.update(render_recipe(cached, plan["tile_url_template"]))
    return result


def prepare_hex_tiles(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    finest_res: int | None = None,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = 2,
) -> dict:
    """Inspect user SQL, resolve finest_res, compute the content hash, and
    check the S3 cache. Fast — no COPY runs here.

    The connection must have httpfs, spatial, and h3 extensions loaded.

    When finest_res is None (the LLM-facing path), it's auto-detected from the
    H3 column's actual resolution via h3_get_resolution on a one-row probe.
    Pass an explicit finest_res only from test / REPL code where you need to
    force a specific value.

    Value-column contract:
    - agg="COUNT": user SQL needs only the H3 index column. Output has a single
      `count` column (row count per hex at parent resolutions; 1 at finest).
      Any extra columns in the user SQL are ignored.
    - Other aggs: user SQL must return at least one value column after the H3
      index. Each is aggregated via `agg` at every resolution including the finest
      (one row per (h, h0) cell at every level).

    Returns a "plan" dict with everything build_hex_tiles needs, plus a
    `cached` field that is the persisted metadata if the tileset already
    exists on disk (otherwise None).
    """
    h3_column, sql_value_columns = _inspect_user_sql(con, sql)
    if finest_res is None:
        # Read one cell from the user SQL and ask H3 what its resolution is.
        # Avoids forcing the LLM to invent a finest_res — the data already knows.
        probe = con.sql(
            f'SELECT h3_get_resolution("{h3_column}") FROM ({sql}) LIMIT 1'
        ).fetchone()
        if probe is None or probe[0] is None:
            raise ValueError(
                "Cannot auto-detect finest_res: user SQL returned no rows, or "
                f"the first column ({h3_column!r}) is not an H3 cell. Narrow "
                "the SQL until it returns at least one row whose first column "
                "is an H3 index."
            )
        finest_res = int(probe[0])
    if finest_res < min_res:
        raise ValueError(f"finest_res ({finest_res}) must be >= min_res ({min_res})")

    if agg.upper() == "COUNT":
        value_columns = ["count"]
    else:
        if not sql_value_columns:
            raise ValueError(
                "user SQL must return at least one value column after the H3 index "
                "(or use agg='COUNT')"
            )
        value_columns = sql_value_columns

    h = content_hash(sql=sql, finest_res=finest_res, min_res=min_res, agg=agg, zoom_offset=zoom_offset)
    paths = tile_paths_for_hash(h)
    output_uri = paths["output_uri"]

    cached = _read_existing_metadata(con, output_uri)
    if cached is not None and not ("bounds" in cached and "feature_count_finest" in cached):
        cached = None  # metadata.json is malformed — treat as miss, will rebuild.

    return {
        "hash": h,
        "output_uri": output_uri,
        "tile_url_template": paths["tile_url_template"],
        "sql": sql,
        "agg": agg,
        "finest_res": finest_res,
        "min_res": min_res,
        "zoom_offset": zoom_offset,
        "h3_column": h3_column,
        "value_columns": value_columns,
        "cached": cached,
    }


def build_hex_tiles(con: duckdb.DuckDBPyConnection, plan: dict) -> dict:
    """Run the COPY, compute per-resolution stats and bounds, and write
    metadata.json. Returns the same shape as register_hex_tiles minus
    cache_hit (caller decides whether to add that key).

    The connection must have httpfs, spatial, and h3 extensions loaded and
    SHOULD be exclusive to this build — DuckDB serialises queries on a
    connection, so sharing a connection with other writers will block them
    for the duration of the COPY.
    """
    sql = plan["sql"]
    agg = plan["agg"]
    finest_res = plan["finest_res"]
    min_res = plan["min_res"]
    zoom_offset = plan["zoom_offset"]
    h3_column = plan["h3_column"]
    value_columns = plan["value_columns"]
    output_uri = plan["output_uri"]

    h = plan["hash"]
    statements = build_pyramid_statements(
        user_sql=sql,
        finest_res=finest_res,
        min_res=min_res,
        agg=agg,
        value_columns=value_columns,
        h3_column=h3_column,
        output_uri=output_uri,
    )
    if not output_uri.startswith("s3://"):
        os.makedirs(output_uri, exist_ok=True)

    def _jsonable(v):
        # DuckDB returns DECIMAL for literal-typed numerics; coerce to float
        # so the metadata sidecar stays JSON-serializable.
        if isinstance(v, Decimal):
            return float(v)
        return v

    # Per-phase timing — the COPY loop is the dominant build cost (#178).
    build_t0 = time.perf_counter()
    copy_seconds = 0.0

    # Phase 1 always: materialize the finest resolution. This is both the
    # pyramid's base level and the source set for the GeoJSON fast path.
    s0 = time.perf_counter()
    con.sql(statements[0])
    dt = time.perf_counter() - s0
    copy_seconds += dt
    print(f"[tile-build] hash={h} phase=1 res={finest_res} copy={dt:.1f}s", file=sys.stderr)

    bounds_t0 = time.perf_counter()
    finest_uri = f"{output_uri}res={finest_res}/**/*.parquet"
    bounds_row = con.sql(
        f"SELECT "
        f"MIN(h3_cell_to_lat(h)) AS s, MAX(h3_cell_to_lat(h)) AS n, "
        f"MIN(h3_cell_to_lng(h)) AS w, MAX(h3_cell_to_lng(h)) AS e, "
        f"COUNT(*) AS ct "
        f"FROM read_parquet('{finest_uri}')"
    ).fetchone()
    w, s, e, n, feature_count = bounds_row[2], bounds_row[0], bounds_row[3], bounds_row[1], bounds_row[4]
    bounds_seconds = time.perf_counter() - bounds_t0

    # Auto-select (#178): small/bounded sets render fastest as a single
    # client-side GeoJSON; larger sets need the tiled pyramid. The count comes
    # free from the already-materialized Phase 1 level.
    serve_format = "geojson" if feature_count <= _GEOJSON_MAX_FEATURES else "vector"

    # Phase 2 (parent resolutions) is only for the tile path — GeoJSON renders
    # the finest set directly, so coarser pyramid levels are never requested.
    if serve_format == "vector":
        for i, stmt in enumerate(statements[1:], start=1):
            s0 = time.perf_counter()
            con.sql(stmt)
            dt = time.perf_counter() - s0
            copy_seconds += dt
            print(
                f"[tile-build] hash={h} phase=2 res={finest_res - i} copy={dt:.1f}s",
                file=sys.stderr,
            )

    # Per-resolution value stats. GeoJSON only uses the finest level (the
    # client colors from it), so we skip the parent levels it never builds.
    stats_t0 = time.perf_counter()
    stat_levels = range(min_res, finest_res + 1) if serve_format == "vector" else [finest_res]
    value_stats = {}
    for col in value_columns:
        by_res = {}
        for res in stat_levels:
            # Recursive glob — h0 hive sub-partitions live under res=N/.
            uri = f"{output_uri}res={res}/**/*.parquet"
            row = con.sql(
                f'SELECT MIN("{col}") AS mn, MAX("{col}") AS mx '
                f"FROM read_parquet('{uri}')"
            ).fetchone()
            by_res[str(res)] = {"min": _jsonable(row[0]), "max": _jsonable(row[1])}
        value_stats[col] = {"by_res": by_res}
    stats_seconds = time.perf_counter() - stats_t0

    # GeoJSON path: stream the FeatureCollection straight to object storage so
    # MapLibre fetches it directly from the public gateway (no pod serving).
    geojson_url = None
    geojson_seconds = 0.0
    if serve_format == "geojson":
        gj_t0 = time.perf_counter()
        geojson_uri = f"{output_uri}data.geojson"
        con.sql(build_geojson_statement(finest_uri, geojson_uri, value_columns))
        geojson_url = _to_public_http_url(geojson_uri)
        geojson_seconds = time.perf_counter() - gj_t0

    print(
        f"[tile-build] hash={h} DONE format={serve_format} "
        f"finest_res={finest_res} min_res={min_res} "
        f"copy={copy_seconds:.1f}s stats={stats_seconds:.1f}s bounds={bounds_seconds:.1f}s "
        f"geojson={geojson_seconds:.1f}s total={time.perf_counter() - build_t0:.1f}s "
        f"feature_count_finest={feature_count}",
        file=sys.stderr,
    )

    metadata = {
        "finest_res": finest_res,
        "min_res": min_res,
        "agg": agg,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "value_stats": value_stats,
        "layer_name": MVT_LAYER_NAME,
        "bounds": [w, s, e, n],
        "feature_count_finest": feature_count,
        "format": serve_format,
        "geojson_url": geojson_url,
    }
    metadata_sql = (
        f"COPY (SELECT '{_json_dumps_escaped(metadata)}' AS j) "
        f"TO '{output_uri}metadata.json' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(metadata_sql)

    result = {
        "tile_url_template": plan["tile_url_template"],
        "hash": plan["hash"],
        "bounds": [w, s, e, n],
        "finest_res": finest_res,
        "min_res": min_res,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "value_stats": value_stats,
        "layer_name": MVT_LAYER_NAME,
        "feature_count_finest": feature_count,
        "geojson_url": geojson_url,
    }
    result.update(render_recipe(metadata, plan["tile_url_template"]))
    return result


def register_hex_tiles(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    finest_res: int | None = None,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = 2,
) -> dict:
    """Materialize a partitioned parquet pyramid and return tile-endpoint metadata.

    Synchronous: prepare → (cached return | full build) on the calling thread.
    The MCP server wraps this with a background-executor pattern so the agent
    sees a quick "running" response on long jobs; library callers (tests,
    REPL) keep the simple sync semantics.

    The connection must have httpfs, spatial, and h3 extensions loaded.

    When finest_res is None (the LLM-facing path), it's auto-detected from the
    H3 column's actual resolution via h3_get_resolution on a one-row probe.

    Value-column contract:
    - agg="COUNT": user SQL needs only the H3 index column. Output has a single
      `count` column (row count per hex at parent resolutions; 1 at finest).
      Any extra columns in the user SQL are ignored.
    - Other aggs: user SQL must return at least one value column after the H3
      index. Each is aggregated via `agg` at parent resolutions and passed
      through raw at the finest level.
    """
    plan = prepare_hex_tiles(
        con=con, sql=sql, finest_res=finest_res,
        min_res=min_res, agg=agg, zoom_offset=zoom_offset,
    )
    if plan["cached"] is not None:
        return cached_result_dict(plan, plan["cached"])
    return build_hex_tiles(con, plan)
