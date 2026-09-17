#!/usr/bin/env python3
"""Bird observations vs. median household income, by tract, New Haven CT.

The Python twin of new_haven_birds_dplyr.R. Runs entirely on YOUR machine:
a local DuckDB streams Parquet from the source.coop mirror. No MCP server,
no API key, no data written to disk.

See new_haven_birds_mcp.py for the same pipeline executed on the server.
"""

import tempfile

import ibis
from ibis import _

ibis.options.interactive = False

# --- connection -------------------------------------------------------------
# Extensions and DuckDB settings go straight into connect(). The memory limit
# keeps a laptop-sized footprint: DuckDB streams and spills to disk rather than
# trying to hold the join in RAM.
#
# Spill to node-local scratch, not the working directory. On JupyterHub, HPC or
# any cluster, $HOME is networked storage and spilling there is far slower than
# the local ephemeral disk that mkdtemp() resolves to. mkdtemp() also keeps
# two users on the same node from colliding on one directory.
con = ibis.duckdb.connect(
    extensions=["httpfs"],
    memory_limit="3GB",
    temp_directory=tempfile.mkdtemp(prefix="duckdb-"),
)

# Anonymous reads from the source.coop mirror on AWS us-west-2. The bucket name
# contains dots, so URL_STYLE 'path' is required for the TLS cert to match.
con.raw_sql("""
    CREATE SECRET source_coop (
        TYPE S3, KEY_ID '', SECRET '',
        ENDPOINT 's3.us-west-2.amazonaws.com', REGION 'us-west-2',
        URL_STYLE 'path', USE_SSL 'true',
        SCOPE 's3://us-west-2.opendata.source.coop'
    )""")

# --- datasets ---------------------------------------------------------------
# con.sql() (rather than con.read_parquet(), which would register a named view)
# keeps the S3 path *inside* the SQL ibis generates -- which is what lets
# new_haven_birds_mcp.py ship the very same pipeline to the server.
BASE = "s3://us-west-2.opendata.source.coop/cboettig"


def hexed(path: str) -> ibis.Table:
    return con.sql(f"SELECT * FROM read_parquet('{BASE}{path}')")


places = hexed("/census/census-2024/place/hex/h0=*/data_0.parquet")
blockgroups = hexed("/census/acs-2020-2024/blockgroup/hex/h0=*/data_0.parquet")
gbif = hexed("/gbif/2026-06/hex/h0=*/data_0.parquet")


# --- the pipeline -----------------------------------------------------------
# Every dataset is indexed on the same H3 grid, so "which tract is this bird
# observation in?" is a key equality, not a point-in-polygon test. h10 cells are
# ~0.015 km2, fine enough that one rarely straddles two city block groups.
#
# Join on both h0 and h10. h0 is the partition column, and including it lets
# DuckDB skip all but one file per dataset.

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


# Nothing has touched the network yet -- `result` is a deferred expression.
print(ibis.to_sql(result))

# --- run it -----------------------------------------------------------------
tracts = result.execute()  # ~90 s; only these 32 rows come back to Python
print(tracts.to_string(index=False))

try:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.5, 5))
    ax.scatter(tracts["median_income"], tracts["bird_observations"],
               s=40, alpha=0.85, color="#1b7837")
    ax.set_yscale("log")
    ax.set_xlabel("Median household income (ACS 2020-2024)")
    ax.set_ylabel("GBIF bird observations (log scale)")
    ax.set_title("Bird observations vs. median income, New Haven CT tracts")
    fig.tight_layout()
    fig.savefig("new-haven-birds.png", dpi=150)
    print("wrote new-haven-birds.png")
except ImportError:
    print("install matplotlib to render the scatter plot")
