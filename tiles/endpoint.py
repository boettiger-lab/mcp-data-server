"""Starlette request handler for /tiles/{namespace}/{name}/{z}/{x}/{y}.pbf.

Verified DuckDB signatures (from Task 1 probe, commit 0ff46e8):
- ST_AsMVTGeom(geom, bounds[, extent, buffer, clip_geom]) -> GEOMETRY
  bounds must be BOX_2D, not GEOMETRY. Use struct cast: {'min_x':…}::BOX_2D
- ST_AsMVT(col0[, col1..col4]) -> BLOB  (aggregate)
- h3_polygon_wkt_to_cells(wkt, resolution) -> UBIGINT[]
- h3_cell_to_boundary_wkt(cell) -> VARCHAR
- ST_Transform requires always_xy=true (4th arg) for EPSG:4326→EPSG:3857
"""
import math
import os
import sys
import anyio
from starlette.requests import Request
from starlette.responses import Response

from tiles.tile_math import tile_xyz_to_lnglat_bounds, zoom_to_h3_res


MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"


def _lng_to_merc_x(lng: float) -> float:
    """Convert longitude (degrees) to Web Mercator X (EPSG:3857)."""
    return lng * 20037508.34 / 180.0


def _lat_to_merc_y(lat: float) -> float:
    """Convert latitude (degrees) to Web Mercator Y (EPSG:3857)."""
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    return y * 20037508.34 / 180.0


def _bucket_base() -> str:
    return os.environ.get("TILE_BUCKET_BASE", "s3://public-output").rstrip("/")


def _tileset_dir(namespace: str, name: str) -> str:
    return f"{_bucket_base()}/{namespace}/{name}"


def _pyramid_exists(con, namespace: str, name: str, res: int) -> bool:
    """Check whether the pyramid partition for (namespace, name, res) is readable."""
    uri = f"{_tileset_dir(namespace, name)}/res={res}/*.parquet"
    try:
        cur = con.cursor()
        cur.sql(f"SELECT 1 FROM read_parquet('{uri}') LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _build_tile_sql(namespace: str, name: str, z: int, x: int, y: int,
                    target_res: int, finest_res: int) -> str:
    """Produce the SQL that returns a single BLOB row (the MVT for this tile).

    Strategy:
      1. Compute the tile's web-mercator bounds as a BOX_2D struct (Python-side).
      2. Compute H3 cells at target_res covering the tile's lng/lat polygon.
      3. Select rows from the pyramid partition whose cell is in that set.
      4. Project cell geometries with ST_AsMVTGeom then aggregate with ST_AsMVT.

    Notes:
    - ST_AsMVTGeom requires BOX_2D (not GEOMETRY). We build it as a struct cast.
    - ST_Transform needs always_xy=true (4th arg) for EPSG:4326→EPSG:3857.
    """
    west, south, east, north = tile_xyz_to_lnglat_bounds(z, x, y)
    tileset = _tileset_dir(namespace, name)
    tile_wkt = (
        f"POLYGON(("
        f"{west} {south}, {east} {south}, {east} {north}, "
        f"{west} {north}, {west} {south}))"
    )
    # Pre-compute web-mercator bounds in Python — avoids ST_Transform inside SQL
    # for the envelope, and lets us pass a literal BOX_2D struct.
    mx_w = _lng_to_merc_x(west)
    mx_e = _lng_to_merc_x(east)
    my_s = _lat_to_merc_y(south)
    my_n = _lat_to_merc_y(north)
    return f"""
        WITH cells AS (
          SELECT UNNEST(h3_polygon_wkt_to_cells('{tile_wkt}', {target_res})) AS cell
        ),
        src AS (
          SELECT p.* FROM read_parquet('{tileset}/res={target_res}/*.parquet') p
          SEMI JOIN cells c ON p.h = c.cell
        ),
        projected AS (
          SELECT
            ST_AsMVTGeom(
              ST_Transform(h3_cell_to_boundary_wkt(h)::GEOMETRY, 'EPSG:4326', 'EPSG:3857', true),
              {{'min_x': {mx_w}, 'min_y': {my_s}, 'max_x': {mx_e}, 'max_y': {my_n}}}::BOX_2D
            ) AS geom,
            src.* EXCLUDE (h)
          FROM src
        )
        SELECT ST_AsMVT(projected) FROM projected WHERE geom IS NOT NULL
    """


def _run_tile_query(con, sql: str) -> bytes:
    """Run the tile SQL on a fresh cursor and return raw MVT bytes (or empty)."""
    cur = con.cursor()
    row = cur.sql(sql).fetchone()
    if row is None or row[0] is None:
        return b""
    return bytes(row[0])


async def serve_tile(request: Request) -> Response:
    """GET /tiles/{namespace}/{name}/{z}/{x}/{y}.pbf"""
    namespace = request.path_params["namespace"]
    name = request.path_params["name"]
    z = int(request.path_params["z"])
    x = int(request.path_params["x"])
    y = int(request.path_params["y"])

    if namespace != "hex":
        return Response(status_code=404)

    con = request.app.state.tile_con

    # Probe whether the tileset exists at any res — use finest since it's always present.
    # We don't know finest_res at tile-serving time without metadata; try a few.
    # Convention: we accept whatever res in [2..15] has data.
    target_res = None
    for guess_finest in range(15, 1, -1):
        if await anyio.to_thread.run_sync(_pyramid_exists, con, namespace, name, guess_finest):
            finest_res = guess_finest
            # (Floor of) zoom_to_h3_res with default zoom_offset=4 and min_res=2.
            # In v1 we don't persist zoom_offset/min_res per-tileset — caller's registered
            # URL captures the content hash, so clients get stable tiles. Use defaults.
            target_res = zoom_to_h3_res(z, min_res=2, finest_res=finest_res, zoom_offset=4)
            break
    if target_res is None:
        return Response(status_code=404)

    sql = _build_tile_sql(namespace, name, z, x, y, target_res, finest_res)
    try:
        mvt_bytes = await anyio.to_thread.run_sync(_run_tile_query, con, sql)
    except Exception as e:
        print(f"Tile {z}/{x}/{y} error: {e}", file=sys.stderr)
        return Response(status_code=500)

    if not mvt_bytes:
        return Response(status_code=204)
    return Response(content=mvt_bytes, media_type=MVT_CONTENT_TYPE)
