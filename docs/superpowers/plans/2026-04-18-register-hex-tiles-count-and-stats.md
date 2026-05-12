# register_hex_tiles: COUNT property + per-resolution value stats

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `register_hex_tiles` so `agg="COUNT"` emits a colorable `count` MVT property and returns per-resolution min/max stats for all value columns — closes [#78](https://github.com/boettiger-lab/mcp-data-server/issues/78).

**Architecture:** When `agg="COUNT"`, the pyramid builder emits a dedicated `count` column (`COUNT(*)` at parent levels, literal `1` at the finest level) regardless of whether the caller's SQL supplies value columns. After the pyramid COPY completes, `register_hex_tiles` runs one `MIN`/`MAX` aggregate per resolution per output column and returns a nested `value_stats.{col}.by_res.{res}.{min,max}` structure, also persisting it in the `metadata.json` sidecar. Ships jointly with client-side paint changes in [geo-agent#174](https://github.com/boettiger-lab/geo-agent/issues/174).

**Tech Stack:** Python 3.11, DuckDB (h3, spatial, httpfs extensions), pytest. Tests run with `.venv/bin/pytest`.

---

## File Structure

**Modified:**
- `tiles/pyramid.py` — `build_pyramid_sql` branches on COUNT vs other aggs; `_inspect_user_sql` permits zero value columns; `register_hex_tiles` computes stats and attaches them to the return value + metadata sidecar.
- `tests/test_tile_pyramid.py` — adds COUNT-column cases to `TestBuildPyramidSQL`, stats cases to `TestRegisterHexTiles` and `TestTilesetMetadata`.
- `server.py` — docstring for the MCP-registered `register_hex_tiles` tool (used as injected tool description) updated to reflect the new contract.

**Not modified:**
- `tiles/endpoint.py` — tile serving is unchanged; output columns in the pyramid parquet are already auto-propagated as MVT properties via `SELECT src.* EXCLUDE (h)`.
- `tiles/db.py`, `tiles/tile_math.py` — unaffected.

---

## Design Decisions (locked in — do not rehash)

1. **`agg="COUNT"` emits exactly one output column named `count`.** Any value columns in the caller's SQL beyond the H3 index are dropped silently. Rationale: callers of `agg="COUNT"` want row counts; the current "value column name holds count" behavior is the bug being fixed and no one depends on it.
2. **For non-COUNT aggs, behavior is unchanged** — at least one value column is still required, and each is aggregated via the chosen agg.
3. **`count` at the finest level is the literal `1`.** Rows in the user's SQL are assumed to be distinct hexes at `finest_res` (either because the caller used `DISTINCT` or their data is naturally unique). Document this expectation; do not add a `GROUP BY` at finest level — it would change existing semantics for non-COUNT aggs.
4. **`value_stats` is computed for every output value column, uniformly** — not special-cased to `count`. Shape: `{<col>: {"by_res": {"<res>": {"min": <num>, "max": <num>}}}}`. Keys under `by_res` are string-typed resolutions (JSON-friendly; matches client-side `['get', 'res']` which returns a number but coerces cleanly in a `match` expression).
5. **Persist `value_stats` in `metadata.json`** in addition to returning it from `register_hex_tiles`. Single source of truth for any future `/stats` endpoint.
6. **Do not add a global `count` column for non-COUNT aggs.** Out of scope for this issue.

---

## Task 1: `build_pyramid_sql` emits `count` column for `agg="COUNT"`

**Files:**
- Modify: `tiles/pyramid.py:15-58`
- Test: `tests/test_tile_pyramid.py` (extend `TestBuildPyramidSQL`)

- [ ] **Step 1: Add failing unit tests for COUNT-mode SQL shape**

Append to `tests/test_tile_pyramid.py` inside `class TestBuildPyramidSQL` (before the `import os` split around line 82):

```python
    def test_count_mode_emits_count_column_no_value_cols(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8 FROM src",
            finest_res=8, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Parents: COUNT(*) AS count at every parent level.
        assert "COUNT(*) AS count" in sql
        # Finest: literal 1 AS count (no aggregation at finest level).
        assert "1 AS count" in sql
        # No stray references to user value columns (there are none).
        assert 'AVG(' not in sql and 'SUM(' not in sql

    def test_count_mode_finest_level_has_literal_one(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8 FROM src",
            finest_res=5, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Finest-level SELECT is identifiable by the "5 AS res" literal.
        finest_select = re.search(r"SELECT[^)]*?5 AS res FROM src", sql, re.DOTALL)
        assert finest_select is not None
        assert "1 AS count" in finest_select.group(0)
        assert "COUNT(*)" not in finest_select.group(0)
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py::TestBuildPyramidSQL::test_count_mode_emits_count_column_no_value_cols tests/test_tile_pyramid.py::TestBuildPyramidSQL::test_count_mode_finest_level_has_literal_one -v
```

Expected: both FAIL — current `build_pyramid_sql` wraps value columns with `COUNT("col")` and has no `COUNT(*)` path.

- [ ] **Step 3: Modify `build_pyramid_sql` to branch on `agg="COUNT"`**

Replace `tiles/pyramid.py:15-58` with:

```python
def build_pyramid_sql(
    user_sql: str,
    finest_res: int,
    min_res: int,
    agg: str,
    value_columns: List[str],
    h3_column: str,
    output_uri: str,
) -> str:
    """Return the COPY ... TO SQL that writes a partitioned pyramid.

    The finest-resolution level stores raw per-row values; parent resolutions
    aggregate via the chosen `agg` function.

    When agg="COUNT", `value_columns` must be exactly ["count"] and the SQL
    emits `COUNT(*) AS count` at parent levels and `1 AS count` at the finest
    level. Any value columns from the user SQL are ignored — callers requesting
    COUNT get row-count semantics, nothing else.
    """
    _VALID_AGG = {"AVG", "SUM", "MIN", "MAX", "COUNT"}
    agg_upper = agg.upper()
    if agg_upper not in _VALID_AGG:
        raise ValueError(f"agg must be one of {_VALID_AGG}, got {agg!r}")

    qh = f'"{h3_column}"'

    if agg_upper == "COUNT":
        parent_values = "COUNT(*) AS count"
        finest_values = "1 AS count"
    else:
        parent_values = ", ".join(f'{agg_upper}("{c}") AS "{c}"' for c in value_columns)
        finest_values = ", ".join(f'"{c}"' for c in value_columns)

    selects = []
    for res in range(min_res, finest_res):
        selects.append(
            f"  SELECT h3_cell_to_parent({qh}, {res}) AS h, "
            f"{parent_values}, {res} AS res FROM src GROUP BY 1"
        )
    selects.append(
        f"  SELECT {qh} AS h, {finest_values}, {finest_res} AS res FROM src"
    )

    body = "\n  UNION ALL\n".join(selects)

    return (
        "COPY (\n"
        f"  WITH src AS (\n{user_sql}\n  )\n"
        f"{body}\n"
        f") TO '{output_uri}' "
        f"(FORMAT PARQUET, PARTITION_BY (res), OVERWRITE_OR_IGNORE)"
    )
```

- [ ] **Step 4: Run the new tests and confirm they pass**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py::TestBuildPyramidSQL -v
```

Expected: all `TestBuildPyramidSQL` tests PASS (including pre-existing ones — the non-COUNT paths are unchanged).

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "pyramid: emit count column when agg=COUNT"
```

---

## Task 2: `_inspect_user_sql` permits zero value columns

**Files:**
- Modify: `tiles/pyramid.py:75-84`
- Test: `tests/test_tile_pyramid.py` (new `TestInspectUserSQL` class)

- [ ] **Step 1: Add failing test for zero-value-column SQL**

Append after the existing `TestBuildPyramidSQL` class in `tests/test_tile_pyramid.py`:

```python
from tiles.pyramid import _inspect_user_sql


class TestInspectUserSQL:
    def test_allows_single_column_sql(self, h3_conn):
        # SQL returning only the h3 index — valid input for agg="COUNT".
        h3_col, value_cols = _inspect_user_sql(
            h3_conn, "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5"
        )
        assert h3_col == "h5"
        assert value_cols == []

    def test_still_extracts_value_cols_when_present(self, h3_conn):
        h3_col, value_cols = _inspect_user_sql(
            h3_conn,
            "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS v1, 2.0 AS v2",
        )
        assert h3_col == "h5"
        assert value_cols == ["v1", "v2"]

    def test_rejects_empty_sql(self, h3_conn):
        with pytest.raises(ValueError, match="no columns"):
            _inspect_user_sql(h3_conn, "SELECT * FROM (VALUES (1)) t WHERE 1=0 LIMIT 0")
```

Note: `h3_conn` fixture is already defined later in the file (line 98); placement order matters only for readability, not pytest discovery.

- [ ] **Step 2: Run the new tests and confirm the first two fail**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py::TestInspectUserSQL -v
```

Expected: `test_allows_single_column_sql` FAILS with `ValueError: user SQL must return at least one value column after the H3 index`. The others pass.

- [ ] **Step 3: Relax `_inspect_user_sql`**

Replace `tiles/pyramid.py:75-84` with:

```python
def _inspect_user_sql(con: duckdb.DuckDBPyConnection, user_sql: str):
    """Run user SQL with LIMIT 0 to extract column names without materializing data.

    Returns (h3_column, value_columns). value_columns may be empty — the caller
    is responsible for validating that an empty list is acceptable for the
    chosen aggregation (only agg="COUNT" supports it).
    """
    columns = con.sql(f"SELECT * FROM ({user_sql}) LIMIT 0").columns
    if not columns:
        raise ValueError("user SQL returned no columns")
    return columns[0], list(columns[1:])
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py::TestInspectUserSQL -v
```

Expected: all three tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "pyramid: allow user SQL with h3 index only when agg=COUNT"
```

---

## Task 3: `register_hex_tiles` wires COUNT mode end-to-end

**Files:**
- Modify: `tiles/pyramid.py:87-156` (the `register_hex_tiles` function)
- Test: `tests/test_tile_pyramid.py` (extend `TestRegisterHexTiles`)

- [ ] **Step 1: Add failing integration tests for COUNT mode**

Append inside `class TestRegisterHexTiles` in `tests/test_tile_pyramid.py`:

```python
    def test_count_agg_with_index_only_sql(self, local_bucket, h3_conn):
        # Three rows → two map to the same res=5 cell, one to another.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5
            FROM (VALUES (37.8, -122.3),
                         (37.80001, -122.30001),
                         (40.0, -100.0)) t(lat, lng)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=2, agg="COUNT", zoom_offset=4,
        )
        assert result["value_columns"] == ["count"]
        # Verify the parquet pyramid actually has the count column.
        import duckdb as _d
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "*.parquet")
            cols = _d.connect(":memory:").sql(f"SELECT * FROM read_parquet('{uri}') LIMIT 0").columns
            assert "count" in cols, f"res={res} missing 'count' column: {cols}"

    def test_count_agg_ignores_user_value_columns(self, local_bucket, h3_conn):
        # User supplies an extra column; COUNT mode drops it.
        user_sql = """
            SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 'ignored' AS extra
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=5, agg="COUNT", zoom_offset=4,
        )
        assert result["value_columns"] == ["count"]

    def test_non_count_still_requires_value_columns(self, local_bucket, h3_conn):
        with pytest.raises(ValueError, match="value column"):
            register_hex_tiles(
                con=h3_conn,
                sql="SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5",
                finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
            )
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py::TestRegisterHexTiles::test_count_agg_with_index_only_sql tests/test_tile_pyramid.py::TestRegisterHexTiles::test_count_agg_ignores_user_value_columns tests/test_tile_pyramid.py::TestRegisterHexTiles::test_non_count_still_requires_value_columns -v
```

Expected: all three FAIL. The first two fail because `_inspect_user_sql` returns an empty value list, Task 2 accepts it, but `register_hex_tiles` then passes `[]` as `value_columns` to `build_pyramid_sql` — the result is syntactically broken SQL or a COUNT emission with no `count` in `value_columns`. The third fails because `register_hex_tiles` no longer raises on empty value columns after Task 2.

- [ ] **Step 3: Update `register_hex_tiles` to own the value-columns decision**

Replace `tiles/pyramid.py:87-156` with:

```python
def register_hex_tiles(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    finest_res: int,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = 4,
) -> dict:
    """Materialize a partitioned parquet pyramid and return tile-endpoint metadata.

    The connection must have httpfs, spatial, and h3 extensions loaded.

    Value-column contract:
    - agg="COUNT": user SQL needs only the H3 index column. Output has a single
      `count` column (row count per hex at parent resolutions; 1 at finest).
      Any extra columns in the user SQL are ignored.
    - Other aggs: user SQL must return at least one value column after the H3
      index. Each is aggregated via `agg` at parent resolutions and passed
      through raw at the finest level.
    """
    if finest_res < min_res:
        raise ValueError(f"finest_res ({finest_res}) must be >= min_res ({min_res})")

    h3_column, sql_value_columns = _inspect_user_sql(con, sql)
    agg_upper = agg.upper()
    if agg_upper == "COUNT":
        value_columns = ["count"]
    else:
        if not sql_value_columns:
            raise ValueError(
                "user SQL must return at least one value column after the H3 index "
                "(or use agg='COUNT')"
            )
        value_columns = sql_value_columns

    h = content_hash(sql=sql, finest_res=finest_res, min_res=min_res, agg=agg, zoom_offset=zoom_offset)
    output_uri = f"{_bucket_base()}/hex/{h}/"

    pyramid_sql = build_pyramid_sql(
        user_sql=sql,
        finest_res=finest_res,
        min_res=min_res,
        agg=agg,
        value_columns=value_columns,
        h3_column=h3_column,
        output_uri=output_uri,
    )
    if not output_uri.startswith("s3://"):
        os.makedirs(output_uri, exist_ok=True)
    con.sql(pyramid_sql)

    metadata = {
        "finest_res": finest_res,
        "min_res": min_res,
        "agg": agg,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
    }
    metadata_sql = (
        f"COPY (SELECT '{_json_dumps_escaped(metadata)}' AS j) "
        f"TO '{output_uri}metadata.json' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(metadata_sql)

    finest_uri = f"{output_uri}res={finest_res}/*.parquet"
    bounds_row = con.sql(
        f"SELECT "
        f"MIN(h3_cell_to_lat(h)) AS s, MAX(h3_cell_to_lat(h)) AS n, "
        f"MIN(h3_cell_to_lng(h)) AS w, MAX(h3_cell_to_lng(h)) AS e, "
        f"COUNT(*) AS ct "
        f"FROM read_parquet('{finest_uri}')"
    ).fetchone()
    w, s, e, n, feature_count = bounds_row[2], bounds_row[0], bounds_row[3], bounds_row[1], bounds_row[4]

    tile_url_template = f"{_public_base_url()}/tiles/hex/{h}/{{z}}/{{x}}/{{y}}.pbf"

    return {
        "tile_url_template": tile_url_template,
        "hash": h,
        "bounds": [w, s, e, n],
        "finest_res": finest_res,
        "min_res": min_res,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "feature_count_finest": feature_count,
    }
```

- [ ] **Step 4: Run the full pyramid test file and confirm all pass**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py -v
```

Expected: all tests PASS, including the three new integration tests and the pre-existing `test_returns_value_columns` (which uses `agg="AVG"` and is unaffected).

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "pyramid: register_hex_tiles emits count for agg=COUNT"
```

---

## Task 4: Compute per-resolution `value_stats`

**Files:**
- Modify: `tiles/pyramid.py` (inside `register_hex_tiles`, after the pyramid COPY)
- Test: `tests/test_tile_pyramid.py` (extend `TestRegisterHexTiles` and `TestTilesetMetadata`)

- [ ] **Step 1: Add failing tests for `value_stats` in return value and metadata**

Append inside `class TestRegisterHexTiles`:

```python
    def test_value_stats_returned_per_resolution(self, local_bucket, h3_conn):
        # 5 rows, 3 distinct res=5 cells. COUNT per cell at res=5 should be
        # {1, 1, 3} → min=1, max=3 at res=5. At coarser parent resolutions all
        # rows likely collapse into fewer cells, producing larger max counts.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5
            FROM (VALUES (37.80, -122.30),
                         (37.80001, -122.30001),
                         (37.80002, -122.30002),
                         (38.00, -122.50),
                         (40.00, -100.00)) t(lat, lng)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=3, agg="COUNT", zoom_offset=4,
        )
        stats = result["value_stats"]
        assert set(stats.keys()) == {"count"}
        by_res = stats["count"]["by_res"]
        # Resolutions 3, 4, 5 all present (string keys).
        assert set(by_res.keys()) == {"3", "4", "5"}
        # At finest res each row contributes a cell count; min is 1.
        assert by_res["5"]["min"] == 1
        # At least one cell has 3 rows at res=5 (the three clustered points).
        assert by_res["5"]["max"] == 3
        # Coarser resolutions can only aggregate further — max is non-decreasing.
        assert by_res["4"]["max"] >= by_res["5"]["max"]
        assert by_res["3"]["max"] >= by_res["4"]["max"]

    def test_value_stats_for_non_count_agg(self, local_bucket, h3_conn):
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES (37.8, -122.3, 1.0),
                         (38.0, -122.5, 5.0)) t(lat, lng, val)
        """
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql,
            finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        stats = result["value_stats"]
        assert set(stats.keys()) == {"val"}
        assert stats["val"]["by_res"]["5"]["min"] == 1.0
        assert stats["val"]["by_res"]["5"]["max"] == 5.0
```

Append inside `class TestTilesetMetadata`:

```python
    def test_metadata_includes_value_stats(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=3, agg="COUNT", zoom_offset=4,
        )
        meta_path = local_bucket / "hex" / result["hash"] / "metadata.json"
        meta = _json.loads(meta_path.read_text())
        assert "value_stats" in meta
        assert set(meta["value_stats"]["count"]["by_res"].keys()) == {"3", "4", "5"}
        # Sanity: persisted stats equal the returned stats.
        assert meta["value_stats"] == result["value_stats"]
```

- [ ] **Step 2: Run the new tests and confirm they fail**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py::TestRegisterHexTiles::test_value_stats_returned_per_resolution tests/test_tile_pyramid.py::TestRegisterHexTiles::test_value_stats_for_non_count_agg tests/test_tile_pyramid.py::TestTilesetMetadata::test_metadata_includes_value_stats -v
```

Expected: all three FAIL with `KeyError: 'value_stats'`.

- [ ] **Step 3: Add stats computation to `register_hex_tiles`**

In `tiles/pyramid.py` inside `register_hex_tiles`, locate the block that writes the metadata sidecar (after `con.sql(pyramid_sql)` and before the bounds query). Replace it with the following — this both adds the stats computation and folds `value_stats` into the metadata dict BEFORE the sidecar is written:

```python
    # Per-resolution min/max for every output value column.
    value_stats = {}
    for col in value_columns:
        by_res = {}
        for res in range(min_res, finest_res + 1):
            uri = f"{output_uri}res={res}/*.parquet"
            row = con.sql(
                f'SELECT MIN("{col}") AS mn, MAX("{col}") AS mx '
                f"FROM read_parquet('{uri}')"
            ).fetchone()
            by_res[str(res)] = {"min": row[0], "max": row[1]}
        value_stats[col] = {"by_res": by_res}

    metadata = {
        "finest_res": finest_res,
        "min_res": min_res,
        "agg": agg,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "value_stats": value_stats,
    }
    metadata_sql = (
        f"COPY (SELECT '{_json_dumps_escaped(metadata)}' AS j) "
        f"TO '{output_uri}metadata.json' (FORMAT CSV, HEADER false, QUOTE '')"
    )
    con.sql(metadata_sql)
```

Then update the final `return {...}` dict to include `value_stats`:

```python
    return {
        "tile_url_template": tile_url_template,
        "hash": h,
        "bounds": [w, s, e, n],
        "finest_res": finest_res,
        "min_res": min_res,
        "zoom_offset": zoom_offset,
        "value_columns": value_columns,
        "value_stats": value_stats,
        "feature_count_finest": feature_count,
    }
```

- [ ] **Step 4: Run all pyramid tests and confirm they pass**

Run:
```bash
.venv/bin/pytest tests/test_tile_pyramid.py -v
```

Expected: every test PASSES — the three new stats tests plus all previously green tests.

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "pyramid: add per-resolution value_stats to register_hex_tiles"
```

---

## Task 5: Update the MCP tool docstring

**Files:**
- Modify: `server.py:221-249` (the `register_hex_tiles` wrapper docstring is the tool description the small LLM sees)

- [ ] **Step 1: Rewrite the docstring to describe the new contract**

Replace the docstring between `def register_hex_tiles(...)` (server.py:221) and `con = _get_tile_con()` (server.py:245) with:

```python
def register_hex_tiles(
    sql: str,
    finest_res: int,
    min_res: int = 2,
    agg: str = "AVG",
    zoom_offset: int = 4,
) -> dict:
    """Materialize a partitioned H3 hex pyramid to public object storage and return
    a MapLibre-compatible vector tile URL template.

    Use this tool for H3 hex datasets too large to return as markdown table —
    roughly >100k cells, or any case where the user wants to visualize hexes
    directly on the map (rather than color an existing polygon layer).

    Input SQL contract:
    - First column must be an H3 index at resolution `finest_res`.
    - For agg="COUNT": no other columns required. Output has a single `count`
      column (row count per hex). If the user SQL returns extra columns, they
      are ignored.
    - For agg in {AVG, SUM, MIN, MAX}: at least one numeric value column must
      follow the H3 index. Each is aggregated by `agg` at each coarser
      resolution down to `min_res`.

    Returns a dict with:
    - `tile_url_template`: MapLibre vector tile URL with {z}/{x}/{y} placeholders.
    - `value_columns`: list of output column names available as MVT feature
      properties. For agg="COUNT" this is ["count"]; otherwise the user's
      value columns.
    - `value_stats`: {<col>: {"by_res": {"<res>": {"min": <num>, "max": <num>}}}}.
      Clients colouring across multiple zooms should match on the per-feature
      `res` property to pick the right min/max band for each resolution, since
      COUNT/SUM ranges differ by ~7× per H3 level.
    - `bounds`, `finest_res`, `min_res`, `zoom_offset`, `feature_count_finest`:
      tileset metadata.

    MapLibre usage:
        map.addSource(id, {type: 'vector', tiles: [tile_url_template], minzoom: 0, maxzoom: 14});
        map.addLayer({..., 'source-layer': 'hex', paint: {...}});
    """
```

- [ ] **Step 2: Verify the full test suite still passes**

Run:
```bash
.venv/bin/pytest tests/ -v
```

Expected: all tests PASS. No new tests added in this task; we are only verifying the docstring change did not affect behavior.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "server: document register_hex_tiles COUNT and value_stats contract"
```

---

## Task 6: Open PR

**Files:** none — this task is git / GitHub only.

- [ ] **Step 1: Push branch and open PR**

Run:
```bash
git push -u origin HEAD
gh pr create --title "register_hex_tiles: emit count property and per-resolution value_stats" --body "$(cat <<'EOF'
## Summary
- `agg=\"COUNT\"` now emits a dedicated `count` MVT property (row count per hex at parents, `1` at finest) and accepts SQL that returns only the H3 index column.
- `register_hex_tiles` returns and persists `value_stats.{col}.by_res.{res}.{min,max}` for every output value column, enabling clients to paint correctly across H3 resolutions whose aggregate ranges differ by ~7× per level.
- MCP tool docstring updated to reflect the new contract.

Fixes #78.

Requires matching client-side paint changes in boettiger-lab/geo-agent#174 — ship together.

## Test plan
- [x] `.venv/bin/pytest tests/test_tile_pyramid.py -v` passes.
- [ ] After merge + deploy: call `register_hex_tiles(sql=\"SELECT DISTINCT h8 FROM ... WHERE State_Nm = 'AZ'\", finest_res=8, min_res=2, agg=\"COUNT\")`; confirm response has `value_columns == [\"count\"]` and non-trivial `value_stats.count.by_res` spanning res 2–8.
- [ ] Decode one tile per zoom level; confirm every feature has a numeric `count` property.
- [ ] Cross-check returned per-res `max` against `SELECT MAX(ct) FROM (SELECT COUNT(*) ct FROM src GROUP BY h3_cell_to_parent(h8, <res>))`.
EOF
)"
```

Expected: PR created and URL printed.

- [ ] **Step 2: Report the PR URL**

Paste the PR URL into the session so the user can review.

---

## Verification Checklist (from issue #78)

After merge and deploy:

- [ ] Call `register_hex_tiles(sql="SELECT DISTINCT h8 FROM ... WHERE State_Nm = 'AZ'", finest_res=8, min_res=2, agg="COUNT")`.
- [ ] Returned `value_columns == ["count"]`.
- [ ] Decode `.../tiles/hex/<hash>/10/193/407.pbf`; every feature exposes a numeric `count` property.
- [ ] Per-tile `MIN`/`MAX` of `count` across the whole pyramid matches per-hex `COUNT(*)` computed directly from the source Parquet.
- [ ] `value_stats.count.by_res` contains entries for res 2 through 8, with `max` monotonically non-increasing from coarse to fine.
- [ ] Coordinate deploy with boettiger-lab/geo-agent#174 so MapLibre paint expressions pick up per-resolution ranges.
