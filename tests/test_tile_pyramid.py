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
        # Parent selects use SUM(val)
        assert "SUM(val)" in sql

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
        assert "AVG(v1)" in sql
        assert "AVG(v2)" in sql

    def test_min_res_equals_finest_res_single_level(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=8, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Only the finest level — no UNION ALL.
        assert "UNION ALL" not in sql


import os
import duckdb
from pathlib import Path
from tiles.pyramid import register_hex_tiles


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

    def test_identical_inputs_same_hash(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        r1 = register_hex_tiles(con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4)
        r2 = register_hex_tiles(con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4)
        assert r1["hash"] == r2["hash"]


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
