"""Coverage for the GeoJSON fast path and the auto-selector (#178).

register_hex_tiles serves small/bounded hex sets as a single GeoJSON
FeatureCollection (streamed to object storage, fetched client-side) instead of
building an MVT tile pyramid. Larger sets fall back to the pyramid. The return
carries a paste-ready MapLibre `source`/`layer` either way.
"""
import json

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tiles.endpoint import serve_tile
from tiles.pyramid import register_hex_tiles, render_recipe
from tiles.db import build_tile_connection

# A small grid of res-8 cells around Berkeley — well under the default cutoff.
SMALL_SQL = (
    "SELECT h3_latlng_to_cell(37.87 + (i * 0.01), -122.27 + (j * 0.01), 8) AS h8 "
    "FROM range(5) t1(i), range(5) t2(j)"
)


@pytest.fixture
def con():
    c = build_tile_connection()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def local_bucket(tmp_path, monkeypatch):
    bucket = tmp_path / "tiles-bucket"
    bucket.mkdir()
    monkeypatch.setenv("TILE_BUCKET_BASE", str(bucket))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://test.local")
    return bucket


class TestAutoSelect:
    def test_default_cutoff_is_30k(self):
        # The GeoJSON/MVT routing cutoff is a measured decision (#190 step 2):
        # single-res GeoJSON is client-render-bound and only looks right at its
        # native zoom, so bounded sets above ~30k cells go to the MVT pyramid.
        # Guard the value so a change is deliberate, not accidental.
        from tiles import pyramid
        assert pyramid._GEOJSON_MAX_FEATURES == 30000

    def test_small_set_selects_geojson(self, con, local_bucket):
        result = register_hex_tiles(con=con, sql=SMALL_SQL, agg="COUNT")
        assert result["format"] == "geojson"
        assert result["source"]["type"] == "geojson"
        # GeoJSON layers carry no source-layer (that's a vector-tile concept).
        assert "source-layer" not in result["layer"]
        assert result["source"]["data"] == result["geojson_url"]
        assert result["feature_count_finest"] == 24

    def test_large_set_falls_back_to_vector(self, con, local_bucket, monkeypatch):
        # Force the pyramid path by setting the cutoff below the feature count.
        monkeypatch.setattr("tiles.pyramid._GEOJSON_MAX_FEATURES", 0)
        result = register_hex_tiles(con=con, sql=SMALL_SQL, agg="COUNT")
        assert result["format"] == "vector"
        assert result["source"]["type"] == "vector"
        assert result["source"]["tiles"] == [result["tile_url_template"]]
        assert result["layer"]["source-layer"] == result["layer_name"]
        assert result["geojson_url"] is None

    def test_geojson_skips_parent_pyramid_levels(self, con, local_bucket):
        result = register_hex_tiles(con=con, sql=SMALL_SQL, agg="COUNT")
        h = result["hash"]
        tileset = local_bucket / "hex" / h
        # Only the finest level is materialized; parent res dirs are not built.
        res_dirs = sorted(p.name for p in tileset.iterdir() if p.name.startswith("res="))
        assert res_dirs == ["res=8"]
        assert (tileset / "data.geojson").exists()


class TestGeoJSONOutput:
    def test_data_geojson_is_valid_feature_collection(self, con, local_bucket):
        result = register_hex_tiles(con=con, sql=SMALL_SQL, agg="COUNT")
        path = local_bucket / "hex" / result["hash"] / "data.geojson"
        gj = json.loads(path.read_text())
        assert gj["type"] == "FeatureCollection"
        assert len(gj["features"]) == 24
        f0 = gj["features"][0]
        assert f0["type"] == "Feature"
        assert f0["geometry"]["type"] == "Polygon"
        # H3 cell boundary is a closed 7-point ring (6 vertices + repeat).
        assert len(f0["geometry"]["coordinates"][0]) == 7
        assert "count" in f0["properties"]

    def test_globe_spanning_pole_cell_is_dropped(self, con, local_bucket):
        """A South-Pole (lat=-90) junk cell — the kind GBIF coordinate noise
        produces — has a boundary that wraps every longitude and renders in
        MapLibre as a smear across the whole map. The GeoJSON builder must drop
        such globe-spanning cells while keeping the legitimate ones (#196)."""
        # 24 distinct Berkeley cells + one South-Pole cell = 25 input cells.
        sql = (
            "SELECT h3_latlng_to_cell(37.87 + (i * 0.01), -122.27 + (j * 0.01), 8) AS h8 "
            "FROM range(5) t1(i), range(5) t2(j) "
            "UNION ALL SELECT h3_latlng_to_cell(-90.0, 0.0, 8) AS h8"
        )
        result = register_hex_tiles(con=con, sql=sql, agg="COUNT")
        assert result["format"] == "geojson"
        # The metadata count reflects the input (the pole cell is real data)...
        assert result["feature_count_finest"] == 25
        path = local_bucket / "hex" / result["hash"] / "data.geojson"
        gj = json.loads(path.read_text())
        # ...but the rendered FeatureCollection has the smear-causing cell dropped.
        assert len(gj["features"]) == 24
        for feat in gj["features"]:
            lngs = [pt[0] for ring in feat["geometry"]["coordinates"] for pt in ring]
            assert max(lngs) - min(lngs) <= 180  # no globe-spanning ring survives

    def test_value_column_lands_in_properties(self, con, local_bucket):
        sql = (
            "SELECT h3_latlng_to_cell(37.87 + (i * 0.01), -122.27, 8) AS h8, "
            "       (i + 1) * 1.5 AS score "
            "FROM range(4) t1(i)"
        )
        result = register_hex_tiles(con=con, sql=sql, agg="AVG")
        assert result["format"] == "geojson"
        assert result["value_columns"] == ["score"]
        path = local_bucket / "hex" / result["hash"] / "data.geojson"
        gj = json.loads(path.read_text())
        for feat in gj["features"]:
            assert "score" in feat["properties"]
        # The default ramp interpolates on the value column.
        assert result["layer"]["paint"]["fill-color"][2] == ["get", "score"]


