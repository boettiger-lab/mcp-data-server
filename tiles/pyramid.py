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


def build_pyramid_sql(
    user_sql: str,
    finest_res: int,
    min_res: int,
    agg: str,
    value_columns: List[str],
    h3_column: str,
    output_uri: str,
) -> str:
    """Return the COPY ... TO SQL that writes a partitioned pyramid.

    The finest-resolution level stores raw per-row values; parent resolutions
    aggregate via the chosen `agg` function.

    When agg="COUNT", `value_columns` must be exactly ["count"] and the SQL
    emits `COUNT(*) AS count` at parent levels and `1 AS count` at the finest
    level. Any value columns from the user SQL are ignored — callers requesting
    COUNT get row-count semantics, nothing else.
    """
    _VALID_AGG = {"AVG", "SUM", "MIN", "MAX", "COUNT"}
    agg_upper = agg.upper()
    if agg_upper not in _VALID_AGG:
        raise ValueError(f"agg must be one of {_VALID_AGG}, got {agg!r}")

    qh = f'"{h3_column}"'

    if agg_upper == "COUNT":
        parent_values = "COUNT(*) AS count"
        finest_values = "1 AS count"
    else:
        parent_values = ", ".join(f'{agg_upper}("{c}") AS "{c}"' for c in value_columns)
        finest_values = ", ".join(f'"{c}"' for c in value_columns)

    selects = []
    for res in range(min_res, finest_res):
        selects.append(
            f"  SELECT h3_cell_to_parent({qh}, {res}) AS h, "
            f"{parent_values}, {res} AS res FROM src GROUP BY 1"
        )
    selects.append(
        f"  SELECT {qh} AS h, {finest_values}, {finest_res} AS res FROM src"
    )

    body = "\n  UNION ALL\n".join(selects)

    return (
        "COPY (\n"
        f"  WITH src AS (\n{user_sql}\n  )\n"
        f"{body}\n"
        f") TO '{output_uri}' "
        f"(FORMAT PARQUET, PARTITION_BY (res), OVERWRITE_OR_IGNORE)"
    )


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


def register_hex_tiles(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    finest_res: int,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = -1,
) -> dict:
    """Materialize a partitioned parquet pyramid and return tile-endpoint metadata.

    The connection must have httpfs, spatial, and h3 extensions loaded.

    Value-column contract:
    - agg="COUNT": user SQL needs only the H3 index column. Output has a single
      `count` column (row count per hex at parent resolutions; 1 at finest).
      Any extra columns in the user SQL are ignored.
    - Other aggs: user SQL must return at least one value column after the H3
      index. Each is aggregated via `agg` at parent resolutions and passed
      through raw at the finest level.
    """
    if finest_res < min_res:
        raise ValueError(f"finest_res ({finest_res}) must be >= min_res ({min_res})")

    h3_column, sql_value_columns = _inspect_user_sql(con, sql)
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

    pyramid_sql = build_pyramid_sql(
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
    con.sql(pyramid_sql)

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
            uri = f"{output_uri}res={res}/*.parquet"
            row = con.sql(
                f'SELECT MIN("{col}") AS mn, MAX("{col}") AS mx '
                f"FROM read_parquet('{uri}')"
            ).fetchone()
            by_res[str(res)] = {"min": _jsonable(row[0]), "max": _jsonable(row[1])}
        value_stats[col] = {"by_res": by_res}

    metadata = {
        "finest_res": finest_res,
        "min_res": min_res,
        "agg": agg,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "value_stats": value_stats,
        "layer_name": MVT_LAYER_NAME,
    }
    metadata_sql = (
        f"COPY (SELECT '{_json_dumps_escaped(metadata)}' AS j) "
        f"TO '{output_uri}metadata.json' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(metadata_sql)

    finest_uri = f"{output_uri}res={finest_res}/*.parquet"
    bounds_row = con.sql(
        f"SELECT "
        f"MIN(h3_cell_to_lat(h)) AS s, MAX(h3_cell_to_lat(h)) AS n, "
        f"MIN(h3_cell_to_lng(h)) AS w, MAX(h3_cell_to_lng(h)) AS e, "
        f"COUNT(*) AS ct "
        f"FROM read_parquet('{finest_uri}')"
    ).fetchone()
    w, s, e, n, feature_count = bounds_row[2], bounds_row[0], bounds_row[3], bounds_row[1], bounds_row[4]

    tile_url_template = f"{_public_base_url()}/tiles/hex/{h}/{{z}}/{{x}}/{{y}}.pbf"

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
