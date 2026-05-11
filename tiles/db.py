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

    # Cap per-query parallelism so several concurrent tile requests can run side
    # by side instead of one query monopolizing the pod. With CPU limit 64 and
    # THREADS=16, ~4 concurrent tile queries saturate cleanly; pyramid builds
    # (which share this connection) are bandwidth-bound on S3 anyway.
    con.sql("SET THREADS=16")
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
