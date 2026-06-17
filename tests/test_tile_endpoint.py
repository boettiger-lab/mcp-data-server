import os
import pytest
import duckdb
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tiles.endpoint import serve_tile
from tiles.pyramid import register_hex_tiles
from tiles.db import build_tile_connection


@pytest.fixture(autouse=True)
def _force_vector_path(monkeypatch):
    # This suite exercises MVT tile serving. Pin the GeoJSON cutoff to 0 so the
    # auto-selector (#178) always builds the tile pyramid; the GeoJSON path has
    # dedicated coverage in test_tile_geojson.py.
    monkeypatch.setattr("tiles.pyramid._GEOJSON_MAX_FEATURES", 0)


@pytest.fixture
def local_bucket(tmp_path, monkeypatch):
    bucket = tmp_path / "tiles-bucket"
    bucket.mkdir()
    monkeypatch.setenv("TILE_BUCKET_BASE", str(bucket))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://test.local")
    return bucket


@pytest.fixture
def app_with_tiles(local_bucket):
    """Build a minimal Starlette app that mounts the tile route with a live connection."""
    con = build_tile_connection()
    app = Starlette(routes=[
        Route("/tiles/{namespace}/{name}/{z:int}/{x:int}/{y:int}.pbf", serve_tile),
    ])
    app.state.tile_con = con
    yield app
    con.close()


@pytest.fixture
def registered_ca(app_with_tiles):
    """Register a small California tileset and return its hash."""
    con = app_with_tiles.state.tile_con
    # Generate ~few hundred r5 cells across California.
    user_sql = """
        SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, 1.0 AS val
        FROM (
            SELECT 36 + random()*3 AS lat, -121 - random()*3 AS lng
            FROM range(500)
        )
    """
    result = register_hex_tiles(
        con=con, sql=user_sql, finest_res=5, min_res=2, agg="AVG", zoom_offset=4,
    )
    return result["hash"]


