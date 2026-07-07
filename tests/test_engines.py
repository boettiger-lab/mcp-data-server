"""Tests for the query-engine seam (#227).

Three groups:
  - sql_translate unit tests (pure, no GPU, no network)
  - resolve_storage_options routing (the Polars analogue of DuckDB secret scoping)
  - DuckDB ⇄ polars-cpu parity on a local parquet fixture (the correctness net)
  - select_engine registry behaviour

All of this runs in ordinary CI: polars-cpu needs no GPU.
"""
import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engines import select_engine, sql_translate
from engines.base import S3Request
from engines.duckdb_engine import DuckDBEngine
from engines.polars_engine import PolarsEngine
from engines.sql_translate import UnsupportedSQL


# ---------------------------------------------------------------------------
# sql_translate: extraction / substitution / rewrites / guards
# ---------------------------------------------------------------------------
class TestSqlTranslate:
    def test_extract_parquet_sources_dedups_and_aliases(self):
        sql = ("SELECT * FROM read_parquet('s3://b/a.parquet') a "
               "JOIN read_parquet('s3://b/c.parquet') c USING (id) "
               "WHERE id IN (SELECT id FROM read_parquet('s3://b/a.parquet'))")
        paths = sql_translate.extract_parquet_sources(sql)
        assert paths == {"s3://b/a.parquet": "__tbl_0", "s3://b/c.parquet": "__tbl_1"}

    def test_extract_parquet_sources_with_options(self):
        sql = "SELECT * FROM read_parquet('s3://b/hex/h0=*/d.parquet', hive_partitioning=true)"
        assert sql_translate.extract_parquet_sources(sql) == {
            "s3://b/hex/h0=*/d.parquet": "__tbl_0"
        }

    def test_substitute_aliases(self):
        sql = "SELECT COUNT(*) FROM read_parquet('s3://b/a.parquet')"
        paths = sql_translate.extract_parquet_sources(sql)
        assert sql_translate.substitute_aliases(sql, paths) == "SELECT COUNT(*) FROM __tbl_0"

    def test_rewrite_functions_approx_count_distinct(self):
        out = sql_translate.rewrite_functions("SELECT APPROX_COUNT_DISTINCT(x) FROM t")
        assert "COUNT(DISTINCT x)" in out
        assert "APPROX_COUNT_DISTINCT" not in out.upper()

    def test_guard_rejects_h3_functions(self):
        with pytest.raises(UnsupportedSQL):
            sql_translate.guard_unsupported("SELECT h3_cell_to_parent(h8, 5) FROM t")
        with pytest.raises(UnsupportedSQL):
            sql_translate.guard_unsupported("SELECT h3_h3_to_string(h8) FROM t")

    def test_guard_allows_plain_h_columns(self):
        # h0..h11 are columns, not h3_* functions — must pass.
        sql_translate.guard_unsupported("SELECT h8, h0 FROM t WHERE h0 = 42")

    @pytest.mark.parametrize("sql,expected", [
        ("SELECT * FROM t WHERE h0 IN (1, 2, 3)", frozenset({1, 2, 3})),
        ("SELECT * FROM t WHERE h0 = 7", frozenset({7})),
        ("SELECT * FROM t WHERE h0 IN (10,20) OR h0 = 30", frozenset({10, 20, 30})),
        ("SELECT * FROM t", None),
    ])
    def test_extract_h0_predicates(self, sql, expected):
        assert sql_translate.extract_h0_predicates(sql) == expected


