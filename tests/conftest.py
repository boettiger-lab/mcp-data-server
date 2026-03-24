import pytest
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



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


