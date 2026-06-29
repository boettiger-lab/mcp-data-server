"""Starlette request handler for /tiles/{namespace}/{name}/{z}/{x}/{y}.pbf.

Verified DuckDB signatures (from Task 1 probe, commit 0ff46e8):
- ST_AsMVTGeom(geom, bounds[, extent, buffer, clip_geom]) -> GEOMETRY
  bounds must be BOX_2D, not GEOMETRY. Use struct cast: {'min_x':…}::BOX_2D
- ST_AsMVT(col0[, col1..col4]) -> BLOB  (aggregate)
- h3_polygon_wkt_to_cells(wkt, resolution) -> UBIGINT[]
- h3_cell_to_boundary_wkt(cell) -> VARCHAR
- ST_Transform requires always_xy=true (4th arg) for EPSG:4326→EPSG:3857
"""
import json
import math
import os
import sys
import time
import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tiles.tile_math import (
    h3_edge_padding_deg,
    tile_xyz_to_lnglat_bounds,
    zoom_to_h3_res,
)


MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"

# Tile content is deterministic per (hash, z, x, y): the hash is content-addressed
# from the registration inputs, so the same URL always produces the same bytes
# unless the tileset is explicitly purged and rebuilt. "immutable" tells browsers
# and CDNs to serve cached responses without revalidation.
TILE_CACHE_CONTROL = "public, max-age=86400, immutable"


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


def _read_metadata(con, namespace: str, name: str):
    """Read the metadata.json sidecar for a tileset using an existing connection.

    Uses the persistent tile_con (already has httpfs loaded) rather than
    opening a new DuckDB connection per request.
    Returns None if the sidecar is missing or unreadable.
    """
    uri = f"{_tileset_dir(namespace, name)}/metadata.json"
    try:
        # Local path shortcut for tests.
        if not uri.startswith("s3://") and not uri.startswith("http"):
            with open(uri, "r") as f:
                return json.loads(f.read().strip())
        row = con.cursor().sql(f"SELECT content FROM read_text('{uri}')").fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _get_cached_metadata(app_state, con, namespace: str, name: str):
    """Return metadata for a tileset, caching it on app.state after first read.

    Metadata never changes after registration, so one read per server lifetime
    is sufficient.
    """
    cache_attr = "_tile_meta_cache"
    cache = getattr(app_state, cache_attr, None)
    if cache is None:
        cache = {}
        setattr(app_state, cache_attr, cache)
    key = f"{namespace}/{name}"
    if key not in cache:
        meta = _read_metadata(con, namespace, name)
        if meta is not None:
            cache[key] = meta
        return meta
    return cache[key]


def _boundary_geom_sql(touches_dateline: bool) -> str:
    """Return a SQL scalar expression for cell `src.h`'s H3 boundary polygon,
    already projected to EPSG:3857 (web mercator).

    Non-dateline tiles: ST_Transform of the raw EPSG:4326 boundary.

    Dateline tiles: `h3_cell_to_boundary_wkt` emits a cell straddling +/-180 as
    a single ring whose longitudes jump +179.9 -> -179.9 (#164). We rebuild it
    in a continuous frame (shift each vertex +/-360 to stay within 180 deg of
    the cell's center longitude) AND project to mercator *by hand* rather than
    via ST_Transform. ST_Transform re-normalizes longitude into [-180,180],
    which would undo the unwrap (e.g. -180.16 -> +179.84) and recreate a
    globe-spanning polygon -> the horizontal streaks of #201. Mercator x is
    linear in lng, so projecting the shifted (possibly out-of-range) longitudes
    directly keeps the cell continuous just past the world edge, where
    ST_AsMVTGeom clips it cleanly. Only the edge tile columns pass
    touches_dateline=True. x/y formulas match _lng_to_merc_x / _lat_to_merc_y
    (and EPSG:3857) so dateline cells line up with the ST_Transform'd rest.
    """
    if not touches_dateline:
        return ("ST_Transform(h3_cell_to_boundary_wkt(src.h)::GEOMETRY, "
                "'EPSG:4326', 'EPSG:3857', true)")
    scale = 20037508.34 / 180.0
    return f"""(
        WITH _v AS (
          SELECT ST_X(d.geom) AS x, ST_Y(d.geom) AS y, d.path[1] AS idx
          FROM UNNEST(ST_Dump(ST_Points(
                 h3_cell_to_boundary_wkt(src.h)::GEOMETRY))) AS u(d)
        ),
        _c AS (
          SELECT (MAX(x) - MIN(x)) > 180 AS crosses,
                 h3_cell_to_lng(src.h) AS ref
          FROM _v
        )
        SELECT ST_MakePolygon(ST_MakeLine(list(ST_Point(
          (CASE WHEN _c.crosses AND (_v.x - _c.ref) >  180 THEN _v.x - 360
                WHEN _c.crosses AND (_v.x - _c.ref) < -180 THEN _v.x + 360
                ELSE _v.x END) * {scale},
          ln(tan((90.0 + _v.y) * pi() / 360.0)) / (pi() / 180.0) * {scale}
          ) ORDER BY _v.idx)))
        FROM _v, _c
      )"""


