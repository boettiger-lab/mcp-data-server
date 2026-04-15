"""Persistent :memory: DuckDB connection for the tile endpoint.

Separate from the per-request isolated connections used by the `query` tool:
tile requests never take user credentials, so the connection can be long-lived
and shared across requests via con.cursor() for per-request isolation.
"""
import sys
from contextlib import asynccontextmanager
import duckdb


def build_tile_connection() -> duckdb.DuckDBPyConnection:
    """Create a :memory: connection with extensions loaded.

    Extensions are assumed to be pre-installed in the image (see mcp-data-server#54);
    LOAD is per-session and always required.
    """
    con = duckdb.connect(":memory:")
    # Extensions may not be pre-installed in dev environments — install defensively.
    con.sql("INSTALL httpfs; LOAD httpfs")
    con.sql("INSTALL spatial; LOAD spatial")
    con.sql("INSTALL h3 FROM community; LOAD h3")
    return con


@asynccontextmanager
async def tile_lifespan(app):
    """Starlette lifespan that creates and tears down the persistent connection.

    Stored on app.state.tile_con so request handlers can reach it.
    """
    con = build_tile_connection()
    print("📦 Tile endpoint: persistent DuckDB connection ready", file=sys.stderr)
    app.state.tile_con = con
    try:
        yield
    finally:
        con.close()
        print("📦 Tile endpoint: connection closed", file=sys.stderr)
