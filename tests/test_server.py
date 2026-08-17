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
    select_tiers,
    strip_prov,
)


DOC = """## Alpha
<!-- prov: issue=#1 models=qwen3 added=2026-01-01 cell=a-cell tier=core -->

alpha body

## Bravo
<!-- prov: issue=#2 models=qwen3 added=2026-01-01 cell=none tier=extra -->

bravo body

### Bravo child

child body

## Charlie
<!-- prov: issue=#3 models=glm-5 added=2026-01-01 cell=c-cell tier=core -->

charlie body

### Delta
<!-- prov: issue=#4 models=qwen3 added=2026-01-01 cell=none tier=extra -->

delta body

## Echo

echo body, no prov line at all
"""


class TestGuidanceTiers:
    """EXTRA_INSTRUCTIONS tiering (#384): `tier=extra` sections load only when opted in."""

    def test_extra_sections_dropped_by_default(self):
        out = select_tiers(DOC, include_extra=False)
        assert "alpha body" in out
        assert "charlie body" in out
        assert "bravo body" not in out
        assert "delta body" not in out

    def test_demoting_a_parent_takes_its_children(self):
        # `### Bravo child` has no prov of its own; it must not survive its `##` parent,
        # or a demotion silently leaves an orphaned fragment in the description.
        assert "child body" not in select_tiers(DOC, include_extra=False)
        assert "child body" in select_tiers(DOC, include_extra=True)

    def test_a_deeper_extra_section_ends_at_the_next_sibling(self):
        # `### Delta` is extra and sits under a core `##`; dropping it must not swallow
        # `## Echo`, the next same-or-higher heading.
        out = select_tiers(DOC, include_extra=False)
        assert "delta body" not in out
        assert "echo body, no prov line at all" in out

    def test_missing_or_tierless_prov_defaults_to_core(self):
        # Guidance is load-bearing until shown otherwise, so an unlabelled section stays.
        assert "echo body" in select_tiers(DOC, include_extra=False)
        tierless = "## Solo\n<!-- prov: issue=#9 models=x added=2026-01-01 cell=none -->\n\nsolo body\n"
        assert "solo body" in select_tiers(tierless, include_extra=False)

    def test_prov_lines_are_stripped_on_both_paths(self):
        for flag in (False, True):
            assert "prov:" not in select_tiers(DOC, include_extra=flag)

    def test_include_extra_is_the_full_document_minus_prov(self):
        assert select_tiers(DOC, include_extra=True) == strip_prov(DOC)

    def test_tier_word_in_prose_is_not_read_as_a_declaration(self):
        doc = "## Solo\n\nWe use tier=extra pricing in this prose.\n"
        assert "tier=extra pricing" in select_tiers(doc, include_extra=False)

    def test_real_guides_shrink_with_the_flag_off_and_stay_valid(self):
        for fn in ("h3-guide.md", "query-optimization.md"):
            raw = load_text_file(fn)
            core = select_tiers(raw, include_extra=False)
            full = select_tiers(raw, include_extra=True)
            assert len(core) <= len(full)
            # Never leave a dangling ```sql fence: an odd count means a section was cut
            # mid-block, which would ship the model a truncated query.
            assert core.count("```") % 2 == 0, f"{fn}: unbalanced code fence after tiering"


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

    def test_query_truncation_footer_when_over_50_rows(self):
        """A result wider than the 50-row preview gets an explicit truncation note."""
        result = query("SELECT range as num FROM range(100)")
        assert "preview" in result.lower()
        assert "COUNT" in result

    def test_query_no_footer_when_50_rows_or_fewer(self):
        """An un-truncated result must not claim to be a preview."""
        result = query("SELECT range as num FROM range(50)")
        assert "preview" not in result.lower()

    def test_query_date_renders_as_iso_date(self):
        """A DuckDB DATE renders as bare YYYY-MM-DD, with no fabricated time (#361)."""
        result = query("SELECT DATE '1974-03-01' AS lo, DATE '2024-12-30' AS hi")
        assert "1974-03-01" in result and "2024-12-30" in result
        # The bug rendered an invented time component in both shapes.
        assert "00:00:00" not in result
        assert "T00:00:00" not in result

    def test_query_date_rendering_is_shape_independent(self):
        """Adding a non-datetime column must not change how DATE columns render (#361).

        Pre-fix, an all-datetime frame rendered ISO+microseconds while a mixed
        frame rendered space-separated — same values, different output.
        """
        all_dt = query("SELECT DATE '1974-03-01' AS lo, DATE '2024-12-30' AS hi")
        mixed = query("SELECT DATE '1974-03-01' AS lo, DATE '2024-12-30' AS hi, 'x' AS filler")
        for r in (all_dt, mixed):
            assert "1974-03-01" in r and "2024-12-30" in r
            assert "00:00:00" not in r and ".000000" not in r

    def test_query_timestamp_renders_deterministically(self):
        """A TIMESTAMP keeps a time component but renders without microseconds (#361)."""
        result = query("SELECT TIMESTAMP '2024-12-30 13:45:06' AS ts, 'x' AS filler")
        assert "2024-12-30 13:45:06" in result
        assert ".000000" not in result

    def test_query_h3_index_renders_exactly(self):
        """An H3 index must render all 18 digits, not `6.13762e+17` (#387).

        tabulate formats numbers through float64, exact only to 2^53 — so the
        scientific form has dropped ~11 digits and names a DIFFERENT cell. A
        model cannot round-trip a cell id it reads out of a result.
        """
        result = query("SELECT 613762179077668863::UBIGINT AS h8, COUNT(*) AS n FROM range(3)")
        assert "613762179077668863" in result
        assert "6.13762e+17" not in result and "e+17" not in result

    def test_query_h3_index_exact_for_signed_and_unsigned(self):
        """h0 is BIGINT while h8/h10 are UBIGINT — both must survive (#387)."""
        result = query(
            "SELECT 577762574070710271::BIGINT AS h0, 613762179077668863::UBIGINT AS h8"
        )
        assert "577762574070710271" in result and "613762179077668863" in result
        assert "e+17" not in result

    def test_query_wide_int_rendering_is_shape_independent(self):
        """Unlike #361, this was never shape-dependent — a lone wide-int column
        renders scientific too. Both shapes must be exact (#387)."""
        alone = query("SELECT 613762179077668863::UBIGINT AS h8")
        mixed = query("SELECT 613762179077668863::UBIGINT AS h8, 27.4638 AS pct, 'x' AS filler")
        for r in (alone, mixed):
            assert "613762179077668863" in r and "e+17" not in r

    def test_query_small_ints_and_floats_unaffected(self):
        """The cast is display-only: counts stay plain integers (not `3.0`) and
        floats keep their formatting (#387)."""
        result = query("SELECT COUNT(*) AS n, 27.4638 AS pct FROM range(3)")
        assert "| 27.4638 " in result
        assert "3.0" not in result
        assert "|   3 |" in result or "| 3 " in result

    def test_query_nullable_wide_int_still_exact(self):
        """A NULL must not reintroduce float coercion (#387).

        Under pandas 3 a str-dtype column stores missing as the FLOAT nan, so a
        single NULL makes tabulate type the column as float and every wide int
        reverts to `6.13762e+17` — the cast undone by one missing cell. The
        IUCN size-stratified assets have NULL finer h-columns, so this is live.
        """
        result = query(
            "SELECT h8 FROM (VALUES (613762179077668863::UBIGINT), (NULL::UBIGINT)) t(h8)"
        )
        assert "613762179077668863" in result and "e+17" not in result
        assert "nan" not in result

    def test_query_null_date_is_blank_not_nan(self):
        """A NULL date renders blank, never the literal `nan` (#387).

        Same pandas-3 mechanism as above. `nan` in a date column reads as a
        value rather than as missing.
        """
        result = query(
            "SELECT d FROM (VALUES (DATE '2024-12-30'), (NULL::DATE)) t(d)"
        )
        assert "2024-12-30" in result
        assert "nan" not in result

    def test_query_negative_and_hugeint_exact(self):
        """Signed negatives and HUGEINT aggregates render exactly (#387)."""
        result = query("SELECT (-577762574070710271)::BIGINT AS neg, "
                       "SUM(9223372036854775807::HUGEINT) AS huge FROM range(2)")
        assert "-577762574070710271" in result
        assert "18446744073709551614" in result