class TestRenderRecipe:
    def test_legacy_metadata_without_format_defaults_to_vector(self):
        meta = {
            "format": None,  # pre-#178 metadata has no format
            "value_columns": ["count"],
            "finest_res": 8,
            "value_stats": {"count": {"by_res": {"8": {"min": 1, "max": 10}}}},
            "layer_name": "layer",
        }
        recipe = render_recipe(meta, "http://x/tiles/hex/abc/{z}/{x}/{y}.pbf")
        assert recipe["format"] == "vector"
        assert recipe["source"]["type"] == "vector"

    def test_degenerate_domain_keeps_distinct_stops(self):
        # min == max would make MapLibre's interpolate stops collide.
        meta = {
            "format": "geojson",
            "geojson_url": "http://x/data.geojson",
            "value_columns": ["count"],
            "finest_res": 8,
            "value_stats": {"count": {"by_res": {"8": {"min": 5, "max": 5}}}},
        }
        ramp = render_recipe(meta, "")["layer"]["paint"]["fill-color"]
        vmin, vmax = ramp[3], ramp[5]
        assert vmin != vmax


class TestGzipUpload:
    """The S3 path pre-gzips the FeatureCollection and PUTs it with a
    Content-Encoding header so the public gateway serves it compressed (#190)."""

    def test_put_gzip_sets_content_encoding_and_roundtrips(self, monkeypatch):
        import gzip

        from tiles import pyramid

        captured = {}

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, *a, **k):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["body"] = req.data
            return _FakeResp()

        monkeypatch.setattr(pyramid.urllib.request, "urlopen", fake_urlopen)

        raw = b'{"type":"FeatureCollection","features":[]}'
        gz_len = pyramid._put_gzip("https://gw/public-output/hex/x/data.geojson", raw)

        assert captured["url"] == "https://gw/public-output/hex/x/data.geojson"
        # Single clean token — NOT "gzip,aws-chunked", which browsers won't inflate.
        assert captured["headers"]["content-encoding"] == "gzip"
        assert captured["headers"]["content-type"] == "application/json"
        assert gzip.decompress(captured["body"]) == raw
        assert int(captured["headers"]["content-length"]) == len(captured["body"]) == gz_len

    def test_put_gzip_raises_on_error_status(self, monkeypatch):
        from tiles import pyramid

        class _FakeResp:
            status = 403

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            pyramid.urllib.request, "urlopen", lambda req, *a, **k: _FakeResp()
        )
        with pytest.raises(RuntimeError, match="HTTP 403"):
            pyramid._put_gzip("https://gw/x", b"{}")


class TestEndpointRejectsGeoJSON:
    def test_tile_request_for_geojson_tileset_returns_404(self, con, local_bucket):
        result = register_hex_tiles(con=con, sql=SMALL_SQL, agg="COUNT")
        assert result["format"] == "geojson"

        app = Starlette(routes=[
            Route("/tiles/{namespace}/{name}/{z:int}/{x:int}/{y:int}.pbf", serve_tile),
        ])
        app.state.tile_con = con
        client = TestClient(app)
        # A tile for a GeoJSON tileset is a client error — there are no MVTs.
        r = client.get(f"/tiles/hex/{result['hash']}/8/41/98.pbf")
        assert r.status_code == 404
