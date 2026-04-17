# STAC Catalog Fetch Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fetch_stac_catalog()` in `stac.py` survive slow/unreliable S3 by adding per-child timeouts, bounded parallelism via a `ThreadPoolExecutor`, and partial-result fallback so one bad child doesn't kill the whole load.

**Architecture:** Replace the serial try/except-wrapped pystac walk with a three-phase dynamic-enqueue walk: (1) fetch the root with a generous timeout, (2) dispatch parent and sub-child fetches to a shared thread pool where a completed parent's sub-children get submitted immediately, (3) swap module state from the main thread. Failures are isolated per-child, recorded in a module-level `STAC_LOAD_ERRORS` dict, and surfaced via a footer in `list_datasets()` output.

**Tech Stack:** Python 3.10+, `pystac`, `requests`, `concurrent.futures.ThreadPoolExecutor`, `pytest` with `unittest.mock.patch`.

**Companion spec:** `docs/superpowers/specs/2026-04-16-stac-catalog-resilience-design.md`

---

## File Structure

**Files to modify:**
- `stac.py` — add module-level state and config; extend `_TimeoutStacIO`; add `_child_identifier`, `_fetch_parent`, `_fetch_subchild` helpers; rewrite `fetch_stac_catalog()`; modify `list_datasets()` to append error footer.
- `tests/test_stac.py` — add a `TestFetchResilience` class covering new behavior.

No new files, no directory changes. All changes contained in one source module + one test module.

**Testing conventions:**
- Always run tests with the project `.venv`: `.venv/bin/python -m pytest ...`.
- Mock `pystac.Catalog.from_file` and `pystac.Collection.from_file` for control over success/failure without real HTTP.
- Use `monkeypatch.setenv(...)` to test env-var driven config.
- Each test should be independent; reset module-level state (`STAC_LOAD_ERRORS`, `STAC_DATASETS`, `_STAC_RAW`) in fixtures where relevant.

---

## Task 1: Add new module-level config and error state

**Files:**
- Modify: `stac.py` (top of file, near existing env-var block)
- Test: `tests/test_stac.py` (new `TestFetchResilience` class)

Extends the config with two split timeouts plus a concurrency knob, and adds a module-level dict to track per-child load errors.

- [ ] **Step 1: Write failing tests for new module-level state**

Append to `tests/test_stac.py` (after the existing imports and before any existing class, add the new class at the end of the file):

```python
import importlib


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v`

Expected: FAIL with `AttributeError: module 'stac' has no attribute '_STAC_ROOT_TIMEOUT'` (or similar) for all tests.

- [ ] **Step 3: Add the new module-level config and state to `stac.py`**

Find the existing `_STAC_TIMEOUT` line (around line 22) in `stac.py`:

```python
_STAC_TIMEOUT = int(os.environ.get("STAC_TIMEOUT", "15"))
```

Replace it with:

```python
# Backwards-compatible: STAC_TIMEOUT alone still works as a single knob; the two
# new vars override it when set. Root is a hard prerequisite (generous timeout);
# children are individually skippable (tight timeout). See
# docs/superpowers/specs/2026-04-16-stac-catalog-resilience-design.md.
_STAC_ROOT_TIMEOUT = int(
    os.environ.get("STAC_ROOT_TIMEOUT", os.environ.get("STAC_TIMEOUT", "15"))
)
_STAC_CHILD_TIMEOUT = int(
    os.environ.get("STAC_CHILD_TIMEOUT", os.environ.get("STAC_TIMEOUT", "5"))
)
_STAC_FETCH_CONCURRENCY = int(os.environ.get("STAC_FETCH_CONCURRENCY", "8"))

# Legacy alias retained so external callers (if any) that read the old name still work.
_STAC_TIMEOUT = _STAC_CHILD_TIMEOUT
```

Find the existing `_STAC_RAW` declaration (around line 264):

```python
_STAC_RAW: dict[str, dict] = {}
```

Add immediately after it:

```python
# Populated by fetch_stac_catalog on the default-catalog path; keys are the best
# available identifier (real collection id when parse succeeded, else href tail),
# values are short reason strings. Cleared on each successful default-catalog load.
STAC_LOAD_ERRORS: dict[str, str] = {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v`

Expected: PASS (6 passed).

