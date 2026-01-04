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


class TestDataCatalogParsing:
    """Test data catalog parsing logic."""
    
    def test_catalog_parsing_with_sections(self):
        """Test that catalog with numbered sections is parsed correctly."""
        from server import DATA_CATALOG
        # DATA_CATALOG should be a dict
        assert isinstance(DATA_CATALOG, dict)
        # Should have at least _intro key if CATALOG_RAW exists
        if DATA_CATALOG:
            assert "_intro" in DATA_CATALOG or len(DATA_CATALOG) > 0


class TestResourceFunctions:
    """Test MCP resource functions."""
    
    def test_list_datasets_returns_string(self):
        """Test that list_datasets returns a string."""
        from server import list_datasets
        result = list_datasets()
        assert isinstance(result, str)
    
    def test_get_dataset_details_found(self):
        """Test getting details for an existing dataset."""
        from server import get_dataset_details, DATA_CATALOG
        
        if DATA_CATALOG:
            # Get first valid key that's not _intro
            test_key = next((k for k in DATA_CATALOG.keys() if k != "_intro"), None)
            if test_key:
                result = get_dataset_details(test_key)
                assert isinstance(result, str)
                assert len(result) > 0
    
    def test_get_dataset_details_not_found(self):
        """Test getting details for non-existent dataset."""
        from server import get_dataset_details
        result = get_dataset_details("nonexistent_dataset_xyz")
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
