# Hex Tile Cross-Pod State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `register_hex_tiles` and `get_hex_tile_status` correct under the 6-replica prod deployment by storing build state in shared S3 markers instead of per-pod in-memory dicts.

**Architecture:** Three sibling JSON files (`lock.json`, `failed.json`, plus the existing `metadata.json`) live in each per-hash output directory. Reads check them in order `metadata → failed → lock`. No deletes — successful builds' `metadata.json` shadows their lock; failures write `failed.json` which shadows the lock; stale locks (TTL 15 min) are ignored by readers. All file I/O reuses the existing DuckDB COPY mechanism, so no new dependencies.

**Tech Stack:** Python, DuckDB (httpfs extension for S3), pytest. Tests use local `tmp_path` paths instead of real S3 — the existing test harness already does this.

**Spec:** `docs/superpowers/specs/2026-05-15-hex-tile-cross-pod-state-design.md`

**Working branch:** Create `fix/hex-tile-cross-pod-state` from `main` before Task 1. All commits go on this branch; final task opens the PR.

**Test command (use the project venv per repo convention):**
```
.venv/bin/pytest tests/test_tile_pyramid.py tests/test_server.py -v
```

---

### Task 0: Create the feature branch

**Files:** none

- [ ] **Step 1: Cut a branch from main**

```bash
git switch main
git pull --ff-only
git switch -c fix/hex-tile-cross-pod-state
```

Per AGENTS.md, never commit directly to `main`; this repo squash-merges.

---

### Task 1: Add lock-marker helpers (`write_lock`, `read_lock`, `lock_is_stale`)

**Files:**
- Modify: `tiles/pyramid.py` — add helpers after `_read_existing_metadata` (~line 165)
- Test: `tests/test_tile_pyramid.py` — append a new `TestLockMarkers` class

**Why first:** Pure data helpers, no server-flow integration. Establishes the read/write pattern used by all subsequent tasks.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tile_pyramid.py`:

```python
import json
import time
import duckdb
from tiles.pyramid import (
    build_pyramid_statements,
    write_lock,
    read_lock,
    lock_is_stale,
)


