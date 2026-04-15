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
