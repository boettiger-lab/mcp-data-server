import importlib
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stac import list_datasets, get_dataset, fetch_stac_catalog, get_collection, _collection_to_dict, _fuzzy_lookup, _format_collection


class TestCatalogUrlParameter:
    """Test that list_datasets and get_dataset accept an optional catalog_url."""

    def _make_mock_catalog(self):
        """Return (catalog_mock, collection_mock) compatible with the link-walk interface."""
        mock_catalog = MagicMock()
        mock_col = MagicMock()
        mock_col.id = "custom-dataset"
        mock_col.title = "Custom Dataset"
        mock_col.description = "From custom catalog"
        mock_col.assets = {}
        mock_col.extra_fields = {}
        mock_col.links = []  # no sub-children
        # Root catalog exposes one child link
        child_link = MagicMock()
        child_link.rel = "child"
        child_link.href = "https://example.com/custom/custom-dataset/collection.json"
        child_link.title = "Custom Dataset"
        mock_catalog.links = [child_link]
        return mock_catalog, mock_col

    def test_list_datasets_custom_url(self):
        cat, col = self._make_mock_catalog()
        with patch('stac.pystac.Catalog.from_file', return_value=cat) as mock_from_file, \
             patch('stac.pystac.Collection.from_file', return_value=col):
            result = list_datasets(catalog_url="https://example.com/custom/catalog.json")
            args, kwargs = mock_from_file.call_args
            assert args[0] == "https://example.com/custom/catalog.json"
            assert "stac_io" in kwargs
            assert "custom-dataset" in result
            assert "https://example.com/custom/catalog.json" in result

    def test_get_dataset_custom_url(self):
        cat, col = self._make_mock_catalog()
        with patch('stac.pystac.Catalog.from_file', return_value=cat), \
             patch('stac.pystac.Collection.from_file', return_value=col):
            result = get_dataset("custom-dataset", catalog_url="https://example.com/custom/catalog.json")
            assert "Custom Dataset" in result

    def test_get_dataset_catalog_token_forwarded(self):
        """catalog_token is forwarded through get_dataset to the StacIO instance."""
        cat, col = self._make_mock_catalog()
        with patch('stac.pystac.Catalog.from_file', return_value=cat) as mock_from_file, \
             patch('stac.pystac.Collection.from_file', return_value=col):
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
        """Create (catalog, parent, child1, child2) mocks for the link-walk interface.

        The catalog has one child link pointing to the parent.
        The parent has two child links pointing to child1 and child2.
        """
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
        child1.links = []  # no grandchildren

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
        child2.links = []  # no grandchildren

        # Parent collection — has two sub-child links
        parent = MagicMock()
        parent.id = "us-census"
        parent.title = "US Census"
        parent.description = "Census boundary datasets"
        parent.assets = {}
        parent.extra_fields = {}
        sub1_link = MagicMock()
        sub1_link.rel = "child"
        sub1_link.href = "https://example.com/public-census/sldu/collection.json"
        sub1_link.title = None
        sub2_link = MagicMock()
        sub2_link.rel = "child"
        sub2_link.href = "https://example.com/public-census/cd/collection.json"
        sub2_link.title = None
        parent.links = [sub1_link, sub2_link]

        # Root catalog — one child link pointing to parent
        parent_link = MagicMock()
        parent_link.rel = "child"
        parent_link.href = "https://example.com/public-census/collection.json"
        parent_link.title = None
        mock_catalog.links = [parent_link]

        return mock_catalog, parent, child1, child2

    def _collection_side_effect(self, parent, child1, child2):
        """Return a side_effect for pystac.Collection.from_file given the mocks."""
        def _side_effect(href, *args, **kwargs):
            if "public-census/collection.json" in href:
                return parent
            if "sldu" in href:
                return child1
            if "/cd/" in href:
                return child2
            raise ValueError(f"Unexpected href: {href}")
        return _side_effect

    def test_child_collections_indexed(self):
        """fetch_stac_catalog indexes both parent and child collection IDs."""
        cat, parent, child1, child2 = self._make_mock_catalog_with_children()
        with patch('stac.pystac.Catalog.from_file', return_value=cat), \
             patch('stac.pystac.Collection.from_file',
                   side_effect=self._collection_side_effect(parent, child1, child2)):
            datasets = fetch_stac_catalog(catalog_url="https://example.com/catalog.json")
            assert "us-census" in datasets
            assert "census-2025-sldu" in datasets
            assert "census-2025-cd" in datasets

    def test_child_has_own_columns(self):
        """Child collection metadata contains its own column schema."""
        cat, parent, child1, child2 = self._make_mock_catalog_with_children()
        with patch('stac.pystac.Catalog.from_file', return_value=cat), \
             patch('stac.pystac.Collection.from_file',
                   side_effect=self._collection_side_effect(parent, child1, child2)):
            datasets = fetch_stac_catalog(catalog_url="https://example.com/catalog.json")
            sldu = datasets["census-2025-sldu"]
            assert "SLDUST" in sldu
            cd = datasets["census-2025-cd"]
            assert "CD119FP" in cd

    def test_parent_does_not_show_child_columns(self):
        """Parent collection no longer inherits an arbitrary child's columns."""
        cat, parent, child1, child2 = self._make_mock_catalog_with_children()
        with patch('stac.pystac.Catalog.from_file', return_value=cat), \
             patch('stac.pystac.Collection.from_file',
                   side_effect=self._collection_side_effect(parent, child1, child2)):
            datasets = fetch_stac_catalog(catalog_url="https://example.com/catalog.json")
            parent_md = datasets["us-census"]
            # Parent has no table:columns of its own; should not show child columns
            assert "SLDUST" not in parent_md
            assert "CD119FP" not in parent_md

    def test_get_dataset_with_child_id(self):
        """get_dataset accepts a child collection ID and returns its metadata."""
        cat, parent, child1, child2 = self._make_mock_catalog_with_children()
        with patch('stac.pystac.Catalog.from_file', return_value=cat), \
             patch('stac.pystac.Collection.from_file',
                   side_effect=self._collection_side_effect(parent, child1, child2)):
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
        # Contract (issue #105): children are emitted as nested dicts so the
        # output round-trips back into get_collection(collection=...).
        sub = self._make_collection()
        sub.id = "child-1"
        col = self._make_collection()
        result = _collection_to_dict(col, sub_children=[sub])
        assert isinstance(result["children"], list)
        assert isinstance(result["children"][0], dict)
        assert result["children"][0]["id"] == "child-1"

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
        nav_links = []
        for rel in ("root", "parent", "self", "child", "item"):
            lnk = MagicMock()
            lnk.rel = rel
            lnk.href = f"https://example.com/{rel}"
            lnk.title = None
            nav_links.append(lnk)
        doc_link = MagicMock()
        doc_link.rel = "documentation"
        doc_link.href = "https://docs.example.com"
        doc_link.title = "Docs"
        col.links = nav_links + [doc_link]
        result = _collection_to_dict(col)
        assert any(lnk["rel"] == "documentation" for lnk in result["links"])
        for lnk in result["links"]:
            assert lnk["rel"] not in {"root", "parent", "self", "child", "item"}

    def test_empty_assets(self):
        col = self._make_collection(has_asset=False)
        result = _collection_to_dict(col)
        assert result["assets"] == {}

    def test_none_values_stripped_from_top_level(self):
        col = self._make_collection()
        col.license = None
        col.description = None
        result = _collection_to_dict(col)
        assert "license" not in result
        assert "description" not in result
        # Non-None fields still present
        assert "id" in result
        assert "assets" in result

    def test_extent_iso_format(self):
        col = self._make_collection()
        result = _collection_to_dict(col)
        intervals = result["extent"]["temporal"]["interval"]
        assert intervals[0][0].startswith("2020-01-01")
        assert intervals[0][1] is None


