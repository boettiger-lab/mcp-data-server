#!/usr/bin/env python3
"""Cirrus CPU (DuckDB) baseline for the GPU-vs-CPU suite, adapted to the
current data model (see notes in benchmarks/gpu-vs-cpu.md).

Differences from the April fork suite (mcp-gpu-data-server/benchmarks/benchmark.py):
  * WDPA hex moved: s3://public-wdpa/hex/** -> s3://public-wdpa/wdpa/hex/**
  * IUCN hex reprocessed: s3://public-iucn/hex/* lost h8 (now h3/h4/h5).
    The h8-grain replacement is s3://public-iucn/richness/hex/*, which stores
    h8/h0 as h3 hex STRINGS, so joins need h3_string_to_h3() and the h0
    partition filter needs hex-string literals to still prune.
"""
import asyncio, csv, os, statistics, sys, time
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("CPU_MCP_URL", "https://duckdb-mcp.carlboettiger.info/mcp")
RUNS = int(os.environ.get("RUNS", "1"))
ONLY = [q for q in os.environ.get("ONLY", "").split(",") if q]

H0_INT = ("576531121047601151, 576707042908045311, 576742227280134143, 576812596024311807, "
          "576882964768489471, 576953333512667135, 576988517884755967, 577094071001022463, "
          "577164439745200127, 577199624117288959, 577234808489377791, 577692205326532607, "
          "577727389698621439, 577762574070710271, 578114417791598591, 578149602163687423, "
          "578290339652042751, 578395892768309247, 578747736489197567, 578923658349641727, "
          "578994027093819391, 579381055186796543, 579451423930974207, 579592161419329535, "
          "579627345791418367, 579908820768129023, 580119927000662015, 580401401977372671")
H0_HEX = ("'8003fffffffffff', '800dfffffffffff', '800ffffffffffff', '8013fffffffffff', "
          "'8017fffffffffff', '801bfffffffffff', '801dfffffffffff', '8023fffffffffff', "
          "'8027fffffffffff', '8029fffffffffff', '802bfffffffffff', '8045fffffffffff', "
          "'8047fffffffffff', '8049fffffffffff', '805dfffffffffff', '805ffffffffffff', "
          "'8067fffffffffff', '806dfffffffffff', '8081fffffffffff', '808bfffffffffff', "
          "'808ffffffffffff', '80a5fffffffffff', '80a9fffffffffff', '80b1fffffffffff', "
          "'80b3fffffffffff', '80c3fffffffffff', '80cffffffffffff', '80dffffffffffff'")

CARBON = f"""  SELECT h8, h0, SUM(carbon) AS total_carbon
  FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/**')
  WHERE h0 IN ({H0_INT})
  GROUP BY h8, h0"""

def iucn(col):
    return f"""  SELECT h3_string_to_h3(h8) AS h8, h3_string_to_h3(h0) AS h0, {col}
  FROM read_parquet('s3://public-iucn/richness/hex/{col}/**')
  WHERE h0 IN ({H0_HEX})"""

QUERIES = {
 "Q3a-c": f"""WITH carbon AS (
{CARBON}
), iucn AS (
{iucn('combined_sr')}
)
SELECT a.h8, a.total_carbon, b.combined_sr
FROM carbon a JOIN iucn b ON a.h8 = b.h8 AND a.h0 = b.h0""",

 "Q4a-c": f"""WITH carbon AS (
  SELECT h8 AS carbon_h8, h0 AS carbon_h0, SUM(carbon) AS total_carbon
  FROM read_parquet('s3://public-carbon/irrecoverable-carbon-2024/hex/**')
  WHERE h0 IN ({H0_INT})
  GROUP BY h8, h0
)
SELECT b.carbon_h8 AS h8, b.total_carbon, COUNT(DISTINCT a.SITE_ID) AS n_protected_areas
FROM read_parquet('s3://public-wdpa/wdpa/hex/**') a
JOIN carbon b ON a.h8 = b.carbon_h8 AND a.h0 = b.carbon_h0
GROUP BY b.carbon_h8, b.total_carbon""",

 "Q5a-c": f"""WITH carbon AS (
{CARBON}
), iucn_c AS (
{iucn('combined_sr')}
), iucn_b AS (
{iucn('birds_sr')}
)
SELECT a.h8, a.total_carbon, b.combined_sr, c.birds_sr
FROM carbon a
JOIN iucn_c b ON a.h8 = b.h8 AND a.h0 = b.h0
JOIN iucn_b c ON a.h8 = c.h8 AND a.h0 = c.h0""",
}

SETUP = ["SET s3_allow_recursive_globbing=false", "SET preserve_insertion_order=false",
         "SET enable_object_cache=false"]

async def main():
    rows = []
    ids = ONLY or list(QUERIES)
    async with streamablehttp_client(URL, timeout=900) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for stmt in SETUP:
                await s.call_tool("query", {"sql_query": stmt})
            for qid in ids:
                for run in range(1, RUNS + 1):
                    t = time.perf_counter()
                    res = await asyncio.wait_for(
                        s.call_tool("query", {"sql_query": QUERIES[qid]}), timeout=900)
                    el = time.perf_counter() - t
                    txt = "".join(getattr(c, "text", "") for c in res.content)
                    err = txt.strip()[:90] if txt.lstrip().startswith(("SQL Error", "Error")) else ""
                    rows.append({"query_id": qid, "server": "cpu-cirrus", "run": run,
                                 "elapsed_s": round(el, 3), "error": err})
                    print(f"  {qid} run {run}/{RUNS}: {el:7.2f}s {'ERROR ' + err if err else 'ok'}",
                          flush=True)
    with open("results_cirrus_cpu.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["query_id", "server", "run", "elapsed_s", "error"])
        wr.writeheader(); wr.writerows(rows)
    print("\nmedians:")
    for qid in ids:
        ok = [r["elapsed_s"] for r in rows if r["query_id"] == qid and not r["error"]]
        if ok:
            print(f"  {qid}: {statistics.median(ok):.2f}s  (n={len(ok)})")

asyncio.run(main())
