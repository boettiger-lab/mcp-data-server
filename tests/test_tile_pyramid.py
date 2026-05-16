import pytest
from tiles.pyramid import build_pyramid_statements


class TestBuildPyramidStatements:
    def test_returns_list_with_one_statement_per_resolution(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # finest_res=8, min_res=2 → res=8,7,6,5,4,3,2 = 7 statements.
        assert isinstance(stmts, list)
        assert len(stmts) == 7

    def test_first_statement_is_phase_1_finest_from_source(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        s = stmts[0]
        assert "FROM src" in s
        assert "8 AS res" in s
        assert "GROUP BY 1, 2" in s
        assert "PARTITION_BY (res, h0)" in s
        assert "OVERWRITE_OR_IGNORE" in s
        # h0 is computed from the h3 column in the src CTE once.
        assert 'h3_cell_to_parent("h8", 0)' in s

    def test_phase_2_reads_from_previous_resolution(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Index 1 is res=7, reading res=8.
        s = stmts[1]
        assert "res=8/**/*.parquet" in s
        assert "7 AS res" in s
        assert "h3_cell_to_parent(h, 7)" in s
        assert "hive_partitioning=true" in s
        # Phase 2 must not re-scan the source.
        assert "FROM src" not in s

    def test_phase_2_iterates_down_to_min_res(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # res=7 reads res=8, res=6 reads res=7, ..., res=2 reads res=3.
        for i, parent_res in enumerate(range(7, 1, -1), start=1):
            s = stmts[i]
            assert f"res={parent_res + 1}/**/*.parquet" in s
            assert f"{parent_res} AS res" in s

    def test_min_res_equals_finest_res_single_statement(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=5, agg="AVG",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert len(stmts) == 1
        # Only Phase 1, no Phase 2.
        assert "FROM src" in stmts[0]
        assert "5 AS res" in stmts[0]

    def test_count_mode_phase_1_uses_count_star(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5 FROM src",
            finest_res=5, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert "COUNT(*) AS count" in stmts[0]

    def test_count_mode_phase_2_rolls_up_with_sum(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5 FROM src",
            finest_res=5, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        # COUNT at parent = SUM(child count).
        for s in stmts[1:]:
            assert "SUM(count) AS count" in s

    def test_sum_mode_uses_sum_at_every_level(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        # Finest: SUM at Phase 1 to collapse duplicate source rows into the cell.
        assert 'SUM("val") AS "val"' in stmts[0]
        # Parents: SUM rolls up.
        for s in stmts[1:]:
            assert 'SUM("val") AS "val"' in s

    def test_sum_mode_with_multiple_value_columns(self):
        # Verify the join across multiple value columns produces well-formed SQL
        # for SUM mode (analogous to the existing AVG multi-column test).
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, v1, v2 FROM src",
            finest_res=5, min_res=2, agg="SUM",
            value_columns=["v1", "v2"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert 'SUM("v1") AS "v1"' in stmts[0]
        assert 'SUM("v2") AS "v2"' in stmts[0]
        for s in stmts[1:]:
            assert 'SUM("v1") AS "v1"' in s
            assert 'SUM("v2") AS "v2"' in s

    def test_min_max_modes_use_min_max_at_every_level(self):
        for agg in ("MIN", "MAX"):
            stmts = build_pyramid_statements(
                user_sql="SELECT h5, val FROM src",
                finest_res=5, min_res=2, agg=agg,
                value_columns=["val"], h3_column="h5",
                output_uri="s3://public-output/hex/abc/",
            )
            assert f'{agg}("val") AS "val"' in stmts[0]
            for s in stmts[1:]:
                assert f'{agg}("val") AS "val"' in s

    def test_avg_mode_phase_1_includes_weight_alongside_avg(self):
        # AVG mode needs COUNT(*) at finest so parent rollups can compute
        # weighted averages — `AVG of AVGs` is wrong when child cardinalities differ.
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert 'AVG("val") AS "val"' in stmts[0]
        assert "COUNT(*) AS __pyramid_weight" in stmts[0]

    def test_avg_mode_phase_2_uses_weighted_average(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        for s in stmts[1:]:
            assert 'SUM("val" * __pyramid_weight) / SUM(__pyramid_weight) AS "val"' in s
            # weight must propagate so further rollups can keep weighting correctly.
            assert "SUM(__pyramid_weight) AS __pyramid_weight" in s

    def test_avg_mode_with_multiple_value_columns(self):
        # Single shared `__pyramid_weight` for all value columns at every level.
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, v1, v2 FROM src",
            finest_res=5, min_res=2, agg="AVG",
            value_columns=["v1", "v2"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert 'AVG("v1") AS "v1"' in stmts[0]
        assert 'AVG("v2") AS "v2"' in stmts[0]
        assert "COUNT(*) AS __pyramid_weight" in stmts[0]
        # Parent rollup weights each value column by the same weight.
        assert 'SUM("v1" * __pyramid_weight) / SUM(__pyramid_weight) AS "v1"' in stmts[1]
        assert 'SUM("v2" * __pyramid_weight) / SUM(__pyramid_weight) AS "v2"' in stmts[1]
        assert "SUM(__pyramid_weight) AS __pyramid_weight" in stmts[1]

    def test_invalid_agg_raises(self):
        import pytest
        with pytest.raises(ValueError, match="agg must be one of"):
            build_pyramid_statements(
                user_sql="SELECT h5, val FROM src",
                finest_res=5, min_res=2, agg="MEDIAN",
                value_columns=["val"], h3_column="h5",
                output_uri="s3://public-output/hex/abc/",
            )

    def test_non_count_requires_value_columns(self):
        # Empty value_columns list is only valid for COUNT (which uses
        # a synthetic ["count"]). Other aggs must reject it.
        import pytest
        with pytest.raises(ValueError, match="value column"):
            build_pyramid_statements(
                user_sql="SELECT h5 FROM src",
                finest_res=5, min_res=2, agg="SUM",
                value_columns=[], h3_column="h5",
                output_uri="s3://public-output/hex/abc/",
            )


import os
import duckdb
from pathlib import Path
from tiles.pyramid import register_hex_tiles, _inspect_user_sql


@pytest.fixture
def local_bucket(tmp_path, monkeypatch):
    """Point TILE_BUCKET_BASE at a local directory so no S3 is required."""
    bucket = tmp_path / "tiles-bucket"
    bucket.mkdir()
    monkeypatch.setenv("TILE_BUCKET_BASE", str(bucket))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://test.local")
    return bucket


@pytest.fixture
def h3_conn():
    con = duckdb.connect(":memory:")
    con.sql("INSTALL h3 FROM community; LOAD h3")
    con.sql("INSTALL spatial; LOAD spatial")
    con.sql("INSTALL httpfs; LOAD httpfs")
    yield con
    con.close()


class TestInspectUserSQL:
    def test_allows_single_column_sql(self, h3_conn):
        # SQL returning only the h3 index — valid input for agg="COUNT".
        h3_col, value_cols = _inspect_user_sql(
            h3_conn, "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5"
        )
        assert h3_col == "h5"
        assert value_cols == []

    def test_still_extracts_value_cols_when_present(self, h3_conn):
        h3_col, value_cols = _inspect_user_sql(
            h3_conn,
            "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS v1, 2.0 AS v2",
        )
        assert h3_col == "h5"
        assert value_cols == ["v1", "v2"]


class TestRegisterHexTiles:
    def test_writes_h0_subpartitions(self, local_bucket, h3_conn):
        # h0 partitioning enables file-level pruning when tile requests filter
        # by candidate h0 cells. Verify both that the on-disk layout includes
        # h0=... subdirectories and that h0 is recoverable when reading back
        # with hive_partitioning=true (matches what the tile endpoint does).
        # Use lat/lng spans that span multiple h0 cells so the partition split
        # is observable.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 4) AS h4, val
            FROM (VALUES (37.8, -122.3, 1.0),
                         (0.0, 0.0, 2.0),
                         (-30.0, 140.0, 3.0)) t(lat, lng, val)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=4, min_res=2,
            agg="AVG", zoom_offset=-1,
        )
        # At least 2 h0 partitions under each res= directory.
        for res in (2, 3, 4):
            res_dir = local_bucket / "hex" / result["hash"] / f"res={res}"
            h0_subdirs = [p for p in res_dir.iterdir() if p.is_dir() and p.name.startswith("h0=")]
            assert len(h0_subdirs) >= 2, f"res={res} expected ≥2 h0 partitions, got {[p.name for p in h0_subdirs]}"
        # h0 column comes back via hive_partitioning and equals h3_cell_to_parent(h, 0).
        uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        cols = h3_conn.sql(
            f"SELECT * FROM read_parquet('{uri}', hive_partitioning=true) LIMIT 1"
        ).columns
        assert "h0" in cols
        mismatches = h3_conn.sql(
            f"SELECT COUNT(*) FROM read_parquet('{uri}', hive_partitioning=true) "
            "WHERE h0::BIGINT != h3_cell_to_parent(h, 0)::BIGINT"
        ).fetchone()[0]
        assert mismatches == 0

    def test_writes_pyramid_partitions(self, local_bucket, h3_conn):
        # Source: 5 cells at r5 around a point.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES (37.8, -122.3, 1.0),
                         (37.9, -122.4, 2.0),
                         (38.0, -122.5, 3.0),
                         (38.1, -122.6, 4.0),
                         (38.2, -122.7, 5.0)) t(lat, lng, val)
        """
        result = register_hex_tiles(
            con=h3_conn,
            sql=user_sql,
            finest_res=5,
            min_res=2,
            agg="AVG",
            zoom_offset=4,
        )
        assert "hash" in result
        # Partitioned files written: res=2 through res=5.
        for res in (2, 3, 4, 5):
            partition = local_bucket / "hex" / result["hash"] / f"res={res}"
            assert partition.exists(), f"Missing res={res}"
            assert any(partition.iterdir()), f"Empty res={res}"

    def test_auto_detects_finest_res_from_h3_column(self, local_bucket, h3_conn):
        # No finest_res passed — should be detected via h3_get_resolution
        # on the H3 column. h3_latlng_to_cell at res 6 → finest_res=6.
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 6) AS h6, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, min_res=3, agg="AVG", zoom_offset=-1,
        )
        assert result["finest_res"] == 6

    def test_metadata_persists_bounds_and_feature_count(self, local_bucket, h3_conn):
        # bounds and feature_count_finest must be inside metadata.json so the
        # next call with identical inputs can short-circuit.
        import json
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES (37.8, -122.3, 1.0), (38.0, -122.5, 2.0)) t(lat, lng, val)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=3, agg="AVG", zoom_offset=-1,
        )
        meta_path = local_bucket / "hex" / result["hash"] / "metadata.json"
        with open(meta_path) as f:
            meta = json.loads(f.read().strip())
        assert "bounds" in meta and len(meta["bounds"]) == 4
        assert meta["feature_count_finest"] == 2

    def test_cache_hit_short_circuits_repeat_registration(self, local_bucket, h3_conn):
        # Second registration with identical inputs reads metadata.json and
        # returns cache_hit=True without re-running the build.
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        first = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=2, agg="AVG", zoom_offset=-1,
        )
        assert first.get("cache_hit") is not True
        second = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=2, agg="AVG", zoom_offset=-1,
        )
        assert second.get("cache_hit") is True
        # Identical observable fields across both calls.
        for key in ("hash", "bounds", "finest_res", "min_res", "value_columns",
                    "value_stats", "layer_name", "feature_count_finest"):
            assert second[key] == first[key], f"{key} differs across cache hit"

    def test_auto_detect_raises_on_empty_sql(self, local_bucket, h3_conn):
        # SQL filters to zero rows — auto-detect can't sample a cell.
        user_sql = (
            "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val "
            "WHERE 1 = 0"
        )
        with pytest.raises(ValueError, match="auto-detect"):
            register_hex_tiles(
                con=h3_conn, sql=user_sql, min_res=2, agg="COUNT",
            )

    def test_tile_url_template_uses_public_base(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        assert result["tile_url_template"].startswith("http://test.local/tiles/hex/")
        assert result["tile_url_template"].endswith("/{z}/{x}/{y}.pbf")

    def test_returns_value_columns(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS v1, 2.0 AS v2"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        assert result["value_columns"] == ["v1", "v2"]

    def test_returns_layer_name(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        assert result["layer_name"] == "layer"

    def test_identical_inputs_same_hash(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        r1 = register_hex_tiles(con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4)
        r2 = register_hex_tiles(con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4)
        assert r1["hash"] == r2["hash"]

    def test_count_agg_with_index_only_sql(self, local_bucket, h3_conn):
        # Three rows → two map to the same res=5 cell, one to another.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5
            FROM (VALUES (37.8, -122.3),
                         (37.80001, -122.30001),
                         (40.0, -100.0)) t(lat, lng)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=2, agg="COUNT", zoom_offset=4,
        )
        assert result["value_columns"] == ["count"]
        # Verify the parquet pyramid actually has the count column. Files now
        # live under res=N/h0=X/ — use a recursive glob.
        import duckdb as _d
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "**" / "*.parquet")
            cols = _d.connect(":memory:").sql(f"SELECT * FROM read_parquet('{uri}') LIMIT 0").columns
            assert "count" in cols, f"res={res} missing 'count' column: {cols}"

    def test_count_agg_ignores_user_value_columns(self, local_bucket, h3_conn):
        # User supplies an extra column; COUNT mode drops it.
        user_sql = """
            SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 'ignored' AS extra
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=5, agg="COUNT", zoom_offset=4,
        )
        assert result["value_columns"] == ["count"]

    def test_non_count_still_requires_value_columns(self, local_bucket, h3_conn):
        with pytest.raises(ValueError, match="value column"):
            register_hex_tiles(
                con=h3_conn,
                sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5",
                finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
            )

    def test_value_stats_returned_per_resolution(self, local_bucket, h3_conn):
        # 5 rows, 3 distinct res=5 cells. At finest res the pyramid stores
        # `1 AS count` per input row (ungrouped), so naive MIN/MAX gives
        # {1, 1}. At coarser parent resolutions rows collapse into fewer
        # cells and COUNT(*) produces max counts > 1.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5
            FROM (VALUES (37.80, -122.30),
                         (37.80001, -122.30001),
                         (37.80002, -122.30002),
                         (38.00, -122.50),
                         (40.00, -100.00)) t(lat, lng)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=3, agg="COUNT", zoom_offset=4,
        )
        stats = result["value_stats"]
        assert set(stats.keys()) == {"count"}
        by_res = stats["count"]["by_res"]
        # Resolutions 3, 4, 5 all present (string keys).
        assert set(by_res.keys()) == {"3", "4", "5"}
        # Finest-res parquet aggregates via COUNT(*); the three clustered
        # points share the same h5 cell, so max is 3 there too.
        assert by_res["5"]["min"] == 1
        assert by_res["5"]["max"] == 3
        # Coarser resolutions aggregate further; the same cluster at res=4.
        assert by_res["4"]["max"] >= 3
        # Coarser levels can only aggregate further — max is non-decreasing.
        assert by_res["3"]["max"] >= by_res["4"]["max"]

    def test_value_stats_for_non_count_agg(self, local_bucket, h3_conn):
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES (37.8, -122.3, 1.0),
                         (38.0, -122.5, 5.0)) t(lat, lng, val)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        stats = result["value_stats"]
        assert set(stats.keys()) == {"val"}
        assert stats["val"]["by_res"]["5"]["min"] == 1.0
        assert stats["val"]["by_res"]["5"]["max"] == 5.0


import json as _json


class TestTilesetMetadata:
    def test_metadata_written_alongside_pyramid(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=3, agg="SUM", zoom_offset=3,
        )
        meta_path = local_bucket / "hex" / result["hash"] / "metadata.json"
        assert meta_path.exists()
        meta = _json.loads(meta_path.read_text())
        assert meta["finest_res"] == 5
        assert meta["min_res"] == 3
        assert meta["agg"] == "SUM"
        assert meta["zoom_offset"] == 3
        assert meta["value_columns"] == ["val"]

    def test_metadata_includes_value_stats(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=3, agg="COUNT", zoom_offset=4,
        )
        meta_path = local_bucket / "hex" / result["hash"] / "metadata.json"
        meta = _json.loads(meta_path.read_text())
        assert "value_stats" in meta
        assert set(meta["value_stats"]["count"]["by_res"].keys()) == {"3", "4", "5"}
        # Sanity: persisted stats equal the returned stats.
        assert meta["value_stats"] == result["value_stats"]

    def test_metadata_includes_layer_name(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        meta_path = local_bucket / "hex" / result["hash"] / "metadata.json"
        meta = _json.loads(meta_path.read_text())
        assert meta["layer_name"] == result["layer_name"]


from tiles.db import build_tile_connection


class TestBuildTileConnection:
    def test_loads_required_extensions(self):
        con = build_tile_connection()
        try:
            # Proxy: LOAD must have succeeded if these functions are callable.
            con.sql("SELECT h3_latlng_to_cell(37.8, -122.3, 5)").fetchone()
            con.sql("SELECT ST_Point(0, 0)").fetchone()
            # httpfs loaded — confirm by presence of its config.
            con.sql("SELECT current_setting('s3_region')").fetchone()
        finally:
            con.close()

    def test_no_s3_secret_created(self):
        con = build_tile_connection()
        try:
            names = [r[0] for r in con.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            # Persistent tile connection reads only from public buckets.
            assert "client_s3" not in names
        finally:
            con.close()


class TestPyramidMathCorrectness:
    """Assert mathematical invariants on built pyramids — independent of
    SQL shape. Each test registers a small multi-h0 synthetic dataset and
    checks that parent values are correct rollups of child values.
    """

    def _seed_sql(self, points):
        """Build a user_sql VALUES clause from a list of (lat, lng, val) tuples."""
        rows = ", ".join(f"({lat}, {lng}, {val})" for lat, lng, val in points)
        return f"""
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES {rows}) t(lat, lng, val)
        """

    def test_count_total_at_parent_equals_total_at_finest(self, local_bucket, h3_conn):
        # COUNT mode: SUM of all count values at any res equals the total
        # number of source rows (the dataset's true count).
        points = [
            (37.8, -122.3, 1.0), (37.80001, -122.30001, 2.0),  # same cluster
            (38.0, -122.5, 3.0),
            (40.0, -100.0, 4.0),
            (-30.0, 140.0, 5.0),  # different h0
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=2, agg="COUNT", zoom_offset=-1,
        )
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "**" / "*.parquet")
            total = h3_conn.sql(
                f"SELECT SUM(count) FROM read_parquet('{uri}', hive_partitioning=true)"
            ).fetchone()[0]
            assert total == len(points), f"res={res}: SUM(count)={total}, expected {len(points)}"

    def test_sum_total_at_parent_equals_total_at_finest(self, local_bucket, h3_conn):
        # SUM mode: conservation across resolutions.
        points = [
            (37.8, -122.3, 10.0), (37.80001, -122.30001, 5.0),
            (38.0, -122.5, 7.0),
            (40.0, -100.0, 3.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=2, agg="SUM", zoom_offset=-1,
        )
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "**" / "*.parquet")
            total = h3_conn.sql(
                f'SELECT SUM("val") FROM read_parquet(\'{uri}\', hive_partitioning=true)'
            ).fetchone()[0]
            assert total == 25.0, f"res={res}: SUM(val)={total}, expected 25.0"

    def test_min_at_parent_is_min_of_children(self, local_bucket, h3_conn):
        # MIN mode: per-cell parent.MIN equals MIN over its res+1 children.
        points = [
            (37.8, -122.3, 10.0), (37.80001, -122.30001, 2.0),
            (37.80002, -122.30002, 7.0),
            (40.0, -100.0, 3.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=3, agg="MIN", zoom_offset=-1,
        )
        # For each (h@res=4) cell, the parent MIN should equal MIN of children.
        child_uri = str(local_bucket / "hex" / result["hash"] / "res=5" / "**" / "*.parquet")
        parent_uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        children_min_by_parent = h3_conn.sql(
            f"""
            SELECT h3_cell_to_parent(h, 4) AS ph, MIN("val") AS min_v
            FROM read_parquet('{child_uri}', hive_partitioning=true)
            GROUP BY 1
            """
        ).fetchdf()
        parents = h3_conn.sql(
            f'SELECT h AS ph, "val" AS min_v FROM read_parquet(\'{parent_uri}\', hive_partitioning=true)'
        ).fetchdf()
        merged = parents.merge(children_min_by_parent, on="ph", suffixes=("_p", "_c"))
        assert (merged["min_v_p"] == merged["min_v_c"]).all()

    def test_max_at_parent_is_max_of_children(self, local_bucket, h3_conn):
        # Mirror of the MIN test.
        points = [
            (37.8, -122.3, 10.0), (37.80001, -122.30001, 2.0),
            (37.80002, -122.30002, 7.0),
            (40.0, -100.0, 3.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=3, agg="MAX", zoom_offset=-1,
        )
        child_uri = str(local_bucket / "hex" / result["hash"] / "res=5" / "**" / "*.parquet")
        parent_uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        children_max_by_parent = h3_conn.sql(
            f"""
            SELECT h3_cell_to_parent(h, 4) AS ph, MAX("val") AS max_v
            FROM read_parquet('{child_uri}', hive_partitioning=true)
            GROUP BY 1
            """
        ).fetchdf()
        parents = h3_conn.sql(
            f'SELECT h AS ph, "val" AS max_v FROM read_parquet(\'{parent_uri}\', hive_partitioning=true)'
        ).fetchdf()
        merged = parents.merge(children_max_by_parent, on="ph", suffixes=("_p", "_c"))
        assert (merged["max_v_p"] == merged["max_v_c"]).all()

    def test_avg_at_parent_is_weighted_source_mean(self, local_bucket, h3_conn):
        # AVG mode: parent.val equals the row-weighted mean of source rows
        # falling under that parent. The test is meaningful only when the
        # parent has TWO OR MORE distinct children at finest res — otherwise
        # the Phase-2 SUM(v*w)/SUM(w) collapses to the single child's mean
        # and an incorrect formula like AVG(v) would also pass.
        #
        # (37.0, -122.0) and (37.2, -122.0) map to two distinct h5 cells
        # that share the same h4 parent.
        # Cluster A: 4 rows at h5 cell A, values [10, 20, 30, 40] (cell mean 25, count 4).
        # Cluster B: 1 row at h5 cell B, value [100] (cell mean 100, count 1).
        # Unweighted mean-of-means at h4 = (25 + 100) / 2 = 62.5  ← WRONG
        # Correctly weighted mean       = (4*25 + 1*100) / 5 = 40.0
        points = [
            (37.0, -122.0, 10.0),
            (37.0, -122.0, 20.0),
            (37.0, -122.0, 30.0),
            (37.0, -122.0, 40.0),
            (37.2, -122.0, 100.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=4, agg="AVG", zoom_offset=-1,
        )
        # Sanity: confirm the finest level really has 2 distinct h5 cells
        # under one h4 parent — otherwise the test is checking nothing useful.
        finest_uri = str(local_bucket / "hex" / result["hash"] / "res=5" / "**" / "*.parquet")
        child_count = h3_conn.sql(
            f"SELECT COUNT(DISTINCT h) FROM read_parquet('{finest_uri}', hive_partitioning=true)"
        ).fetchone()[0]
        assert child_count == 2, f"fixture must produce 2 distinct h5 cells, got {child_count}"

        parent_uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        rows = h3_conn.sql(
            f'SELECT h, "val" FROM read_parquet(\'{parent_uri}\', hive_partitioning=true)'
        ).fetchall()
        assert len(rows) == 1, f"expected 1 parent cell, got {len(rows)}: {rows}"
        parent_val = rows[0][1]
        assert abs(parent_val - 40.0) < 1e-9, (
            f"parent AVG = {parent_val}, expected weighted mean 40.0 "
            f"(unweighted mean-of-means would be 62.5)"
        )


import json
import time
import duckdb
from tiles.pyramid import (
    write_lock,
    read_lock,
    lock_is_stale,
)


class TestLockMarkers:
    def _con(self):
        return duckdb.connect(":memory:")

    def test_write_then_read_round_trips(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        write_lock(con, output_uri, pod_id="pod-A")
        lock = read_lock(con, output_uri)
        assert lock is not None
        assert lock["pod_id"] == "pod-A"
        assert isinstance(lock["started_at"], (int, float))
        # Lock file should physically exist on disk for local paths.
        import os
        assert os.path.exists(output_uri + "lock.json")

    def test_read_returns_none_when_absent(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        assert read_lock(con, output_uri) is None

    def test_overwrite_replaces_previous(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        write_lock(con, output_uri, pod_id="pod-A")
        first = read_lock(con, output_uri)
        time.sleep(0.01)
        write_lock(con, output_uri, pod_id="pod-B")
        second = read_lock(con, output_uri)
        assert second["pod_id"] == "pod-B"
        assert second["started_at"] >= first["started_at"]

    def test_lock_is_stale_returns_false_when_fresh(self):
        lock = {"started_at": time.time(), "pod_id": "pod-A"}
        assert lock_is_stale(lock) is False

    def test_lock_is_stale_returns_true_past_ttl(self):
        lock = {"started_at": time.time() - 10_000, "pod_id": "pod-A"}
        assert lock_is_stale(lock) is True

    def test_lock_is_stale_handles_none(self):
        assert lock_is_stale(None) is True


from tiles.pyramid import write_failed, read_failed


class TestFailedMarkers:
    def _con(self):
        return duckdb.connect(":memory:")

    def test_write_then_read_round_trips(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        write_failed(con, output_uri, error="Out of memory during COPY")
        failed = read_failed(con, output_uri)
        assert failed is not None
        assert failed["error"] == "Out of memory during COPY"
        assert isinstance(failed["failed_at"], (int, float))

    def test_read_returns_none_when_absent(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        assert read_failed(con, output_uri) is None

    def test_error_string_with_single_quotes_round_trips(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        msg = "table 't' doesn't exist; check 'schema'"
        write_failed(con, output_uri, error=msg)
        assert read_failed(con, output_uri)["error"] == msg