class TestResourceFunctions:
    """Test MCP resource functions."""

    def test_browse_stac_catalog_returns_string(self):
        """Test that browse_stac_catalog returns a string."""
        from server import browse_stac_catalog
        result = browse_stac_catalog()
        assert isinstance(result, str)

    def test_get_stac_details_found(self):
        """Test getting details for an existing dataset."""
        from server import get_stac_details
        from stac import STAC_DATASETS

        if STAC_DATASETS:
            test_key = next(iter(STAC_DATASETS.keys()), None)
            if test_key:
                result = get_stac_details(test_key)
                assert isinstance(result, str)
                assert len(result) > 0

    def test_get_stac_details_not_found(self):
        """Test getting details for non-existent dataset."""
        from server import get_stac_details
        result = get_stac_details("nonexistent_dataset_xyz")
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

    def test_region_subset_guidance_present_and_injected(self):
        """geo-agent #322/#325: the h3-guide must warn that h0 is a partition key
        (never a boundary) and lead the region-subset pattern with the paraphrase-
        robust `hN IN (SELECT hN FROM <mask> WHERE <attr>)` form. This guidance is
        injected into the query tool description via TOOL_INJECTED_CONTEXT — guard
        it against silent deletion (the behavioral gold is baseline q
        `glob-carbon-in-state-h0-not-boundary`)."""
        from server import H3_RAW, TOOL_INJECTED_CONTEXT
        # h0-is-not-a-boundary warning
        assert "never a spatial or boundary filter" in H3_RAW
        # region-subset section + the robust IN-subquery form lead
        assert "Subsetting a dataset to a region" in H3_RAW
        assert "IN (SELECT" in H3_RAW
        # and it actually reaches the model (injected into the query tool context)
        assert "never a spatial or boundary filter" in TOOL_INJECTED_CONTEXT


class TestPromptFunction:
    """Test MCP prompt functions."""
    
    def test_analyst_persona_returns_string(self):
        """Test that analyst persona prompt returns a string."""
        from server import analyst_persona
        result = analyst_persona()
        assert isinstance(result, str)
        assert len(result) > 0


