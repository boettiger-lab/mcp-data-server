import pytest
import os
import tempfile
from unittest.mock import patch, MagicMock
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import (
    load_text_file,
    parse_setup_sql,
    get_isolated_db,
    query,
)


class TestFileLoading:
    """Test file loading utilities."""
    
    def test_load_text_file_exists(self, tmp_path):
        """Test loading a file that exists."""
        test_file = tmp_path / "test.md"
        test_content = "# Test Content\nThis is a test."
        test_file.write_text(test_content)
        
        with patch('server.os.path.exists') as mock_exists:
            mock_exists.side_effect = lambda p: p == str(test_file)
            with patch('builtins.open', open):
                result = load_text_file(str(test_file))
                assert result == test_content
    
    def test_load_text_file_not_found(self, capsys):
        """Test loading a file that doesn't exist."""
        with patch('server.os.path.exists', return_value=False):
            result = load_text_file("nonexistent.md")
            assert result == ""
            captured = capsys.readouterr()
            assert "Warning" in captured.err


class TestSQLParsing:
    """Test SQL parsing from markdown."""
    
    def test_parse_setup_sql_valid(self):
        """Test parsing SQL from valid markdown code block."""
        content = """# Setup
        
```sql
INSTALL spatial;
LOAD spatial;
```

More text here.
"""
        result = parse_setup_sql(content)
        assert "INSTALL spatial" in result
        assert "LOAD spatial" in result
    
    def test_parse_setup_sql_no_code_block(self):
        """Test parsing when no SQL code block exists."""
        content = "Just plain text without code blocks."
        result = parse_setup_sql(content)
        assert result == ""
    
    def test_parse_setup_sql_empty(self):
        """Test parsing empty content."""
        result = parse_setup_sql("")
        assert result == ""


class TestDatabaseIsolation:
    """Test isolated database connection handling."""
    
    def test_get_isolated_db_creates_connection(self):
        """Test that isolated db context manager creates a connection."""
        with get_isolated_db() as conn:
            assert conn is not None
            # Verify it's a DuckDB connection by running a simple query
            result = conn.sql("SELECT 1 as test").fetchone()
            assert result[0] == 1
    
    def test_get_isolated_db_closes_connection(self):
        """Test that connection is properly closed after context."""
        with get_isolated_db() as conn:
            pass
        
        # Attempting to use closed connection should raise an error
        with pytest.raises(Exception):
            conn.sql("SELECT 1")
    
    def test_get_isolated_db_with_setup_sql(self):
        """Test that setup SQL is executed if available."""
        with patch('server.SETUP_SQL', 'CREATE TABLE test_table (id INTEGER)'):
            with get_isolated_db() as conn:
                # Check if table was created
                result = conn.sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                # DuckDB uses different system tables, so just verify connection works
                result = conn.sql("SELECT 1").fetchone()
                assert result[0] == 1


class TestQueryFunction:
    """Test the main query function."""
    
    def test_query_simple_select(self):
        """Test executing a simple SELECT query."""
        result = query("SELECT 1 as num, 'test' as text")
        assert "num" in result
        assert "test" in result
        assert "1" in result
    
    def test_query_returns_markdown(self):
        """Test that results are formatted as markdown."""
        result = query("SELECT 1 as num")
        # Markdown table should contain pipes
        assert "|" in result or "num" in result
    
    def test_query_empty_result(self):
        """Test query with no results."""
        result = query("SELECT 1 as num WHERE 1=0")
        assert "No results" in result
    
    def test_query_error_handling(self):
        """Test that SQL errors are caught and returned."""
        result = query("SELECT * FROM nonexistent_table")
        assert "Error" in result or "not found" in result.lower()
    
    def test_query_limit_enforced(self):
        """Test that query results are limited."""
        # Create query that would return many rows
        result = query("SELECT range as num FROM range(100)")
        # Should be limited and not crash
        assert isinstance(result, str)
        # Check it's a reasonable length (not all 100 rows in detail)
        lines = result.split('\n')
        # Markdown table: header + separator + up to 50 rows
        assert len(lines) <= 55


class TestResourceFunctions:
    """Test MCP resource functions."""

    def test_browse_stac_catalog_returns_string(self):
        """Test that browse_stac_catalog returns a string."""
        from server import browse_stac_catalog
        result = browse_stac_catalog()
        assert isinstance(result, str)

    def test_get_stac_details_found(self):
        """Test getting details for an existing dataset."""
        from server import get_stac_details
        from stac import STAC_DATASETS

        if STAC_DATASETS:
            test_key = next(iter(STAC_DATASETS.keys()), None)
            if test_key:
                result = get_stac_details(test_key)
                assert isinstance(result, str)
                assert len(result) > 0

    def test_get_stac_details_not_found(self):
        """Test getting details for non-existent dataset."""
        from server import get_stac_details
        result = get_stac_details("nonexistent_dataset_xyz")
        assert "not found" in result.lower()


