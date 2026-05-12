"""Persistent :memory: DuckDB connection for the tile endpoint.

Separate from the per-request isolated connections used by the `query` tool:
tile requests never take user credentials, so the connection can be long-lived
and shared across requests via con.cursor() for per-request isolation.
"""
import duckdb


def build_tile_connection() -> duckdb.DuckDBPyConnection:
    """Create a :memory: connection with extensions loaded and S3 configured.

    Extensions are assumed to be pre-installed in the image (see mcp-data-server#54);
    LOAD is per-session and always required.
    """
    con = duckdb.connect(":memory:")
    # Extensions may not be pre-installed in dev environments — install defensively.
    con.sql("INSTALL httpfs; LOAD httpfs")
    con.sql("INSTALL spatial; LOAD spatial")
    con.sql("INSTALL h3 FROM community; LOAD h3")

    # Per-query parallelism. THREADS=48 gives register_hex_tiles enough
    # parallel S3 readers / hash-aggregation workers to push Phase 1 of the
    # pyramid build past its previous ~30 MB/s effective S3 read ceiling
    # (the bottleneck observed on the global irrecoverable-carbon build,
    # which took ~6 min at THREADS=16). Headroom of 16 of the 64-core
    # CPU limit is left for concurrent tile-serving requests that hit the
    # same connection while a pyramid build is in flight.
    con.sql("SET THREADS=48")
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
