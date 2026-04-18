"""Pyramid SQL generation and registration.

register_hex_tiles() materializes a partitioned parquet pyramid to object storage.
Tile requests read directly from the pyramid — no coordination needed.
"""
import json
import os
from typing import List

import duckdb

from tiles.tile_math import content_hash


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
    zoom_offset: int = 4,
) -> dict:
    """Materialize a partitioned parquet pyramid and return tile-endpoint metadata.

    The connection must have httpfs, spatial, and h3 extensions loaded.
    """
    if finest_res < min_res:
        raise ValueError(f"finest_res ({finest_res}) must be >= min_res ({min_res})")

    h3_column, value_columns = _inspect_user_sql(con, sql)
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
    # For local filesystem URIs, DuckDB does not create intermediate directories.
    if not output_uri.startswith("s3://"):
        os.makedirs(output_uri, exist_ok=True)
    con.sql(pyramid_sql)

    # Write a sidecar metadata.json so the tile handler knows finest_res / zoom_offset.
    metadata = {
        "finest_res": finest_res,
        "min_res": min_res,
        "agg": agg,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
    }
    metadata_sql = (
        f"COPY (SELECT '{_json_dumps_escaped(metadata)}' AS j) "
        f"TO '{output_uri}metadata.json' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(metadata_sql)

    # Bounds of finest-level cells (approximate via simple min/max on cell centers).
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
        "feature_count_finest": feature_count,
    }
