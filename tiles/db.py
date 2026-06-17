"""Persistent :memory: DuckDB connection for the tile endpoint.

Separate from the per-request isolated connections used by the `query` tool:
tile requests never take user credentials, so the connection can be long-lived
and shared across requests via con.cursor() for per-request isolation.
"""
import os

import duckdb


def build_tile_connection(threads: int | None = None) -> duckdb.DuckDBPyConnection:
    """Create a :memory: connection with extensions loaded and S3 configured.

    Extensions are assumed to be pre-installed in the image (see mcp-data-server#54);
    LOAD is per-session and always required.

    `threads` sets DuckDB's per-query parallelism for this connection:
    - The shared READ connection (tile-serve GETs + prepare-phase probes) uses
      the default (TILE_THREADS, 48). Harmless there — those queries are tiny
      (LIMIT 0 / LIMIT 1 / single-tile reads).
    - Pyramid BUILD connections pass a lower cap (server._BUILD_THREADS). A build
      pegs every thread for minutes; at 48 it can saturate all cores and starve
      the uvicorn event loop, failing /healthz → liveness SIGKILL (#184). Leaving
      cores free keeps the loop schedulable. Builds run on their own connection
      (server._build_executor) so the cap doesn't slow tile reads.
    """
    con = duckdb.connect(":memory:")
    # Extensions may not be pre-installed in dev environments — install defensively.
    con.sql("INSTALL httpfs; LOAD httpfs")
    con.sql("INSTALL spatial; LOAD spatial")
    con.sql("INSTALL h3 FROM community; LOAD h3")

    if threads is None:
        threads = int(os.environ.get("TILE_THREADS", "48"))
    con.sql(f"SET THREADS={int(threads)}")
    con.sql("SET preserve_insertion_order=false")
    con.sql("SET enable_object_cache=true")
    con.sql("SET temp_directory='/tmp'")

    # S3 access: use the cluster-internal Ceph endpoint (same as query tool).
    con.sql(
        "CREATE OR REPLACE SECRET s3 ("
        "TYPE S3, ENDPOINT 'rook-ceph-rgw-nautiluss3.rook', "
        "URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '')"
    )
    return con