class TestServeTile:
    def test_returns_mvt_content_type_for_covering_tile(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        # z=5 tile that covers part of California.
        # California is near tile (5, 5, 12) in XYZ.
        r = client.get(f"/tiles/hex/{registered_ca}/5/5/12.pbf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/vnd.mapbox-vector-tile"
        assert len(r.content) > 0

    def test_empty_tile_returns_204(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        # A tile over open ocean far from California should be empty.
        r = client.get(f"/tiles/hex/{registered_ca}/5/0/0.pbf")
        assert r.status_code in (200, 204)  # implementations may differ
        if r.status_code == 200:
            assert len(r.content) < 100  # empty MVT is very small

    def test_response_has_immutable_cache_control(self, app_with_tiles, registered_ca):
        # Tile content is deterministic per (hash, z, x, y); we tell intermediate
        # caches (browser, CDN, HAProxy) they can serve repeats without revalidation.
        client = TestClient(app_with_tiles)
        r = client.get(f"/tiles/hex/{registered_ca}/5/5/12.pbf")
        assert r.status_code in (200, 204)
        cc = r.headers.get("cache-control", "")
        assert "immutable" in cc
        assert "max-age=" in cc
        assert "public" in cc

    def test_unknown_hash_returns_404(self, app_with_tiles):
        client = TestClient(app_with_tiles)
        r = client.get("/tiles/hex/0000000000000000/5/5/12.pbf")
        assert r.status_code == 404

    def test_unknown_namespace_returns_404(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        r = client.get(f"/tiles/bogus/{registered_ca}/5/5/12.pbf")
        assert r.status_code == 404

    def test_mvt_layer_name_matches_return(self, app_with_tiles, local_bucket):
        con = app_with_tiles.state.tile_con
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, 1.0 AS val
            FROM (SELECT 36 + random()*3 AS lat, -121 - random()*3 AS lng FROM range(500))
        """
        result = register_hex_tiles(
            con=con, sql=user_sql, finest_res=5, min_res=2, agg="AVG", zoom_offset=4,
        )
        layer_name = result["layer_name"]
        client = TestClient(app_with_tiles)
        r = client.get(f"/tiles/hex/{result['hash']}/5/5/12.pbf")
        assert r.status_code == 200
        # MVT protobuf encodes the layer name as a length-prefixed string.
        expected = bytes([len(layer_name)]) + layer_name.encode()
        assert expected in r.content, f"layer name {layer_name!r} not found in MVT bytes"


class TestTileEdgeSeams:
    def test_boundary_hex_emitted_into_neighbor_tiles(self, app_with_tiles, local_bucket):
        # Seam fix (#188): a hex whose *center* lands in one tile but whose body
        # overhangs into adjacent tiles must be emitted by every tile it touches.
        # With the old center-in-tile selection the overhang went undrawn, leaving
        # a straight clip line (seam) along the tile edge.
        #
        # One res-2 cell at (36.5, -119.5): at z=7 its center is in tile (21,50)
        # but its boundary spans columns 20..22 and rows 49..50. finest=min=2
        # pins every zoom to res 2 so z=7 serves that single coarse hex.
        con = app_with_tiles.state.tile_con
        result = register_hex_tiles(
            con=con,
            sql="SELECT h3_latlng_to_cell(36.5, -119.5, 2) AS h2, 1.0 AS val",
            finest_res=2, min_res=2, agg="AVG", zoom_offset=2,
        )
        h = result["hash"]
        client = TestClient(app_with_tiles)

        def tile_len(z, x, y):
            r = client.get(f"/tiles/hex/{h}/{z}/{x}/{y}.pbf")
            assert r.status_code in (200, 204)
            return len(r.content) if r.status_code == 200 else 0

        empty = tile_len(7, 0, 0)  # far ocean: bare layer envelope, no feature
        assert tile_len(7, 21, 50) > empty, "center tile should contain the hex"
        # Each neighbor the hex overhangs into must now also draw it.
        for nx, ny in [(20, 50), (22, 50), (21, 49)]:
            assert tile_len(7, nx, ny) > empty, (
                f"neighbor tile ({nx},{ny}) missing the boundary hex — seam"
            )


class TestCoarseTileGeometryRobustness:
    def test_pole_spanning_set_does_not_500_at_coarse_zoom(self, app_with_tiles, local_bucket):
        # #197: a global, pole-spanning tileset rendered at coarse zoom used to
        # 500 — near the web-mercator latitude limit (~±85.05°) a transformed H3
        # boundary can become invalid, and ST_AsMVTGeom (aggregating the whole
        # tile) raised TopologyException, killing the *entire* tile on one bad
        # cell. ST_MakeValid per cell must keep the tile rendering.
        con = app_with_tiles.state.tile_con
        sql = """
            SELECT h3_latlng_to_cell(lat, lng, 4) AS h4, 1.0 AS val FROM (
                SELECT 80 + random()*9.5 AS lat, -180 + random()*360 AS lng FROM range(2000)
                UNION ALL SELECT -80 - random()*9.5, -180 + random()*360 FROM range(2000)
                UNION ALL SELECT random()*40, -180 + random()*360 FROM range(1000)
            )
        """
        result = register_hex_tiles(
            con=con, sql=sql, finest_res=4, min_res=2, agg="AVG", zoom_offset=2,
        )
        h = result["hash"]
        client = TestClient(app_with_tiles)
        # Coarse tiles (incl. the z=0 whole-world tile and dateline edges) must
        # render, not 500.
        for z, x, y in [(0, 0, 0), (1, 0, 0), (1, 1, 0), (2, 0, 0)]:
            r = client.get(f"/tiles/hex/{h}/{z}/{x}/{y}.pbf")
            assert r.status_code in (200, 204), (
                f"coarse tile {z}/{x}/{y} returned {r.status_code} (TopologyException regression)"
            )


class TestAntimeridian:
    def test_dateline_cell_geometry_does_not_span_globe(self):
        # A hex straddling +/-180 must be unwrapped into a continuous frame, not
        # emitted as a single ring whose vertices jump +179.9 -> -179.9 (which
        # MapLibre draws "the long way" as a globe-spanning streak). See #164.
        #
        # #201: _boundary_geom_sql now returns the geometry already in EPSG:3857.
        # The earlier version measured the *4326* span (which the unwrap fixed)
        # but ST_Transform then re-normalized the out-of-range longitudes and
        # recreated a globe-spanning polygon in mercator. So assert the span in
        # *mercator* (post-projection) is well under the world width — this is
        # the assertion that actually catches the streak regression.
        from tiles.endpoint import _boundary_geom_sql
        WORLD_M = 20037508.34 * 2  # full web-mercator world width (meters)
        con = build_tile_connection()
        try:
            con.sql(
                "CREATE TABLE src AS SELECT * FROM (VALUES "
                "(h3_latlng_to_cell(0.0,  179.95, 5)), "   # east-side crosser
                "(h3_latlng_to_cell(0.0, -179.97, 7)), "   # west-side crosser
                "(h3_latlng_to_cell(36.5, -119.5, 5))"     # normal (no crossing)
                ") t(h)"
            )
            geom = _boundary_geom_sql(touches_dateline=True)
            rows = con.sql(
                f"SELECT ST_XMax(g) - ST_XMin(g) AS span, ST_IsValid(g) AS valid "
                f"FROM (SELECT {geom} AS g FROM src)"
            ).fetchall()
            assert rows, "no geometry produced"
            for span, valid in rows:
                assert valid, "unwrapped boundary geometry must be valid"
                # A single H3 cell is tiny (<<1% of the world); the streak bug
                # made it ~the full world width in mercator.
                assert span < WORLD_M * 0.5, (
                    f"cell geometry spans the globe in mercator (x-span={span:.0f})"
                )
        finally:
            con.close()

    def test_dateline_edge_tile_renders_end_to_end(self, app_with_tiles, local_bucket):
        # Full pipeline: a tileset with cells right on +/-180 must render its
        # easternmost tile (the correlated unwrap subquery has to survive inside
        # ST_AsMVTGeom/ST_AsMVT), not error or come back empty.
        con = app_with_tiles.state.tile_con
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 4) AS h4, 1.0 AS val
            FROM (
                SELECT (random()-0.5)*2 AS lat, 179.0 + random()*1.0 AS lng
                FROM range(400)
            )
        """
        result = register_hex_tiles(
            con=con, sql=user_sql, finest_res=4, min_res=2, agg="AVG", zoom_offset=4,
        )
        client = TestClient(app_with_tiles)
        # z=2, n=4: easternmost column is x=3 (east edge = +180); equator row y=2.
        r = client.get(f"/tiles/hex/{result['hash']}/2/3/2.pbf")
        assert r.status_code == 200, f"edge tile failed: {r.status_code}"
        assert len(r.content) > 0

    def test_build_tile_sql_unwraps_only_on_dateline_tiles(self):
        # At z=3 (n=8): x=0 touches -180, x=7 touches +180, x=4 is interior.
        # The unwrap machinery (ST_MakePolygon rebuild) should appear only for
        # the edge columns so interior tiles keep the cheap direct projection.
        from tiles.endpoint import _build_tile_sql
        kw = dict(namespace="hex", name="abc", z=3, y=4, target_res=5, finest_res=5)
        interior = _build_tile_sql(x=4, **kw)
        east = _build_tile_sql(x=7, **kw)
        west = _build_tile_sql(x=0, **kw)
        assert "ST_MakePolygon" not in interior
        assert "ST_MakePolygon" in east
        assert "ST_MakePolygon" in west


class TestH0Pruning:
    def test_build_tile_sql_filters_by_h0(self):
        # Per-tile pruning depends on the SQL restricting reads to the h0
        # partitions overlapping the tile bbox. The exact predicate shape
        # is flexible (literal IN list, SEMI JOIN, or IN subquery) but the
        # query must reference h0 and use a recursive glob so hive_partitioning
        # can populate it.
        from tiles.endpoint import _build_tile_sql
        sql = _build_tile_sql(
            namespace="hex", name="abc",
            z=8, x=42, y=98,
            target_res=8, finest_res=8,
        )
        # Recursive glob (** ) so hive partition columns are discovered.
        assert "**" in sql or "hive_partitioning" in sql.lower()
        # Some restriction on h0 must be present.
        assert "h0" in sql


class TestMetadataDriven:
    def test_endpoint_reads_finest_res_from_metadata(self, app_with_tiles, local_bucket):
        """After registering with finest_res=4, a tile request should use res=4 max."""
        con = app_with_tiles.state.tile_con
        user_sql = """
            SELECT h3_latlng_to_cell(37.8, -122.3, 4) AS h4, 1.0 AS val
        """
        result = register_hex_tiles(
            con=con, sql=user_sql, finest_res=4, min_res=2, agg="AVG", zoom_offset=4,
        )
        client = TestClient(app_with_tiles)
        # At z=20 with zoom_offset=4 → target_res=16, clamped to finest_res=4.
        # Tile over (37.8, -122.3) at z=20 covers one r4 hex or so. Main assertion:
        # request succeeds (not 404 from "no pyramid at res=16").
        # Compute approximate tile (x,y) at z=20 for (37.8, -122.3):
        import math
        lat_rad = math.radians(37.8)
        z = 20
        n = 2 ** z
        x = int((-122.3 + 180) / 360 * n)
        y = int((1 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2 * n)
        r = client.get(f"/tiles/hex/{result['hash']}/{z}/{x}/{y}.pbf")
        assert r.status_code in (200, 204)  # not 404