class TestLockMarkers:
    def _con(self):
        return duckdb.connect(":memory:")

    def test_write_then_read_round_trips(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        write_lock(con, output_uri, pod_id="pod-A")
        lock = read_lock(con, output_uri)
        assert lock is not None
        assert lock["pod_id"] == "pod-A"
        assert isinstance(lock["started_at"], (int, float))
        # Lock file should physically exist on disk for local paths.
        import os
        assert os.path.exists(output_uri + "lock.json")

    def test_read_returns_none_when_absent(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        assert read_lock(con, output_uri) is None

    def test_overwrite_replaces_previous(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        write_lock(con, output_uri, pod_id="pod-A")
        first = read_lock(con, output_uri)
        time.sleep(0.01)
        write_lock(con, output_uri, pod_id="pod-B")
        second = read_lock(con, output_uri)
        assert second["pod_id"] == "pod-B"
        assert second["started_at"] >= first["started_at"]

    def test_lock_is_stale_returns_false_when_fresh(self):
        lock = {"started_at": time.time(), "pod_id": "pod-A"}
        assert lock_is_stale(lock) is False

    def test_lock_is_stale_returns_true_past_ttl(self):
        lock = {"started_at": time.time() - 10_000, "pod_id": "pod-A"}
        assert lock_is_stale(lock) is True

    def test_lock_is_stale_handles_none(self):
        assert lock_is_stale(None) is True
```

- [ ] **Step 2: Run tests, expect failure**

```
.venv/bin/pytest tests/test_tile_pyramid.py::TestLockMarkers -v
```

Expected: `ImportError: cannot import name 'write_lock'` (or equivalent).

- [ ] **Step 3: Add `import time` to `tiles/pyramid.py` top imports**

Change the top of `tiles/pyramid.py` from:

```python
import json
import os
from decimal import Decimal
from typing import List

import duckdb

from tiles.tile_math import content_hash
```

to:

```python
import json
import os
import time
from decimal import Decimal
from typing import List

import duckdb

from tiles.tile_math import content_hash
```

- [ ] **Step 4: Add the stale-TTL constant**

Insert right after `MVT_LAYER_NAME = "layer"` (line 18):

```python
# Builds are tracked across pods via lock.json. A lock older than this
# is treated as abandoned (the owning pod likely crashed mid-build).
# Configurable for ops; observed builds complete in 100-120s so 900s
# gives ~8x headroom.
_LOCK_STALE_SECONDS = int(os.environ.get("TILE_LOCK_STALE_SECONDS", "900"))
```

- [ ] **Step 5: Add the marker helpers**

Insert immediately after `_read_existing_metadata` (around line 165, before `tile_paths_for_hash`):

```python
def _read_json_marker(con: duckdb.DuckDBPyConnection, uri: str):
    """Return parsed JSON dict at uri, or None if absent/unreadable.
    Mirrors _read_existing_metadata's local-vs-remote handling."""
    try:
        if not uri.startswith("s3://") and not uri.startswith("http"):
            if not os.path.exists(uri):
                return None
            with open(uri, "r") as f:
                return json.loads(f.read().strip())
        row = con.sql(f"SELECT content FROM read_text('{uri}')").fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _write_json_marker(con: duckdb.DuckDBPyConnection, uri: str, payload: dict) -> None:
    """Write payload as a single-row CSV-as-JSON file at uri. Uses the
    same COPY pattern as the existing metadata.json write. For local-fs
    paths, ensures the parent dir exists (DuckDB COPY does not auto-mkdir,
    and these markers may fire before build_hex_tiles' own makedirs)."""
    if not uri.startswith("s3://") and not uri.startswith("http"):
        parent = os.path.dirname(uri)
        if parent:
            os.makedirs(parent, exist_ok=True)
    sql = (
        f"COPY (SELECT '{_json_dumps_escaped(payload)}' AS j) "
        f"TO '{uri}' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(sql)


def write_lock(con: duckdb.DuckDBPyConnection, output_uri: str, pod_id: str) -> None:
    """Write {output_uri}lock.json announcing this pod owns the in-progress
    build for this hash. Overwrites any prior lock at the same path."""
    payload = {"started_at": time.time(), "pod_id": pod_id}
    _write_json_marker(con, f"{output_uri}lock.json", payload)


def read_lock(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Return the lock dict {started_at, pod_id} or None if no lock.json."""
    return _read_json_marker(con, f"{output_uri}lock.json")


def lock_is_stale(lock: dict | None, now: float | None = None) -> bool:
    """A missing lock is 'stale' (treated the same as absent). A lock older
    than _LOCK_STALE_SECONDS is considered abandoned."""
    if lock is None:
        return True
    started = lock.get("started_at")
    if not isinstance(started, (int, float)):
        return True
    if now is None:
        now = time.time()
    return (now - started) > _LOCK_STALE_SECONDS
```

- [ ] **Step 6: Run tests, expect pass**

```
.venv/bin/pytest tests/test_tile_pyramid.py::TestLockMarkers -v
```

Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "tiles: add lock.json marker helpers for cross-pod build state"
```

---

### Task 2: Add failed-marker helpers (`write_failed`, `read_failed`)

**Files:**
- Modify: `tiles/pyramid.py` — add two helpers below `lock_is_stale`
- Test: `tests/test_tile_pyramid.py` — append a `TestFailedMarkers` class

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tile_pyramid.py`:

```python
from tiles.pyramid import write_failed, read_failed


class TestFailedMarkers:
    def _con(self):
        return duckdb.connect(":memory:")

    def test_write_then_read_round_trips(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        write_failed(con, output_uri, error="Out of memory during COPY")
        failed = read_failed(con, output_uri)
        assert failed is not None
        assert failed["error"] == "Out of memory during COPY"
        assert isinstance(failed["failed_at"], (int, float))

    def test_read_returns_none_when_absent(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        assert read_failed(con, output_uri) is None

    def test_error_string_with_single_quotes_round_trips(self, tmp_path):
        con = self._con()
        output_uri = str(tmp_path) + "/"
        msg = "table 't' doesn't exist; check 'schema'"
        write_failed(con, output_uri, error=msg)
        assert read_failed(con, output_uri)["error"] == msg
```

- [ ] **Step 2: Run tests, expect failure**

```
.venv/bin/pytest tests/test_tile_pyramid.py::TestFailedMarkers -v
```

Expected: `ImportError: cannot import name 'write_failed'`.

- [ ] **Step 3: Add helpers to `tiles/pyramid.py`**

Add immediately after `lock_is_stale`:

```python
def write_failed(con: duckdb.DuckDBPyConnection, output_uri: str, error: str) -> None:
    """Write {output_uri}failed.json recording a build exception. Readers
    treat this as terminal-failed for the hash until a new register_hex_tiles
    overwrites it."""
    payload = {"error": str(error), "failed_at": time.time()}
    _write_json_marker(con, f"{output_uri}failed.json", payload)


def read_failed(con: duckdb.DuckDBPyConnection, output_uri: str):
    """Return the failed dict {error, failed_at} or None if no failed.json."""
    return _read_json_marker(con, f"{output_uri}failed.json")
```

- [ ] **Step 4: Run tests, expect pass**

```
.venv/bin/pytest tests/test_tile_pyramid.py::TestFailedMarkers -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "tiles: add failed.json marker helpers"
```

---

### Task 3: Short-circuit on `failed.json` in `get_hex_tile_status`

**Files:**
- Modify: `server.py:467-526` (`get_hex_tile_status`)
- Modify: `server.py` top imports — add `read_failed` to the existing pyramid import
- Test: `tests/test_server.py` — append to `TestGetHexTileStatus`

**Why before lock-write integration:** Establishes the read-order pattern (`metadata → failed → lock`) and tests cleanly without needing to simulate a failing build yet.

- [ ] **Step 1: Find the existing pyramid import in `server.py`**

```bash
grep -n "from tiles.pyramid import" server.py
```

- [ ] **Step 2: Write failing test**

Append inside `TestGetHexTileStatus` in `tests/test_server.py`:

```python
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
```

- [ ] **Step 3: Run test, expect failure**

```
.venv/bin/pytest tests/test_server.py::TestGetHexTileStatus::test_failed_marker_returns_status_failed -v
```

Expected: assertion fails — result["status"] is "unknown" rather than "failed".

- [ ] **Step 4: Update `server.py`**

In the existing pyramid import block (around line 35), add `read_failed`:

```python
from tiles.pyramid import (
    # ... existing imports ...
    read_failed,
)
```

(If the existing import is one-line, expand it to the multi-line form.)

In `get_hex_tile_status`, right after the `cached = read_existing_metadata(...)` block returns done, add a failed-marker check. The function currently looks like:

```python
    cached = read_existing_metadata(_get_tile_con(), paths["output_uri"])
    if cached is not None and "bounds" in cached and "feature_count_finest" in cached:
        return _done_response(base, cached)

    with _jobs_lock:
        job = _jobs.get(hash)
```

Insert between the cached check and the `_jobs_lock` block:

```python
    failed = read_failed(_get_tile_con(), paths["output_uri"])
    if failed is not None:
        return {**base, "status": "failed", "error": failed.get("error", "")}
```

- [ ] **Step 5: Run test, expect pass**

```
.venv/bin/pytest tests/test_server.py::TestGetHexTileStatus::test_failed_marker_returns_status_failed -v
```

Expected: 1 passed.

- [ ] **Step 6: Run the full status test class to confirm no regressions**

```
.venv/bin/pytest tests/test_server.py::TestGetHexTileStatus -v
```

Expected: all tests pass (existing + new).

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "server: get_hex_tile_status checks failed.json before _jobs"
```

---

### Task 4: Short-circuit on `failed.json` in `register_hex_tiles`

**Files:**
- Modify: `server.py:319-442` (`register_hex_tiles`)
- Test: `tests/test_server.py` — append to `TestRegisterHexTilesAsync`

- [ ] **Step 1: Write failing test**

Append inside `TestRegisterHexTilesAsync`:

```python
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
```

- [ ] **Step 2: Run test, expect failure**

```
.venv/bin/pytest tests/test_server.py::TestRegisterHexTilesAsync::test_register_returns_failed_when_failed_marker_exists -v
```

Expected: assertion fails — register kicks off a build and returns "running" or "done".

- [ ] **Step 3: Update `register_hex_tiles` in `server.py`**

The function currently has this control flow at the top:

```python
    read_con = _get_tile_con()
    plan = prepare_hex_tiles(
        con=read_con, sql=sql, agg=agg,
        finest_res=finest_res, min_res=min_res, zoom_offset=zoom_offset,
    )
    if plan["cached"] is not None:
        result = cached_result_dict(plan, plan["cached"])
        result["status"] = "done"
        return result

    future = _submit_build(plan)
```

Insert a failed-marker check between the cache-hit return and `_submit_build`:

```python
    if plan["cached"] is not None:
        result = cached_result_dict(plan, plan["cached"])
        result["status"] = "done"
        return result

    failed = read_failed(read_con, plan["output_uri"])
    if failed is not None:
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "failed",
            "error": failed.get("error", ""),
        }

    future = _submit_build(plan)
```

- [ ] **Step 4: Run test, expect pass**

```
.venv/bin/pytest tests/test_server.py::TestRegisterHexTilesAsync::test_register_returns_failed_when_failed_marker_exists -v
```

Expected: 1 passed.

- [ ] **Step 5: Confirm no regressions in the register test class**

```
.venv/bin/pytest tests/test_server.py::TestRegisterHexTilesAsync -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "server: register_hex_tiles short-circuits when failed.json exists"
```

---

### Task 5: Wrap `_do_build` so build exceptions write `failed.json`

**Files:**
- Modify: `server.py:287-307` (`_submit_build` / `_do_build`)
- Test: `tests/test_server.py` — new class `TestBuildFailureMarker`

- [ ] **Step 1: Write failing test**

Append at module level in `tests/test_server.py`:

```python
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
```

- [ ] **Step 2: Run test, expect failure**

```
.venv/bin/pytest tests/test_server.py::TestBuildFailureMarker -v
```

Expected: `read_failed` returns None (no marker written), assertion fails.

- [ ] **Step 3: Update `server.py`**

Add `write_failed` to the pyramid import in `server.py`:

```python
from tiles.pyramid import (
    # ...
    read_failed,
    write_failed,
)
```

Modify `_submit_build` so the wrapper writes a failed marker on exception. The current code is:

```python
def _submit_build(plan: dict) -> concurrent.futures.Future:
    h = plan["hash"]
    with _jobs_lock:
        existing = _jobs.get(h)
        if existing is not None and not existing["future"].done():
            return existing["future"]

        def _do_build():
            build_con = build_tile_connection()
            try:
                return build_hex_tiles(build_con, plan)
            finally:
                build_con.close()

        future = _build_executor.submit(_do_build)
        _jobs[h] = {"future": future, "started_at": time.time()}
        return future
```

Replace `_do_build` with:

```python
        def _do_build():
            build_con = build_tile_connection()
            try:
                return build_hex_tiles(build_con, plan)
            except Exception as exc:
                # Persist failure so other pods (and this pod after _jobs
                # eviction) can return status=failed instead of "unknown".
                try:
                    write_failed(build_con, plan["output_uri"], error=str(exc))
                except Exception:
                    # Marker write failed (S3 blip); preserve original raise.
                    pass
                raise
            finally:
                build_con.close()
```

- [ ] **Step 4: Run test, expect pass**

```
.venv/bin/pytest tests/test_server.py::TestBuildFailureMarker -v
```

Expected: 1 passed.

- [ ] **Step 5: Run the full server test file for regressions**

```
.venv/bin/pytest tests/test_server.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "server: _do_build writes failed.json on build exception"
```

---

### Task 6: Write `lock.json` and dedup across pods in `register_hex_tiles`

**Files:**
- Modify: `server.py:319-442` (`register_hex_tiles`)
- Modify: `server.py` near top — add `_POD_ID` constant
- Test: `tests/test_server.py` — append to `TestRegisterHexTilesAsync`

- [ ] **Step 1: Write failing tests**

Append inside `TestRegisterHexTilesAsync`:

```python
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
```

- [ ] **Step 2: Run tests, expect failure**

```
.venv/bin/pytest tests/test_server.py::TestRegisterHexTilesAsync -v -k "lock"
```

Expected: 3 new tests fail.

- [ ] **Step 3: Update `server.py`**

Add `_POD_ID`, `write_lock`, `read_lock`, `lock_is_stale` to imports and add a pod-id constant near the top of the file (after the existing `_BUILD_*` constants, around line 270):

```python
import socket  # add to imports near top of file if not already there

# Pod identity for cross-pod attribution in lock.json. In k8s, HOSTNAME
# is the pod name; falling back to the OS hostname for local dev.
_POD_ID = os.environ.get("HOSTNAME") or socket.gethostname()
```

Extend the pyramid import block:

```python
from tiles.pyramid import (
    # existing entries ...
    read_failed,
    write_failed,
    read_lock,
    write_lock,
    lock_is_stale,
)
```

Update `register_hex_tiles` to write the lock and dedup. After the failed-marker check from Task 4 and before `_submit_build`, insert the lock check + lock write. The new flow:

```python
    if plan["cached"] is not None:
        result = cached_result_dict(plan, plan["cached"])
        result["status"] = "done"
        return result

    failed = read_failed(read_con, plan["output_uri"])
    if failed is not None:
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "failed",
            "error": failed.get("error", ""),
        }

    existing_lock = read_lock(read_con, plan["output_uri"])
    if existing_lock is not None and not lock_is_stale(existing_lock):
        # Another pod owns this build. Don't submit a duplicate.
        return {
            "hash": plan["hash"],
            "tile_url_template": plan["tile_url_template"],
            "status": "running",
            "elapsed_seconds": round(time.time() - existing_lock["started_at"], 1),
        }

    try:
        write_lock(read_con, plan["output_uri"], pod_id=_POD_ID)
    except Exception:
        # S3 blip writing lock; proceed anyway. Worst case is a duplicate
        # build elsewhere — see spec "Race we knowingly accept".
        pass

    future = _submit_build(plan)
```

- [ ] **Step 4: Run tests, expect pass**

```
.venv/bin/pytest tests/test_server.py::TestRegisterHexTilesAsync -v
```

Expected: all pass (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "server: register_hex_tiles writes lock.json + dedups across pods"
```

---

### Task 7: Cross-pod long-poll in `get_hex_tile_status`

**Files:**
- Modify: `server.py:467-526` (`get_hex_tile_status`)
- Test: `tests/test_server.py` — append to `TestGetHexTileStatus`

- [ ] **Step 1: Write failing tests**

Append inside `TestGetHexTileStatus`:

```python
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
```

- [ ] **Step 2: Run tests, expect failure**

```
.venv/bin/pytest tests/test_server.py::TestGetHexTileStatus -v -k "cross_pod or appearing"
```

Expected: 3 new tests fail (the existing "unknown" test still passes — that contract is preserved).

- [ ] **Step 3: Update `get_hex_tile_status` in `server.py`**

Add `read_lock` / `lock_is_stale` to the import block (already done in Task 6 if doing in order — verify).

Replace the bottom half of `get_hex_tile_status`. The current code after the failed-marker check from Task 3 is:

```python
    failed = read_failed(_get_tile_con(), paths["output_uri"])
    if failed is not None:
        return {**base, "status": "failed", "error": failed.get("error", "")}

    with _jobs_lock:
        job = _jobs.get(hash)
    if job is None:
        return {**base, "status": "unknown"}
    future = job["future"]

    if not future.done() and wait_seconds > 0:
        # ... existing local long-poll on Future ...
```

Replace the `if job is None: return ... unknown` branch with the cross-pod logic:

```python
    with _jobs_lock:
        job = _jobs.get(hash)

    if job is None:
        # No local job — but another pod may own this build. Consult lock.json.
        lock = read_lock(_get_tile_con(), paths["output_uri"])
        if lock is None or lock_is_stale(lock):
            return {**base, "status": "unknown"}

        # Fresh lock from another pod. Long-poll S3 for metadata.json /
        # failed.json appearance up to wait_seconds. 2s granularity is fine —
        # this is server-internal, the LLM sees one tool call.
        deadline = time.time() + wait_seconds
        while True:
            cached = read_existing_metadata(_get_tile_con(), paths["output_uri"])
            if cached is not None and "bounds" in cached and "feature_count_finest" in cached:
                return _done_response(base, cached)
            failed_now = read_failed(_get_tile_con(), paths["output_uri"])
            if failed_now is not None:
                return {**base, "status": "failed", "error": failed_now.get("error", "")}
            if time.time() >= deadline:
                break
            time.sleep(min(2.0, max(0.1, deadline - time.time())))

        # Wait expired without resolution. Report running with elapsed from lock.
        return {
            **base,
            "status": "running",
            "elapsed_seconds": round(time.time() - lock["started_at"], 1),
        }

    future = job["future"]
    # ... rest unchanged ...
```

- [ ] **Step 4: Run tests, expect pass**

```
.venv/bin/pytest tests/test_server.py::TestGetHexTileStatus -v
```

Expected: all pass (old + 4 new).

- [ ] **Step 5: Run the entire test suite for regressions**

```
.venv/bin/pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "server: get_hex_tile_status long-polls S3 when build is on another pod"
```

---

### Task 8: Push branch and open PR

**Files:** none — git/gh only.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin fix/hex-tile-cross-pod-state
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "fix: cross-pod state for hex tile builds" --body "$(cat <<'EOF'
## Summary
- Adds S3-shared `lock.json` and `failed.json` markers alongside the existing `metadata.json` so async pyramid build state is visible across all 6 prod replicas.
- `register_hex_tiles` writes a lock before submitting and short-circuits if it sees a fresh lock from another pod (cross-pod dedup) or a `failed.json` from a prior failure.
- `get_hex_tile_status` checks `metadata → failed → local _jobs → lock` in order; when a fresh lock from another pod is the only signal, it long-polls S3 (2s cadence, server-internal — agent sees one tool call) for `metadata.json` / `failed.json` appearance.
- No deletes: successful build's `metadata.json` shadows its lock; failures write `failed.json` which shadows the lock; stale locks past `TILE_LOCK_STALE_SECONDS` (default 900s) are ignored.
- No new dependencies. All marker I/O reuses the existing DuckDB COPY pattern.

Spec: `docs/superpowers/specs/2026-05-15-hex-tile-cross-pod-state-design.md`
Plan: `docs/superpowers/plans/2026-05-15-hex-tile-cross-pod-state.md`

## Test plan
- [ ] `.venv/bin/pytest tests/test_tile_pyramid.py tests/test_server.py -v` — all green locally
- [ ] After merge, tag `v0.6.5` and open a follow-up PR bumping `k8s/deployment.yaml` to v0.6.5
- [ ] After rollout, repro the wyoming-public-demo "Show elk and bear sightings as hexes" prompt; verify `register_hex_tiles` + `get_hex_tile_status` round-trip without `status: unknown` interruptions in `open-llm-proxy` logs
EOF
)"
```

- [ ] **Step 3: Report the PR URL**

The `gh pr create` output ends with the PR URL — paste it back to the user.

---

## Out of scope for this PR (follow-ups)

These are explicitly **not** done as part of this plan, per the spec's non-goals:

- **Bump `k8s/deployment.yaml` to v0.6.5.** Convention in this repo is a separate "k8s: bump prod" PR after the code PR merges and is tagged.
- **Reduce LLM-facing poll budget** (increase `_STATUS_POLL_MAX_WAIT_SECONDS`, tighten docstrings to direct the agent toward fewer long polls). Separate concern that needs coordination with agent rate-limiting.
- **Atomic lock acquisition via Ceph S3 conditional writes.** Race window between read-lock and write-lock is accepted; revisit only if observed in practice.
