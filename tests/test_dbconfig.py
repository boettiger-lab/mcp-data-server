"""Unit tests for dbconfig — the DuckDB memory_limit (#270) and spill (#423) derivation."""
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dbconfig import (
    _parse_bytes,
    duckdb_max_temp_directory_size,
    duckdb_memory_limit,
    memory_limit_sql,
    spill_sql,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with no memory/spill var set."""
    for var in ("DUCKDB_MEMORY_LIMIT", "POD_MEMORY_LIMIT", "DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
                "POD_EPHEMERAL_LIMIT", "DUCKDB_TEMP_ROOT"):
        monkeypatch.delenv(var, raising=False)


class TestParseBytes:
    def test_plain_integer_is_bytes(self):
        # The Downward API emits the limit in bytes as a plain integer.
        assert _parse_bytes("104857600") == 100 * 2**20

    @pytest.mark.parametrize("text,expected", [
        ("96Gi", 96 * 2**30),
        ("2Gi", 2 * 2**30),
        ("500Mi", 500 * 2**20),
        ("10G", 10 * 10**9),
        ("1Ti", 2**40),
    ])
    def test_k8s_and_si_quantities(self, text, expected):
        assert _parse_bytes(text) == expected

    @pytest.mark.parametrize("bad", ["", "abc", "10Xi", "Gi"])
    def test_unparseable_raises(self, bad):
        with pytest.raises(ValueError):
            _parse_bytes(bad)


class TestDuckdbMemoryLimit:
    def test_unset_returns_none(self):
        assert duckdb_memory_limit() is None
        assert memory_limit_sql() == ""

    def test_explicit_override_passthrough(self, monkeypatch):
        monkeypatch.setenv("DUCKDB_MEMORY_LIMIT", "120GB")
        assert duckdb_memory_limit() == "120GB"
        assert memory_limit_sql() == "SET memory_limit='120GB'"

    def test_explicit_override_wins_over_pod_limit(self, monkeypatch):
        monkeypatch.setenv("POD_MEMORY_LIMIT", str(96 * 2**30))
        monkeypatch.setenv("DUCKDB_MEMORY_LIMIT", "100GB")
        assert duckdb_memory_limit() == "100GB"

    def test_pod_limit_bytes_is_80_percent_in_mib(self, monkeypatch):
        # 100 MiB → 80% = 80 MiB.
        monkeypatch.setenv("POD_MEMORY_LIMIT", str(100 * 2**20))
        assert duckdb_memory_limit() == "80MiB"
        assert memory_limit_sql() == "SET memory_limit='80MiB'"

    def test_pod_limit_k8s_quantity(self, monkeypatch):
        monkeypatch.setenv("POD_MEMORY_LIMIT", "96Gi")
        # ~80% of 96 GiB, expressed in MiB.
        expected_mib = int(96 * 2**30 * 0.8) // 2**20
        assert duckdb_memory_limit() == f"{expected_mib}MiB"

    def test_unparseable_pod_limit_falls_back_to_none(self, monkeypatch, capsys):
        monkeypatch.setenv("POD_MEMORY_LIMIT", "not-a-size")
        assert duckdb_memory_limit() is None
        assert "POD_MEMORY_LIMIT ignored" in capsys.readouterr().err

    def test_zero_pod_limit_returns_none(self, monkeypatch):
        monkeypatch.setenv("POD_MEMORY_LIMIT", "0")
        assert duckdb_memory_limit() is None


class TestMaxTempDirectorySize:
    def test_unset_returns_none(self):
        assert duckdb_max_temp_directory_size() is None

    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("POD_EPHEMERAL_LIMIT", "50Gi")
        monkeypatch.setenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "10GiB")
        assert duckdb_max_temp_directory_size() == "10GiB"

    def test_pod_limit_is_half_in_mib(self, monkeypatch):
        # The NRP namespace default: 50Gi → 25 GiB per connection.
        monkeypatch.setenv("POD_EPHEMERAL_LIMIT", str(50 * 2**30))
        assert duckdb_max_temp_directory_size() == f"{25 * 1024}MiB"

    def test_unparseable_falls_back_to_none(self, monkeypatch, capsys):
        monkeypatch.setenv("POD_EPHEMERAL_LIMIT", "lots")
        assert duckdb_max_temp_directory_size() is None
        assert "POD_EPHEMERAL_LIMIT ignored" in capsys.readouterr().err


class TestSpillSql:
    def test_private_directory_per_connection(self):
        a, b = spill_sql(), spill_sql()
        assert len(a) == 1  # no cap when the pod limit is unknown
        assert a[0].startswith("SET temp_directory='/tmp/duckdb-")
        assert a[0] != b[0]

    def test_temp_root_and_cap(self, monkeypatch):
        monkeypatch.setenv("DUCKDB_TEMP_ROOT", "/scratch")
        monkeypatch.setenv("POD_EPHEMERAL_LIMIT", "2Gi")
        tmp, cap = spill_sql()
        assert tmp.startswith("SET temp_directory='/scratch/duckdb-")
        assert cap == "SET max_temp_directory_size='1024MiB'"


# Real DuckDB, tiny limits: a sort of ~10M md5 strings under a 64MB memory_limit
# has to spill a few hundred MB.
_SPILL_QUERY = ("SELECT sum(hash(s)) FROM (SELECT md5(i::VARCHAR) s "
                "FROM range(4000000) t(i) ORDER BY s)")


def _spill_conn(duckdb):
    con = duckdb.connect(":memory:")
    con.sql("SET threads=2; SET memory_limit='64MB'; SET preserve_insertion_order=false")
    for stmt in spill_sql():
        con.sql(stmt)
    return con


class TestSpillBehaviour:
    def test_cap_fails_the_query_not_the_process(self, monkeypatch, tmp_path):
        duckdb = pytest.importorskip("duckdb")
        monkeypatch.setenv("DUCKDB_TEMP_ROOT", str(tmp_path))
        monkeypatch.setenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "32MiB")
        con = _spill_conn(duckdb)
        with pytest.raises(duckdb.OutOfMemoryException, match="max_temp_directory_size"):
            con.sql(_SPILL_QUERY).fetchall()
        con.close()
        assert list(tmp_path.iterdir()) == []  # DuckDB removed its spill dir

    def test_concurrent_spills_do_not_collide(self, tmp_path):
        # Shared '/tmp' segfaulted here every time (#423); run in a subprocess so a
        # regression reports as a failure rather than killing the test runner.
        pytest.importorskip("duckdb")
        code = textwrap.dedent(f"""
            import sys, threading
            sys.path.insert(0, {os.path.join(os.path.dirname(__file__), "..")!r})
            import duckdb
            from tests.test_dbconfig import _SPILL_QUERY, _spill_conn
            out = []
            def run():
                con = _spill_conn(duckdb)
                out.append(con.sql(_SPILL_QUERY).fetchall())
                con.close()
            ts = [threading.Thread(target=run) for _ in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
            assert len(out) == 3 and out[0] == out[1] == out[2], out
        """)
        env = {**os.environ, "DUCKDB_TEMP_ROOT": str(tmp_path)}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                           text=True, timeout=300)
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr[-2000:]}"
        assert list(tmp_path.iterdir()) == []