# ---------------------------------------------------------------------------
# resolve_storage_options: per-path S3 routing
# ---------------------------------------------------------------------------
class TestResolveStorageOptions:
    def test_local_path_returns_none(self):
        assert sql_translate.resolve_storage_options("/tmp/x.parquet", S3Request()) is None
        assert sql_translate.resolve_storage_options("s3x/not-s3", S3Request()) is None

    def test_default_endpoint_anonymous(self, monkeypatch):
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "minio.example.org")
        monkeypatch.setenv("S3_SOURCES", "")
        opts = sql_translate.resolve_storage_options("s3://public-data/x.parquet", S3Request())
        assert opts["endpoint_url"] == "https://minio.example.org"
        assert opts["skip_signature"] == "true"
        assert "aws_access_key_id" not in opts

    def test_http_default_endpoint_allows_http(self, monkeypatch):
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "rook-ceph-rgw-nautiluss3.rook")
        opts = sql_translate.resolve_storage_options("s3://public-data/x.parquet", S3Request())
        assert opts["endpoint_url"] == "http://rook-ceph-rgw-nautiluss3.rook"
        assert opts["allow_http"] == "true"

    def test_default_use_ssl_false_forces_http(self, monkeypatch):
        # Regression: an SSL-inferring endpoint (e.g. in-cluster MinIO) with
        # S3_DEFAULT_USE_SSL=false must resolve to http:// — the DuckDB default
        # secret honours this and the Polars path must too, else object_store
        # sends https to a plaintext port. (Caught on the cirrus GPU deploy.)
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "minio-svc.minio.svc.cluster.local:9000")
        monkeypatch.setenv("S3_DEFAULT_USE_SSL", "false")
        opts = sql_translate.resolve_storage_options("s3://public-data/x.parquet", S3Request())
        assert opts["endpoint_url"] == "http://minio-svc.minio.svc.cluster.local:9000"
        assert opts["allow_http"] == "true"
        assert opts["aws_virtual_hosted_style_request"] == "false"

    def test_client_endpoint_unscoped_owns_all(self, monkeypatch):
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "default.example")
        req = S3Request(s3_endpoint="byo.example.org")
        opts = sql_translate.resolve_storage_options("s3://any-bucket/x.parquet", req)
        assert opts["endpoint_url"] == "https://byo.example.org"

    def test_client_endpoint_scoped_in_and_out(self, monkeypatch):
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "default.example")
        monkeypatch.setenv("S3_SOURCES", "")
        req = S3Request(s3_endpoint="byo.example.org", s3_scope="s3://public-")
        in_scope = sql_translate.resolve_storage_options("s3://public-x/f.parquet", req)
        out_scope = sql_translate.resolve_storage_options("s3://private-y/f.parquet", req)
        assert in_scope["endpoint_url"] == "https://byo.example.org"
        assert out_scope["endpoint_url"] == "https://default.example"

    def test_client_credentials_included(self):
        req = S3Request(s3_key="AKID", s3_secret="SEKRET", s3_endpoint="byo.example.org")
        opts = sql_translate.resolve_storage_options("s3://b/f.parquet", req)
        assert opts["aws_access_key_id"] == "AKID"
        assert opts["aws_secret_access_key"] == "SEKRET"
        assert "skip_signature" not in opts

    def test_source_registry_scoped_secret(self, monkeypatch):
        # The built-in source.coop entry carries a scoped secret; a matching path
        # should route to its endpoint/region.
        monkeypatch.setenv("S3_SOURCES", "")
        opts = sql_translate.resolve_storage_options(
            "s3://us-west-2.opendata.source.coop/some/file.parquet", S3Request()
        )
        assert opts["endpoint_url"] == "https://s3.us-west-2.amazonaws.com"
        assert opts["aws_region"] == "us-west-2"


# ---------------------------------------------------------------------------
# DuckDB ⇄ polars-cpu parity
# ---------------------------------------------------------------------------
@pytest.fixture
def fixture_parquet(tmp_path):
    p = tmp_path / "data.parquet"
    pl.DataFrame({
        "id": [1, 2, 3, 4],
        "grp": ["a", "b", "a", "b"],
        "val": [10, 20, 30, 40],
        "f": [1.5, 2.5, 3.5, 4.5],
    }).write_parquet(p)
    return str(p)


PARITY_QUERIES = [
    "SELECT 2+2 AS r",
    "SELECT 'hello' || ' ' || 'world' AS greeting",
    "SELECT COUNT(*) AS n FROM read_parquet('{fx}')",
    "SELECT grp, COUNT(*) AS n, SUM(val) AS s FROM read_parquet('{fx}') GROUP BY grp ORDER BY grp",
    "SELECT id, val FROM read_parquet('{fx}') WHERE val > 15 ORDER BY id",
    "SELECT AVG(f) AS a FROM read_parquet('{fx}')",
    "SELECT grp, MIN(val) AS lo, MAX(val) AS hi FROM read_parquet('{fx}') GROUP BY grp ORDER BY grp",
    "SELECT DISTINCT grp FROM read_parquet('{fx}') ORDER BY grp",
    "SELECT COUNT(*) AS n FROM read_parquet('{fx}') WHERE grp = 'zzz'",
]


@pytest.mark.parametrize("template", PARITY_QUERIES)
def test_duckdb_polars_cpu_parity(template, fixture_parquet):
    sql = template.format(fx=fixture_parquet)
    duck = DuckDBEngine().run(sql, S3Request())
    pola = PolarsEngine("polars-cpu").run(sql, S3Request())
    assert duck == pola, f"\n--- DuckDB ---\n{duck}\n--- polars-cpu ---\n{pola}"


def test_polars_empty_result(fixture_parquet):
    sql = f"SELECT * FROM read_parquet('{fixture_parquet}') WHERE grp = 'zzz'"
    assert PolarsEngine("polars-cpu").run(sql, S3Request()) == "No results found."


def test_polars_unsupported_returns_sql_error(fixture_parquet):
    sql = f"SELECT h3_cell_to_parent(id, 5) FROM read_parquet('{fixture_parquet}')"
    out = PolarsEngine("polars-cpu").run(sql, S3Request())
    assert out.startswith("SQL Error:")
    assert "h3" in out.lower()


# ---------------------------------------------------------------------------
# select_engine registry
# ---------------------------------------------------------------------------
class TestSelectEngine:
    def test_default_is_duckdb(self, monkeypatch):
        monkeypatch.delenv("QUERY_ENGINE", raising=False)
        assert select_engine().name == "duckdb"

    @pytest.mark.parametrize("mode", ["polars-cpu", "polars-gpu", "polars-gpu-cudf"])
    def test_polars_modes(self, monkeypatch, mode):
        monkeypatch.setenv("QUERY_ENGINE", mode)
        eng = select_engine()
        assert eng.name == mode
        assert isinstance(eng, PolarsEngine)

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("QUERY_ENGINE", "DuckDB")
        assert select_engine().name == "duckdb"

    def test_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("QUERY_ENGINE", "sqlite")
        with pytest.raises(ValueError):
            select_engine()
