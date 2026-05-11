import re
import pytest
from tiles.pyramid import build_pyramid_sql


class TestBuildPyramidSQL:
    def test_contains_one_select_per_resolution(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8,
            min_res=2,
            agg="AVG",
            value_columns=["val"],
            h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Parents at res 2..7, plus finest level at res 8. 7 SELECTs total.
        union_alls = len(re.findall(r"\bUNION ALL\b", sql, re.IGNORECASE))
        assert union_alls == 6  # 7 selects → 6 UNION ALLs

    def test_finest_level_is_ungrouped(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8,
            min_res=2,
            agg="AVG",
            value_columns=["val"],
            h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # The finest-res SELECT should NOT have AVG/SUM wrapping the value column.
        # Look for the "... AS res" marker equal to finest_res.
        finest_select = re.search(r"SELECT[^)]*?8 AS res FROM src", sql, re.DOTALL)
        assert finest_select is not None
        assert "AVG" not in finest_select.group(0)

    def test_parent_levels_aggregate(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8,
            min_res=2,
            agg="SUM",
            value_columns=["val"],
            h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Parent selects use SUM("val") — column names are quoted
        assert 'SUM("val")' in sql

    def test_partitions_by_res(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        assert "PARTITION_BY (res)" in sql
        assert "OVERWRITE_OR_IGNORE" in sql
        assert "FORMAT PARQUET" in sql

    def test_multiple_value_columns(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, v1, v2 FROM src",
            finest_res=8, min_res=2, agg="AVG",
            value_columns=["v1", "v2"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        assert 'AVG("v1")' in sql
        assert 'AVG("v2")' in sql

    def test_min_res_equals_finest_res_single_level(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=8, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Only the finest level — no UNION ALL.
        assert "UNION ALL" not in sql

    def test_count_mode_emits_count_column_no_value_cols(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8 FROM src",
            finest_res=8, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # COUNT(*) AS count at every level — parents and finest.
        assert "COUNT(*) AS count" in sql
        # No stray references to user value columns (there are none).
        assert 'AVG(' not in sql and 'SUM(' not in sql

    def test_count_mode_finest_level_aggregates(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8 FROM src",
            finest_res=5, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # COUNT mode must GROUP BY at finest too so duplicate source rows
        # collapse to one row per cell with the real count. The finest
        # SELECT uses the bare h3_column (not h3_cell_to_parent) and "5 AS res".
        assert 'SELECT "h8" AS h, COUNT(*) AS count, 5 AS res FROM src GROUP BY 1' in sql


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
        # Verify the parquet pyramid actually has the count column.
        import duckdb as _d
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "*.parquet")
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
