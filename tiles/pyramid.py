"""Pyramid SQL generation and registration.

register_hex_tiles() materializes a partitioned parquet pyramid to object storage.
Tile requests read directly from the pyramid — no coordination needed.
"""
import json
import os
import re
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

# Builds are tracked across pods via lock.json. The owning pod refreshes the
# lock's heartbeat_at every server._LOCK_HEARTBEAT_SECONDS while the build runs;
# a lock whose heartbeat is older than this is treated as abandoned (the owning
# pod crashed / was killed / rolled out mid-build). Kept a few heartbeat
# intervals so brief S3 hiccups don't false-expire a live build, but far below
# the old fixed 900s so a dead build's "running" ghost resolves in ~2 min, not
# 15 (#184). Configurable for ops.
_LOCK_STALE_SECONDS = int(os.environ.get("TILE_LOCK_STALE_SECONDS", "120"))


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

    Partitioning strategy (#189): only the FINEST level is partitioned by
    (res, h0). The h0 partitioning earns its keep at *serve* time only on the
    finest level — high-zoom tiles hit a huge level and need file-level h0
    pruning. Coarser levels are written PARTITION_BY (res) only, with h0 kept as
    a data column (so the serve-side `SEMI JOIN ... USING (h0)` still works).
    Reason: writing a level as ~122 tiny h0 files costs ~0.8s/file of Ceph
    object overhead regardless of cell count, so coarse levels (which hold few
    cells) paid 60-100s each to write near-empty partitions; a single file
    writes in <1s. Benchmarked in-cluster: ~2.6-3x faster builds, and coarse
    tiles even serve ~1.6x faster single-file (one file beats globbing 122).

    AVG mode stores an internal `__pyramid_weight` column alongside the aggregate
    so parent rollups produce correctly weighted averages instead of an
    unweighted mean-of-means.
    """
    _VALID_AGG = {"AVG", "SUM", "MIN", "MAX", "COUNT", "COUNT_DISTINCT"}
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
    elif agg_upper == "COUNT_DISTINCT":
        # Distinct-count per hex (e.g. species richness). NOT re-aggregatable:
        # a parent's distinct count can't be derived from its children's counts
        # because sibling children share keys (#331). So this is exact only at
        # the finest level; parents use MAX as a defensible lower bound.
        # Phase 1: exact distinct count of each key column, grouped by (h, h0).
        phase1_values = ", ".join(
            f'COUNT(DISTINCT "{c}") AS "{c}"' for c in value_columns
        )
        # Phase 2: MAX up the pyramid. A parent's true richness is >= its
        # largest child, so MAX never overstates (unlike SUM, which would
        # double-count keys shared across siblings) — a lower bound at coarse
        # zoom. See render_recipe / value_stats note and h3-guide.md.
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
    # Coarser levels are PARTITION_BY (res) only — h0 stays a data column (still
    # SELECTed below), so serve-side h0 filtering works but we don't pay the
    # per-h0-file write overhead on these small levels (#189).
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
            f"(FORMAT PARQUET, PARTITION_BY (res), OVERWRITE_OR_IGNORE)"
        )
        statements.append(stmt)

    return statements


def _bucket_base() -> str:
    return os.environ.get("TILE_BUCKET_BASE", "s3://public-output").rstrip("/")


def _public_base_url() -> str:
    return os.environ.get("MCP_PUBLIC_BASE_URL", "https://duckdb-mcp.nrp-nautilus.io").rstrip("/")



# Viridis color ramp — matplotlib's `viridis` sampled at 6 evenly-spaced
# points. Perceptually uniform and colorblind-safe, and legible on both light
# and dark basemaps; replaces the old red ramp (#238). 6 stops is plenty for
# MapLibre's linear interpolation to read as a smooth gradient.
_VIRIDIS_STOPS = (
    (0.0, "#440154"),
    (0.2, "#414487"),
    (0.4, "#2a788e"),
    (0.6, "#22a884"),
    (0.8, "#7ad151"),
    (1.0, "#fde725"),
)


def _suggest_scale(by_res: dict, finest_res: int) -> str:
    """Heuristic scale hint (#238): right-skewed data (max far above the mean)
    reads better on a log color ramp. Computed from the finest-res stats already
    scanned, so it costs nothing extra. Informational only — render_recipe stays
    linear unless the caller explicitly passes color_scale="log"."""
    finest = by_res.get(str(finest_res), {})
    mx, mean = finest.get("max"), finest.get("mean")
    if mean and mean > 0 and mx is not None and mx / mean > 10:
        return "log"
    return "linear"


# Max fill-extrusion height (meters) the largest value maps to. A starting
# default for the 3D variant (#238) — the client owns pitch and can rescale
# this paint property for its zoom range.
_EXTRUSION_MAX_HEIGHT = 50000


def _ramp_value(frac: float, vmin: float, vmax: float, color_scale: str) -> float:
    """Map a ramp fraction (0..1) to a data value across [vmin, vmax].
    color_scale="log" spaces inputs geometrically (low values get more of the
    ramp — right for the skewed data hex maps usually show); log needs positive
    inputs, so the low bound is floored to a positive value. Anything other than
    "log" is linear."""
    if color_scale == "log":
        lo = vmin if vmin and vmin > 0 else (vmax / 1000.0 if vmax and vmax > 0 else 1.0)
        hi = vmax if vmax and vmax > lo else lo * 10.0
        return lo * ((hi / lo) ** frac)
    return vmin + frac * (vmax - vmin)


def _color_ramp_stops(vmin: float, vmax: float, color_scale: str = "linear") -> list:
    """Flatten the viridis ramp into a MapLibre `interpolate` stop list over
    [vmin, vmax]: [value0, color0, value1, color1, ...]. Inputs are strictly
    ascending (vmax > vmin is guaranteed by the caller's degenerate guard), as
    MapLibre requires."""
    stops: list = []
    for frac, color in _VIRIDIS_STOPS:
        stops.extend([_ramp_value(frac, vmin, vmax, color_scale), color])
    return stops


def _height_ramp_stops(vmin: float, vmax: float, color_scale: str, height_max: float) -> list:
    """Like _color_ramp_stops but the outputs are extrusion heights (0..height_max)
    instead of colors, over the same (possibly log-spaced) stop inputs — so the
    3D height and the color encode the value identically."""
    stops: list = []
    for frac, _color in _VIRIDIS_STOPS:
        stops.extend([_ramp_value(frac, vmin, vmax, color_scale), frac * height_max])
    return stops


def render_recipe(
    meta: dict,
    tile_url_template: str,
    color_scale: str = "linear",
    layer_style: str = "fill",
) -> dict:
    """Return {source, layer}: a paste-ready MapLibre vector tile source + layer
    with a viridis color ramp over the first value column.

    color_scale ∈ {"linear", "log"} controls how the ramp is spread across the
    data domain; anything other than "log" is treated as linear.

    layer_style ∈ {"fill", "fill-extrusion"} picks a flat 2D fill (default) or a
    3D extrusion whose height also encodes the value; anything other than
    "fill-extrusion" is treated as "fill"."""
    col = meta["value_columns"][0]
    by_res = meta.get("value_stats", {}).get(col, {}).get("by_res", {})
    stats = by_res.get(str(meta["finest_res"])) or (next(iter(by_res.values())) if by_res else None)
    vmin = stats["min"] if stats and stats.get("min") is not None else 0
    vmax = stats["max"] if stats and stats.get("max") is not None else vmin
    if vmax == vmin:
        vmax = vmin + 1  # degenerate domain — keep the interpolate stops distinct
    source = {"type": "vector", "tiles": [tile_url_template], "minzoom": 0, "maxzoom": 14}
    source_layer = meta.get("layer_name", MVT_LAYER_NAME)
    color_stops = ["interpolate", ["linear"], ["get", col], *_color_ramp_stops(vmin, vmax, color_scale)]
    if layer_style == "fill-extrusion":
        paint = {
            "fill-extrusion-color": color_stops,
            "fill-extrusion-height": [
                "interpolate", ["linear"], ["get", col],
                *_height_ramp_stops(vmin, vmax, color_scale, _EXTRUSION_MAX_HEIGHT),
            ],
            "fill-extrusion-base": 0,
            "fill-extrusion-opacity": 0.85,
        }
        layer = {"type": "fill-extrusion", "source-layer": source_layer, "paint": paint}
    else:
        paint = {"fill-color": color_stops, "fill-opacity": 0.7}
        layer = {"type": "fill", "source-layer": source_layer, "paint": paint}
    return {"source": source, "layer": layer}


# COUNT_DISTINCT is exact only at the finest resolution; coarser levels roll up
# with MAX, a lower bound (see build_pyramid_statements, #331). Surface this in
# the result so a caller styling coarse zooms knows the values under-count.
_COUNT_DISTINCT_ROLLUP_NOTE = (
    "COUNT_DISTINCT is exact at the finest resolution (finest_res); coarser "
    "pyramid levels roll up with MAX, a lower bound on the true distinct count "
    "(a parent's richness is at least its largest child, but siblings may share "
    "keys). Values are accurate at high zoom and under-count at low zoom."
)


def _rollup_note(agg: str) -> str | None:
    """Human-facing caveat about rollup fidelity for aggs whose coarse levels
    are not exact. None when the agg is exact at every level."""
    if agg.upper() == "COUNT_DISTINCT":
        return _COUNT_DISTINCT_ROLLUP_NOTE
    return None


def _json_dumps_escaped(obj) -> str:
    # DuckDB's COPY ... (FORMAT CSV, QUOTE '') writes the raw string. We must
    # escape any single quotes in the JSON so they don't break the SQL literal.
    return json.dumps(obj).replace("'", "''")


# H3 cell/index/partition columns follow the `h<res>` convention (h0..h15) —
# `h0` is the hive partition key the pyramid build derives itself, and callers
# routinely carry an `h<res>` index column through the SELECT for joins. None of
# these are values, so they must not appear in value_columns (#319): they'd
# corrupt suggested_scale, waste a value_stats scan, and — since downstream
# defaults value_column to value_columns[0] — color the map by meaningless H3
# integers instead of the real metric.
_H3_COLUMN_RE = re.compile(r"^h\d+$")


def _strip_h3_columns(columns: List[str]) -> List[str]:
    """Drop H3 cell/index/partition columns (names matching `^h\\d+$`) from a
    list of candidate value columns. See #319."""
    return [c for c in columns if not _H3_COLUMN_RE.match(c)]


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


def write_lock(con: duckdb.DuckDBPyConnection, output_uri: str, pod_id: str,
               started_at: float | None = None) -> None:
    """Write {output_uri}lock.json announcing this pod owns the in-progress
    build for this hash. Overwrites any prior lock at the same path.

    `started_at` marks when the build began and is preserved across heartbeats
    (so reported elapsed keeps growing); `heartbeat_at` is bumped to now on every
    write and is what staleness is judged on. The owning pod re-writes the lock
    periodically (server heartbeat) so a live build stays fresh; once the pod
    stops (done/crash), heartbeat_at ages out and lock_is_stale flips true."""
    now = time.time()
    payload = {
        "started_at": now if started_at is None else started_at,
        "pod_id": pod_id,
        "heartbeat_at": now,
    }
    _write_json_marker(con, f"{output_uri}lock.json", payload)


def read_lock(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Return the lock dict {started_at, pod_id} or None if no lock.json."""
    return _read_json_marker(con, f"{output_uri}lock.json")


def lock_is_stale(lock: dict | None, now: float | None = None) -> bool:
    """A missing lock is 'stale' (treated the same as absent). A lock whose last
    heartbeat is older than _LOCK_STALE_SECONDS is considered abandoned. Falls
    back to started_at for pre-heartbeat locks written by older pods."""
    if lock is None:
        return True
    beat = lock.get("heartbeat_at", lock.get("started_at"))
    if not isinstance(beat, (int, float)):
        return True
    if now is None:
        now = time.time()
    return (now - beat) > _LOCK_STALE_SECONDS


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
        "cache_hit": True,
    }
    note = _rollup_note(cached.get("agg", ""))
    if note:
        result["rollup_note"] = note
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
      (one row per (h, h0) cell at every level). For agg="COUNT_DISTINCT" the
      value column is the KEY to count distinctly (e.g. specieskey); the result
      is exact at finest_res and a MAX-based lower bound at coarser levels (#331).

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
        # Drop carried H3 index/partition columns (h0, h3, …) so a partition key
        # is never advertised as a value (#319). Runs before the empty-check so
        # `SELECT h8, h0` (no real value) still raises below.
        value_columns = _strip_h3_columns(sql_value_columns)
        if not value_columns:
            raise ValueError(
                "user SQL must return at least one value column after the H3 index "
                "(or use agg='COUNT')"
            )

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

    # Phase 2: build parent resolutions.
    for i, stmt in enumerate(statements[1:], start=1):
        s0 = time.perf_counter()
        con.sql(stmt)
        dt = time.perf_counter() - s0
        copy_seconds += dt
        print(
            f"[tile-build] hash={h} phase=2 res={finest_res - i} copy={dt:.1f}s",
            file=sys.stderr,
        )

    stats_t0 = time.perf_counter()
    stat_levels = range(min_res, finest_res + 1)
    value_stats = {}
    for col in value_columns:
        by_res = {}
        for res in stat_levels:
            # Recursive glob — h0 hive sub-partitions live under res=N/.
            # MEAN comes free from the same scan as MIN/MAX and drives the
            # log-scale suggestion below (#238).
            uri = f"{output_uri}res={res}/**/*.parquet"
            row = con.sql(
                f'SELECT MIN("{col}") AS mn, MAX("{col}") AS mx, AVG("{col}") AS av '
                f"FROM read_parquet('{uri}')"
            ).fetchone()
            by_res[str(res)] = {
                "min": _jsonable(row[0]),
                "max": _jsonable(row[1]),
                "mean": _jsonable(row[2]),
            }
        value_stats[col] = {"by_res": by_res, "suggested_scale": _suggest_scale(by_res, finest_res)}
    stats_seconds = time.perf_counter() - stats_t0

    print(
        f"[tile-build] hash={h} DONE "
        f"finest_res={finest_res} min_res={min_res} "
        f"copy={copy_seconds:.1f}s stats={stats_seconds:.1f}s bounds={bounds_seconds:.1f}s "
        f"total={time.perf_counter() - build_t0:.1f}s "
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
    }
    note = _rollup_note(agg)
    if note:
        result["rollup_note"] = note
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
      through raw at the finest level. Carried H3 index/partition columns
      (names matching `h<res>`, e.g. h0, h3) are dropped from value_columns —
      they're join/partition keys, not values (#319).
    - agg="COUNT_DISTINCT": the value column is the key counted distinctly per
      hex (species richness = distinct specieskey per cell). Exact at finest_res;
      coarser levels roll up with MAX (a lower bound — see `rollup_note`, #331).
    """
    plan = prepare_hex_tiles(
        con=con, sql=sql, finest_res=finest_res,
        min_res=min_res, agg=agg, zoom_offset=zoom_offset,
    )
    if plan["cached"] is not None:
        return cached_result_dict(plan, plan["cached"])
    return build_hex_tiles(con, plan)
