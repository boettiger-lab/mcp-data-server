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

    def test_guard_rejects_ranking_functions_on_any_engine(self):
        # RANK/ROW_NUMBER/etc. have no Polars SQLContext equivalent at all
        # (polars==1.32.3) — rejected regardless of want_gpu.
        for fn_sql in [
            "SELECT RANK() OVER (ORDER BY val DESC) FROM t",
            "SELECT ROW_NUMBER() OVER (ORDER BY val DESC) FROM t",
            "SELECT LAG(val) OVER (ORDER BY val DESC) FROM t",
        ]:
            with pytest.raises(UnsupportedSQL):
                sql_translate.guard_unsupported(fn_sql, want_gpu=False)
            with pytest.raises(UnsupportedSQL):
                sql_translate.guard_unsupported(fn_sql, want_gpu=True)

    def test_guard_rejects_aggregate_window_functions_only_on_gpu(self):
        sql = "SELECT h5, SUM(val) OVER (ORDER BY val DESC) FROM t"
        # CPU Polars runs aggregate window functions fine — must pass with
        # want_gpu=False, and only reject once GPU compute is requested.
        sql_translate.guard_unsupported(sql, want_gpu=False)
        with pytest.raises(UnsupportedSQL):
            sql_translate.guard_unsupported(sql, want_gpu=True)

    @pytest.mark.parametrize("sql,expected", [
        ("SELECT * FROM t WHERE h0 IN (1, 2, 3)", frozenset({1, 2, 3})),
        ("SELECT * FROM t WHERE h0 = 7", frozenset({7})),
        ("SELECT * FROM t WHERE h0 IN (10,20) OR h0 = 30", frozenset({10, 20, 30})),
        ("SELECT * FROM t", None),
    ])
    def test_extract_h0_predicates(self, sql, expected):
        assert sql_translate.extract_h0_predicates(sql) == expected

    def test_filter_files_by_h0_dpp(self):
        files = [
            "s3://b/hex/h0=1/data_0.parquet",
            "s3://b/hex/h0=2/data_0.parquet",
            "s3://b/hex/h0=3/data_0.parquet",
            "s3://b/lookup.parquet",  # no h0= component → always kept
        ]
        kept = sql_translate._filter_files_by_h0(files, frozenset({1, 3}))
        assert kept == [
            "s3://b/hex/h0=1/data_0.parquet",
            "s3://b/hex/h0=3/data_0.parquet",
            "s3://b/lookup.parquet",
        ]


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


def test_polars_ranking_function_returns_sql_error_even_on_cpu(fixture_parquet):
    # Unlike aggregate window functions, RANK() has no Polars equivalent at
    # all — polars-cpu rejects it too, not just the GPU compute path.
    sql = f"SELECT RANK() OVER (ORDER BY val DESC) FROM read_parquet('{fixture_parquet}')"
    out = PolarsEngine("polars-cpu").run(sql, S3Request())
    assert out.startswith("SQL Error:")
    assert "rank" in out.lower()


def test_polars_aggregate_window_function_runs_on_cpu(fixture_parquet):
    # Not a parity test: DuckDB and Polars compute ORDER BY ... DESC window
    # frames differently (a separate, unrelated quirk — not investigated
    # here). This only confirms the GPU-only guard doesn't overreach and
    # block aggregate window functions on polars-cpu.
    sql = (f"SELECT grp, val, SUM(val) OVER (ORDER BY val) AS running "
           f"FROM read_parquet('{fixture_parquet}')")
    out = PolarsEngine("polars-cpu").run(sql, S3Request())
    assert not out.startswith("SQL Error:")


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


# ---------------------------------------------------------------------------
# ENABLE_HEX_TILES gating (query-only GPU deploys)
# ---------------------------------------------------------------------------
class TestHexTileGating:
    def test_duckdb_registers_hex_tools(self):
        """Default DuckDB engine: hex tools present (unchanged behaviour)."""
        import asyncio
        import server
        tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert server.ENABLE_HEX_TILES is True
        assert {"register_hex_tiles", "get_hex_tile_status", "query"} <= tools

    def test_polars_engine_is_query_only(self):
        """A Polars/GPU engine hides the DuckDB-only hex tools by default."""
        import json
        import subprocess
        import sys
        repo = os.path.dirname(os.path.dirname(__file__))
        code = (
            "import server, asyncio, json;"
            "tools=sorted(t.name for t in asyncio.run(server.mcp.list_tools()));"
            "print('RESULT ' + json.dumps({'enabled': server.ENABLE_HEX_TILES, 'tools': tools}))"
        )
        env = {**os.environ, "QUERY_ENGINE": "polars-cpu",
               "STAC_ALLOW_DEGRADED_START": "true"}
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=repo,
                             capture_output=True, text=True, timeout=120)
        line = [l for l in out.stdout.splitlines() if l.startswith("RESULT ")][-1]
        data = json.loads(line[len("RESULT "):])
        assert data["enabled"] is False
        assert "query" in data["tools"]
        assert "register_hex_tiles" not in data["tools"]
        assert "get_hex_tile_status" not in data["tools"]