- [ ] **Step 5: Run the full stac test suite to ensure no regressions**

Run: `.venv/bin/python -m pytest tests/test_stac.py -v`

Expected: All existing tests + new tests pass.

- [ ] **Step 6: Commit**

```bash
git add stac.py tests/test_stac.py
git commit -m "stac: add split timeouts, concurrency knob, and error-tracking dict

Add _STAC_ROOT_TIMEOUT (default 15s), _STAC_CHILD_TIMEOUT (default 5s),
_STAC_FETCH_CONCURRENCY (default 8), and module-level STAC_LOAD_ERRORS
dict. The legacy STAC_TIMEOUT env var still works as a back-compat
single knob; the new vars override it when set.

Part of mcp-data-server#65."
```

---

## Task 2: Teach `_TimeoutStacIO` about a per-instance timeout

**Files:**
- Modify: `stac.py:49-61` (the `_TimeoutStacIO` class)
- Test: `tests/test_stac.py` (add to `TestFetchResilience`)

The existing class hard-codes `_STAC_TIMEOUT` for every `requests.get` call. We need two instances per catalog load (one for the root fetch, one for child fetches), each with its own timeout.

- [ ] **Step 1: Write the failing test**

Append to `TestFetchResilience` in `tests/test_stac.py`:

```python
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

        from unittest.mock import patch, MagicMock
        with patch("stac.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = "{}"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            io.read_text_from_href("https://example.com/foo.json")

        _, kwargs = mock_get.call_args
        assert kwargs.get("timeout") == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience::test_timeout_stac_io_uses_configured_timeout tests/test_stac.py::TestFetchResilience::test_timeout_stac_io_default_timeout_falls_back_to_child -v`

Expected: FAIL (TypeError on `timeout` kwarg, or wrong timeout value).

- [ ] **Step 3: Modify `_TimeoutStacIO` to accept a timeout kwarg**

Find the current `_TimeoutStacIO` class in `stac.py` (around line 49):

```python
class _TimeoutStacIO(DefaultStacIO):
    def __init__(self, token: str = None):
        self._token = token

    def read_text_from_href(self, href: str) -> str:
        if href.startswith(_S3_PUBLIC):
            href = _S3_INTERNAL + href[len(_S3_PUBLIC):]
        if href.startswith("http"):
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            resp = requests.get(href, timeout=_STAC_TIMEOUT, headers=headers)
            resp.raise_for_status()
            return resp.text
        return super().read_text_from_href(href)
```

Replace it with:

```python
class _TimeoutStacIO(DefaultStacIO):
    def __init__(self, token: str = None, timeout: int = None):
        self._token = token
        self._timeout = timeout if timeout is not None else _STAC_CHILD_TIMEOUT

    def read_text_from_href(self, href: str) -> str:
        if href.startswith(_S3_PUBLIC):
            href = _S3_INTERNAL + href[len(_S3_PUBLIC):]
        if href.startswith("http"):
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            resp = requests.get(href, timeout=self._timeout, headers=headers)
            resp.raise_for_status()
            return resp.text
        return super().read_text_from_href(href)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v`

Expected: All TestFetchResilience tests so far pass.

- [ ] **Step 5: Run the full stac test suite**

Run: `.venv/bin/python -m pytest tests/test_stac.py -v`

Expected: All pass. In particular `TestCatalogUrlParameter::test_catalog_token_passed_as_bearer` and `test_no_token_no_auth_header` should still pass — they construct `_TimeoutStacIO()` with no args, which is why the timeout kwarg needs a default.

- [ ] **Step 6: Commit**

```bash
git add stac.py tests/test_stac.py
git commit -m "stac: allow per-instance timeout on _TimeoutStacIO

Accept an optional timeout kwarg on construction; default falls back
to _STAC_CHILD_TIMEOUT. Sets up the two-instance pattern (root_io
with 15s, child_io with 5s) needed by the parallel loader.

Part of mcp-data-server#65."
```

---

## Task 3: Add `_child_identifier` helper

**Files:**
- Modify: `stac.py` (new private function near the other helpers)
- Test: `tests/test_stac.py` (add to `TestFetchResilience`)

Small pure helper used by worker threads to produce a human-readable identifier for error reporting when a fetch fails before we can parse the JSON and read the real `id` field.

