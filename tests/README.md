# Test Suite for MCP Data Server

This directory contains the test suite for the MCP Data Server project.

## Structure

- `test_server.py` - Unit tests for server functions, utilities, and components
- `test_integration.py` - Integration tests for end-to-end query execution
- `conftest.py` - Pytest fixtures and configuration
- `__init__.py` - Package initialization

## Running Tests

### Run all tests:
```bash
pytest tests/
```

### Run with verbose output:
```bash
pytest tests/ -v
```

### Run a specific test file:
```bash
pytest tests/test_server.py
```

### Run a specific test:
```bash
pytest tests/test_server.py::TestFileLoading::test_load_text_file_exists
```

### Run with coverage:
```bash
pytest tests/ --cov=. --cov-report=html
```

## Test Categories

### Unit Tests (`test_server.py`)
- File loading and parsing utilities
- SQL parsing from markdown
- Database isolation and connection handling
- Query execution and result formatting
- Data catalog parsing
- MCP resource and prompt functions
- Error handling

### Integration Tests (`test_integration.py`)
- End-to-end query execution
- Database extensions (spatial, httpfs)
- Error recovery scenarios
- Performance and result limiting

## Notes

- Tests are designed to be resilient to changes in markdown file content
- Mock fixtures are used where appropriate to avoid hard dependencies
- Some tests may skip if certain DuckDB extensions are unavailable
- Tests assume DuckDB and required dependencies are installed

## Requirements

Install test dependencies:
```bash
pip install pytest pytest-cov
```

Or if using the project's virtual environment, the dependencies should already be installed.