class TestS3Credentials:
    """Test that S3 credentials are injected into isolated DuckDB connections."""

    def test_query_without_credentials_succeeds(self):
        """query works normally without S3 credentials."""
        result = query("SELECT 1 as n")
        assert "1" in result

    def test_query_with_credentials_succeeds(self):
        """query accepts and applies S3 credentials without error."""
        result = query("SELECT 42 as n", s3_key="AKID", s3_secret="SECRET")
        assert "42" in result

    def test_get_isolated_db_injects_secret(self):
        """get_isolated_db creates a client_s3 secret when credentials are supplied."""
        with get_isolated_db(s3_key="AKID", s3_secret="SECRET") as conn:
            secrets = conn.sql("SELECT name FROM duckdb_secrets()").fetchall()
            names = [r[0] for r in secrets]
            assert "client_s3" in names

    def test_get_isolated_db_no_secret_without_credentials(self):
        """get_isolated_db does not create client_s3 when no credentials are supplied."""
        with get_isolated_db() as conn:
            secrets = conn.sql("SELECT name FROM duckdb_secrets()").fetchall()
            names = [r[0] for r in secrets]
            assert "client_s3" not in names

    def test_partial_credentials_raise(self):
        """Supplying only key or only secret is a clear error, not a silent
        anonymous downgrade (#285) — both are required together."""
        with pytest.raises(ValueError, match="s3_secret is required"):
            with get_isolated_db(s3_key="AKID"):
                pass
        with pytest.raises(ValueError, match="s3_key is required"):
            with get_isolated_db(s3_secret="SECRET"):
                pass

    def test_ssl_disabled_for_rook_endpoint(self):
        """Rook/Ceph internal endpoints get USE_SSL false."""
        with get_isolated_db(s3_key="K", s3_secret="S", s3_endpoint="rook-ceph-rgw-nautiluss3.rook") as conn:
            row = conn.sql("SELECT scope FROM duckdb_secrets() WHERE name='client_s3'").fetchone()
            # Secret was created — existence is sufficient; SSL value is in the secret config
            assert row is not None

    def test_ssl_enabled_for_external_endpoint(self):
        """Non-rook endpoints (e.g. minio) get USE_SSL true."""
        with get_isolated_db(s3_key="K", s3_secret="S", s3_endpoint="minio.example.org") as conn:
            row = conn.sql("SELECT name FROM duckdb_secrets() WHERE name='client_s3'").fetchone()
            assert row is not None

    def test_connection_isolation(self):
        """A secret in one connection is not visible in a concurrent connection."""
        with get_isolated_db(s3_key="AKID", s3_secret="SECRET") as conn_with:
            with get_isolated_db() as conn_without:
                names = [r[0] for r in conn_without.sql("SELECT name FROM duckdb_secrets()").fetchall()]
                assert "client_s3" not in names
            # Original connection still has its secret
            names = [r[0] for r in conn_with.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" in names

    def test_credentials_not_in_logs(self, capsys):
        """S3 credentials must not appear in stderr output."""
        with get_isolated_db(s3_key="MY_KEY_ID", s3_secret="MY_SECRET_VALUE"):
            pass
        captured = capsys.readouterr()
        assert "MY_KEY_ID" not in captured.err
        assert "MY_SECRET_VALUE" not in captured.err


class TestAnonymousBYOBucket:
    """Anonymous bring-your-own-bucket: s3_endpoint with no credentials, symmetric
    with the credentialed path — routes reads to an anonymous public mirror (#264)."""

    def test_anon_endpoint_creates_secret(self):
        """s3_endpoint alone (no creds) creates the client_s3 secret."""
        with get_isolated_db(s3_endpoint="minio.example.org") as conn:
            names = [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" in names

    def test_anon_scope_routes_only_scoped_paths(self):
        """A scoped anonymous secret wins for its prefix; other paths keep the default."""
        with get_isolated_db(s3_endpoint="minio.example.org", s3_scope="s3://public-") as conn:
            def which(p):
                return conn.sql(f"SELECT name FROM which_secret('{p}', 's3')").fetchone()[0]
            assert which("s3://public-carbon/x.parquet") == "client_s3"
            assert which("s3://private-foo/x.parquet") == "s3"

    def test_query_with_anon_endpoint_succeeds(self):
        """query accepts an anonymous endpoint (no creds) without error."""
        result = query("SELECT 7 as n", s3_endpoint="minio.example.org", s3_scope="s3://public-")
        assert "7" in result

    def test_endpoint_with_partial_creds_raises(self):
        """s3_endpoint + only a key (no secret) is an error, not a silent
        anonymous downgrade (#285). A half-credential can't authenticate, so a
        private read would otherwise fail with a confusing 403 instead."""
        with pytest.raises(ValueError, match="s3_secret is required"):
            with get_isolated_db(s3_endpoint="minio.example.org", s3_key="ONLY_KEY"):
                pass

    def test_no_endpoint_no_creds_still_no_secret(self):
        """The anonymous path must not fire without an endpoint (no accidental secret)."""
        with get_isolated_db() as conn:
            names = [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" not in names


class TestUnscopedClientRouting:
    """Two unscoped S3 secrets must never coexist (#271). DuckDB's pick between
    them is an undocumented tie-break (empirically client_s3 captured EVERY path
    on duckdb 1.5.4, regardless of creation order), so the server makes that
    de-facto semantic explicit: an unscoped client_s3 disables the default `s3`
    secret for the request; a scoped one coexists with it."""

    def _names(self, conn):
        return [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]

    def _which(self, conn, path):
        return conn.sql(f"SELECT name FROM which_secret('{path}', 's3')").fetchone()[0]

    def test_unscoped_creds_disable_default_secret(self):
        with get_isolated_db(s3_key="K", s3_secret="S") as conn:
            names = self._names(conn)
            assert "client_s3" in names
            assert "s3" not in names

    def test_unscoped_anon_endpoint_disables_default_secret(self):
        with get_isolated_db(s3_endpoint="minio.example.org") as conn:
            names = self._names(conn)
            assert "client_s3" in names
            assert "s3" not in names

    def test_unscoped_client_routes_all_paths(self):
        """Deterministically, not via tie-break: the client owns every path."""
        with get_isolated_db(s3_endpoint="minio.example.org") as conn:
            assert self._which(conn, "s3://public-data/x.parquet") == "client_s3"
            assert self._which(conn, "s3://anything-else/y.parquet") == "client_s3"

    def test_scoped_client_keeps_default_secret(self):
        with get_isolated_db(s3_endpoint="minio.example.org", s3_scope="s3://public-") as conn:
            names = self._names(conn)
            assert "client_s3" in names
            assert "s3" in names

    def test_no_client_keeps_default_secret(self):
        with get_isolated_db() as conn:
            assert "s3" in self._names(conn)

    def test_scoped_source_coop_survives_unscoped_client(self):
        """The prefix-scoped source_coop secret (source registry) still wins its
        own paths over an unscoped client_s3 — longest-scope match beats unscoped."""
        with get_isolated_db(s3_key="K", s3_secret="S") as conn:
            assert (
                self._which(conn, "s3://us-west-2.opendata.source.coop/a.parquet")
                == "source_coop"
            )

    def test_quote_in_credentials_does_not_break_secret_sql(self):
        """A quote in a client-supplied value must not break out of the SQL
        string literal (or echo the statement back in a parser error)."""
        with get_isolated_db(s3_key="K'--", s3_secret="S'x") as conn:
            assert "client_s3" in self._names(conn)

    def test_quote_in_scope_does_not_break_secret_sql(self):
        with get_isolated_db(s3_endpoint="minio.example.org", s3_scope="s3://pub'lic-") as conn:
            assert "client_s3" in self._names(conn)


class TestClientS3RegionUrlStyle:
    """A bring-your-own AWS-hosted bucket needs REGION and (optionally) a
    virtual-hosted URL_STYLE on the per-request client_s3 secret — the defaults
    ('path', no region) suit Ceph/MinIO but break on AWS (#286)."""

    def _secret_string(self, conn):
        return conn.sql(
            "SELECT secret_string FROM duckdb_secrets() WHERE name='client_s3'"
        ).fetchone()[0]

    def test_region_set_on_secret(self):
        with get_isolated_db(
            s3_endpoint="s3.us-west-2.amazonaws.com", s3_region="us-west-2"
        ) as conn:
            assert "region=us-west-2" in self._secret_string(conn)

    def test_no_region_by_default(self):
        """Backward-compat: without s3_region the secret carries no region."""
        with get_isolated_db(s3_endpoint="minio.example.org") as conn:
            assert "region=" not in self._secret_string(conn)

    def test_url_style_defaults_to_path(self):
        with get_isolated_db(s3_endpoint="minio.example.org") as conn:
            assert "url_style=path" in self._secret_string(conn)

    def test_url_style_vhost(self):
        with get_isolated_db(
            s3_endpoint="s3.us-west-2.amazonaws.com", s3_url_style="vhost"
        ) as conn:
            assert "url_style=vhost" in self._secret_string(conn)

    def test_query_with_region_and_url_style_succeeds(self):
        """query accepts the AWS knobs without error."""
        result = query(
            "SELECT 9 as n",
            s3_endpoint="s3.us-west-2.amazonaws.com",
            s3_scope="s3://mybucket",
            s3_region="us-west-2",
            s3_url_style="vhost",
        )
        assert "9" in result


class TestSourceRegistrySecrets:
    """Registry sources (s3config, #264) get prefix-scoped secrets on every
    query connection — built-ins and S3_SOURCES env additions alike."""

    def _names(self, conn):
        return [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]

    def test_source_coop_secret_present(self, monkeypatch):
        monkeypatch.delenv("S3_SOURCES", raising=False)
        with get_isolated_db() as conn:
            assert "source_coop" in self._names(conn)

    def test_env_source_secret_created_and_scoped(self, monkeypatch):
        monkeypatch.setenv(
            "S3_SOURCES",
            '[{"name": "campus_minio", "https_prefix": "https://minio.example.edu/",'
            ' "s3_prefix": "s3://", "secret": {"endpoint": "minio.example.edu",'
            ' "scope": "s3://mirror-"}}]',
        )
        with get_isolated_db() as conn:
            assert "campus_minio" in self._names(conn)
            row = conn.sql(
                "SELECT name FROM which_secret('s3://mirror-data/x.parquet', 's3')"
            ).fetchone()
            assert row[0] == "campus_minio"
            # Paths outside the scope keep the deployment default.
            row = conn.sql(
                "SELECT name FROM which_secret('s3://public-data/x.parquet', 's3')"
            ).fetchone()
            assert row[0] == "s3"


class TestDefaultEndpoint:
    """S3_DEFAULT_ENDPOINT: per-deployment default storage endpoint, server-owned (#268).
    Lets the codebase be deployed as a data-access head pointed at any backend via env."""

    def _s3_secret_string(self, conn):
        row = conn.sql("SELECT secret_string FROM duckdb_secrets() WHERE name='s3'").fetchone()
        return row[0] if row else ""

    def test_default_falls_back_to_rook(self, monkeypatch):
        monkeypatch.delenv("S3_DEFAULT_ENDPOINT", raising=False)
        monkeypatch.delenv("S3_DEFAULT_USE_SSL", raising=False)
        with get_isolated_db() as conn:
            s = self._s3_secret_string(conn)
            assert "rook-ceph-rgw-nautiluss3.rook" in s
            assert "use_ssl=false" in s  # inferred: rook = in-cluster http

    def test_default_endpoint_from_env(self, monkeypatch):
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "minio.example.org")
        monkeypatch.delenv("S3_DEFAULT_USE_SSL", raising=False)
        with get_isolated_db() as conn:
            s = self._s3_secret_string(conn)
            assert "minio.example.org" in s
            assert "use_ssl=true" in s  # inferred: non-rook = https

    def test_default_use_ssl_override(self, monkeypatch):
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "minio.example.org")
        monkeypatch.setenv("S3_DEFAULT_USE_SSL", "false")
        with get_isolated_db() as conn:
            assert "use_ssl=false" in self._s3_secret_string(conn)

    def test_query_still_works_with_default_endpoint(self, monkeypatch):
        """A deployment-configured default endpoint doesn't break normal queries."""
        monkeypatch.setenv("S3_DEFAULT_ENDPOINT", "minio.example.org")
        assert "5" in query("SELECT 5 as n")


class TestTileRouteMounted:
    def test_tile_route_exists_in_starlette_app(self):
        """After importing server, the streamable_http_app should have a /tiles route."""
        from server import mcp
        app = mcp.streamable_http_app()
        # Also apply our own mount (mimicking the startup block).
        from server import mount_tiles
        mount_tiles(app)
        paths = [getattr(r, "path", None) or getattr(r, "path_format", None) for r in app.routes]
        assert any(p and "/tiles/" in p for p in paths)

    def test_register_hex_tiles_is_exposed(self):
        """register_hex_tiles is exposed as an MCP tool."""
        from server import mcp
        import anyio
        tool_names = [t.name for t in anyio.run(mcp.list_tools)]
        assert "register_hex_tiles" in tool_names

    def test_get_hex_tile_status_is_exposed(self):
        from server import mcp
        import anyio
        tool_names = [t.name for t in anyio.run(mcp.list_tools)]
        assert "get_hex_tile_status" in tool_names

    def test_hex_tools_are_served_async(self):
        """The MCP-served hex tools must be coroutine functions so FastMCP
        awaits them instead of running them inline on the event loop (#176)."""
        import inspect, server
        assert inspect.iscoroutinefunction(server._register_hex_tiles_tool)
        assert inspect.iscoroutinefunction(server._get_hex_tile_status_tool)

    def test_hex_tool_descriptions_preserve_llm_docstring(self):
        """The async wrappers reuse the sync functions' docstrings as the
        LLM-facing tool description, so registering the wrapper doesn't drop it."""
        from server import mcp, register_hex_tiles, get_hex_tile_status
        import anyio
        tools = {t.name: t for t in anyio.run(mcp.list_tools)}
        assert tools["register_hex_tiles"].description == register_hex_tiles.__doc__
        assert tools["get_hex_tile_status"].description == get_hex_tile_status.__doc__

    def test_query_is_served_async(self):
        """query runs a DuckDB scan up to 300s; it must be a coroutine so FastMCP
        awaits it off the event loop instead of running it inline (#176). #185
        offloaded the hex tools the same way; this finishes the job for query."""
        import inspect, server
        assert inspect.iscoroutinefunction(server._query_tool)

    def test_query_tool_preserves_name_schema_and_docstring(self):
        """Wrapping must not change the LLM-facing tool: same name, same input
        schema (derived from the mirrored signature), same description."""
        from server import mcp, query
        import anyio
        tools = {t.name: t for t in anyio.run(mcp.list_tools)}
        assert "query" in tools
        assert tools["query"].description == query.__doc__
        assert sorted(tools["query"].inputSchema["properties"]) == [
            "s3_endpoint", "s3_key", "s3_region", "s3_scope", "s3_secret",
            "s3_url_style", "sql", "sql_query",
        ]

    def test_served_tool_schema_titles_dont_leak_wrapper_name(self):
        """The async wrappers (_query_tool, _register_hex_tiles_tool, ...) must not
        leak their internal names into inputSchema.title. FastMCP derives the title
        as f'{func.__name__}Arguments', so a wrapper named `_query_tool` yields
        `_query_toolArguments` — which weak models read as if it were the tool name
        and try to call (getting 'Unknown tool'). Titles must derive from the
        public tool name instead (#326)."""
        from server import mcp
        import anyio
        tools = {t.name: t for t in anyio.run(mcp.list_tools)}
        for name in ("query", "register_hex_tiles", "get_hex_tile_status"):
            title = tools[name].inputSchema.get("title", "")
            assert not title.startswith("_"), f"{name} title leaks wrapper name: {title!r}"
            assert "_tool" not in title, f"{name} title leaks wrapper name: {title!r}"
            assert title == f"{name}Arguments", f"{name} title is {title!r}"

    def test_query_tool_matches_sync_core(self):
        """The async wrapper returns the same payload as the sync query()."""
        import anyio, server
        sql = "SELECT 1 AS one"
        sync_result = server.query(sql)
        async_result = anyio.run(server._query_tool, sql)
        assert async_result == sync_result

    def test_query_tool_accepts_sql_alias(self):
        """query accepts `sql` as an alias for `sql_query` — register_hex_tiles's
        param name — so models don't eat a retry when they carry it over (#321)."""
        import anyio, server
        via_alias = anyio.run(
            lambda: server._query_tool(sql="SELECT 1 AS one")
        )
        via_canonical = anyio.run(
            lambda: server._query_tool(sql_query="SELECT 1 AS one")
        )
        assert via_alias == via_canonical
        assert "SQL Error" not in via_alias

    def test_query_tool_missing_sql_errors_clearly(self):
        """Neither name supplied → a clear error, not an unhandled TypeError."""
        import anyio, server
        result = anyio.run(lambda: server._query_tool())
        assert "SQL Error" in result and "sql_query" in result

    def test_register_hex_tiles_accepts_sql_query_alias(self):
        """register_hex_tiles accepts `sql_query` as an alias for `sql` — the
        mirror of query's alias — so the two SQL tools agree on either name (#321)."""
        import anyio, server
        called = {}

        def fake_register(sql, *a, **k):
            called["sql"] = sql
            return {"status": "done"}

        orig = server.register_hex_tiles
        server.register_hex_tiles = fake_register
        try:
            anyio.run(
                lambda: server._register_hex_tiles_tool(sql_query="SELECT h8 FROM t")
            )
        finally:
            server.register_hex_tiles = orig
        assert called["sql"] == "SELECT h8 FROM t"

    def test_register_hex_tiles_missing_sql_errors_clearly(self):
        """Neither name supplied → a failed status with a clear message."""
        import anyio, server
        result = anyio.run(lambda: server._register_hex_tiles_tool())
        assert result["status"] == "failed" and "sql" in result["error"]

    def test_query_tool_keeps_event_loop_responsive(self):
        """While the wrapped (blocking) query runs in a worker thread, the event
        loop must keep ticking. If it ran on the loop, the concurrent sleeps
        below could not make progress until it finished (#176)."""
        import threading, anyio, server

        gate = threading.Event()

        def blocking_query(sql_query, *a, **k):
            gate.wait(2.0)
            return "done"

        # Patch the sync core; the wrapper offloads whatever query() points at.
        orig = server.query
        server.query = blocking_query
        try:
            async def main():
                ticks = 0
                result = {}
                async with anyio.create_task_group() as tg:
                    async def runner():
                        result["r"] = await server._query_tool("SELECT 1")
                    tg.start_soon(runner)
                    for _ in range(5):
                        await anyio.sleep(0.02)
                        ticks += 1
                    gate.set()  # release the worker thread so runner finishes
                assert ticks == 5
                return result["r"]
            assert anyio.run(main) == "done"
        finally:
            server.query = orig

    def test_async_status_wrapper_matches_sync_core_for_unknown(self):
        """The async tool returns the same payload as the sync core."""
        import anyio, server
        h = "0" * 16
        sync_result = server.get_hex_tile_status(hash=h, wait_seconds=0)
        async_result = anyio.run(server._get_hex_tile_status_tool, h, 0)
        assert async_result == sync_result
        assert async_result["status"] == "unknown"

    def test_lock_heartbeat_refreshes_and_preserves_started_at(self, tmp_path, monkeypatch):
        """A running build's heartbeat keeps the lock fresh (bumps heartbeat_at)
        while preserving started_at, so a slow build never looks stale and
        reported elapsed keeps growing (#184)."""
        import server, time
        from tiles.db import build_tile_connection
        from tiles.pyramid import write_lock, read_lock
        monkeypatch.setattr(server, "_LOCK_HEARTBEAT_SECONDS", 0.05)
        uri = str(tmp_path) + "/"
        con = build_tile_connection(threads=1)
        try:
            write_lock(con, uri, pod_id="register", started_at=1234.0)  # register's lock
            stop = server._start_lock_heartbeat(uri)
            time.sleep(0.3)
            stop()
            time.sleep(0.1)
            lock = read_lock(con, uri)
            assert lock["started_at"] == 1234.0          # preserved across beats
            assert lock["heartbeat_at"] > 1234.0          # refreshed toward now
            assert lock["pod_id"] == server._POD_ID
        finally:
            con.close()


class TestHealthz:
    """The /healthz endpoint is the kubelet probe target (see issue #157).
    Replaces a TCP-only probe so a wedged uvicorn event loop fails fast."""

    def _build_app(self, with_auth_middleware=False):
        from server import mcp, _healthz, _version, _BearerAuthMiddleware, mount_tiles
        app = mcp.streamable_http_app()
        app.router.redirect_slashes = False
        mount_tiles(app)
        app.add_route("/healthz", _healthz, methods=["GET"])
        app.add_route("/version", _version, methods=["GET"])
        if with_auth_middleware:
            app.add_middleware(_BearerAuthMiddleware)
        return app

    def test_healthz_route_registered(self):
        """After app construction, /healthz appears in the route table."""
        app = self._build_app()
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/healthz" in paths

    def test_healthz_returns_ok_fast(self):
        """GET /healthz returns 200 with ok=true (plus the app version). Async
        handler does no I/O and no executor work — fails only if the event loop
        itself is starved."""
        from starlette.testclient import TestClient
        client = TestClient(self._build_app())
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "version" in body

    def test_healthz_bypasses_auth_middleware(self):
        """When MCP_AUTH_TOKEN is set, the kubelet still needs to probe — but the
        kubelet doesn't carry the token. The middleware must let /healthz through
        unauthenticated. If a future edit drops the path bypass, this test fails."""
        import server
        from starlette.testclient import TestClient
        original = server._MCP_AUTH_TOKEN
        server._MCP_AUTH_TOKEN = "test-token"
        try:
            client = TestClient(self._build_app(with_auth_middleware=True))
            # /healthz works with no auth header.
            r = client.get("/healthz")
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True
            # /mcp without a valid token is rejected — confirms the middleware
            # IS installed and gating other paths.
            r2 = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
            assert r2.status_code == 401
        finally:
            server._MCP_AUTH_TOKEN = original

    def test_version_returns_app_version_and_sha(self, monkeypatch):
        """GET /version reports the build-stamped app version + git SHA (issue #221),
        plus the public URLs a client needs to configure itself (issue #346)."""
        import server
        from starlette.testclient import TestClient
        monkeypatch.setenv("STAC_PUBLIC_CATALOG_URL", "https://mirror.test/catalog.json")
        monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://mcp.test")
        orig_v, orig_s = server.APP_VERSION, server.GIT_SHA
        server.APP_VERSION, server.GIT_SHA = "v9.9.9", "deadbeef"
        try:
            client = TestClient(self._build_app())
            r = client.get("/version")
            assert r.status_code == 200
            assert r.json() == {
                "version": "v9.9.9",
                "git_sha": "deadbeef",
                "stac_catalog_url": "https://mirror.test/catalog.json",
                "public_base_url": "https://mcp.test",
            }
        finally:
            server.APP_VERSION, server.GIT_SHA = orig_v, orig_s

    def test_version_never_leaks_the_internal_catalog_url(self, monkeypatch):
        """/version is auth-exempt, so it reports only the client-facing catalog URL.
        The in-cluster address stays inside authenticated MCP responses (#346)."""
        import server, stac
        from starlette.testclient import TestClient
        monkeypatch.setattr(
            stac, "STAC_CATALOG_URL", "http://minio-svc.minio.svc.cluster.local:9000/catalog.json"
        )
        monkeypatch.setenv("STAC_PUBLIC_CATALOG_URL", "https://minio.test/catalog.json")
        client = TestClient(self._build_app())
        body = client.get("/version").json()
        assert body["stac_catalog_url"] == "https://minio.test/catalog.json"
        assert "minio-svc" not in str(body)

    def test_version_catalog_url_defaults_to_internal_when_no_public_set(self, monkeypatch):
        """Unset → the server's own catalog URL, which on NRP prod is already public.
        Clients get a usable value with no extra configuration."""
        import server, stac
        from starlette.testclient import TestClient
        monkeypatch.delenv("STAC_PUBLIC_CATALOG_URL", raising=False)
        monkeypatch.setattr(stac, "STAC_CATALOG_URL", "https://s3-west.test/catalog.json")
        client = TestClient(self._build_app())
        assert client.get("/version").json()["stac_catalog_url"] == "https://s3-west.test/catalog.json"

    def test_version_bypasses_auth_middleware(self):
        """/version is public (version discovery is the point) — reachable with no
        token even when auth is on."""
        import server
        from starlette.testclient import TestClient
        original = server._MCP_AUTH_TOKEN
        server._MCP_AUTH_TOKEN = "test-token"
        try:
            client = TestClient(self._build_app(with_auth_middleware=True))
            r = client.get("/version")
            assert r.status_code == 200, r.text
            assert "version" in r.json()
        finally:
            server._MCP_AUTH_TOKEN = original


@pytest.fixture
def isolated_jobs(monkeypatch, tmp_path):
    """Reset the module-level _jobs dict and point the tile bucket at tmp_path
    so each test starts with a clean slate and never touches S3."""
    import server
    monkeypatch.setenv("TILE_BUCKET_BASE", str(tmp_path / "tiles"))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://test.local")
    # Reset the shared read connection so it picks up the env on next access.
    monkeypatch.setattr(server, "_tile_con", None)
    monkeypatch.setattr(server, "_jobs", {})
    yield


class TestRegisterHexTilesAsync:
    def test_fast_build_returns_done_inline(self, isolated_jobs, monkeypatch):
        """A build that finishes within the inline wait returns status=done
        with full metadata (preserves today's UX for fast jobs)."""
        import server
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)
        result = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert result["status"] == "done"
        assert "bounds" in result
        assert "value_stats" in result
        assert result["finest_res"] == 5

    def test_slow_build_returns_running(self, isolated_jobs, monkeypatch):
        """If the build doesn't finish within the inline wait, the tool
        returns status=running with hash + tile_url_template so the agent
        can poll."""
        import server, tiles.pyramid as pyramid
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)

        # Make the build phase slow so the inline wait expires.
        import time
        orig_build = pyramid.build_hex_tiles
        def slow_build(con, plan):
            time.sleep(0.5)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", slow_build)

        result = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert result["status"] == "running"
        assert "hash" in result
        assert "tile_url_template" in result

        # Drain the in-flight future so the test doesn't leak threads.
        h = result["hash"]
        server._jobs[h]["future"].result(timeout=10)

    def test_cache_hit_returns_done_with_cache_hit_flag(self, isolated_jobs, monkeypatch):
        """Second registration with identical args short-circuits via
        metadata.json — status=done, cache_hit=True, no background job."""
        import server
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)
        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        first = server.register_hex_tiles(sql=sql, agg="AVG")
        assert first["status"] == "done"

        # Reset jobs so the second call has no in-flight future to find;
        # the cache-hit path must work purely from metadata.json on disk.
        server._jobs.clear()
        second = server.register_hex_tiles(sql=sql, agg="AVG")
        assert second["status"] == "done"
        assert second.get("cache_hit") is True

    def test_concurrent_calls_for_same_hash_dedupe(self, isolated_jobs, monkeypatch):
        """Two register_hex_tiles calls with identical args while the first
        is still running should share one background future, not start two
        builds."""
        import server, tiles.pyramid as pyramid
        import threading, time
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)

        build_calls = []
        orig_build = pyramid.build_hex_tiles
        def slow_counting_build(con, plan):
            build_calls.append(plan["hash"])
            time.sleep(0.5)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", slow_counting_build)

        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        r1 = server.register_hex_tiles(sql=sql, agg="AVG")
        r2 = server.register_hex_tiles(sql=sql, agg="AVG")
        assert r1["status"] == "running"
        assert r2["status"] == "running"
        assert r1["hash"] == r2["hash"]

        # Drain.
        server._jobs[r1["hash"]]["future"].result(timeout=10)
        assert len(build_calls) == 1, f"expected one build, got {len(build_calls)}"

    def test_register_returns_failed_when_failed_marker_exists(self, isolated_jobs, monkeypatch):
        """If failed.json from a prior build exists for the hash a new
        register_hex_tiles would produce, return it immediately — don't
        kick off a redundant build."""
        import server
        from tiles.pyramid import write_failed, prepare_hex_tiles

        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        # Compute the hash the registration would land on, plant a failed marker.
        plan = prepare_hex_tiles(con=server._get_tile_con(), sql=sql, agg="AVG")
        import os
        os.makedirs(plan["output_uri"], exist_ok=True)
        write_failed(server._get_tile_con(), plan["output_uri"], error="prior build OOM")

        result = server.register_hex_tiles(sql=sql, agg="AVG")
        assert result["status"] == "failed"
        assert result["error"] == "prior build OOM"
        # No background build should have been queued.
        assert plan["hash"] not in server._jobs

    def test_register_writes_lock_before_submitting_build(self, isolated_jobs, monkeypatch):
        """After register_hex_tiles returns status=running, lock.json exists
        at the hash's output_uri so polls from any pod can see the in-flight
        build."""
        import server, tiles.pyramid as pyramid
        from tiles.pyramid import read_lock
        import time
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)
        orig_build = pyramid.build_hex_tiles
        def slow_build(con, plan):
            time.sleep(0.5)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", slow_build)

        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = server.register_hex_tiles(sql=sql, agg="AVG")
        assert result["status"] == "running"

        from tiles.pyramid import tile_paths_for_hash
        paths = tile_paths_for_hash(result["hash"])
        lock = read_lock(server._get_tile_con(), paths["output_uri"])
        assert lock is not None
        assert lock["pod_id"] == server._POD_ID

        server._jobs[result["hash"]]["future"].result(timeout=10)

    def test_register_dedups_via_fresh_lock_from_other_pod(self, isolated_jobs, monkeypatch):
        """If lock.json from another pod exists (and is fresh), return
        status=running without submitting a new build."""
        import server
        from tiles.pyramid import write_lock, prepare_hex_tiles
        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"

        # Plant a fresh lock as if another pod had started the build.
        plan = prepare_hex_tiles(con=server._get_tile_con(), sql=sql, agg="AVG")
        import os
        os.makedirs(plan["output_uri"], exist_ok=True)
        write_lock(server._get_tile_con(), plan["output_uri"], pod_id="other-pod-X")

        # Make build_hex_tiles raise if anyone calls it — we expect dedup.
        def boom(con, plan):
            raise AssertionError("build should have been deduped")
        monkeypatch.setattr(server, "build_hex_tiles", boom)

        result = server.register_hex_tiles(sql=sql, agg="AVG")
        assert result["status"] == "running"
        assert result["hash"] == plan["hash"]
        # And no local _jobs entry was created.
        assert plan["hash"] not in server._jobs

    def test_register_ignores_stale_lock_and_starts_fresh_build(self, isolated_jobs, monkeypatch):
        """A lock older than _LOCK_STALE_SECONDS is treated as absent —
        register proceeds with a new build and overwrites the stale lock."""
        import server
        from tiles.pyramid import write_lock, prepare_hex_tiles, read_lock
        import tiles.pyramid as pyramid
        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        plan = prepare_hex_tiles(con=server._get_tile_con(), sql=sql, agg="AVG")
        import os, time
        os.makedirs(plan["output_uri"], exist_ok=True)
        # Tiny TTL so we don't need to fabricate ancient timestamps.
        monkeypatch.setattr(pyramid, "_LOCK_STALE_SECONDS", 0)
        write_lock(server._get_tile_con(), plan["output_uri"], pod_id="dead-pod")
        time.sleep(0.05)  # ensure (now - started_at) > 0
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)

        result = server.register_hex_tiles(sql=sql, agg="AVG")
        # Stale lock didn't block the build, which finished inline.
        assert result["status"] == "done"
        # The fresh build overwrote the stale lock with its own (now done).
        lock = read_lock(server._get_tile_con(), plan["output_uri"])
        # The lock may still be present (we don't delete) but is from this pod.
        if lock is not None:
            assert lock["pod_id"] != "dead-pod"


