import os
import pytest
import duckdb
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tiles.endpoint import serve_tile
from tiles.pyramid import register_hex_tiles
from tiles.db import build_tile_connection


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
