import pytest
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def sample_catalog_content():
    """Fixture providing sample catalog content for testing."""
    return """# Data Catalog

**1. Dataset One (Test)**
- Description: First test dataset
- Path: s3://bucket/dataset1.parquet
- Format: Parquet

**2. Dataset Two (Sample)**
- Description: Second test dataset  
- Path: s3://bucket/dataset2.parquet
- Format: Parquet
"""


@pytest.fixture
def sample_setup_sql():
    """Fixture providing sample setup SQL."""
    return """```sql
INSTALL spatial;
LOAD spatial;
INSTALL httpfs;
LOAD httpfs;
```"""


@pytest.fixture
def sample_query_optimization():
    """Fixture providing sample optimization content."""
    return """# Query Optimization

1. Use column pruning
2. Apply filters early
3. Limit result sets
"""


@pytest.fixture
def mock_md_files(tmp_path, sample_catalog_content, sample_setup_sql, sample_query_optimization):
    """Fixture that creates temporary markdown files for testing."""
    catalog_file = tmp_path / "datasets.md"
    catalog_file.write_text(sample_catalog_content)
    
    setup_file = tmp_path / "query-setup.md"
    setup_file.write_text(sample_setup_sql)
    
    optim_file = tmp_path / "query-optimization.md"
    optim_file.write_text(sample_query_optimization)
    
    h3_file = tmp_path / "h3-guide.md"
    h3_file.write_text("# H3 Guide\nH3 hexagonal spatial indexing.")
    
    return {
        'catalog': catalog_file,
        'setup': setup_file,
        'optimization': optim_file,
        'h3': h3_file
    }