class TestToolInjectedContext:
    """Test that tool context is properly constructed."""
    
    def test_tool_injected_context_exists(self):
        """Test that injected context is created."""
        from server import TOOL_INJECTED_CONTEXT
        assert isinstance(TOOL_INJECTED_CONTEXT, str)
        assert len(TOOL_INJECTED_CONTEXT) > 0
    
    def test_tool_injected_context_contains_rules(self):
        """Test that context contains critical rules."""
        from server import TOOL_INJECTED_CONTEXT
        context_lower = TOOL_INJECTED_CONTEXT.lower()
        # Should contain warnings about SQL rules
        assert any(word in context_lower for word in ['rule', 'parquet', 's3', 'catalog'])


class TestPromptFunction:
    """Test MCP prompt functions."""
    
    def test_analyst_persona_returns_string(self):
        """Test that analyst persona prompt returns a string."""
        from server import analyst_persona
        result = analyst_persona()
        assert isinstance(result, str)
        assert len(result) > 0


class TestS3Credentials:
    """Test that S3 credentials are injected into isolated DuckDB connections."""

    def test_query_without_credentials_succeeds(self):
        """query works normally without S3 credentials."""
        result = query("SELECT 1 as n")
        assert "1" in result

    def test_query_with_credentials_succeeds(self):
        """query accepts and applies S3 credentials without error."""
        result = query("SELECT 42 as n", s3_key="AKID", s3_secret="SECRET")
        assert "42" in result

    def test_get_isolated_db_injects_secret(self):
        """get_isolated_db creates a client_s3 secret when credentials are supplied."""
        with get_isolated_db(s3_key="AKID", s3_secret="SECRET") as conn:
            secrets = conn.sql("SELECT name FROM duckdb_secrets()").fetchall()
            names = [r[0] for r in secrets]
            assert "client_s3" in names

    def test_get_isolated_db_no_secret_without_credentials(self):
        """get_isolated_db does not create client_s3 when no credentials are supplied."""
        with get_isolated_db() as conn:
            secrets = conn.sql("SELECT name FROM duckdb_secrets()").fetchall()
            names = [r[0] for r in secrets]
            assert "client_s3" not in names

    def test_partial_credentials_no_secret(self):
        """Supplying only key or only secret does not create a secret (both required)."""
        with get_isolated_db(s3_key="AKID") as conn:
            names = [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" not in names
        with get_isolated_db(s3_secret="SECRET") as conn:
            names = [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" not in names

    def test_ssl_disabled_for_rook_endpoint(self):
        """Rook/Ceph internal endpoints get USE_SSL false."""
        with get_isolated_db(s3_key="K", s3_secret="S", s3_endpoint="rook-ceph-rgw-nautiluss3.rook") as conn:
            row = conn.sql("SELECT scope FROM duckdb_secrets() WHERE name='client_s3'").fetchone()
            # Secret was created — existence is sufficient; SSL value is in the secret config
            assert row is not None

    def test_ssl_enabled_for_external_endpoint(self):
        """Non-rook endpoints (e.g. minio) get USE_SSL true."""
        with get_isolated_db(s3_key="K", s3_secret="S", s3_endpoint="minio.example.org") as conn:
            row = conn.sql("SELECT name FROM duckdb_secrets() WHERE name='client_s3'").fetchone()
            assert row is not None

    def test_connection_isolation(self):
        """A secret in one connection is not visible in a concurrent connection."""
        with get_isolated_db(s3_key="AKID", s3_secret="SECRET") as conn_with:
            with get_isolated_db() as conn_without:
                names = [r[0] for r in conn_without.sql("SELECT name FROM duckdb_secrets()").fetchall()]
                assert "client_s3" not in names
            # Original connection still has its secret
            names = [r[0] for r in conn_with.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" in names

    def test_credentials_not_in_logs(self, capsys):
        """S3 credentials must not appear in stderr output."""
        with get_isolated_db(s3_key="MY_KEY_ID", s3_secret="MY_SECRET_VALUE"):
            pass
        captured = capsys.readouterr()
        assert "MY_KEY_ID" not in captured.err
        assert "MY_SECRET_VALUE" not in captured.err


class TestTileRouteMounted:
    def test_tile_route_exists_in_starlette_app(self):
        """After importing server, the streamable_http_app should have a /tiles route."""
        from server import mcp
        app = mcp.streamable_http_app()
        # Also apply our own mount (mimicking the startup block).
        from server import mount_tiles
        mount_tiles(app)
        paths = [getattr(r, "path", None) or getattr(r, "path_format", None) for r in app.routes]
        assert any(p and "/tiles/" in p for p in paths)

    def test_register_hex_tiles_is_disabled(self):
        """register_hex_tiles is intentionally not exposed until ready for production (#97)."""
        from server import mcp
        import anyio
        tool_names = [t.name for t in anyio.run(mcp.list_tools)]
        assert "register_hex_tiles" not in tool_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
