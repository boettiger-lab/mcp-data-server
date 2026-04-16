import math
import pytest
from tiles.tile_math import tile_xyz_to_lnglat_bounds


class TestTileBounds:
    def test_z0_is_whole_world(self):
        w, s, e, n = tile_xyz_to_lnglat_bounds(0, 0, 0)
        assert w == pytest.approx(-180.0)
        assert e == pytest.approx(180.0)
        assert s == pytest.approx(-math.degrees(math.atan(math.sinh(math.pi))))
        assert n == pytest.approx(math.degrees(math.atan(math.sinh(math.pi))))

    def test_z1_nw_quadrant(self):
        # Tile (0,0) at z=1 is the northwest quadrant.
        w, s, e, n = tile_xyz_to_lnglat_bounds(1, 0, 0)
        assert w == pytest.approx(-180.0)
        assert e == pytest.approx(0.0)
        assert n > 0
        assert s == pytest.approx(0.0)

    def test_z1_se_quadrant(self):
        w, s, e, n = tile_xyz_to_lnglat_bounds(1, 1, 1)
        assert w == pytest.approx(0.0)
        assert e == pytest.approx(180.0)
        assert s < 0
        assert n == pytest.approx(0.0)

    def test_bounds_monotonic_in_x(self):
        # Adjacent tiles share an edge.
        _, _, e1, _ = tile_xyz_to_lnglat_bounds(5, 10, 12)
        w2, _, _, _ = tile_xyz_to_lnglat_bounds(5, 11, 12)
        assert e1 == pytest.approx(w2)


from tiles.tile_math import zoom_to_h3_res, content_hash


class TestZoomToRes:
    def test_default_offset_maps_z8_to_r4(self):
        assert zoom_to_h3_res(8, min_res=2, finest_res=9, zoom_offset=4) == 4

    def test_clamped_at_min_res(self):
        assert zoom_to_h3_res(0, min_res=2, finest_res=9, zoom_offset=4) == 2
        assert zoom_to_h3_res(3, min_res=2, finest_res=9, zoom_offset=4) == 2

    def test_clamped_at_finest_res(self):
        assert zoom_to_h3_res(20, min_res=2, finest_res=8, zoom_offset=4) == 8

    def test_custom_offset(self):
        assert zoom_to_h3_res(10, min_res=2, finest_res=9, zoom_offset=2) == 8


class TestContentHash:
    def test_stable_for_identical_inputs(self):
        h1 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        h2 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        assert h1 == h2

    def test_differs_when_sql_differs(self):
        h1 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        h2 = content_hash(sql="SELECT 2", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        assert h1 != h2

    def test_differs_when_agg_differs(self):
        h1 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        h2 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="SUM", zoom_offset=4)
        assert h1 != h2

    def test_length_is_16_hex_chars(self):
        h = content_hash(sql="x", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)