- [ ] **Step 1: Write the failing test**

Append to `TestFetchResilience`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k child_identifier`

Expected: FAIL with `AttributeError: module 'stac' has no attribute '_child_identifier'`.

- [ ] **Step 3: Implement `_child_identifier` in `stac.py`**

Add this function in `stac.py` immediately after the existing `_fuzzy_lookup` function (around line 47):

```python
def _child_identifier(href: str, title_hint: str = None, fetched_id: str = None) -> str:
    """Best-effort identifier for a STAC child — used for error reporting when the
    real collection `id` may not be available (fetch failed before JSON parse).

    Precedence:
    1. `fetched_id` if given — the real collection id
    2. The last path segment of the href (directory name for `.../dir/stac-collection.json`,
       or the file stem for `.../dir/name.json` / `.../dir/name-stac.json`)
    3. Optionally augmented with `title_hint` when present and non-redundant
    """
    if fetched_id:
        return fetched_id
    # Strip trailing slash, then take the last non-empty segment.
    path = href.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return title_hint or href
    tail = segments[-1]
    # If the tail is a generic collection-json filename, use the parent directory.
    if tail in ("stac-collection.json", "catalog.json"):
        tail = segments[-2] if len(segments) >= 2 else tail
    else:
        # Strip common suffixes so "foo-stac.json" / "foo.json" become "foo-stac" / "foo".
        for suffix in (".json",):
            if tail.endswith(suffix):
                tail = tail[: -len(suffix)]
                break
    if title_hint and title_hint.lower() not in tail.lower():
        return f"{tail} ({title_hint})"
    return tail
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k child_identifier`

Expected: All four `child_identifier` tests pass.

- [ ] **Step 5: Commit**

```bash
git add stac.py tests/test_stac.py
git commit -m "stac: add _child_identifier helper for error reporting

Pure helper that picks the best available identifier for a STAC child:
the real collection id if we parsed the JSON, else the href's tail
segment (with generic filenames stripped), optionally augmented with
the link title.

Part of mcp-data-server#65."
```

---

## Task 4: Add `_fetch_parent` and `_fetch_subchild` worker helpers

**Files:**
- Modify: `stac.py` (new private functions)
- Test: `tests/test_stac.py` (add to `TestFetchResilience`)

These are the functions run in the thread pool. Each does one HTTP GET and catches every exception so workers never raise. They return the parsed `pystac.Collection` (or `None` on failure) plus error info so the main thread can render markdown after all fetches have completed.

- [ ] **Step 1: Write the failing test for `_fetch_parent`**

Append to `TestFetchResilience`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k "fetch_parent or fetch_subchild"`

Expected: FAIL with `AttributeError: module 'stac' has no attribute '_fetch_parent'`.

- [ ] **Step 3: Implement `_fetch_parent` and `_fetch_subchild` in `stac.py`**

Add these functions in `stac.py` immediately after the existing `_collection_to_dict` function (around line 260), BEFORE the `_STAC_RAW` declaration:

```python
def _fetch_parent(href: str, title: str | None, token: str | None):
    """Thread-worker: fetch one top-level child Collection.

    Returns a 3-tuple (col, subchild_hrefs, error):
    - col: the parsed pystac.Collection on success, else None
    - subchild_hrefs: list of sub-child hrefs to enqueue next (empty on failure
      OR when the collection has no children — both cases are valid)
    - error: None on success, or {identifier: reason} on failure

    Rendering (to markdown and dict) happens in the caller after all fetches
    complete, so that parents can be rendered with their successfully-fetched
    sub-children in hand.

    This function must NEVER raise — all exceptions are caught and translated
    to the error dict.
    """
    child_io = _TimeoutStacIO(token=token, timeout=_STAC_CHILD_TIMEOUT)
    try:
        col = pystac.Collection.from_file(href, stac_io=child_io)
        subchild_hrefs = [l.href for l in (col.links or []) if l.rel == "child"]
        return col, subchild_hrefs, None
    except Exception as e:
        ident = _child_identifier(href, title_hint=title)
        reason = f"{type(e).__name__}: {e}"
        return None, [], {ident: reason}


def _fetch_subchild(href: str, parent_id: str, token: str | None):
    """Thread-worker: fetch one sub-child Collection (a leaf of a parent).

    Returns a 2-tuple (col, error):
    - col: the parsed pystac.Collection on success, else None
    - error: None on success, or {identifier: reason} on failure

    Never raises — all exceptions caught.
    """
    child_io = _TimeoutStacIO(token=token, timeout=_STAC_CHILD_TIMEOUT)
    try:
        col = pystac.Collection.from_file(href, stac_io=child_io)
        return col, None
    except Exception as e:
        ident = _child_identifier(href, title_hint=None)
        reason = f"{type(e).__name__}: {e}"
        return None, {ident: reason}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k "fetch_parent or fetch_subchild"`

