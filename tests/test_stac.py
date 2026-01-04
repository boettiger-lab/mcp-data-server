import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stac import fetch_stac_collections, DATA_CATALOG


class TestSTACCatalogParser:
    """Test STAC catalog description parser."""
    
    def test_fetch_stac_collections_success(self):
        """Test successful parsing of STAC collections."""
        mock_catalog = MagicMock()
        
        # Create mock child collections
        mock_collection1 = MagicMock()
        mock_collection1.title = "Test Dataset 1"
        mock_collection1.id = "test-dataset-1"
        mock_collection1.description = "A test dataset for unit testing"
        mock_collection1.license = "CC-BY-4.0"
        
        # Mock providers
        mock_provider = MagicMock()
        mock_provider.name = "Test Producer"
        mock_provider.roles = ["producer"]
        mock_collection1.providers = [mock_provider]
        
        # Mock summaries for formats
        mock_collection1.summaries = MagicMock()
        mock_collection1.summaries.get_list.return_value = ["parquet", "pmtiles"]
        
        # Mock links for documentation
        mock_doc_link = MagicMock()
        mock_doc_link.rel = "documentation"
        mock_doc_link.href = "https://example.com/docs"
        mock_collection1.links = [mock_doc_link]
        
        # Mock assets
        mock_asset1 = MagicMock()
        mock_asset1.title = "Test Asset 1"
        mock_asset1.href = "https://s3-west.nrp-nautilus.io/bucket/data.parquet"
        mock_asset1.description = None
        
        mock_asset2 = MagicMock()
        mock_asset2.title = "Test Hex Directory"
        mock_asset2.href = "https://s3-west.nrp-nautilus.io/bucket/hex/"
        mock_asset2.description = "Partitioned hex files"
        
        mock_collection1.assets = {"asset1": mock_asset1, "asset2": mock_asset2}
        
        mock_collection2 = MagicMock()
        mock_collection2.title = "Test Dataset 2"
        mock_collection2.id = "test-dataset-2"
        mock_collection2.description = "Another test dataset"
        mock_collection2.license = None
        mock_collection2.providers = []
        mock_collection2.summaries = None
        mock_collection2.links = []
        mock_collection2.assets = {}
        
        mock_catalog.get_children.return_value = [mock_collection1, mock_collection2]
        
        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            datasets = fetch_stac_collections()
            
            # Assert both datasets are present
            assert "test-dataset-1" in datasets
            assert "test-dataset-2" in datasets
            
            # Check dataset 1 contains expected information
            dataset1_info = datasets["test-dataset-1"]
            assert "Test Dataset 1" in dataset1_info
            assert "test-dataset-1" in dataset1_info
            assert "A test dataset for unit testing" in dataset1_info
            assert "Test Producer" in dataset1_info
            assert "parquet, pmtiles" in dataset1_info
            assert "CC-BY-4.0" in dataset1_info
            assert "https://example.com/docs" in dataset1_info
            assert "Test Asset 1: https://s3-west.nrp-nautilus.io/bucket/data.parquet" in dataset1_info
            assert "Test Hex Directory: s3://bucket/hex/" in dataset1_info  # Converted to s3://
            assert "Partitioned hex files" in dataset1_info
            
            # Check dataset 2 contains expected information
            dataset2_info = datasets["test-dataset-2"]
            assert "Test Dataset 2" in dataset2_info
            assert "test-dataset-2" in dataset2_info
            assert "Another test dataset" in dataset2_info
            assert "Unknown" in dataset2_info  # No producer
            assert "N/A" in dataset2_info  # No formats or documentation
    
    def test_fetch_stac_collections_empty_catalog(self):
        """Test handling of empty STAC catalog."""
        mock_catalog = MagicMock()
        mock_catalog.get_children.return_value = []
        
        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            datasets = fetch_stac_collections()
            assert datasets == {}
    
    def test_fetch_stac_collections_connection_error(self):
        """Test handling of connection errors."""
        with patch('stac.pystac.Catalog.from_file', side_effect=Exception("Connection failed")):
            datasets = fetch_stac_collections()
            assert "error" in datasets
            assert "Failed to load STAC" in datasets["error"]
            assert "Connection failed" in datasets["error"]
    
    def test_fetch_stac_collections_missing_optional_fields(self):
        """Test handling of collections with missing optional fields."""
        mock_catalog = MagicMock()
        
        # Create a minimal mock collection with only required fields
        mock_collection = MagicMock()
        mock_collection.title = "Minimal Dataset"
        mock_collection.id = "minimal-dataset"
        mock_collection.description = "Dataset with minimal metadata"
        mock_collection.license = None
        mock_collection.providers = []
        mock_collection.summaries = None
        mock_collection.links = []
        mock_collection.assets = {}
        
        mock_catalog.get_children.return_value = [mock_collection]
        
        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            datasets = fetch_stac_collections()
            
            assert "minimal-dataset" in datasets
            dataset_info = datasets["minimal-dataset"]
            assert "Minimal Dataset" in dataset_info
            assert "Dataset with minimal metadata" in dataset_info
            assert "Unknown" in dataset_info  # No producer
            assert "N/A" in dataset_info  # No formats/docs/license
    
    def test_fetch_stac_collections_special_characters(self):
        """Test handling of special characters in metadata."""
        mock_catalog = MagicMock()
        
        mock_collection = MagicMock()
        mock_collection.title = "Dataset with 'quotes' & <symbols>"
        mock_collection.id = "special-chars-dataset"
        mock_collection.description = "Description with\nnewlines and\ttabs"
        mock_collection.license = "MIT"
        mock_collection.providers = []
        mock_collection.summaries = None
        mock_collection.links = []
        
        # Mock asset with special characters in path
        mock_asset = MagicMock()
        mock_asset.title = "Asset with spaces"
        mock_asset.href = "https://s3-west.nrp-nautilus.io/bucket/path/with spaces/"
        mock_asset.description = None
        mock_collection.assets = {"asset1": mock_asset}
        
        mock_catalog.get_children.return_value = [mock_collection]
        
        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            datasets = fetch_stac_collections()
            
            assert "special-chars-dataset" in datasets
            dataset_info = datasets["special-chars-dataset"]
            assert "Dataset with 'quotes' & <symbols>" in dataset_info
            assert "s3://bucket/path/with spaces/" in dataset_info  # Converted to s3://
    
    def test_data_catalog_initialization(self):
        """Test that DATA_CATALOG is initialized at module load."""
        # DATA_CATALOG should be a dictionary (or contain an error key if loading failed)
        assert isinstance(DATA_CATALOG, dict)
        # It should either have datasets or an error key
        assert len(DATA_CATALOG) >= 0

