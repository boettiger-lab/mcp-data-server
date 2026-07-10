"""Unit tests for dbconfig — the DuckDB memory_limit derivation (#270)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dbconfig import _parse_bytes, duckdb_memory_limit, memory_limit_sql


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Each test starts with neither memory var set."""
    monkeypatch.delenv("DUCKDB_MEMORY_LIMIT", raising=False)
    monkeypatch.delenv("POD_MEMORY_LIMIT", raising=False)


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