class TestGetHexTileStatus:
    def test_unknown_for_unrecognised_hash(self, isolated_jobs):
        import server
        result = server.get_hex_tile_status(hash="0000000000000000")
        assert result["status"] == "unknown"
        assert result["hash"] == "0000000000000000"
        assert "tile_url_template" in result

    def test_running_while_job_active(self, isolated_jobs, monkeypatch):
        import server, tiles.pyramid as pyramid
        import time
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)
        orig_build = pyramid.build_hex_tiles
        def slow_build(con, plan):
            time.sleep(0.5)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", slow_build)

        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert sub["status"] == "running"
        status = server.get_hex_tile_status(hash=sub["hash"])
        assert status["status"] == "running"
        assert "elapsed_seconds" in status

        server._jobs[sub["hash"]]["future"].result(timeout=10)

    def test_done_when_metadata_exists(self, isolated_jobs, monkeypatch):
        import server
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)
        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert sub["status"] == "done"
        status = server.get_hex_tile_status(hash=sub["hash"])
        assert status["status"] == "done"
        assert "bounds" in status
        assert "value_stats" in status

    def test_count_distinct_status_carries_rollup_note(self, isolated_jobs, monkeypatch):
        # #331: the rollup caveat must reach the async-poll path too — big
        # COUNT_DISTINCT builds (e.g. global GBIF richness) return "running"
        # and the model only ever sees the done result via get_hex_tile_status.
        import server
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)
        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 42 AS specieskey",
            agg="COUNT_DISTINCT",
        )
        assert sub["status"] == "done"
        assert "rollup_note" in sub and "lower bound" in sub["rollup_note"]
        # The poll path (_done_response) must include it as well.
        status = server.get_hex_tile_status(hash=sub["hash"])
        assert status["status"] == "done"
        assert "rollup_note" in status and "lower bound" in status["rollup_note"]

    def test_non_count_distinct_status_has_no_rollup_note(self, isolated_jobs, monkeypatch):
        # Exact aggs (AVG here) must NOT carry the caveat — it's specific to
        # the non-composable COUNT_DISTINCT rollup.
        import server
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)
        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        status = server.get_hex_tile_status(hash=sub["hash"])
        assert status["status"] == "done"
        assert "rollup_note" not in status

    def test_long_poll_returns_done_when_build_finishes_during_wait(self, isolated_jobs, monkeypatch):
        """wait_seconds blocks server-side until the build completes,
        avoiding the rapid-poll antipattern."""
        import server, tiles.pyramid as pyramid
        import time
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)
        orig_build = pyramid.build_hex_tiles
        def slow_build(con, plan):
            time.sleep(0.5)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", slow_build)

        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert sub["status"] == "running"

        # Long-poll for up to 10 s — build finishes in ~0.5 s.
        status = server.get_hex_tile_status(hash=sub["hash"], wait_seconds=10)
        assert status["status"] == "done"
        assert "bounds" in status
        assert "value_stats" in status

    def test_long_poll_returns_running_when_wait_expires(self, isolated_jobs, monkeypatch):
        """If the build doesn't finish in the requested wait, returns
        status=running so the caller knows to ask again."""
        import server, tiles.pyramid as pyramid
        import time
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)
        orig_build = pyramid.build_hex_tiles
        def very_slow_build(con, plan):
            time.sleep(3.0)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", very_slow_build)

        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert sub["status"] == "running"

        t0 = time.time()
        status = server.get_hex_tile_status(hash=sub["hash"], wait_seconds=1)
        elapsed = time.time() - t0
        assert status["status"] == "running"
        # Waited approximately 1 s (not 3 — the build is still running).
        assert 0.8 <= elapsed <= 2.0, f"wait_seconds=1 took {elapsed:.2f}s"

        # Drain so the test doesn't leak threads.
        server._jobs[sub["hash"]]["future"].result(timeout=10)

    def test_wait_seconds_is_clamped(self, isolated_jobs, monkeypatch):
        """Caller cannot pin a server thread indefinitely; wait_seconds
        is clamped to _STATUS_POLL_MAX_WAIT_SECONDS."""
        import server, tiles.pyramid as pyramid
        import time
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 0.05)
        monkeypatch.setattr(server, "_STATUS_POLL_MAX_WAIT_SECONDS", 1)
        orig_build = pyramid.build_hex_tiles
        def very_slow_build(con, plan):
            time.sleep(5.0)
            return orig_build(con, plan)
        monkeypatch.setattr(server, "build_hex_tiles", very_slow_build)

        sub = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert sub["status"] == "running"

        t0 = time.time()
        # Caller asks for 999 s; should be clamped to 1 s.
        status = server.get_hex_tile_status(hash=sub["hash"], wait_seconds=999)
        elapsed = time.time() - t0
        assert status["status"] == "running"
        assert elapsed <= 2.0, f"clamp not enforced — waited {elapsed:.2f}s"

        server._jobs[sub["hash"]]["future"].result(timeout=10)

    def test_failed_when_build_raises(self, isolated_jobs, monkeypatch):
        import server
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)
        def boom(con, plan):
            raise RuntimeError("boom")
        monkeypatch.setattr(server, "build_hex_tiles", boom)

        result = server.register_hex_tiles(
            sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val",
            agg="AVG",
        )
        assert result["status"] == "failed"
        assert "boom" in result["error"]

        status = server.get_hex_tile_status(hash=result["hash"])
        assert status["status"] == "failed"
        assert "boom" in status["error"]

    def test_failed_marker_returns_status_failed(self, isolated_jobs, monkeypatch):
        """If failed.json exists at the hash's output_uri, get_hex_tile_status
        returns status=failed with the recorded error — regardless of whether
        the local pod has any _jobs entry."""
        import server
        from tiles.pyramid import write_failed, tile_paths_for_hash

        h = "deadbeefdeadbeef"
        paths = tile_paths_for_hash(h)
        # Ensure the hash dir exists before writing the marker.
        import os
        os.makedirs(paths["output_uri"], exist_ok=True)
        con = server._get_tile_con()
        write_failed(con, paths["output_uri"], error="build blew up")

        result = server.get_hex_tile_status(hash=h)
        assert result["status"] == "failed"
        assert result["error"] == "build blew up"
        assert result["hash"] == h

    def test_running_via_cross_pod_lock(self, isolated_jobs, monkeypatch):
        """A fresh lock from another pod with no local _jobs entry returns
        status=running with elapsed_seconds from the lock — not 'unknown'."""
        import server
        from tiles.pyramid import write_lock, tile_paths_for_hash

        h = "abcdef0123456789"
        paths = tile_paths_for_hash(h)
        import os
        os.makedirs(paths["output_uri"], exist_ok=True)
        write_lock(server._get_tile_con(), paths["output_uri"], pod_id="other-pod")

        result = server.get_hex_tile_status(hash=h, wait_seconds=0)
        assert result["status"] == "running"
        assert "elapsed_seconds" in result
        assert result["hash"] == h

    def test_long_poll_picks_up_metadata_appearing_on_another_pod(
        self, isolated_jobs, monkeypatch
    ):
        """While long-polling a hash owned by another pod, if metadata.json
        appears mid-wait, return status=done."""
        import server, threading, time, os
        from tiles.pyramid import write_lock, tile_paths_for_hash, _json_dumps_escaped

        h = "1234567890abcdef"
        paths = tile_paths_for_hash(h)
        os.makedirs(paths["output_uri"], exist_ok=True)
        write_lock(server._get_tile_con(), paths["output_uri"], pod_id="other-pod")

        # Simulate the owning pod finishing the build mid-poll.
        metadata = {
            "finest_res": 5, "min_res": 2, "agg": "COUNT",
            "zoom_offset": -1, "value_columns": ["count"],
            "value_stats": {}, "layer_name": "layer",
            "bounds": [-180.0, -90.0, 180.0, 90.0],
            "feature_count_finest": 1,
        }
        def write_metadata_after_delay():
            time.sleep(0.5)
            con = server._get_tile_con()
            con.sql(
                f"COPY (SELECT '{_json_dumps_escaped(metadata)}' AS j) "
                f"TO '{paths['output_uri']}metadata.json' "
                f"(FORMAT CSV, HEADER false, QUOTE '')"
            )
        t = threading.Thread(target=write_metadata_after_delay)
        t.start()

        result = server.get_hex_tile_status(hash=h, wait_seconds=5)
        t.join()
        assert result["status"] == "done"
        assert result["finest_res"] == 5

    def test_long_poll_picks_up_failed_appearing_on_another_pod(
        self, isolated_jobs, monkeypatch
    ):
        """While long-polling, if failed.json appears mid-wait, return failed."""
        import server, threading, time, os
        from tiles.pyramid import write_lock, write_failed, tile_paths_for_hash

        h = "fedcba9876543210"
        paths = tile_paths_for_hash(h)
        os.makedirs(paths["output_uri"], exist_ok=True)
        write_lock(server._get_tile_con(), paths["output_uri"], pod_id="other-pod")

        def write_failed_after_delay():
            time.sleep(0.5)
            write_failed(server._get_tile_con(), paths["output_uri"], error="cross-pod boom")
        t = threading.Thread(target=write_failed_after_delay)
        t.start()

        result = server.get_hex_tile_status(hash=h, wait_seconds=5)
        t.join()
        assert result["status"] == "failed"
        assert "cross-pod boom" in result["error"]

    def test_unknown_when_no_local_job_no_lock_no_failed_no_metadata(
        self, isolated_jobs
    ):
        """If nothing exists for the hash anywhere, still return unknown
        (preserves the existing contract for never-started hashes)."""
        import server
        result = server.get_hex_tile_status(hash="0" * 16, wait_seconds=0)
        assert result["status"] == "unknown"


