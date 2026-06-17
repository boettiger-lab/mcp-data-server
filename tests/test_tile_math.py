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


from tiles.tile_math import zoom_to_h3_res, content_hash, h3_edge_padding_deg


class TestEdgePadding:
    def test_positive_and_finite(self):
        pad_lat, pad_lng = h3_edge_padding_deg(5, 36.0)
        assert pad_lat > 0 and pad_lng > 0
        assert math.isfinite(pad_lat) and math.isfinite(pad_lng)

    def test_coarser_res_pads_more(self):
        # res 2 hexes are far larger than res 8, so they need more padding.
        coarse, _ = h3_edge_padding_deg(2, 0.0)
        fine, _ = h3_edge_padding_deg(8, 0.0)
        assert coarse > fine

    def test_lng_padding_grows_toward_poles(self):
        # A degree of longitude shrinks with latitude, so pad_lng must widen.
        _, lng_equator = h3_edge_padding_deg(5, 0.0)
        _, lng_high = h3_edge_padding_deg(5, 60.0)
        assert lng_high > lng_equator
        # At the equator pad_lng ≈ pad_lat (cos 0 = 1).
        lat0, lng0 = h3_edge_padding_deg(5, 0.0)
        assert lng0 == pytest.approx(lat0, rel=1e-6)

    def test_finite_near_pole(self):
        # cos(lat) is floored so pad_lng never blows up to infinity at lat 90.
        pad_lat, pad_lng = h3_edge_padding_deg(5, 90.0)
        assert math.isfinite(pad_lng)
        assert pad_lng > pad_lat

    def test_res_out_of_range_is_clamped(self):
        # Defensive: an out-of-table res must not IndexError.
        assert h3_edge_padding_deg(99, 0.0) == h3_edge_padding_deg(15, 0.0)
        assert h3_edge_padding_deg(-5, 0.0) == h3_edge_padding_deg(0, 0.0)


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

    def test_default_offset_is_two_maps_z_to_z_minus_2(self):
        # #178: the default must map map-zoom z -> H3 res z-2 (not the old z+1),
        # keeping each tile to ~100-2000 hexes instead of 40k-130k. Pin it so a
        # regression to the over-fine mapping is caught here. (Legacy path: no
        # bounds/count metadata, so the linear mapping applies.)
        for z in range(4, 11):
            assert zoom_to_h3_res(z, min_res=2, finest_res=12) == z - 2


from tiles.tile_math import tiles_covering_bounds


# Real-shaped fixtures for the adaptive mapping (#188).
_CA = dict(bounds=[-124.4, 32.5, -114.1, 42.0], feature_count_finest=539258, finest_res=8)
_GLOBAL = dict(bounds=[-180.0, -60.0, 180.0, 75.0], feature_count_finest=14_000_000, finest_res=6)


class TestTilesCoveringBounds:
    def test_world_at_z0_is_one_tile(self):
        assert tiles_covering_bounds(0, [-180, -85, 180, 85]) == 1

    def test_grows_with_zoom(self):
        prev = 0
        for z in range(0, 8):
            n = tiles_covering_bounds(z, _CA["bounds"])
            assert n >= prev  # more tiles cover the same bbox as zoom increases
            prev = n

    def test_global_covers_more_than_bounded(self):
        for z in range(3, 8):
            assert tiles_covering_bounds(z, _GLOBAL["bounds"]) > tiles_covering_bounds(z, _CA["bounds"])

    def test_never_below_one(self):
        # A degenerate point bbox still spans (at least) the one tile it sits in.
        assert tiles_covering_bounds(10, [-122.3, 37.8, -122.3, 37.8]) == 1


