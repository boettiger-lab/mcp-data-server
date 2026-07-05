"""Tests for the s3config source registry (#264): data-driven href rewriting,
metadata rerouting, per-source secrets, and route hints for unknown hosts."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import s3config


class TestGetSources:
    def test_builtins_present(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        names = [s["name"] for s in s3config.get_sources()]
        assert "nrp_ceph" in names
        assert "source_coop" in names

    def test_env_appends_new_source(self, monkeypatch):
        monkeypatch.setenv(
            "S3_SOURCES",
            '[{"name": "campus_minio", "https_prefix": "https://minio.example.edu/",'
            ' "s3_prefix": "s3://", "secret": {"endpoint": "minio.example.edu",'
            ' "scope": "s3://mirror-"}}]',
        )
        sources = {s["name"]: s for s in s3config.get_sources()}
        assert "campus_minio" in sources
        assert "source_coop" in sources  # built-ins retained

    def test_env_merges_over_builtin_by_name(self, monkeypatch):
        monkeypatch.setenv(
            "S3_SOURCES",
            '[{"name": "source_coop", "s3_prefix": "s3://other-mirror/"}]',
        )
        src = {s["name"]: s for s in s3config.get_sources()}["source_coop"]
        assert src["s3_prefix"] == "s3://other-mirror/"
        # Untouched keys survive the merge.
        assert src["https_prefix"] == "https://data.source.coop/"

    def test_env_disabled_removes_builtin(self, monkeypatch):
        monkeypatch.setenv("S3_SOURCES", '[{"name": "source_coop", "disabled": true}]')
        names = [s["name"] for s in s3config.get_sources()]
        assert "source_coop" not in names

    def test_malformed_env_ignored_with_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("S3_SOURCES", "not json")
        names = [s["name"] for s in s3config.get_sources()]
        assert "nrp_ceph" in names  # built-ins still served
        assert "S3_SOURCES" in capsys.readouterr().err


class TestRewriteHref:
    def test_ceph_strip(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        assert (
            s3config.rewrite_href("https://s3-west.nrp-nautilus.io/public-carbon/x.parquet")
            == "s3://public-carbon/x.parquet"
        )

    def test_source_coop_gateway(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        assert (
            s3config.rewrite_href("https://data.source.coop/cboettig/carbon/h0=*/d.parquet")
            == "s3://us-west-2.opendata.source.coop/cboettig/carbon/h0=*/d.parquet"
        )

    def test_unknown_host_passthrough(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        href = "https://example.com/other.parquet"
        assert s3config.rewrite_href(href) == href

    def test_env_source_rewrites(self, monkeypatch):
        monkeypatch.setenv(
            "S3_SOURCES",
            '[{"name": "campus_minio", "https_prefix": "https://minio.example.edu/",'
            ' "s3_prefix": "s3://"}]',
        )
        assert (
            s3config.rewrite_href("https://minio.example.edu/mirror-x/y.parquet")
            == "s3://mirror-x/y.parquet"
        )


class TestMetadataHref:
    def test_nrp_reroutes_to_internal(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        assert (
            s3config.metadata_href("https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json")
            == "http://rook-ceph-rgw-nautiluss3.rook/public-data/stac/catalog.json"
        )

    def test_s3_endpoint_url_env_honored(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3-west.nrp-nautilus.io")
        assert s3config.metadata_href(
            "https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json"
        ) == "https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json"

    def test_source_coop_metadata_not_rerouted(self, monkeypatch):
        """data.source.coop serves STAC JSON fine over HTTPS — only the DATA
        path needs the bucket-form rewrite, so metadata_href must not touch it."""
        monkeypatch.delenv("S3_SOURCES", raising=False)
        href = "https://data.source.coop/cboettig/carbon/collection.json"
        assert s3config.metadata_href(href) == href


class TestSourceSecretSql:
    def test_source_coop_secret_generated(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        stmts = s3config.source_secret_sql()
        coop = [s for s in stmts if "SECRET source_coop" in s]
        assert len(coop) == 1
        assert "SCOPE 's3://us-west-2.opendata.source.coop'" in coop[0]
        assert "REGION 'us-west-2'" in coop[0]
        assert "ENDPOINT 's3.us-west-2.amazonaws.com'" in coop[0]

    def test_nrp_has_no_secret(self, monkeypatch):
        """NRP paths route via the deployment default `s3` secret — the registry
        entry must not create a competing one."""
        monkeypatch.delenv("S3_SOURCES", raising=False)
        assert not any("nrp" in s for s in s3config.source_secret_sql())

    def test_env_source_secret_with_ssl_inference(self, monkeypatch):
        monkeypatch.setenv(
            "S3_SOURCES",
            '[{"name": "my-mirror!", "secret": {"endpoint": "minio.example.edu",'
            ' "scope": "s3://mirror-"}}]',
        )
        stmt = [s for s in s3config.source_secret_sql() if "minio.example.edu" in s][0]
        assert "SECRET my_mirror_ " in stmt  # name sanitized to an identifier
        assert "USE_SSL 'true'" in stmt  # inferred: non-rook = https

    def test_values_are_quote_escaped(self, monkeypatch):
        monkeypatch.setenv(
            "S3_SOURCES",
            '[{"name": "q", "secret": {"endpoint": "e.example", "scope": "s3://a\'b"}}]',
        )
        stmt = [s for s in s3config.source_secret_sql() if "e.example" in s][0]
        assert "s3://a''b" in stmt


class TestRouteHint:
    def test_path_style(self):
        hint = s3config.route_hint("https://minio.other.edu/public-x/hex/h0=*/d.parquet")
        assert hint == {
            "path": "s3://public-x/hex/h0=*/d.parquet",
            "endpoint": "minio.other.edu",
            "scope": "s3://public-x",
        }

    def test_vhost_aws(self):
        hint = s3config.route_hint("https://mybucket.s3.us-west-2.amazonaws.com/data/x.parquet")
        assert hint["path"] == "s3://mybucket/data/x.parquet"
        assert hint["endpoint"] == "s3.us-west-2.amazonaws.com"
        assert hint["scope"] == "s3://mybucket"

    def test_no_key_returns_none(self):
        """A bare https file at host root has no bucket/key split to derive."""
        assert s3config.route_hint("https://example.com/other.parquet") is None

    def test_non_https_returns_none(self):
        assert s3config.route_hint("s3://already/fine.parquet") is None
        assert s3config.route_hint("/local/path.parquet") is None

    def test_directory_key_preserves_trailing_slash(self):
        hint = s3config.route_hint("https://minio.other.edu/mirror-x/data/")
        assert hint["path"] == "s3://mirror-x/data/"