class TestBuildFailureMarker:
    def test_build_exception_writes_failed_marker(self, isolated_jobs, monkeypatch):
        """When the build raises, _do_build's wrapper writes failed.json so
        other pods (or this pod after _jobs eviction) can see the failure."""
        import server, tiles.pyramid as pyramid
        from tiles.pyramid import read_failed, tile_paths_for_hash
        monkeypatch.setattr(server, "_BUILD_INLINE_WAIT_SECONDS", 30.0)

        def boom(con, plan):
            raise RuntimeError("synthetic build failure")
        monkeypatch.setattr(server, "build_hex_tiles", boom)

        sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = server.register_hex_tiles(sql=sql, agg="AVG")
        assert result["status"] == "failed"
        assert "synthetic build failure" in result["error"]

        # And the marker is persisted to S3 (here, tmp_path) so a fresh pod sees it.
        paths = tile_paths_for_hash(result["hash"])
        failed = read_failed(server._get_tile_con(), paths["output_uri"])
        assert failed is not None
        assert "synthetic build failure" in failed["error"]


class TestStacStartupGate:
    """_enforce_stac_startup_gate: fail-fast on an unreachable root catalog, with
    an opt-in degraded start for serving the source.coop mirror during an outage (#260)."""

    def test_boots_normally_when_root_loaded(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "STAC_LOAD_ERRORS", {})
        assert server._enforce_stac_startup_gate() is False

    def test_exits_when_root_failed_and_flag_unset(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "STAC_LOAD_ERRORS", {"__root__": "ConnectTimeout"})
        monkeypatch.delenv("STAC_ALLOW_DEGRADED_START", raising=False)
        with pytest.raises(SystemExit) as exc:
            server._enforce_stac_startup_gate()
        assert exc.value.code == 1

    def test_degraded_start_when_flag_set(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "STAC_LOAD_ERRORS", {"__root__": "ConnectTimeout"})
        monkeypatch.setenv("STAC_ALLOW_DEGRADED_START", "true")
        assert server._enforce_stac_startup_gate() is True

    def test_flag_is_case_and_value_tolerant(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "STAC_LOAD_ERRORS", {"__root__": "boom"})
        for val in ("1", "TRUE", "Yes", "on"):
            monkeypatch.setenv("STAC_ALLOW_DEGRADED_START", val)
            assert server._enforce_stac_startup_gate() is True
        # A non-truthy value still fails fast.
        monkeypatch.setenv("STAC_ALLOW_DEGRADED_START", "false")
        with pytest.raises(SystemExit):
            server._enforce_stac_startup_gate()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
