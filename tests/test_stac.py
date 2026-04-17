import importlib
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from stac import list_datasets, get_dataset, fetch_stac_catalog, get_collection, _collection_to_dict, _fuzzy_lookup


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
        assert result["children"] == ["child-col"]

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

    def test_fetch_concurrency_default_is_8(self, monkeypatch):
        """With no env var set, STAC_FETCH_CONCURRENCY defaults to 8."""
        monkeypatch.delenv("STAC_FETCH_CONCURRENCY", raising=False)
        import stac
        importlib.reload(stac)
        assert stac._STAC_FETCH_CONCURRENCY == 8

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

