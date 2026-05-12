"""Pyramid SQL generation and registration.

register_hex_tiles() materializes a partitioned parquet pyramid to object storage.
Tile requests read directly from the pyramid — no coordination needed.
"""
import json
import os
from decimal import Decimal
from typing import List

import duckdb

from tiles.tile_math import content_hash

# DuckDB's ST_AsMVT emits a single layer named "layer" (see probe in
# tests/test_tile_pyramid.py). Clients reference this via MapLibre's
# `source-layer` option.
MVT_LAYER_NAME = "layer"


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

    AVG mode stores an internal `count` column alongside the aggregate so
    parent rollups produce correctly weighted averages instead of an
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
            + ", COUNT(*) AS count"
        )
        # Phase 2: weighted average = SUM(v*count) / SUM(count). Propagate count.
        phase2_values = (
            ", ".join(
                f'SUM("{c}" * count) / SUM(count) AS "{c}"' for c in value_columns
            )
            + ", SUM(count) AS count"
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
    uri = f"{output_uri}metadata.json"
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


def register_hex_tiles(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    finest_res: int | None = None,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = -1,
) -> dict:
    """Materialize a partitioned parquet pyramid and return tile-endpoint metadata.

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
      index. Each is aggregated via `agg` at parent resolutions and passed
      through raw at the finest level.
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

    agg_upper = agg.upper()
    if agg_upper == "COUNT":
        value_columns = ["count"]
    else:
        if not sql_value_columns:
            raise ValueError(
                "user SQL must return at least one value column after the H3 index "
                "(or use agg='COUNT')"
            )
        value_columns = sql_value_columns

    h = content_hash(sql=sql, finest_res=finest_res, min_res=min_res, agg=agg, zoom_offset=zoom_offset)
    output_uri = f"{_bucket_base()}/hex/{h}/"
    tile_url_template = f"{_public_base_url()}/tiles/hex/{h}/{{z}}/{{x}}/{{y}}.pbf"

    # Cache short-circuit: if an identical registration already exists, skip
    # the COPY (which would re-scan the source) and return the persisted
    # metadata. Hash is content-addressed, so a hit means the parquet pyramid
    # on disk reflects exactly this registration's inputs.
    cached = _read_existing_metadata(con, output_uri)
    if cached is not None and "bounds" in cached and "feature_count_finest" in cached:
        return {
            "tile_url_template": tile_url_template,
            "hash": h,
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
    for stmt in statements:
        con.sql(stmt)

    # Per-resolution min/max for every output value column.
    def _jsonable(v):
        # DuckDB returns DECIMAL for literal-typed numerics; coerce to float
        # so the metadata sidecar stays JSON-serializable.
        if isinstance(v, Decimal):
            return float(v)
        return v

    value_stats = {}
    for col in value_columns:
        by_res = {}
        for res in range(min_res, finest_res + 1):
            # Recursive glob — h0 hive sub-partitions live under res=N/.
            uri = f"{output_uri}res={res}/**/*.parquet"
            row = con.sql(
                f'SELECT MIN("{col}") AS mn, MAX("{col}") AS mx '
                f"FROM read_parquet('{uri}')"
            ).fetchone()
            by_res[str(res)] = {"min": _jsonable(row[0]), "max": _jsonable(row[1])}
        value_stats[col] = {"by_res": by_res}

    finest_uri = f"{output_uri}res={finest_res}/**/*.parquet"
    bounds_row = con.sql(
        f"SELECT "
        f"MIN(h3_cell_to_lat(h)) AS s, MAX(h3_cell_to_lat(h)) AS n, "
        f"MIN(h3_cell_to_lng(h)) AS w, MAX(h3_cell_to_lng(h)) AS e, "
        f"COUNT(*) AS ct "
        f"FROM read_parquet('{finest_uri}')"
    ).fetchone()
    w, s, e, n, feature_count = bounds_row[2], bounds_row[0], bounds_row[3], bounds_row[1], bounds_row[4]

    # bounds + feature_count_finest persisted in metadata.json so the next
    # registration with identical inputs can short-circuit without
    # re-aggregating the source data.
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

    return {
        "tile_url_template": tile_url_template,
        "hash": h,
        "bounds": [w, s, e, n],
        "finest_res": finest_res,
        "min_res": min_res,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "value_stats": value_stats,
        "layer_name": MVT_LAYER_NAME,
        "feature_count_finest": feature_count,
    }
