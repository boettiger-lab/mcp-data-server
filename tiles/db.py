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
    - Pyramid BUILD connections pass server._BUILD_THREADS (env TILE_BUILD_THREADS,
      default 48 — same as reads, leaving ~16 cores free under the 64-core limit).
      A build pegs every thread for minutes; the headroom keeps the uvicorn event
      loop schedulable so /healthz answers (#184/#185). Drop it via env only on
      under-provisioned nodes. Builds run on their own connection
      (server._build_executor) so this never slows tile reads.
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

    # httpfs read tuning (#190 I/O characterization). The build/scan cost is bound
    # by per-file-open latency over the pod->Ceph path, NOT CPU (a scan pegs ~2 of
    # 64 cores; ~170 MB/s on a many-file scan vs ~370 MB/s few-file). GBIF hex is
    # sharded up to ~126 files per h0, so a country/global scan opens ~all 923
    # files and pays serial per-file round-trips. Prefetch every parquet footer
    # up-front in parallel and cache HTTP metadata + connections so those opens
    # overlap. Measured ~17-26% faster on real GBIF builds (concurrent, swapped-pod
    # A/B), same bytes read. NOTE: prefetch_all_parquet_files prefetches all footers
    # in the read glob — fine at GBIF's ~10^3 files; revisit if a dataset globs 10^4+.
    con.sql("SET enable_http_metadata_cache=true")
    con.sql("SET httpfs_connection_caching=true")
    con.sql("SET prefetch_all_parquet_files=true")

    # S3 access: use the cluster-internal Ceph endpoint (same as query tool).
    con.sql(
        "CREATE OR REPLACE SECRET s3 ("
        "TYPE S3, ENDPOINT 'rook-ceph-rgw-nautiluss3.rook', "
        "URL_STYLE 'path', USE_SSL 'false', KEY_ID '', SECRET '')"
    )
    return con
