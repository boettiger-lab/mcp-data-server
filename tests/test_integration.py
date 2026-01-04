import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import query, get_isolated_db


class TestIntegrationQueries:
    """Integration tests for end-to-end query execution."""
    
    def test_basic_arithmetic(self):
        """Test basic arithmetic query."""
        result = query("SELECT 2 + 2 as result")
        assert "4" in result
    
    def test_string_operations(self):
        """Test string manipulation."""
        result = query("SELECT 'hello' || ' ' || 'world' as greeting")
        assert "hello world" in result.lower()
    
    def test_aggregate_functions(self):
        """Test aggregate functions."""
        result = query("SELECT COUNT(*) as count FROM range(10)")
        assert "10" in result
    
    def test_date_functions(self):
        """Test date handling."""
        result = query("SELECT CURRENT_DATE as today")
        # Should return a date in some format
        assert isinstance(result, str)
        assert len(result) > 0
    
    def test_multiple_columns(self):
        """Test query returning multiple columns."""
        result = query("SELECT 1 as a, 2 as b, 3 as c")
        assert "a" in result and "b" in result and "c" in result
    
    def test_where_clause(self):
        """Test filtering with WHERE clause."""
        result = query("SELECT range as num FROM range(5) WHERE range > 2")
        assert "3" in result
        assert "1" not in result.split('\n')[2:]  # Not in data rows
    
    def test_order_by(self):
        """Test ORDER BY clause."""
        result = query("SELECT range as num FROM range(5) ORDER BY range DESC")
        # Check results are present
        assert "num" in result


class TestDatabaseExtensions:
    """Test that required database extensions can be loaded."""
    
    def test_spatial_extension_available(self):
        """Test that spatial extension can be installed."""
        with get_isolated_db() as conn:
            try:
                conn.sql("INSTALL spatial")
                conn.sql("LOAD spatial")
                # Try a spatial function
                result = conn.sql("SELECT ST_Point(0, 0) as point").fetchone()
                assert result is not None
            except Exception as e:
                # Some environments may not support spatial
                pytest.skip(f"Spatial extension not available: {e}")
    
    def test_httpfs_extension_available(self):
        """Test that httpfs extension can be installed."""
        with get_isolated_db() as conn:
            try:
                conn.sql("INSTALL httpfs")
                conn.sql("LOAD httpfs")
                # Extension should load without error
                result = conn.sql("SELECT 1").fetchone()
                assert result[0] == 1
            except Exception as e:
                pytest.skip(f"HTTPFS extension not available: {e}")


class TestErrorRecovery:
    """Test error handling and recovery."""
    
    def test_syntax_error_recovery(self):
        """Test that syntax errors are handled gracefully."""
        result = query("SELCT bad syntax")
        assert "error" in result.lower() or "syntax" in result.lower()
    
    def test_invalid_function_recovery(self):
        """Test handling of invalid function calls."""
        result = query("SELECT nonexistent_function()")
        assert "error" in result.lower() or "function" in result.lower()
    
    def test_type_error_recovery(self):
        """Test handling of type errors."""
        result = query("SELECT 'text' + 123")
        # Should return an error message, not crash
        assert isinstance(result, str)


class TestPerformance:
    """Test performance-related functionality."""
    
    def test_result_limiting(self):
        """Test that large result sets are properly limited."""
        # Query that generates many rows
        result = query("SELECT range as num FROM range(10000)")
        # Should be limited to 50 rows plus header/formatting
        lines = result.split('\n')
        # Account for header, separator, and markdown formatting
        assert len(lines) < 100, "Result set should be limited"
    
    def test_query_execution_completes(self):
        """Test that queries complete in reasonable time."""
        import time
        start = time.time()
        result = query("SELECT range as num FROM range(1000)")
        duration = time.time() - start
        # Should complete in under 5 seconds
        assert duration < 5.0, "Query took too long to execute"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
