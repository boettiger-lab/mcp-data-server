import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stac import fetch_stac_collections, DATA_CATALOG, list_datasets, get_dataset, fetch_stac_catalog, get_collection, _collection_to_dict


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


class TestCatalogUrlParameter:
    """Test that list_datasets and get_dataset accept an optional catalog_url."""

    def _make_mock_catalog(self):
        mock_catalog = MagicMock()
        mock_col = MagicMock()
        mock_col.id = "custom-dataset"
        mock_col.title = "Custom Dataset"
        mock_col.description = "From custom catalog"
        mock_col.assets = {}
        mock_col.extra_fields = {}
        mock_col.get_children.return_value = []
        mock_catalog.get_children.return_value = [mock_col]
        return mock_catalog

    def test_list_datasets_custom_url(self):
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()) as mock_from_file:
            result = list_datasets(catalog_url="https://example.com/custom/catalog.json")
            args, kwargs = mock_from_file.call_args
            assert args[0] == "https://example.com/custom/catalog.json"
            assert "stac_io" in kwargs
            assert "custom-dataset" in result
            assert "https://example.com/custom/catalog.json" in result

    def test_get_dataset_custom_url(self):
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()):
            result = get_dataset("custom-dataset", catalog_url="https://example.com/custom/catalog.json")
            assert "Custom Dataset" in result

    def test_get_dataset_catalog_token_forwarded(self):
        """catalog_token is forwarded through get_dataset to the StacIO instance."""
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()) as mock_from_file:
            get_dataset("custom-dataset",
                        catalog_url="https://example.com/custom/catalog.json",
                        catalog_token="secret-token")
            _, kwargs = mock_from_file.call_args
            stac_io = kwargs.get("stac_io")
            assert stac_io is not None
            assert stac_io._token == "secret-token"

    def test_list_datasets_default_url(self):
        """Without catalog_url, list_datasets uses the cached STAC_DATASETS (no network call)."""
        with patch('stac.pystac.Catalog.from_file') as mock_from_file:
            list_datasets()
            mock_from_file.assert_not_called()

    def test_catalog_token_passed_as_bearer(self):
        """catalog_token is forwarded to the StacIO instance as a Bearer header."""
        with patch('stac.requests.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = '{"type":"Catalog","id":"test","links":[],"stac_version":"1.0.0","description":""}'
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            from stac import _TimeoutStacIO
            io = _TimeoutStacIO(token="my-secret-token")
            io.read_text_from_href("https://example.com/catalog.json")
            mock_get.assert_called_once()
            _, kwargs = mock_get.call_args
            assert kwargs.get("headers", {}).get("Authorization") == "Bearer my-secret-token"

    def test_no_token_no_auth_header(self):
        """Without a token, no Authorization header is sent."""
        with patch('stac.requests.get') as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = "{}"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            from stac import _TimeoutStacIO
            io = _TimeoutStacIO()
            io.read_text_from_href("https://example.com/catalog.json")
            _, kwargs = mock_get.call_args
            assert not kwargs.get("headers", {}).get("Authorization")


class TestChildCollectionIndexing:
    """Test that child collections are indexed and queryable by their own ID."""

    def _make_mock_catalog_with_children(self):
        """Create a catalog with a parent that has two child sub-collections."""
        mock_catalog = MagicMock()

        # Child 1: senate districts
        child1 = MagicMock()
        child1.id = "census-2025-sldu"
        child1.title = "Senate Districts"
        child1.description = "Upper chamber legislative districts"
        child1.extra_fields = {
            "table:columns": [
                {"name": "SLDUST", "type": "varchar", "description": "Senate district ID"},
                {"name": "STATEFP", "type": "varchar", "description": "State FIPS code"},
            ]
        }
        asset1 = MagicMock()
        asset1.href = "https://s3-west.nrp-nautilus.io/public-census/census-2025/sldu/hex/"
        asset1.media_type = "application/x-parquet"
        asset1.title = "hex"
        asset1.extra_fields = {}
        child1.assets = {"hex": asset1}
        child1.get_children.return_value = []

        # Child 2: congressional districts
        child2 = MagicMock()
        child2.id = "census-2025-cd"
        child2.title = "Congressional Districts"
        child2.description = "Congressional districts"
        child2.extra_fields = {
            "table:columns": [
                {"name": "CD119FP", "type": "varchar", "description": "Congressional district"},
                {"name": "STATEFP", "type": "varchar", "description": "State FIPS code"},
            ]
        }
        asset2 = MagicMock()
        asset2.href = "https://s3-west.nrp-nautilus.io/public-census/census-2025/cd/hex/"
        asset2.media_type = "application/x-parquet"
        asset2.title = "hex"
        asset2.extra_fields = {}
        child2.assets = {"hex": asset2}
        child2.get_children.return_value = []

        # Parent collection
        parent = MagicMock()
        parent.id = "us-census"
        parent.title = "US Census"
        parent.description = "Census boundary datasets"
        parent.assets = {}
        parent.extra_fields = {}
        parent.get_children.return_value = [child1, child2]

        mock_catalog.get_children.return_value = [parent]
        return mock_catalog

    def test_child_collections_indexed(self):
        """fetch_stac_catalog indexes both parent and child collection IDs."""
        with patch('stac.pystac.Catalog.from_file',
                   return_value=self._make_mock_catalog_with_children()):
            datasets = fetch_stac_catalog(catalog_url="https://example.com/catalog.json")
            assert "us-census" in datasets
            assert "census-2025-sldu" in datasets
            assert "census-2025-cd" in datasets

    def test_child_has_own_columns(self):
        """Child collection metadata contains its own column schema."""
        with patch('stac.pystac.Catalog.from_file',
                   return_value=self._make_mock_catalog_with_children()):
            datasets = fetch_stac_catalog(catalog_url="https://example.com/catalog.json")
            sldu = datasets["census-2025-sldu"]
            assert "SLDUST" in sldu
            cd = datasets["census-2025-cd"]
            assert "CD119FP" in cd

    def test_parent_does_not_show_child_columns(self):
        """Parent collection no longer inherits an arbitrary child's columns."""
        with patch('stac.pystac.Catalog.from_file',
                   return_value=self._make_mock_catalog_with_children()):
            datasets = fetch_stac_catalog(catalog_url="https://example.com/catalog.json")
            parent = datasets["us-census"]
            # Parent has no table:columns of its own; should not show child columns
            assert "SLDUST" not in parent
            assert "CD119FP" not in parent

    def test_get_dataset_with_child_id(self):
        """get_dataset accepts a child collection ID and returns its metadata."""
        with patch('stac.pystac.Catalog.from_file',
                   return_value=self._make_mock_catalog_with_children()):
            result = get_dataset("census-2025-sldu",
                                 catalog_url="https://example.com/catalog.json")
            assert "Senate Districts" in result
            assert "SLDUST" in result


class TestCollectionToDict:
    """Unit tests for _collection_to_dict."""

    def _make_collection(self, *, has_asset=True, has_children=None):
        col = MagicMock()
        col.id = "test-col"
        col.title = "Test Collection"
        col.description = "A test."
        col.license = "CC-BY-4.0"
        col.keywords = ["geo", "test"]
        col.providers = []
        col.links = []
        col.summaries = None
        col.extra_fields = {}

        # Minimal extent
        spatial = MagicMock()
        spatial.bboxes = [[-180, -90, 180, 90]]
        temporal = MagicMock()
        from datetime import datetime, timezone
        temporal.intervals = [[datetime(2020, 1, 1, tzinfo=timezone.utc), None]]
        extent = MagicMock()
        extent.spatial = spatial
        extent.temporal = temporal
        col.extent = extent

        if has_asset:
            asset = MagicMock()
            asset.href = "https://s3-west.nrp-nautilus.io/bucket/data.parquet"
            asset.media_type = "application/x-parquet"
            asset.title = "Data"
            asset.description = None
            asset.extra_fields = {"file:size": 1073741824}  # 1 GiB
            col.assets = {"data": asset}
        else:
            col.assets = {}

        if has_children is not None:
            sub1 = MagicMock()
            sub1.id = "sub-col-1"
            col.get_children = MagicMock(return_value=has_children)
        return col

    def test_required_keys_present(self):
        col = self._make_collection()
        result = _collection_to_dict(col)
        for key in ("id", "title", "description", "license", "keywords",
                    "providers", "extent", "links", "summaries", "assets"):
            assert key in result, f"missing key: {key}"

    def test_href_converted_to_s3(self):
        col = self._make_collection()
        result = _collection_to_dict(col)
        assert result["assets"]["data"]["href"] == "s3://bucket/data.parquet"

    def test_asset_extra_fields_included(self):
        col = self._make_collection()
        result = _collection_to_dict(col)
        assert result["assets"]["data"]["file:size"] == 1073741824

    def test_children_populated_when_provided(self):
        sub = MagicMock()
        sub.id = "child-1"
        col = self._make_collection()
        result = _collection_to_dict(col, sub_children=[sub])
        assert result["children"] == ["child-1"]

    def test_children_absent_when_not_provided(self):
        col = self._make_collection()
        result = _collection_to_dict(col)
        assert "children" not in result

    def test_collection_level_table_columns_included(self):
        col = self._make_collection(has_asset=False)
        col.extra_fields = {"table:columns": [{"name": "h8", "type": "ubigint"}]}
        result = _collection_to_dict(col)
        assert "table:columns" in result
        assert result["table:columns"][0]["name"] == "h8"

    def test_nav_links_excluded(self):
        col = self._make_collection()
        for rel in ("root", "parent", "self", "child", "item"):
            lnk = MagicMock()
            lnk.rel = rel
            lnk.href = f"https://example.com/{rel}"
            lnk.title = None
        doc_link = MagicMock()
        doc_link.rel = "documentation"
        doc_link.href = "https://docs.example.com"
        doc_link.title = "Docs"
        col.links = [doc_link]
        result = _collection_to_dict(col)
        assert any(lnk["rel"] == "documentation" for lnk in result["links"])
        for lnk in result["links"]:
            assert lnk["rel"] not in {"root", "parent", "self", "child", "item"}

    def test_empty_assets(self):
        col = self._make_collection(has_asset=False)
        result = _collection_to_dict(col)
        assert result["assets"] == {}

    def test_extent_iso_format(self):
        col = self._make_collection()
        result = _collection_to_dict(col)
        intervals = result["extent"]["temporal"]["interval"]
        assert intervals[0][0].startswith("2020-01-01")
        assert intervals[0][1] is None


class TestGetCollection:
    """Tests for the get_collection function."""

    def _make_mock_catalog(self):
        mock_catalog = MagicMock()
        col = MagicMock()
        col.id = "custom-col"
        col.title = "Custom Collection"
        col.description = "For testing"
        col.license = "ODbL"
        col.keywords = []
        col.providers = []
        col.links = []
        col.summaries = None
        col.extra_fields = {}
        col.assets = {}
        spatial = MagicMock(); spatial.bboxes = []
        temporal = MagicMock(); temporal.intervals = []
        extent = MagicMock(); extent.spatial = spatial; extent.temporal = temporal
        col.extent = extent
        col.get_children.return_value = []
        mock_catalog.get_children.return_value = [col]
        return mock_catalog

    def test_returns_dict_with_id(self):
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()):
            result = get_collection("custom-col",
                                    catalog_url="https://example.com/catalog.json")
        assert isinstance(result, dict)
        assert result["id"] == "custom-col"

    def test_not_found_returns_error_dict(self):
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()):
            result = get_collection("nonexistent",
                                    catalog_url="https://example.com/catalog.json")
        assert "error" in result

    def test_catalog_error_returns_error_dict(self):
        with patch('stac.pystac.Catalog.from_file', side_effect=Exception("timeout")):
            result = get_collection("anything",
                                    catalog_url="https://example.com/catalog.json")
        assert "error" in result
        assert "timeout" in result["error"]

    def test_children_exposed_for_parent(self):
        mock_catalog = MagicMock()
        parent = MagicMock()
        parent.id = "parent-col"
        parent.title = "Parent"
        parent.description = ""
        parent.license = None
        parent.keywords = []
        parent.providers = []
        parent.links = []
        parent.summaries = None
        parent.extra_fields = {}
        parent.assets = {}
        spatial = MagicMock(); spatial.bboxes = []
        temporal = MagicMock(); temporal.intervals = []
        extent = MagicMock(); extent.spatial = spatial; extent.temporal = temporal
        parent.extent = extent

        child = MagicMock(); child.id = "child-col"
        child.title = "Child"; child.description = ""
        child.license = None; child.keywords = []; child.providers = []
        child.links = []; child.summaries = None; child.extra_fields = {}
        child.assets = {}; child.extent = extent
        child.get_children.return_value = []

        parent.get_children.return_value = [child]
        mock_catalog.get_children.return_value = [parent]

        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            result = get_collection("parent-col",
                                    catalog_url="https://example.com/catalog.json")
        assert result["children"] == ["child-col"]


class TestGetCollectionMCPTool:
    """Verify get_collection is registered as an MCP tool."""

    def test_get_collection_is_mcp_tool(self):
        from server import mcp
        import anyio
        tool_names = [t.name for t in anyio.run(mcp.list_tools)]
        assert "get_collection" in tool_names

