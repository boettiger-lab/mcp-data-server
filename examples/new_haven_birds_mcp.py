#!/usr/bin/env python3
"""The same analysis as new_haven_birds_ibis.py, executed on the MCP server.

You still write ibis. ibis compiles it to DuckDB SQL, and instead of running
that SQL against a local DuckDB we hand it to the MCP server's `query` tool,
which runs it next to the data on NRP. Only the 32-row result crosses the
network.

    pip install 'ibis-framework[duckdb]' mcp pandas
"""

import asyncio
import io

import ibis
import pandas as pd
from ibis import _
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "https://duckdb-mcp.nrp-nautilus.io/mcp"

# --- datasets ---------------------------------------------------------------
# The server reads the NRP copy, so the paths here are the NRP ones the STAC
# catalog publishes. con.sql() keeps the path inline (con.read_parquet() would
# register a named view, which means nothing to the server). ibis only needs
# column NAMES to compile the query, which it gets by reading Parquet footers.
con = ibis.duckdb.connect(extensions=["httpfs"])
con.raw_sql("""
    CREATE SECRET nrp (
        TYPE S3, KEY_ID '', SECRET '',
        ENDPOINT 's3-west.nrp-nautilus.io',
        URL_STYLE 'path', USE_SSL 'true', SCOPE 's3://public'
    )""")


def hexed(path: str) -> ibis.Table:
    return con.sql(f"SELECT * FROM read_parquet('s3://public{path}')")


places = hexed("-census/census-2024/place/hex/h0=*/data_0.parquet")
blockgroups = hexed("-census/acs-2020-2024/blockgroup/hex/h0=*/data_0.parquet")
gbif = hexed("-gbif/2026-06/hex/h0=*/data_0.parquet")


# --- the pipeline (identical to new_haven_birds_ibis.py) --------------------
# 1. The res-10 cells that make up the city of New Haven.
nh_cells = (
    places.filter(_.STATEFP == "09", _.NAME == "New Haven")
    .select("h0", "h10")
    .distinct()
)

# 2. Connecticut block groups inside those cells, tagged with their 11-digit
#    tract id (state + county + tract).
nh_bg = (
    blockgroups.filter(_.STATEFP == "09")
    .semi_join(nh_cells, ["h0", "h10"])
    .mutate(tract=_.STATEFP + _.COUNTYFP + _.TRACTCE)
)

# 3. A cell -> tract lookup. min() breaks ties so a cell straddling a boundary
#    is counted once rather than in both tracts.
cell_tract = nh_bg.group_by(["h0", "h10"]).agg(tract=_.tract.min())

# 4. Income and population per tract. Both are block-group attributes repeated
#    on every hex row, so deduplicate by block group first.
#
#    A median is not a sum: to roll block groups up to a tract, take the
#    population-weighted mean of their medians. Block groups whose income
#    estimate is suppressed drop out of both halves of the ratio.
tract_stats = (
    nh_bg.select("tract", "GEOID", "TotPop", "MHHI")
    .distinct()
    .group_by("tract")
    .agg(
        population=_.TotPop.sum(),
        median_income=(_.MHHI * _.TotPop).sum()
        / _.MHHI.isnull().ifelse(0, _.TotPop).sum(),
    )
)

# 5. Bird observations per tract. `class` is a Python keyword, so index it.
birds = (
    gbif.filter(_["class"] == "Aves")
    .inner_join(cell_tract, ["h0", "h10"])
    .group_by("tract")
    .agg(bird_observations=_.count())
)

result = tract_stats.inner_join(birds, "tract").order_by(_.bird_observations.desc())


def parse_md_table(text: str) -> pd.DataFrame:
    """`query` answers with a markdown table.

    Read every column as a string first: tract ids are FIPS codes, and letting
    pandas infer their type would eat the leading zero.
    """
    rows = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    body = [rows[0]] + rows[2:]  # keep the header, drop the |---| separator
    csv = "\n".join(ln.strip().strip("|") for ln in body)
    df = pd.read_csv(io.StringIO(csv), sep="|", skipinitialspace=True, dtype=str)
    return df.rename(columns=lambda c: c.strip()).map(
        lambda v: v.strip() if isinstance(v, str) else v
    )


async def main() -> None:
    # Compile locally, execute remotely.
    sql = str(ibis.to_sql(result))
    print(sql)

    # mcp >= 2.0. On the 1.x SDK this is `streamablehttp_client`, and it
    # yields a third element (the session id).
    async with streamable_http_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            out = await session.call_tool("query", {"sql_query": sql})

    tracts = parse_md_table(out.content[0].text)
    numeric = ["population", "median_income", "bird_observations"]
    tracts[numeric] = tracts[numeric].astype(float)
    print(tracts.to_string(index=False))


if __name__ == "__main__":
    asyncio.run(main())