class TestGetCollection:
    """Tests for the get_collection function."""

    def _make_minimal_col(self, col_id, title="", description="", license=None):
        """Helper to build a minimal mock pystac Collection."""
        col = MagicMock()
        col.id = col_id
        col.title = title
        col.description = description
        col.license = license
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
        return col

    def _make_mock_catalog(self):
        col = self._make_minimal_col("custom-col", title="Custom Collection",
                                     description="For testing", license="ODbL")
        mock_catalog = MagicMock()
        mock_catalog.get_child.return_value = col
        mock_catalog.get_children.return_value = [col]
        return mock_catalog

    def test_returns_dict_with_id(self):
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()):
            result = get_collection("custom-col",
                                    catalog_url="https://example.com/catalog.json")
        assert isinstance(result, dict)
        assert result["id"] == "custom-col"

    def test_not_found_returns_error_dict(self):
        mock_catalog = self._make_mock_catalog()
        mock_catalog.get_child.return_value = None  # direct lookup fails
        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
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
        child = self._make_minimal_col("child-col", title="Child")
        parent = self._make_minimal_col("parent-col", title="Parent")
        parent.get_children.return_value = [child]

        mock_catalog = MagicMock()
        mock_catalog.get_child.return_value = parent
        mock_catalog.get_children.return_value = [parent]

        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            result = get_collection("parent-col",
                                    catalog_url="https://example.com/catalog.json")
        assert isinstance(result["children"], list)
        assert result["children"][0]["id"] == "child-col"

    def test_finds_sub_child_by_exact_id(self):
        """get_collection finds a sub-child without iterating the whole catalog."""
        child = self._make_minimal_col("sub-dataset", title="Sub Dataset")
        parent = self._make_minimal_col("parent-col", title="Parent")
        parent.get_children.return_value = [child]

        mock_catalog = MagicMock()
        mock_catalog.get_child.return_value = None  # not top-level
        mock_catalog.get_children.return_value = [parent]

        with patch('stac.pystac.Catalog.from_file', return_value=mock_catalog):
            result = get_collection("sub-dataset",
                                    catalog_url="https://example.com/catalog.json")
        assert result["id"] == "sub-dataset"

    def test_fuzzy_prefix_match(self):
        """Prefix match: 'custom' finds 'custom-col'."""
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()) as m:
            m.return_value.get_child.return_value = None  # no exact match for 'custom'
            result = get_collection("custom",
                                    catalog_url="https://example.com/catalog.json")
        assert result["id"] == "custom-col"

    def test_fuzzy_substring_match(self):
        """Substring match: 'stom' finds 'custom-col'."""
        with patch('stac.pystac.Catalog.from_file', return_value=self._make_mock_catalog()) as m:
            m.return_value.get_child.return_value = None
            result = get_collection("stom",
                                    catalog_url="https://example.com/catalog.json")
        assert result["id"] == "custom-col"


class TestFuzzyLookup:
    """Tests for the _fuzzy_lookup helper."""

    def test_exact_match(self):
        assert _fuzzy_lookup({"a": 1, "ab": 2}, "a") == 1

    def test_prefix_match(self):
        assert _fuzzy_lookup({"census-2024-state": 1, "wdpa": 2}, "census") == 1

    def test_substring_match(self):
        assert _fuzzy_lookup({"us-census": 1}, "census") == 1

    def test_exact_preferred_over_prefix(self):
        m = {"census": "exact", "census-2024": "prefix"}
        assert _fuzzy_lookup(m, "census") == "exact"

    def test_prefix_preferred_over_substring(self):
        m = {"us-census": "substring", "census-2024": "prefix"}
        assert _fuzzy_lookup(m, "census") == "prefix"

    def test_no_match_returns_none(self):
        assert _fuzzy_lookup({"a": 1}, "z") is None


