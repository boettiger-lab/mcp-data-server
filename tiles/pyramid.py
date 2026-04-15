"""Pyramid SQL generation and registration.

register_hex_tiles() materializes a partitioned parquet pyramid to object storage.
Tile requests read directly from the pyramid — no coordination needed.
"""
from typing import List


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

    The finest-resolution level stores the user's values unaggregated; parents
    at each coarser resolution aggregate via the user-chosen `agg` function.
    """
    value_list_raw = ", ".join(value_columns)
    value_list_agg = ", ".join(f"{agg}({c}) AS {c}" for c in value_columns)

    selects = []
    # Parents: min_res .. finest_res - 1, each aggregated.
    for res in range(min_res, finest_res):
        selects.append(
            f"  SELECT h3_cell_to_parent({h3_column}, {res}) AS h, "
            f"{value_list_agg}, {res} AS res FROM src GROUP BY 1"
        )
    # Finest level: raw values, no aggregation.
    selects.append(
        f"  SELECT {h3_column} AS h, {value_list_raw}, {finest_res} AS res FROM src"
    )

    body = "\n  UNION ALL\n".join(selects)

    return (
        "COPY (\n"
        f"  WITH src AS (\n{user_sql}\n  )\n"
        f"{body}\n"
        f") TO '{output_uri}' "
        f"(FORMAT PARQUET, PARTITION_BY (res), OVERWRITE_OR_IGNORE)"
    )
