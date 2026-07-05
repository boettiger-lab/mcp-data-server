"""Persistent :memory: DuckDB connection for the tile endpoint.

Separate from the per-request isolated connections used by the `query` tool:
tile requests never take user credentials, so the connection can be long-lived
and shared across requests via con.cursor() for per-request isolation.
"""
import os

import duckdb

from s3config import default_s3_secret_sql, source_secret_sql


def build_tile_connection(threads: int | None = None) -> duckdb.DuckDBPyConnection:
    """Create a :memory: connection with extensions loaded and S3 configured.

    Extensions are pre-installed in the image (Dockerfile INSTALL step); LOAD is
    per-session. Dev environments must run the Dockerfile or manually INSTALL once.

    `threads` sets DuckDB's per-query parallelism for this connection:
    - The shared READ connection (tile-serve GETs + prepare-phase probes) uses
      the default (TILE_THREADS, 48). Harmless there — those queries are tiny
      (LIMIT 0 / LIMIT 1 / single-tile reads).
    - Pyramid BUILD connections pass server._BUILD_THREADS (env TILE_BUILD_THREADS,
      default 48 — same as reads, leaving ~16 cores free under the 64-core limit).
      A build pegs every thread for minutes; the headroom keeps the uvicorn event
      loop schedulable so /healthz answers (#184/#185). Drop it via env only on
      under-provisioned nodes. Builds run on their own connection
      (server._build_executor) so this never slows tile reads.
    """
    con = duckdb.connect(":memory:")
    con.sql("LOAD httpfs")
    con.sql("LOAD spatial")
    con.sql("LOAD h3")

    if threads is None:
        threads = int(os.environ.get("TILE_THREADS", "48"))
    con.sql(f"SET THREADS={int(threads)}")
    con.sql("SET preserve_insertion_order=false")
    con.sql("SET enable_object_cache=true")
    con.sql("SET temp_directory='/tmp'")

    # httpfs read tuning. NOTE: the "~126 files per h0 / ~923 files" figure behind
    # the original #190 characterization was a since-fixed GBIF over-sharding bug,
    # NOT steady state. Every queryable hex dataset is now 1 file per h0 (carbon:
    # 122 files @112 MB; gbif 2026: 122 @3 GB; padus: 21 @356 MB), so a pruned query
    # opens ONE footer and a global scan opens <=122 — per-file-open latency is no
    # longer the dominant cost; large scans are bandwidth-bound on the byte stream.
    # metadata + connection caching still help the long-lived tile-serve connection
    # that re-reads the same files across tile requests, so they stay.
    con.sql("SET enable_http_metadata_cache=true")
    con.sql("SET httpfs_connection_caching=true")
    # prefetch_all_parquet_files: no-op for a 1-file pruned read; its measured
    # ~17-26% gain came entirely from the over-sharded case. Kept pending a
    # re-measure at 1-file/h0 (see benchmarks/s3-throughput-bench.py); drop if neutral.
    con.sql("SET prefetch_all_parquet_files=true")

    # S3 access: the deployment default endpoint (S3_DEFAULT_ENDPOINT, same env
    # surface as the query tool — #268/#271). Previously hardcoded to the
    # cluster-internal Ceph endpoint, which broke hex tiles (source reads AND
    # s3://public-output writes) on any deployment repointed via env.
    con.sql(default_s3_secret_sql())
    # Scoped registry-source secrets (#264), same set as the query tool — so a
    # register_hex_tiles SQL over e.g. source.coop mirror paths routes correctly
    # here too (previously only the query connections had the mirror secret).
    for stmt in source_secret_sql():
        con.sql(stmt)
    return con