class TestGetCollectionDefaultCatalog:
    """Tests for get_collection against the default (cached) catalog."""

    def test_cache_miss_triggers_refetch(self):
        """When ID is not in _STAC_RAW, get_collection re-fetches and finds it."""
        import stac
        original_raw = dict(stac._STAC_RAW)
        try:
            stac._STAC_RAW.clear()
            fake_entry = {"id": "new-col", "title": "New"}
            with patch('stac.fetch_stac_catalog') as mock_fetch:
                def side_effect(*a, **kw):
                    stac._STAC_RAW["new-col"] = fake_entry
                    return {}
                mock_fetch.side_effect = side_effect
                result = get_collection("new-col")
            assert result["id"] == "new-col"
            mock_fetch.assert_called_once()
        finally:
            stac._STAC_RAW.clear()
            stac._STAC_RAW.update(original_raw)

    def test_fuzzy_match_on_default_catalog(self):
        """Fuzzy lookup works against the pre-populated _STAC_RAW cache."""
        import stac
        original_raw = dict(stac._STAC_RAW)
        try:
            stac._STAC_RAW.clear()
            stac._STAC_RAW["census-2024-state"] = {"id": "census-2024-state", "title": "State"}
            result = get_collection("census-2024")
            assert result["id"] == "census-2024-state"
        finally:
            stac._STAC_RAW.clear()
            stac._STAC_RAW.update(original_raw)


class TestGetCollectionMCPTool:
    """Verify get_collection is registered as an MCP tool."""

    def test_get_collection_is_mcp_tool(self):
        from server import mcp
        import anyio
        tool_names = [t.name for t in anyio.run(mcp.list_tools)]
        assert "get_collection" in tool_names