Expected: All five tests pass.

- [ ] **Step 5: Run the full stac test suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_stac.py -v`

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add stac.py tests/test_stac.py
git commit -m "stac: add _fetch_parent and _fetch_subchild worker helpers

Thread-pool workers for the upcoming parallel catalog walk. Each does
exactly one HTTP GET via a short-timeout _TimeoutStacIO and catches
every exception — workers never raise. Parent worker returns its
sub-child hrefs for the caller to enqueue separately (keeping each
worker bounded to one HTTP timeout).

Part of mcp-data-server#65."
```

---

## Task 5: Rewrite `fetch_stac_catalog` as a three-phase dynamic-enqueue walk

**Files:**
- Modify: `stac.py` (the `fetch_stac_catalog` function, around lines 267-290)
- Test: `tests/test_stac.py` (add to `TestFetchResilience`)

The core change. Replaces the serial try/except-wrapped walk with a three-phase parallel walk. Phase 1 fetches the root with a generous timeout; phase 2 dispatches parents and sub-children to a `ThreadPoolExecutor` dynamically; phase 3 renders markdown and swaps module state. Partial failures are recorded in `STAC_LOAD_ERRORS`, not propagated.

- [ ] **Step 1: Write failing tests for root-failure and partial-success cases**

Append to `TestFetchResilience`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k "root_failure or one_parent or all_parents or subchild_fails or concurrency_env"`

Expected: FAIL with various assertion or attribute errors (the existing `fetch_stac_catalog` doesn't have the new behavior).

- [ ] **Step 3: Rewrite `fetch_stac_catalog` in `stac.py`**

Add this import at the top of `stac.py` with the other imports (after `import requests`):

```python
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
```

Now replace the existing `fetch_stac_catalog` function (around lines 267-290) entirely with:

```python
def fetch_stac_catalog(catalog_url: str = None, catalog_token: str = None) -> dict[str, str]:
    """Fetch the STAC catalog and return {collection_id: markdown_summary}.

    Resilient to slow / partially-failing S3:
    - Root fetch uses _STAC_ROOT_TIMEOUT (generous); failure returns {} + records __root__ error.
    - Parent and sub-child fetches run in a bounded ThreadPoolExecutor using _STAC_CHILD_TIMEOUT
      (tight); individual failures are isolated and recorded in STAC_LOAD_ERRORS rather than
      aborting the whole walk.
    - For the default catalog (no catalog_url), module-level state (STAC_DATASETS, _STAC_RAW,
      STAC_LOAD_ERRORS) is replaced after the pool drains.
    """
    url = catalog_url or STAC_CATALOG_URL

    # --- Phase 1: root fetch (must succeed) ---
    root_io = _TimeoutStacIO(token=catalog_token, timeout=_STAC_ROOT_TIMEOUT)
    try:
        cat = pystac.Catalog.from_file(url, stac_io=root_io)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        print(f"⚠️ Failed to load STAC root catalog: {reason}", file=sys.stderr)
        if not catalog_url:
            STAC_LOAD_ERRORS.clear()
            STAC_LOAD_ERRORS["__root__"] = reason
        return {}

    # Enumerate parent child-links directly from the parsed root (no HTTP).
    parent_links = [
        (l.href, getattr(l, "title", None))
        for l in (cat.links or [])
        if l.rel == "child"
    ]

    # --- Phase 2: dynamic parallel fetch ---
    # parent_cols[id] = pystac.Collection (only successfully-fetched parents)
    # subchild_cols_by_parent[parent_col_id] = {subchild_id: pystac.Collection}
    # errors[identifier] = reason
    parent_cols: dict = {}
    subchild_cols_by_parent: dict = {}
    errors: dict = {}

    with ThreadPoolExecutor(max_workers=_STAC_FETCH_CONCURRENCY) as pool:
        # future -> ("parent", href) OR ("subchild", parent_col_id)
        pending: dict = {}

        for href, title in parent_links:
            fut = pool.submit(_fetch_parent, href, title, catalog_token)
            pending[fut] = ("parent", href)

        while pending:
            done, _ = wait(pending.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                kind, ctx = pending.pop(fut)
                if kind == "parent":
                    col, subchild_hrefs, error = fut.result()
                    if col is not None:
                        parent_cols[col.id] = col
                        if subchild_hrefs:
                            subchild_cols_by_parent[col.id] = {}
                        for sub_href in subchild_hrefs:
                            sub_fut = pool.submit(
                                _fetch_subchild, sub_href, col.id, catalog_token,
                            )
                            pending[sub_fut] = ("subchild", col.id)
                    if error:
                        errors.update(error)
                else:  # subchild
                    parent_col_id = ctx
                    col, error = fut.result()
                    if col is not None:
                        subchild_cols_by_parent.setdefault(parent_col_id, {})[col.id] = col
                        print(f"📥 Loaded sub-child: {col.id}", file=sys.stderr)
                    if error:
                        errors.update(error)

    # --- Phase 3: render markdown / dicts; swap module state ---
    datasets: dict = {}
    raw: dict = {}
    for parent_id, col in parent_cols.items():
        sub_cols = list(subchild_cols_by_parent.get(parent_id, {}).values())
        datasets[parent_id] = _format_collection(col, sub_children=sub_cols)
        raw[parent_id] = _collection_to_dict(
            col, sub_children=sub_cols if sub_cols else None,
        )
        for sub_id, sub_col in subchild_cols_by_parent.get(parent_id, {}).items():
            # Explicit empty list so _format_collection doesn't fire another HTTP call
            # trying to discover (absent) grandchildren.
            datasets[sub_id] = _format_collection(sub_col, sub_children=[])
            raw[sub_id] = _collection_to_dict(sub_col, sub_children=None)

    print(
        f"📂 Loaded {len(datasets)} collections "
        f"({len(errors)} failed) from STAC: {url}",
        file=sys.stderr,
    )
    for ident, reason in errors.items():
        print(f"⚠️ Child fetch failed: {ident} — {reason}", file=sys.stderr)

    if not catalog_url:
        STAC_DATASETS.clear()
        STAC_DATASETS.update(datasets)
        _STAC_RAW.clear()
        _STAC_RAW.update(raw)
        STAC_LOAD_ERRORS.clear()
        STAC_LOAD_ERRORS.update(errors)

    return datasets
```

Also update the module-level load at the bottom of `stac.py` (around line 294) — it stays the same call but now populates STAC_LOAD_ERRORS too:

```python
# Load once at startup
STAC_DATASETS = fetch_stac_catalog()
```

No change needed to that line — but verify it still reads `STAC_DATASETS = fetch_stac_catalog()` after the rewrite.

Wait — there's an ordering concern. The existing code has `STAC_DATASETS = fetch_stac_catalog()` which creates a new dict and binds the name. Our new code does `STAC_DATASETS.clear(); STAC_DATASETS.update(...)` which mutates an existing dict. But at module-import time, `STAC_DATASETS` doesn't exist yet when `fetch_stac_catalog()` first runs.

Fix: declare `STAC_DATASETS` (and `_STAC_RAW`, `STAC_LOAD_ERRORS`) as empty dicts at the module level BEFORE the `STAC_DATASETS = fetch_stac_catalog()` line, and change that line to simply call `fetch_stac_catalog()` for side effects:

Find and replace the module-level load (around line 294):

```python
# Load once at startup
STAC_DATASETS = fetch_stac_catalog()
```

Replace with:

```python
# Module-level caches — declared before the startup load so the loader's
# clear()/update() pattern works on first call.
STAC_DATASETS: dict[str, str] = {}

# Kick off the initial load at import. Populates STAC_DATASETS, _STAC_RAW,
# STAC_LOAD_ERRORS in place.
fetch_stac_catalog()
```

And the existing `_STAC_RAW` / `STAC_LOAD_ERRORS` declarations (from Task 1 and earlier) need to remain ABOVE this line. Verify their relative order by scanning the file.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k "root_failure or one_parent or all_parents or subchild_fails or concurrency_env"`

Expected: All five tests pass.

- [ ] **Step 5: Run the full stac test suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_stac.py -v`

Expected: All tests pass. The existing `TestChildCollectionIndexing` tests in particular exercise the sub-child indexing path — they should still work because Phase 3 still renders sub-children as their own dict entries.

- [ ] **Step 6: Run the server test suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_server.py -v`

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add stac.py tests/test_stac.py
git commit -m "stac: rewrite fetch_stac_catalog as dynamic-enqueue parallel walk

Replaces the serial try/except-wrapped pystac walk with a three-phase
loader:

  1. Root fetch with _STAC_ROOT_TIMEOUT (default 15s) — hard prereq.
  2. ThreadPoolExecutor (default 8 workers) fetches parents in parallel;
     as each parent's JSON arrives, its sub-child links are submitted
     to the same pool. Each worker does exactly one HTTP GET, bounded
     by _STAC_CHILD_TIMEOUT (default 5s). Workers never raise — all
     exceptions caught and recorded.
  3. Main thread renders markdown/dicts after the pool drains and
     replaces module state in place.

Partial failures do not kill the load. Per-child errors are recorded
in the module-level STAC_LOAD_ERRORS dict keyed by best-available
identifier (real collection id on parse-success, else href-derived).

Wall-clock bound under pathological S3: ~40s worst case, ~20s typical,
fitting within the readiness probe's 40s budget.

Part of mcp-data-server#65."
```

---

## Task 6: Append error footer to `list_datasets()` output

**Files:**
- Modify: `stac.py:297-312` (the `list_datasets` function)
- Test: `tests/test_stac.py` (add to `TestFetchResilience`)

When `STAC_LOAD_ERRORS` is non-empty, agents calling `browse_stac_catalog` should see a short footer noting which collections failed to load, so they don't confidently claim "dataset X doesn't exist" when really X failed to load.

- [ ] **Step 1: Write failing tests**

Append to `TestFetchResilience`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k list_datasets`

Expected: FAIL with assertion error on "⚠️ not in out" or similar.

- [ ] **Step 3: Modify `list_datasets` in `stac.py`**

Find the existing `list_datasets` function (around line 297) and replace it with:

```python
def list_datasets(catalog_url: str = None, catalog_token: str = None) -> str:
    """List all available datasets from the STAC catalog.

    Appends a warning footer when `STAC_LOAD_ERRORS` is non-empty, so callers
    can distinguish "not in catalog" from "failed to load this time."
    """
    if catalog_url:
        datasets = fetch_stac_catalog(catalog_url, catalog_token=catalog_token)
        url = catalog_url
        # Errors for custom catalogs are not tracked in module state; caller
        # can detect failure via returned dict being empty or partial.
        footer_errors: dict = {}
    else:
        datasets = STAC_DATASETS
        url = STAC_CATALOG_URL
        footer_errors = STAC_LOAD_ERRORS
    if not datasets and not footer_errors:
        return f"No datasets loaded. STAC catalog: {url}"
    lines = [f"# Available Datasets ({len(datasets)} collections)\n"]
    lines.append(f"STAC catalog: `{url}`\n")
    for cid, summary in datasets.items():
        first_line = summary.split("\n")[0]
        lines.append(f"- **{cid}**: {first_line}")
    if footer_errors:
        lines.append("")
        err_pairs = ", ".join(f"{k} ({v.split(':', 1)[0]})" for k, v in footer_errors.items())
        lines.append(
            f"⚠️ {len(footer_errors)} collection"
            f"{'s' if len(footer_errors) != 1 else ''} could not be loaded: {err_pairs}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stac.py::TestFetchResilience -v -k list_datasets`

Expected: Both tests pass.

- [ ] **Step 5: Run the full stac test suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_stac.py -v`

Expected: All pass. `TestCatalogUrlParameter::test_list_datasets_default_url` and `test_list_datasets_custom_url` should still pass because the happy-path output shape is unchanged when `STAC_LOAD_ERRORS` is empty.

- [ ] **Step 6: Commit**

```bash
git add stac.py tests/test_stac.py
git commit -m "stac: append partial-load footer to list_datasets output

When STAC_LOAD_ERRORS is non-empty, append a ⚠️ footer to list_datasets()
so callers see which collections failed to load this time. Prevents the
agent from incorrectly concluding 'dataset X doesn't exist' when X
actually timed out.

Completes the partial-result fallback from mcp-data-server#65."
```

---

## Task 7: Final verification and PR

**Files:** none modified — runs the full suites and opens the PR.

- [ ] **Step 1: Run the full test suite across both modules**

Run: `.venv/bin/python -m pytest tests/test_stac.py tests/test_server.py -v`

Expected: All tests pass. If any fail, fix before continuing.

- [ ] **Step 2: Quick manual smoke — import the module, check load completed**

Run:

```bash
.venv/bin/python -c "import stac; print(f'loaded {len(stac.STAC_DATASETS)} datasets, {len(stac.STAC_LOAD_ERRORS)} errors')"
```

Expected: Off-cluster (no S3 access), you'll see `loaded 0 datasets, 1 errors` with `__root__` in STAC_LOAD_ERRORS (DNS failure for the internal hostname). That's correct behavior. On-cluster it would load the real catalog.

- [ ] **Step 3: Push the branch**

Run: `git push -u origin spec/stac-catalog-resilience`

(Or, if a separate implementation branch is preferred, create one from the spec branch and push that.)

- [ ] **Step 4: Open the PR**

```bash
gh pr create --title "stac: resilience to slow/unreliable S3 (Fixes #65)" --body "$(cat <<'EOF'
## Summary

Makes \`fetch_stac_catalog()\` in \`stac.py\` survive slow/unreliable S3 via:
- **Split timeouts**: \`STAC_ROOT_TIMEOUT\` (default 15s, hard prereq) and \`STAC_CHILD_TIMEOUT\` (default 5s, individually skippable). Back-compat: \`STAC_TIMEOUT\` alone still works.
- **Bounded parallelism**: \`ThreadPoolExecutor\` with \`STAC_FETCH_CONCURRENCY\` workers (default 8), dynamic enqueue — sub-children submitted as each parent's JSON arrives.
- **Partial-result fallback**: per-child failures recorded in \`STAC_LOAD_ERRORS\`; \`list_datasets()\` appends a warning footer.

Sync startup retained — with the above, the worst-case load fits in the existing readiness probe's 40s budget.

Design doc: \`docs/superpowers/specs/2026-04-16-stac-catalog-resilience-design.md\`
Implementation plan: \`docs/superpowers/plans/2026-04-16-stac-catalog-resilience.md\`

## Test plan

- [x] Unit tests cover: root failure, single-parent failure, all-parent failure, sub-child failure, env vars honored, back-compat \`STAC_TIMEOUT\`, footer rendering
- [x] \`pytest tests/test_stac.py tests/test_server.py\` — all pass
- [ ] After merge: deploy to dev, verify logs show \`📂 Loaded N collections\` and monitor next S3 incident

Fixes #65
EOF
)"
```

- [ ] **Step 5: Delete local branch after merge**

After the PR is squash-merged:

```bash
git checkout main && git pull && git branch -d spec/stac-catalog-resilience
```

---

## Self-Review (author's pre-flight)

**Spec coverage:** Every item in the design doc maps to a task —
- Split timeouts → Task 1 + Task 2
- `_child_identifier` helper → Task 3
- Workers → Task 4
- Rewritten `fetch_stac_catalog` → Task 5
- Footer in `list_datasets` → Task 6
- Verification + PR → Task 7

**Placeholder scan:** No "TBD" / "TODO" / "similar to Task N" / "handle edge cases" — all code is spelled out inline.

**Type consistency:** Worker return shapes are stable across Tasks 4 and 5. `_fetch_parent` returns `(col_or_None, subchild_hrefs, error_or_None)`; `_fetch_subchild` returns `(col_or_None, error_or_None)`. Matching signatures in Task 4 tests and Task 5 usage.

**Known tradeoffs:**
- Module state swap (`clear()` + `update()`) has a brief window of emptiness visible to racing readers. Design doc acknowledges this and accepts the tradeoff (no lock).
- Wall-clock estimates are projections, not measurements; Task 7 notes the first on-cluster incident as the validation opportunity.
