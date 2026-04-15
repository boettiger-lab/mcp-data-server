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