class TestFetchResilience:
    """Tests for per-child timeout, bounded parallelism, and partial-result handling
    added for mcp-data-server#65."""

    def test_root_timeout_default_is_15(self, monkeypatch):
        """With no env vars set, STAC_ROOT_TIMEOUT defaults to 15s."""
        monkeypatch.delenv("STAC_TIMEOUT", raising=False)
        monkeypatch.delenv("STAC_ROOT_TIMEOUT", raising=False)
        import stac
        importlib.reload(stac)
        assert stac._STAC_ROOT_TIMEOUT == 15

    def test_child_timeout_default_is_5(self, monkeypatch):
        """With no env vars set, STAC_CHILD_TIMEOUT defaults to 5s."""
        monkeypatch.delenv("STAC_TIMEOUT", raising=False)
        monkeypatch.delenv("STAC_CHILD_TIMEOUT", raising=False)
        import stac
        importlib.reload(stac)
        assert stac._STAC_CHILD_TIMEOUT == 5

    def test_fetch_concurrency_default_is_16(self, monkeypatch):
        """Default concurrency is 16 — sized so the main pool plus the retry pass
        both fit in the readiness-probe budget."""
        monkeypatch.delenv("STAC_FETCH_CONCURRENCY", raising=False)
        import stac
        importlib.reload(stac)
        assert stac._STAC_FETCH_CONCURRENCY == 16

    def test_stac_timeout_back_compat_applies_to_both(self, monkeypatch):
        """If only STAC_TIMEOUT is set, both root and child timeouts adopt its value."""
        monkeypatch.setenv("STAC_TIMEOUT", "10")
        monkeypatch.delenv("STAC_ROOT_TIMEOUT", raising=False)
        monkeypatch.delenv("STAC_CHILD_TIMEOUT", raising=False)
        import stac
        importlib.reload(stac)
        assert stac._STAC_ROOT_TIMEOUT == 10
        assert stac._STAC_CHILD_TIMEOUT == 10

    def test_new_vars_override_stac_timeout(self, monkeypatch):
        """When set, STAC_ROOT_TIMEOUT and STAC_CHILD_TIMEOUT take precedence."""
        monkeypatch.setenv("STAC_TIMEOUT", "10")
        monkeypatch.setenv("STAC_ROOT_TIMEOUT", "20")
        monkeypatch.setenv("STAC_CHILD_TIMEOUT", "3")
        import stac
        importlib.reload(stac)
        assert stac._STAC_ROOT_TIMEOUT == 20
        assert stac._STAC_CHILD_TIMEOUT == 3

    def test_stac_load_errors_exists(self):
        """Module exposes a STAC_LOAD_ERRORS dict for operators/tests to inspect."""
        import stac
        assert isinstance(stac.STAC_LOAD_ERRORS, dict)

    def test_timeout_stac_io_uses_configured_timeout(self):
        """_TimeoutStacIO passes its configured timeout to requests.get."""
        from unittest.mock import patch, MagicMock
        import stac

        io = stac._TimeoutStacIO(timeout=7)

        with patch("stac.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = "{}"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            io.read_text_from_href("https://example.com/foo.json")

        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        assert kwargs.get("timeout") == 7

    def test_timeout_stac_io_default_timeout_falls_back_to_child(self, monkeypatch):
        """Without an explicit timeout, _TimeoutStacIO uses _STAC_CHILD_TIMEOUT."""
        monkeypatch.setenv("STAC_CHILD_TIMEOUT", "3")
        monkeypatch.delenv("STAC_TIMEOUT", raising=False)
        monkeypatch.delenv("STAC_ROOT_TIMEOUT", raising=False)
        import stac
        importlib.reload(stac)

        io = stac._TimeoutStacIO()

        with patch("stac.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = "{}"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            io.read_text_from_href("https://example.com/foo.json")

        _, kwargs = mock_get.call_args
        assert kwargs.get("timeout") == 3

    def test_child_identifier_prefers_fetched_id(self):
        """When JSON parse succeeded and we have a real id, use it."""
        import stac
        assert stac._child_identifier(
            "https://s3-west/public-foo/stac-collection.json",
            title_hint="Foo",
            fetched_id="real-foo-id",
        ) == "real-foo-id"

    def test_child_identifier_falls_back_to_href_tail_when_no_id(self):
        """With no fetched id and no useful tail path, use the last segment."""
        import stac
        # Standard tail: the path-segment before /stac-collection.json
        assert stac._child_identifier(
            "https://s3-west/public-wyoming/stac-collection.json",
            title_hint=None,
            fetched_id=None,
        ) == "public-wyoming"

    def test_child_identifier_uses_title_when_href_tail_is_generic(self):
        """For hrefs like .../iplc-poly-stac.json, strip trailing '-stac.json' / '.json'."""
        import stac
        # The href ends in a file name rather than a directory; fall back to stem.
        assert stac._child_identifier(
            "https://s3-west/public-indigenous/landmark/iplc-poly-stac.json",
            title_hint=None,
            fetched_id=None,
        ) == "iplc-poly-stac"

    def test_child_identifier_combines_tail_and_title_if_title_given(self):
        """If a link title is available and distinct, include it for clarity."""
        import stac
        result = stac._child_identifier(
            "https://s3-west/public-wyoming/stac-collection.json",
            title_hint="Wyoming Wildlife",
            fetched_id=None,
        )
        assert "public-wyoming" in result
        assert "Wyoming Wildlife" in result

    def test_fetch_parent_success_returns_collection_and_subchild_hrefs(self):
        """On success: returns the Collection, a list of sub-child hrefs, and None for error."""
        from unittest.mock import patch, MagicMock
        import stac

        mock_col = MagicMock()
        mock_col.id = "test-parent"
        link1 = MagicMock(); link1.rel = "child"; link1.href = "https://example.com/child1.json"
        link2 = MagicMock(); link2.rel = "child"; link2.href = "https://example.com/child2.json"
        link3 = MagicMock(); link3.rel = "self";  link3.href = "https://example.com/self.json"
        mock_col.links = [link1, link2, link3]

        with patch("stac.pystac.Collection.from_file", return_value=mock_col):
            col, subchild_hrefs, error = stac._fetch_parent(
                "https://example.com/parent.json", title="Parent", token=None,
            )

        assert col is mock_col
        assert subchild_hrefs == [
            "https://example.com/child1.json",
            "https://example.com/child2.json",
        ]
        assert error is None

    def test_fetch_parent_timeout_returns_error_with_href_tail(self):
        """On timeout: returns (None, [], {ident: reason}) where ident is href-derived."""
        from unittest.mock import patch
        import requests
        import stac

        with patch(
            "stac.pystac.Collection.from_file",
            side_effect=requests.exceptions.Timeout("connection timed out"),
        ):
            col, subchild_hrefs, error = stac._fetch_parent(
                "https://example.com/public-wyoming/stac-collection.json",
                title=None, token=None,
            )

        assert col is None
        assert subchild_hrefs == []
        assert error is not None
        # Identifier should include the href tail segment
        assert "public-wyoming" in next(iter(error.keys()))
        # Reason should include the exception class name
        assert "Timeout" in next(iter(error.values()))

    def test_fetch_parent_catches_all_exceptions(self):
        """Any exception is caught; worker never raises."""
        from unittest.mock import patch
        import stac

        with patch(
            "stac.pystac.Collection.from_file",
            side_effect=ValueError("malformed JSON"),
        ):
            col, subchild_hrefs, error = stac._fetch_parent(
                "https://example.com/foo.json", title=None, token=None,
            )

        assert col is None
        assert error is not None
        assert "ValueError" in next(iter(error.values()))

    def test_fetch_subchild_success(self):
        """Sub-child worker returns (col, None) on success."""
        from unittest.mock import patch, MagicMock
        import stac

        mock_col = MagicMock()
        mock_col.id = "test-subchild"

        with patch("stac.pystac.Collection.from_file", return_value=mock_col):
            col, error = stac._fetch_subchild(
                "https://example.com/sub.json", parent_id="parent", token=None,
            )

        assert col is mock_col
        assert error is None

    def test_fetch_subchild_failure(self):
        """Sub-child worker returns (None, error) on failure."""
        from unittest.mock import patch
        import requests
        import stac

        with patch(
            "stac.pystac.Collection.from_file",
            side_effect=requests.exceptions.ConnectionError("conn refused"),
        ):
            col, error = stac._fetch_subchild(
                "https://example.com/public-foo/sub/stac-collection.json",
                parent_id="public-foo", token=None,
            )

        assert col is None
        assert error is not None
        assert "ConnectionError" in next(iter(error.values()))

    def _reset_module_state(self, stac_mod):
        """Reset module-level caches between tests to avoid cross-test pollution."""
        stac_mod.STAC_DATASETS.clear()
        stac_mod._STAC_RAW.clear()
        stac_mod.STAC_LOAD_ERRORS.clear()

    def _make_root_catalog(self, child_hrefs):
        """Build a MagicMock pystac.Catalog with the given child hrefs as 'child' links."""
        from unittest.mock import MagicMock
        cat = MagicMock()
        child_links = []
        for href in child_hrefs:
            link = MagicMock()
            link.rel = "child"
            link.href = href
            link.title = None
            child_links.append(link)
        cat.links = child_links
        cat.get_child_links = MagicMock(return_value=child_links)
        return cat

    def _make_leaf_collection(self, cid):
        """Build a MagicMock leaf pystac.Collection with no sub-children."""
        from unittest.mock import MagicMock
        col = MagicMock()
        col.id = cid
        col.title = cid
        col.description = f"Test collection {cid}"
        col.links = []
        col.assets = {}
        col.extra_fields = {}
        col.providers = []
        col.summaries = None
        col.keywords = []
        # Extent mock — minimal spatial + empty temporal
        spatial = MagicMock(); spatial.bboxes = [[-180, -90, 180, 90]]
        temporal = MagicMock(); temporal.intervals = []
        ext = MagicMock(); ext.spatial = spatial; ext.temporal = temporal
        col.extent = ext
        col.get_children = MagicMock(return_value=[])
        return col

    def test_fetch_root_failure_returns_empty_and_records_root_error(self):
        """When root fetch raises, return empty dict; STAC_LOAD_ERRORS['__root__'] is set."""
        from unittest.mock import patch
        import requests
        import stac

        self._reset_module_state(stac)

        with patch(
            "stac.pystac.Catalog.from_file",
            side_effect=requests.exceptions.Timeout("root timed out"),
        ):
            result = stac.fetch_stac_catalog()

        assert result == {}
        assert "__root__" in stac.STAC_LOAD_ERRORS
        assert "Timeout" in stac.STAC_LOAD_ERRORS["__root__"]

    def test_fetch_one_parent_fails_others_succeed(self):
        """One parent timing out does not block the others; its identifier enters STAC_LOAD_ERRORS."""
        from unittest.mock import patch
        import requests
        import stac

        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-a/stac-collection.json",
            "https://example.com/public-b/stac-collection.json",
            "https://example.com/public-c/stac-collection.json",
        ])
        # Pre-built leaf collections for a and c; b will raise.
        col_a = self._make_leaf_collection("a")
        col_c = self._make_leaf_collection("c")

        def collection_side_effect(href, *args, **kwargs):
            if "public-a" in href:
                return col_a
            if "public-c" in href:
                return col_c
            raise requests.exceptions.Timeout("b timed out")

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch("stac.pystac.Collection.from_file", side_effect=collection_side_effect):
            result = stac.fetch_stac_catalog()

        assert "a" in result
        assert "c" in result
        assert "b" not in result  # failed parent
        # One error recorded, keyed by the href's tail segment
        assert len(stac.STAC_LOAD_ERRORS) == 1
        assert any("public-b" in k for k in stac.STAC_LOAD_ERRORS.keys())

    def test_fetch_all_parents_fail(self):
        """When every child fetch fails, datasets is empty and every child is in errors."""
        from unittest.mock import patch
        import requests
        import stac

        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-a/stac-collection.json",
            "https://example.com/public-b/stac-collection.json",
        ])

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch(
                 "stac.pystac.Collection.from_file",
                 side_effect=requests.exceptions.Timeout("all dead"),
             ):
            result = stac.fetch_stac_catalog()

        assert result == {}
        assert len(stac.STAC_LOAD_ERRORS) == 2
        assert "__root__" not in stac.STAC_LOAD_ERRORS  # root succeeded

    def test_fetch_subchild_fails_parent_still_loads(self):
        """One failing sub-child does not kill its parent's entry."""
        from unittest.mock import patch, MagicMock
        import requests
        import stac

        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-parent/stac-collection.json",
        ])
        parent = self._make_leaf_collection("parent")
        # Parent has two sub-child links
        sub1_link = MagicMock(); sub1_link.rel = "child"
        sub1_link.href = "https://example.com/public-parent/sub1/stac-collection.json"
        sub1_link.title = None
        sub2_link = MagicMock(); sub2_link.rel = "child"
        sub2_link.href = "https://example.com/public-parent/sub2/stac-collection.json"
        sub2_link.title = None
        parent.links = [sub1_link, sub2_link]
        sub1 = self._make_leaf_collection("sub1")

        def collection_side_effect(href, *args, **kwargs):
            if href.endswith("/public-parent/stac-collection.json"):
                return parent
            if "sub1" in href:
                return sub1
            raise requests.exceptions.Timeout("sub2 dead")

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch("stac.pystac.Collection.from_file", side_effect=collection_side_effect):
            result = stac.fetch_stac_catalog()

        assert "parent" in result
        assert "sub1" in result
        assert "sub2" not in result
        assert any("sub2" in k for k in stac.STAC_LOAD_ERRORS.keys())

    def test_concurrency_env_var_honored(self, monkeypatch):
        """STAC_FETCH_CONCURRENCY=2 → ThreadPoolExecutor constructed with max_workers=2."""
        from unittest.mock import patch
        import stac

        monkeypatch.setenv("STAC_FETCH_CONCURRENCY", "2")
        importlib.reload(stac)
        self._reset_module_state(stac)

        cat = self._make_root_catalog([])  # no children — just verify executor arg

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch("stac.ThreadPoolExecutor") as mock_executor:
            mock_executor.return_value.__enter__.return_value.submit = lambda *a, **kw: None
            stac.fetch_stac_catalog()

        mock_executor.assert_called_once_with(max_workers=2)

    def test_list_datasets_footer_appears_when_errors_exist(self):
        """list_datasets() appends a ⚠️ footer listing failed ids + reasons."""
        import stac

        self._reset_module_state(stac)
        stac.STAC_DATASETS["alive-1"] = "**Alive 1**\nDescription 1"
        stac.STAC_DATASETS["alive-2"] = "**Alive 2**\nDescription 2"
        stac.STAC_LOAD_ERRORS["public-dead"] = "Timeout: connection timed out"
        stac.STAC_LOAD_ERRORS["public-other"] = "ConnectionError: conn refused"

        out = stac.list_datasets()

        assert "alive-1" in out
        assert "alive-2" in out
        # Footer content
        assert "⚠️" in out
        assert "could not be loaded" in out
        assert "public-dead" in out
        assert "public-other" in out

    def test_list_datasets_no_footer_when_no_errors(self):
        """When STAC_LOAD_ERRORS is empty, the footer is absent."""
        import stac

        self._reset_module_state(stac)
        stac.STAC_DATASETS["alive-1"] = "**Alive 1**\nDescription 1"

        out = stac.list_datasets()

        assert "⚠️" not in out
        assert "could not be loaded" not in out

    def test_cache_miss_refetch_with_all_children_failing_preserves_previous_cache(self):
        """When a refetch's root succeeds but all children fail, keep the previous
        STAC_DATASETS — don't wipe an existing good cache during an S3 incident.

        Regression guard for a final-review finding: the clear()/update() pattern
        previously wiped module state unconditionally on refetch.
        """
        from unittest.mock import patch
        import requests
        import stac

        self._reset_module_state(stac)
        # Seed a previously-loaded cache
        stac.STAC_DATASETS["known-dataset"] = "**Known**\nPreviously loaded"
        stac._STAC_RAW["known-dataset"] = {"id": "known-dataset"}

        cat = self._make_root_catalog([
            "https://example.com/public-new/stac-collection.json",
        ])

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch(
                 "stac.pystac.Collection.from_file",
                 side_effect=requests.exceptions.Timeout("all children dead"),
             ):
            result = stac.fetch_stac_catalog()

        # The new load returned nothing (all children failed)
        assert result == {}
        # But the previously-loaded cache is preserved
        assert "known-dataset" in stac.STAC_DATASETS
        # The failure is still recorded in errors
        assert len(stac.STAC_LOAD_ERRORS) == 1
        assert any("public-new" in k for k in stac.STAC_LOAD_ERRORS.keys())

    def test_child_retry_timeout_default_is_8(self, monkeypatch):
        """Retry pass uses a longer per-child timeout (default 8s) to rescue borderline slow children."""
        monkeypatch.delenv("STAC_CHILD_RETRY_TIMEOUT", raising=False)
        import stac
        importlib.reload(stac)
        assert stac._STAC_CHILD_RETRY_TIMEOUT == 8

    def test_retry_rescues_transient_parent_failure(self):
        """A parent that fails the first pass but succeeds on retry ends up in the catalog
        and out of STAC_LOAD_ERRORS."""
        from unittest.mock import patch
        import requests
        import stac

        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-flaky/stac-collection.json",
        ])
        col = self._make_leaf_collection("flaky")

        # First call raises Timeout; subsequent calls return the Collection.
        call_count = {"n": 0}
        def flaky_from_file(href, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.exceptions.Timeout("first pass slow")
            return col

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch("stac.pystac.Collection.from_file", side_effect=flaky_from_file):
            result = stac.fetch_stac_catalog()

        # Retry saved it
        assert "flaky" in result
        assert call_count["n"] == 2  # one failure + one retry
        # Nothing in the error dict — retry cleared the first-pass failure
        assert stac.STAC_LOAD_ERRORS == {}

    def test_retry_failure_is_final(self):
        """If both the initial attempt and the retry fail, the error stays in STAC_LOAD_ERRORS
        and the collection is missing from the catalog."""
        from unittest.mock import patch
        import requests
        import stac

        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-dead/stac-collection.json",
        ])

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch(
                 "stac.pystac.Collection.from_file",
                 side_effect=requests.exceptions.Timeout("always slow"),
             ):
            result = stac.fetch_stac_catalog()

        assert result == {}
        assert len(stac.STAC_LOAD_ERRORS) == 1
        assert any("public-dead" in k for k in stac.STAC_LOAD_ERRORS.keys())

    def test_retry_uses_child_retry_timeout(self, monkeypatch):
        """The retry pass constructs _TimeoutStacIO with _STAC_CHILD_RETRY_TIMEOUT, not _STAC_CHILD_TIMEOUT."""
        from unittest.mock import patch, MagicMock
        import requests
        import stac

        monkeypatch.setenv("STAC_CHILD_TIMEOUT", "5")
        monkeypatch.setenv("STAC_CHILD_RETRY_TIMEOUT", "8")
        importlib.reload(stac)
        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-flaky/stac-collection.json",
        ])
        col = self._make_leaf_collection("flaky")

        seen_timeouts = []
        orig_TimeoutStacIO = stac._TimeoutStacIO

        def capturing_stac_io(*args, **kwargs):
            seen_timeouts.append(kwargs.get("timeout"))
            return orig_TimeoutStacIO(*args, **kwargs)

        call_count = {"n": 0}
        def flaky_from_file(href, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.exceptions.Timeout("slow")
            return col

        with patch("stac._TimeoutStacIO", side_effect=capturing_stac_io), \
             patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch("stac.pystac.Collection.from_file", side_effect=flaky_from_file):
            stac.fetch_stac_catalog()

        # Filter out the root_io construction (timeout=_STAC_ROOT_TIMEOUT=15)
        # and look only at the child-fetch constructions.
        child_timeouts = [t for t in seen_timeouts if t != stac._STAC_ROOT_TIMEOUT]
        assert 5 in child_timeouts, f"expected first-pass child timeout (5s) in {child_timeouts}"
        assert 8 in child_timeouts, f"expected retry child timeout (8s) in {child_timeouts}"

    def test_retry_rescued_parent_fetches_its_subchildren(self):
        """When a parent-of-children fails the first pass and succeeds on retry,
        its sub-children must ALSO be fetched (in the retry pool). Otherwise the
        parent ends up indexed without any of its sub-datasets — observed on the
        2026-04-18 dev rollout where us-census retried but left 6 sub-collections
        absent.
        """
        from unittest.mock import patch, MagicMock
        import requests
        import stac

        self._reset_module_state(stac)

        cat = self._make_root_catalog([
            "https://example.com/public-census/stac-collection.json",
        ])

        # Parent has two sub-child links. Note: we must set `.links` (not just
        # rely on get_children) because the new loader walks `.links`.
        parent = self._make_leaf_collection("us-census")
        sub1_link = MagicMock(); sub1_link.rel = "child"
        sub1_link.href = "https://example.com/public-census/census-2024/state/stac-collection.json"
        sub1_link.title = None
        sub2_link = MagicMock(); sub2_link.rel = "child"
        sub2_link.href = "https://example.com/public-census/census-2024/county/stac-collection.json"
        sub2_link.title = None
        parent.links = [sub1_link, sub2_link]

        sub1 = self._make_leaf_collection("census-2024-state")
        sub2 = self._make_leaf_collection("census-2024-county")

        call_log = []

        def fetch_side_effect(href, *args, **kwargs):
            call_log.append(href)
            # Parent fails first attempt, succeeds second.
            if href.endswith("/public-census/stac-collection.json"):
                parent_attempts = [h for h in call_log if h.endswith("/public-census/stac-collection.json")]
                if len(parent_attempts) == 1:
                    raise requests.exceptions.Timeout("parent slow first time")
                return parent
            if "state" in href:
                return sub1
            if "county" in href:
                return sub2
            raise ValueError(f"Unexpected href: {href}")

        with patch("stac.pystac.Catalog.from_file", return_value=cat), \
             patch("stac.pystac.Collection.from_file", side_effect=fetch_side_effect):
            result = stac.fetch_stac_catalog()

        # Parent is in the catalog (retry rescued it)
        assert "us-census" in result, f"parent missing from result; got {list(result.keys())}"
        # Both sub-children are indexed — this is the behavior the fix guarantees
        assert "census-2024-state" in result, "sub-child 1 missing — retry didn't enqueue sub-children"
        assert "census-2024-county" in result, "sub-child 2 missing — retry didn't enqueue sub-children"
        # No errors — retry rescued everything cleanly
        assert stac.STAC_LOAD_ERRORS == {}


class TestFormatColumnsFullRender:
    """`_format_columns` must render all columns (no 20-entry cap) and include
    categorical `values` arrays. Regression test for issue #84.
    """

    def test_more_than_twenty_columns_all_rendered(self):
        from stac import _format_columns
        cols = [{"name": f"col_{i:02d}", "type": "varchar"} for i in range(30)]
        lines = _format_columns(cols)
        rendered = "\n".join(lines)
        # All 30 columns should appear, not just the first 20
        for i in range(30):
            assert f"col_{i:02d}" in rendered, f"col_{i:02d} missing — renderer truncated"

    def test_h3_columns_render_with_descriptions(self):
        """H3 index columns render inline with name/type/description like other columns,
        so models see the partition-key role of h0 instead of treating it as a small ordinal."""
        from stac import _format_columns
        cols = [{"name": f"col_{i:02d}", "type": "varchar"} for i in range(25)]
        cols += [
            {"name": "h0", "type": "ubigint",
             "description": "H3 cell ID at resolution 0, used as the partition key for hive-partitioned reads."},
            {"name": "h8", "type": "ubigint",
             "description": "H3 cell ID at resolution 8."},
            {"name": "h9", "type": "ubigint", "description": "H3 cell ID at resolution 9."},
            {"name": "h10", "type": "ubigint", "description": "H3 cell ID at resolution 10."},
        ]
        lines = _format_columns(cols)
        rendered = "\n".join(lines)
        assert "H3 index columns:" not in rendered, "H3 columns must not be collapsed to a name-only line"
        assert "`h0` (ubigint) — H3 cell ID at resolution 0" in rendered
        assert "`h8` (ubigint) — H3 cell ID at resolution 8" in rendered
        assert "`h9` (ubigint) — H3 cell ID at resolution 9" in rendered
        assert "`h10` (ubigint) — H3 cell ID at resolution 10" in rendered

    def test_categorical_values_rendered(self):
        """Columns with a `values` array have those values included in output."""
        from stac import _format_columns
        cols = [{
            "name": "owner_type",
            "type": "varchar",
            "description": "Land owner type",
            "values": ["public", "private", "tribal", "ngo"],
        }]
        lines = _format_columns(cols)
        rendered = "\n".join(lines)
        for v in ("public", "private", "tribal", "ngo"):
            assert v in rendered, f"value {v!r} missing from rendered output"


class TestInlineCollectionContent:
    """Inline `collection=` / `catalog=` parameters bypass the HTTP fetch — caller
    hands the already-loaded JSON over instead of letting the server re-fetch via
    `catalog_url`. See issue #105."""

    def _minimal_collection_dict(self, cid="inline-col", title="Inline", children=None):
        d = {
            "id": cid,
            "title": title,
            "description": "Provided inline.",
            "license": "CC-BY-4.0",
            "extent": {
                "spatial": {"bbox": [[-180, -90, 180, 90]]},
                "temporal": {"interval": [["2020-01-01T00:00:00+00:00", None]]},
            },
            "links": [],
            "assets": {
                "data": {
                    "href": "s3://bucket/data.parquet",
                    "type": "application/x-parquet",
                    "title": "Data",
                }
            },
        }
        if children is not None:
            d["children"] = children
        return d

    def test_get_collection_inline_returns_dict_no_http(self):
        with patch("stac.requests.get") as mock_get, \
             patch("stac.pystac.Catalog.from_file") as mock_cat, \
             patch("stac.pystac.Collection.from_file") as mock_col_file:
            result = get_collection(
                "inline-col",
                collection=self._minimal_collection_dict(),
            )
            mock_get.assert_not_called()
            mock_cat.assert_not_called()
            mock_col_file.assert_not_called()
        assert isinstance(result, dict)
        assert result["id"] == "inline-col"
        assert result["assets"]["data"]["href"] == "s3://bucket/data.parquet"

    def test_get_collection_inline_with_children_renders_nested(self):
        """Embedded children dicts surface as nested dicts in the result."""
        child = self._minimal_collection_dict(cid="child-a", title="Child A")
        parent = self._minimal_collection_dict(cid="parent-x", title="Parent X", children=[child])
        with patch("stac.requests.get") as mock_get, \
             patch("stac.pystac.Catalog.from_file") as mock_cat:
            result = get_collection("parent-x", collection=parent)
            mock_get.assert_not_called()
            mock_cat.assert_not_called()
        assert result["id"] == "parent-x"
        # Contract change: children is a list of nested dicts (not just IDs)
        assert isinstance(result["children"], list)
        assert isinstance(result["children"][0], dict)
        assert result["children"][0]["id"] == "child-a"

    def test_get_dataset_inline_returns_markdown_no_http(self):
        with patch("stac.requests.get") as mock_get, \
             patch("stac.pystac.Catalog.from_file") as mock_cat, \
             patch("stac.pystac.Collection.from_file") as mock_col_file:
            result = get_dataset(
                "inline-col",
                collection=self._minimal_collection_dict(title="Inline Markdown"),
            )
            mock_get.assert_not_called()
            mock_cat.assert_not_called()
            mock_col_file.assert_not_called()
        assert isinstance(result, str)
        assert "Inline Markdown" in result
        assert "s3://bucket/data.parquet" in result

    def test_get_dataset_inline_with_children_renders_sub_datasets(self):
        child = self._minimal_collection_dict(cid="child-a", title="Child A")
        parent = self._minimal_collection_dict(cid="parent-x", title="Parent X", children=[child])
        result = get_dataset("parent-x", collection=parent)
        assert "Sub-datasets" in result
        assert "Child A" in result or "child-a" in result

    def test_list_datasets_inline_catalog_no_http(self):
        """list_datasets(catalog=<dict>) renders without any HTTP fetch and
        indexes both parent and sub-collection IDs."""
        sub = self._minimal_collection_dict(cid="sub-a", title="Sub A")
        parent = self._minimal_collection_dict(cid="parent-x", title="Parent X", children=[sub])
        standalone = self._minimal_collection_dict(cid="standalone-y", title="Standalone Y")
        catalog = {
            "id": "inline-catalog",
            "title": "Inline Catalog",
            "description": "Provided inline",
            "children": [parent, standalone],
        }
        with patch("stac.requests.get") as mock_get, \
             patch("stac.pystac.Catalog.from_file") as mock_cat:
            result = list_datasets(catalog=catalog)
            mock_get.assert_not_called()
            mock_cat.assert_not_called()
        assert "parent-x" in result
        assert "sub-a" in result
        assert "standalone-y" in result

    def test_collection_to_dict_output_round_trips_through_inline(self):
        """The dict emitted by `_collection_to_dict` (with sub_children) is
        accepted as inline input to `get_collection` and reproduces the
        same identity / children IDs."""
        sub_col = MagicMock()
        sub_col.id = "sub-x"
        sub_col.title = "Sub X"
        sub_col.description = "Sub"
        sub_col.license = None
        sub_col.keywords = []
        sub_col.providers = []
        sub_col.links = []
        sub_col.summaries = None
        sub_col.extra_fields = {}
        sub_col.assets = {}
        from datetime import datetime, timezone
        spatial = MagicMock(); spatial.bboxes = [[-180, -90, 180, 90]]
        temporal = MagicMock(); temporal.intervals = [[datetime(2020, 1, 1, tzinfo=timezone.utc), None]]
        ext = MagicMock(); ext.spatial = spatial; ext.temporal = temporal
        sub_col.extent = ext

        parent_col = MagicMock()
        parent_col.id = "parent-x"
        parent_col.title = "Parent X"
        parent_col.description = "Parent"
        parent_col.license = "CC-BY-4.0"
        parent_col.keywords = []
        parent_col.providers = []
        parent_col.links = []
        parent_col.summaries = None
        parent_col.extra_fields = {}
        parent_col.assets = {}
        parent_col.extent = ext

        emitted = _collection_to_dict(parent_col, sub_children=[sub_col])

        # Feed the emitted dict back through the inline path
        round_tripped = get_collection("parent-x", collection=emitted)
        assert round_tripped["id"] == "parent-x"
        assert round_tripped["children"][0]["id"] == "sub-x"

    def test_get_collection_inline_precedence_over_catalog_url(self):
        """When both `collection` and `catalog_url` are passed, inline wins (no fetch)."""
        with patch("stac.requests.get") as mock_get, \
             patch("stac.pystac.Catalog.from_file") as mock_cat:
            result = get_collection(
                "inline-col",
                catalog_url="https://example.com/catalog.json",
                collection=self._minimal_collection_dict(),
            )
            mock_get.assert_not_called()
            mock_cat.assert_not_called()
        assert result["id"] == "inline-col"


class TestFormatCollectionUrl:
    """_format_collection surfaces the collection self href as collection_url."""

    def _make_col(self, self_href=None):
        col = MagicMock()
        col.id = "test-ds"
        col.title = "Test Dataset"
        col.description = "A test."
        col.assets = {}
        col.extra_fields = {}
        col.links = []
        col.get_self_href.return_value = self_href
        return col

    def test_collection_url_present_when_self_href_known(self):
        col = self._make_col(
            self_href="https://s3-west.nrp-nautilus.io/public-data/stac/test-ds/stac-collection.json"
        )
        result = _format_collection(col, sub_children=[])
        assert "collection_url" in result
        assert "https://s3-west.nrp-nautilus.io/public-data/stac/test-ds/stac-collection.json" in result

    def test_collection_url_absent_when_no_self_href(self):
        col = self._make_col(self_href=None)
        result = _format_collection(col, sub_children=[])
        assert "collection_url" not in result

    def test_sub_child_url_is_childs_own_href(self):
        """Sub-children surface their own self href, not the parent's."""
        child_href = "https://s3-west.nrp-nautilus.io/public-data/stac/parent/child/stac-collection.json"
        col = self._make_col(self_href=child_href)
        result = _format_collection(col, sub_children=[])
        assert child_href in result


class TestExtractParquetAssetsExtensions:
    """`_extract_parquet_assets` must render H3, raster, and vector extension
    fields from per-asset extra_fields. Regression test for issue #84.
    """

    def _make_collection_with_asset(self, asset_extra_fields: dict):
        col = MagicMock()
        col.id = "test-col"
        asset = MagicMock()
        asset.href = "https://s3-west.nrp-nautilus.io/bucket/hex/"
        asset.media_type = "application/x-parquet"
        asset.title = "hex"
        asset.extra_fields = asset_extra_fields
        col.assets = {"hex": asset}
        return col

    def test_h3_native_resolution_rendered(self):
        from stac import _extract_parquet_assets
        col = self._make_collection_with_asset({
            "h3:native_resolution": 10,
            "h3:parent_resolutions": [9, 8, 0],
        })
        rendered = "\n".join(_extract_parquet_assets(col))
        assert "h3:native_resolution" in rendered
        assert "10" in rendered

    def test_h3_parent_resolutions_rendered(self):
        from stac import _extract_parquet_assets
        col = self._make_collection_with_asset({
            "h3:native_resolution": 10,
            "h3:parent_resolutions": [9, 8, 0],
        })
        rendered = "\n".join(_extract_parquet_assets(col))
        assert "h3:parent_resolutions" in rendered
        # All parent resolutions should be present
        for r in (9, 8, 0):
            assert str(r) in rendered

    def test_no_h3_fields_when_absent(self):
        """Asset without h3 fields should not render h3 lines."""
        from stac import _extract_parquet_assets
        col = self._make_collection_with_asset({})
        rendered = "\n".join(_extract_parquet_assets(col))
        assert "h3:native_resolution" not in rendered
        assert "h3:parent_resolutions" not in rendered

