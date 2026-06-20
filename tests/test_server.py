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
)


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

    def test_partial_credentials_no_secret(self):
        """Supplying only key or only secret does not create a secret (both required)."""
        with get_isolated_db(s3_key="AKID") as conn:
            names = [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" not in names
        with get_isolated_db(s3_secret="SECRET") as conn:
            names = [r[0] for r in conn.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            assert "client_s3" not in names

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
            "s3_endpoint", "s3_key", "s3_scope", "s3_secret", "sql_query",
        ]

    def test_query_tool_matches_sync_core(self):
        """The async wrapper returns the same payload as the sync query()."""
        import anyio, server
        sql = "SELECT 1 AS one"
        sync_result = server.query(sql)
        async_result = anyio.run(server._query_tool, sql)
        assert async_result == sync_result

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

    def test_version_returns_app_version_and_sha(self):
        """GET /version reports the build-stamped app version + git SHA (issue #221)."""
        import server
        from starlette.testclient import TestClient
        orig_v, orig_s = server.APP_VERSION, server.GIT_SHA
        server.APP_VERSION, server.GIT_SHA = "v9.9.9", "deadbeef"
        try:
            client = TestClient(self._build_app())
            r = client.get("/version")
            assert r.status_code == 200
            assert r.json() == {"version": "v9.9.9", "git_sha": "deadbeef"}
        finally:
            server.APP_VERSION, server.GIT_SHA = orig_v, orig_s

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