def _build_tile_sql(namespace: str, name: str, z: int, x: int, y: int,
                    target_res: int, finest_res: int) -> str:
    """Produce the SQL that returns a single BLOB row (the MVT for this tile).

    Strategy:
      1. Compute the tile's lng/lat bbox and web-mercator BOX_2D (Python-side).
      2. Restrict the parquet scan to the h0 partitions overlapping the tile
         bbox. SEMI JOIN against a `bbox_h0` CTE lets DuckDB hive-prune the
         partitioned pyramid to a handful of files instead of scanning the
         entire globe at the target resolution.
      3. Filter pyramid rows where the cell's center lat/lng falls inside the
         tile bbox *widened by one hex circumradius* (h3_edge_padding_deg).
         A hex straddling a tile edge has its center in one tile but its body
         in both; the pad makes every tile it touches emit it, so the renderer
         draws the full hex on both sides of the seam (#188). The same hex thus
         appears in several neighboring tiles — harmless for rendering, and
         ST_AsMVTGeom's buffer lets MapLibre stitch the duplicates seamlessly.
      4. Project cell geometries with ST_AsMVTGeom then aggregate with ST_AsMVT.

    bbox_h0 candidate-cell derivation:
      H3's polygon_wkt_to_cells uses "center inside polygon" semantics. At
      res=0 a single base cell is ~1700 km wide, so any tile smaller than that
      contains no h0 centers and the polygon call returns nothing. Instead we
      sample h3_latlng_to_cell across a grid covering the bbox. At z<=2 the
      tile is large enough that grid sampling can still miss h0s in the gaps
      (sample spacing > h0 diameter), so we fall back to the full set of 122
      base cells — cheap at low zoom because the target partition is small.

    Notes:
    - ST_AsMVTGeom requires BOX_2D (not GEOMETRY). We build it as a struct cast.
    - ST_Transform needs always_xy=true (4th arg) for EPSG:4326→EPSG:3857.
    """
    west, south, east, north = tile_xyz_to_lnglat_bounds(z, x, y)
    tileset = _tileset_dir(namespace, name)
    # Only the edge tile columns (x=0 west=-180, x=2^z-1 east=+180) can hold
    # cells whose boundary crosses the antimeridian; unwrap geometry only there.
    touches_dateline = west <= -179.999 or east >= 179.999
    boundary_geom = _boundary_geom_sql(touches_dateline)
    # ST_AsMVTGeom bounds stay the *exact* tile bbox — the merc projection must
    # map cell coords to true tile pixels. The seam pad widens only the cell
    # SELECTION window (below), not the projection bounds.
    mx_w = _lng_to_merc_x(west)
    mx_e = _lng_to_merc_x(east)
    my_s = _lat_to_merc_y(south)
    my_n = _lat_to_merc_y(north)

    # Widen the cell-selection window by ~one hex circumradius so boundary-
    # crossing hexes are emitted by every tile they touch (#188). pad_lng grows
    # with latitude; use the pole-most edge so the wider side covers the tile.
    pad_lat, pad_lng = h3_edge_padding_deg(target_res, max(abs(north), abs(south)))
    south_p = south - pad_lat
    north_p = north + pad_lat
    west_p = west - pad_lng
    east_p = east + pad_lng

    if z <= 2:
        bbox_h0_sql = "SELECT CAST(UNNEST(h3_get_res0_cells()) AS BIGINT) AS h0"
    else:
        # 8x8 grid → sample spacing ~tile_width/7. At z>=3 tile width <= 45°
        # while h0 diameter is ~15° (Earth/12 cells), so every overlapping h0
        # cell receives at least one sample point. Sample the padded bbox so an
        # edge hex sitting in a neighboring h0 partition isn't pruned away.
        #
        # Bounds are CAST to DOUBLE: a sub-1 high-precision float literal (e.g.
        # 0.2033…, produced when the seam pad shifts an equator/meridian tile
        # edge off 0.0) is typed by DuckDB as DECIMAL(18,17) — one integer digit
        # — and the grid subtraction with the opposite bound (>=10°) overflows it,
        # 500-ing equatorial mid-zoom tiles on global data (#204). Double
        # arithmetic sidesteps the decimal typing entirely.
        def _d(v: float) -> str:
            return f"CAST({v!r} AS DOUBLE)"
        lat_expr = f"{_d(south_p)} + (i/7.0) * ({_d(north_p)} - {_d(south_p)})"
        lng_expr = f"{_d(west_p)} + (j/7.0) * ({_d(east_p)} - {_d(west_p)})"
        bbox_h0_sql = (
            "SELECT DISTINCT CAST(h3_latlng_to_cell(\n"
            f"  {lat_expr},\n"
            f"  {lng_expr},\n"
            "  0\n"
            ") AS BIGINT) AS h0\n"
            "FROM range(8) t1(i), range(8) t2(j)"
        )

    return f"""
        WITH bbox_h0 AS (
          {bbox_h0_sql}
        ),
        src AS (
          SELECT p.* FROM read_parquet(
            '{tileset}/res={target_res}/**/*.parquet',
            hive_partitioning=true
          ) p
          SEMI JOIN bbox_h0 USING (h0)
          WHERE h3_cell_to_lat(p.h) BETWEEN {south_p} AND {north_p}
            AND h3_cell_to_lng(p.h) BETWEEN {west_p} AND {east_p}
        ),
        projected AS (
          SELECT
            ST_AsMVTGeom(
              -- boundary_geom is already EPSG:3857 (_boundary_geom_sql projects
              -- it; the dateline branch by hand to avoid ST_Transform re-wrapping
              -- the unwrap into globe-spanning streaks, #201).
              -- ST_MakeValid repairs cells whose mercator projection is still
              -- degenerate/self-intersecting — at coarse zoom near the mercator
              -- latitude limit (~±85.05°) a projected H3 boundary can become
              -- invalid, and ST_AsMVTGeom (an aggregate over the whole tile)
              -- raises TopologyException, 500-ing the *entire* tile on one bad
              -- cell. Validating per-cell keeps one degenerate hex from taking
              -- down a global/pole-spanning tileset's coarse tiles (#197).
              ST_MakeValid({boundary_geom}),
              {{'min_x': {mx_w}, 'min_y': {my_s}, 'max_x': {mx_e}, 'max_y': {my_n}}}::BOX_2D,
              4096, 256, true
            ) AS geom,
            src.* EXCLUDE (h, h0)
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


async def serve_metadata(request: Request) -> Response:
    """GET /tiles/{namespace}/{name}/metadata.json

    Serve the tileset's metadata sidecar (value_stats, bounds, layer_name,
    value_columns, finest_res, ...) from the same origin as the tiles. The
    client derives this URL from the tile_url template by swapping the
    `/{z}/{x}/{y}.pbf` suffix for `metadata.json`, then reads the color-scale
    inputs directly — so they never have to be transcribed through an LLM's
    tool-call arguments (where weak models corrupt the large value_stats JSON,
    silently dropping the layer-add; see the hex-not-showing investigation).
    """
    namespace = request.path_params["namespace"]
    name = request.path_params["name"]

    if namespace != "hex":
        return Response(status_code=404)

    con = request.app.state.tile_con
    meta = await anyio.to_thread.run_sync(
        _get_cached_metadata, request.app.state, con, namespace, name
    )
    if meta is None:
        return Response(status_code=404)
    return JSONResponse(meta, headers={"Cache-Control": TILE_CACHE_CONTROL})


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

    meta = await anyio.to_thread.run_sync(
        _get_cached_metadata, request.app.state, con, namespace, name
    )
    if meta is None:
        return Response(status_code=404)
    finest_res = meta["finest_res"]
    min_res = meta["min_res"]
    zoom_offset = meta["zoom_offset"]
    # Adaptive zoom->res (#188): bounds + feature_count_finest let the mapping
    # pick the finest res that keeps each tile within the cell budget, so bounded
    # data (CA) shows finer hexes at mid-zoom than global data does. Both fields
    # are present in all post-#178 metadata; .get keeps pre-#178 tilesets on the
    # legacy linear mapping. Budget is ops-tunable via TILE_TARGET_CELLS_PER_TILE.
    target_res = zoom_to_h3_res(
        z,
        min_res=min_res,
        finest_res=finest_res,
        zoom_offset=zoom_offset,
        feature_count_finest=meta.get("feature_count_finest"),
        bounds=meta.get("bounds"),
        target_cells_per_tile=int(
            os.environ.get("TILE_TARGET_CELLS_PER_TILE", "4000")
        ),
    )

    sql = _build_tile_sql(namespace, name, z, x, y, target_res, finest_res)
    t0 = time.perf_counter()
    try:
        mvt_bytes = await anyio.to_thread.run_sync(_run_tile_query, con, sql)
    except Exception as e:
        print(f"Tile {z}/{x}/{y} error: {e}", file=sys.stderr)
        return Response(status_code=500)
    # Per-tile serve timing (#178) — grep [tile-serve] to spot fat/slow tiles
    # (the symptom of an over-fine zoom->resolution mapping).
    print(
        f"[tile-serve] hex/{name} z={z} x={x} y={y} res={target_res} "
        f"bytes={len(mvt_bytes)} {(time.perf_counter() - t0) * 1000:.0f}ms",
        file=sys.stderr,
    )

    if not mvt_bytes:
        return Response(status_code=204, headers={"Cache-Control": TILE_CACHE_CONTROL})
    return Response(
        content=mvt_bytes,
        media_type=MVT_CONTENT_TYPE,
        headers={"Cache-Control": TILE_CACHE_CONTROL},
    )