class TestAdaptiveZoomToRes:
    def test_bounded_data_renders_finer_than_global_at_mid_zoom(self):
        # The whole point of the adaptive mapping (#188): at the same zoom,
        # bounded data (covers few tiles) can afford finer hexes than global
        # data (which would blow the per-tile budget at that res).
        for z in range(4, 9):
            rc = zoom_to_h3_res(z, 2, **_CA)
            rg = zoom_to_h3_res(z, 2, **_GLOBAL)
            assert rc >= rg
        # And strictly finer somewhere in that band, not merely equal throughout.
        assert any(zoom_to_h3_res(z, 2, **_CA) > zoom_to_h3_res(z, 2, **_GLOBAL)
                   for z in range(4, 9))

    def test_ca_mid_zoom_is_no_longer_too_coarse(self):
        # The reported bug: CA at z=5 rendered res 3 (state-sized blobs). The
        # adaptive mapping should lift mid-zoom CA into the res 5-6 range.
        assert zoom_to_h3_res(5, 2, **_CA) >= 5

    def test_non_decreasing_in_zoom(self):
        prev = 0
        for z in range(0, 14):
            r = zoom_to_h3_res(z, 2, **_CA)
            assert r >= prev
            prev = r

    def test_clamped_to_finest_and_min(self):
        assert zoom_to_h3_res(22, 2, **_CA) == _CA["finest_res"]  # deep zoom
        assert zoom_to_h3_res(0, 2, **_CA) >= 2                    # never below min_res

    def test_per_tile_budget_is_respected(self):
        # The chosen res must keep estimated cells/tile within ~one H3 step of
        # the 4000 budget (discreteness means it can't hit it exactly; one res
        # finer is a 7x jump).
        for z in range(3, 12):
            r = zoom_to_h3_res(z, 2, **_CA, target_cells_per_tile=4000)
            if r in (2, _CA["finest_res"]):
                continue  # clamped — budget not the binding constraint
            cpt = _CA["feature_count_finest"] * 7 ** (r - _CA["finest_res"]) \
                / tiles_covering_bounds(z, _CA["bounds"])
            assert cpt <= 4000 * 7

    def test_smaller_zoom_offset_biases_finer(self):
        # zoom_offset keeps its legacy direction as an adaptive bias knob:
        # smaller offset -> finer res.
        finer = zoom_to_h3_res(6, 2, zoom_offset=0, **_CA)
        base = zoom_to_h3_res(6, 2, zoom_offset=2, **_CA)
        coarser = zoom_to_h3_res(6, 2, zoom_offset=4, **_CA)
        assert finer >= base >= coarser
        assert finer > coarser

    def test_falls_back_to_linear_without_metadata(self):
        # Pre-#178 metadata lacks bounds/feature_count -> legacy linear mapping.
        assert zoom_to_h3_res(8, 2, finest_res=12, zoom_offset=2) == 6
        assert zoom_to_h3_res(8, 2, finest_res=12, zoom_offset=2,
                              feature_count_finest=0, bounds=None) == 6

    def test_lower_target_picks_coarser_res(self):
        # A tighter per-tile budget should never pick a finer res.
        tight = zoom_to_h3_res(7, 2, **_CA, target_cells_per_tile=500)
        loose = zoom_to_h3_res(7, 2, **_CA, target_cells_per_tile=8000)
        assert tight <= loose


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

    def test_h0_partition_layout_does_not_collide_with_pre_h0_hashes(self):
        # The pyramid layout changed from PARTITION_BY (res) to (res, h0) — files
        # written under the new layout must NOT be served via the same content
        # hash a pre-h0 registration would have produced. Pin the old hash here
        # so any future hash-input change is a deliberate decision.
        pre_h0_hash = "b5f7ba93d53a0f19"  # produced by the pre-h0-partition code
        new_hash = content_hash(
            sql="SELECT 1 AS h, 2 AS v",
            finest_res=8, min_res=2, agg="AVG", zoom_offset=-1,
        )
        assert new_hash != pre_h0_hash

    def test_two_phase_layout_does_not_collide_with_v2_h0_hashes(self):
        # v3-iterative layout (PR for two-phase pyramid) writes a different
        # on-disk pyramid for the same user inputs. Bump the layout version
        # so the new hash never overlaps a v2-h0 pyramid on S3.
        v2_h0_hash = "847ee176a3fd805e"  # v2-h0 layout hash
        new_hash = content_hash(
            sql="SELECT 1 AS h, 2 AS v",
            finest_res=8, min_res=2, agg="AVG", zoom_offset=-1,
        )
        assert new_hash != v2_h0_hash
