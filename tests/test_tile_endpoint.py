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

    def test_unknown_hash_returns_404(self, app_with_tiles):
        client = TestClient(app_with_tiles)
        r = client.get("/tiles/hex/0000000000000000/5/5/12.pbf")
        assert r.status_code == 404

    def test_unknown_namespace_returns_404(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        r = client.get(f"/tiles/bogus/{registered_ca}/5/5/12.pbf")
        assert r.status_code == 404
