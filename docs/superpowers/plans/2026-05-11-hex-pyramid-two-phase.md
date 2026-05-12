# Two-phase iterative hex pyramid build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `build_pyramid_sql`'s single COPY with UNION ALL across 7 resolutions with a two-phase build — one COPY from source → finest, then iterative parent rollups from the previous res — so global high-cardinality datasets (GBIF-scale) build without OOM.

**Architecture:** `tiles/pyramid.py` exposes a new function `build_pyramid_statements` returning an ordered list of `COPY` statements; `register_hex_tiles` executes them sequentially via existing `con.sql(...)`. Phase 1 is one finest-from-source COPY; Phase 2 is N-1 small COPYs each reading the previous res partition. Output schema and on-disk layout (`res=N/h0=X/`) are unchanged for COUNT/SUM/MIN/MAX; AVG mode adds an internal `count` column for correct weighted parent averages. Layout version bumped to `v3-iterative` so old hashes don't collide.

**Tech Stack:** DuckDB 1.4+ with h3 community extension, Python 3.10+, pytest.

**Spec:** `docs/superpowers/specs/2026-05-11-hex-pyramid-two-phase-design.md`

---

## File Structure

| File | Change |
|---|---|
| `tiles/tile_math.py` | Bump `_LAYOUT_VERSION` from `"v2-h0"` to `"v3-iterative"` |
| `tiles/pyramid.py` | Rename `build_pyramid_sql` → `build_pyramid_statements`; new return type `list[str]`; rewrite body for two-phase output. Update `register_hex_tiles` to loop over statements |
| `tests/test_tile_math.py` | Add v3-hash collision test alongside the existing v2 pin |
| `tests/test_tile_pyramid.py` | Add `TestBuildPyramidStatements` class; update or remove tests in `TestBuildPyramidSQL` that assert single-string return / UNION ALL shape |

No changes to: `tiles/endpoint.py`, `server.py`, the `register_hex_tiles` MCP signature, tile-serving SQL, or the on-disk pyramid layout for non-AVG modes.

---

## Task 1: Bump `_LAYOUT_VERSION` to `v3-iterative`

**Files:**
- Modify: `tiles/tile_math.py`
- Test: `tests/test_tile_math.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tile_math.py` inside `class TestContentHash`:

```python
    def test_two_phase_layout_does_not_collide_with_v2_h0_hashes(self):
        # v3-iterative layout (PR for two-phase pyramid) writes a different
        # on-disk pyramid for the same user inputs. Bump the layout version
        # so the new hash never overlaps a v2-h0 pyramid on S3.
        v2_h0_hash = "31aff79f1d3a9b6f"  # fill in actual value after step 3
        new_hash = content_hash(
            sql="SELECT 1 AS h, 2 AS v",
            finest_res=8, min_res=2, agg="AVG", zoom_offset=-1,
        )
        assert new_hash != v2_h0_hash
```

- [ ] **Step 2: Find the actual current (v2-h0) hash so the test is meaningful**

Run from the worktree root:

```bash
.venv/bin/python -c "from tiles.tile_math import content_hash; print(content_hash(sql='SELECT 1 AS h, 2 AS v', finest_res=8, min_res=2, agg='AVG', zoom_offset=-1))"
```

Replace the placeholder `"31aff79f1d3a9b6f"` in the test with the printed value. Save.

- [ ] **Step 3: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_tile_math.py::TestContentHash::test_two_phase_layout_does_not_collide_with_v2_h0_hashes -v
```

Expected: FAIL with `assert <same-hash> != "<same-hash>"`.

- [ ] **Step 4: Bump the layout version**

In `tiles/tile_math.py` change:

```python
_LAYOUT_VERSION = "v2-h0"
```

to:

```python
_LAYOUT_VERSION = "v3-iterative"
```

- [ ] **Step 5: Run all tile_math tests to verify**

```bash
.venv/bin/python -m pytest tests/test_tile_math.py -v
```

Expected: all pass, including the new collision test and the existing `test_h0_partition_layout_does_not_collide_with_pre_h0_hashes` (which pins the pre-h0 hash and is unaffected by the v2→v3 bump).

- [ ] **Step 6: Commit**

```bash
git add tiles/tile_math.py tests/test_tile_math.py
git commit -m "tiles: bump layout version to v3-iterative for two-phase pyramid"
```

---

## Task 2: Implement `build_pyramid_statements` and update `register_hex_tiles`

**Files:**
- Modify: `tiles/pyramid.py:21-96` (replace `build_pyramid_sql`), `tiles/pyramid.py:223-234` (update `register_hex_tiles` to loop)
- Test: `tests/test_tile_pyramid.py`

This is the bulk of the work. The new function returns a list — one statement per resolution, ordered from finest to coarsest.

- [ ] **Step 1: Write the failing unit tests for the new builder**

Add a new test class at the top of `tests/test_tile_pyramid.py` (immediately after the imports), and the existing `TestBuildPyramidSQL` class will be updated in Step 6. New class:

```python
from tiles.pyramid import build_pyramid_statements


class TestBuildPyramidStatements:
    def test_returns_list_with_one_statement_per_resolution(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # finest_res=8, min_res=2 → res=8,7,6,5,4,3,2 = 7 statements.
        assert isinstance(stmts, list)
        assert len(stmts) == 7

    def test_first_statement_is_phase_1_finest_from_source(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        s = stmts[0]
        assert "FROM src" in s
        assert "8 AS res" in s
        assert "GROUP BY 1, 2" in s
        assert "PARTITION_BY (res, h0)" in s
        assert "OVERWRITE_OR_IGNORE" in s
        # h0 is computed from the h3 column in the src CTE once.
        assert 'h3_cell_to_parent("h8", 0)' in s

    def test_phase_2_reads_from_previous_resolution(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Index 1 is res=7, reading res=8.
        s = stmts[1]
        assert "res=8/**/*.parquet" in s
        assert "7 AS res" in s
        assert "h3_cell_to_parent(h, 7)" in s
        assert "hive_partitioning=true" in s
        # Phase 2 must not re-scan the source.
        assert "FROM src" not in s

    def test_phase_2_iterates_down_to_min_res(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # res=7 reads res=8, res=6 reads res=7, ..., res=2 reads res=3.
        for i, parent_res in enumerate(range(7, 1, -1), start=1):
            s = stmts[i]
            assert f"res={parent_res + 1}/**/*.parquet" in s
            assert f"{parent_res} AS res" in s

    def test_min_res_equals_finest_res_single_statement(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=5, agg="AVG",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert len(stmts) == 1
        # Only Phase 1, no Phase 2.
        assert "FROM src" in stmts[0]
        assert "5 AS res" in stmts[0]

    def test_count_mode_phase_1_uses_count_star(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5 FROM src",
            finest_res=5, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert "COUNT(*) AS count" in stmts[0]

    def test_count_mode_phase_2_rolls_up_with_sum(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5 FROM src",
            finest_res=5, min_res=2, agg="COUNT",
            value_columns=["count"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        # COUNT at parent = SUM(child count).
        for s in stmts[1:]:
            assert "SUM(count) AS count" in s

    def test_sum_mode_uses_sum_at_every_level(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=2, agg="SUM",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        # Finest: SUM at Phase 1 to collapse duplicate source rows into the cell.
        assert 'SUM("val") AS "val"' in stmts[0]
        # Parents: SUM rolls up.
        for s in stmts[1:]:
            assert 'SUM("val") AS "val"' in s

    def test_min_max_modes_use_min_max_at_every_level(self):
        for agg in ("MIN", "MAX"):
            stmts = build_pyramid_statements(
                user_sql="SELECT h5, val FROM src",
                finest_res=5, min_res=2, agg=agg,
                value_columns=["val"], h3_column="h5",
                output_uri="s3://public-output/hex/abc/",
            )
            assert f'{agg}("val") AS "val"' in stmts[0]
            for s in stmts[1:]:
                assert f'{agg}("val") AS "val"' in s

    def test_avg_mode_phase_1_includes_count_alongside_avg(self):
        # AVG mode needs COUNT(*) at finest so parent rollups can compute
        # weighted averages — `AVG of AVGs` is wrong when child cardinalities differ.
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert 'AVG("val") AS "val"' in stmts[0]
        assert "COUNT(*) AS count" in stmts[0]

    def test_avg_mode_phase_2_uses_weighted_average(self):
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, val FROM src",
            finest_res=5, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        for s in stmts[1:]:
            assert 'SUM("val" * count) / SUM(count) AS "val"' in s
            # count must propagate so further rollups can keep weighting correctly.
            assert "SUM(count) AS count" in s

    def test_avg_mode_with_multiple_value_columns(self):
        # Single shared `count` for all value columns at every level.
        stmts = build_pyramid_statements(
            user_sql="SELECT h5, v1, v2 FROM src",
            finest_res=5, min_res=2, agg="AVG",
            value_columns=["v1", "v2"], h3_column="h5",
            output_uri="s3://public-output/hex/abc/",
        )
        assert 'AVG("v1") AS "v1"' in stmts[0]
        assert 'AVG("v2") AS "v2"' in stmts[0]
        assert "COUNT(*) AS count" in stmts[0]
        # Parent rollup weights each value column by the same count.
        assert 'SUM("v1" * count) / SUM(count) AS "v1"' in stmts[1]
        assert 'SUM("v2" * count) / SUM(count) AS "v2"' in stmts[1]
        assert "SUM(count) AS count" in stmts[1]

    def test_invalid_agg_raises(self):
        import pytest
        with pytest.raises(ValueError, match="agg must be one of"):
            build_pyramid_statements(
                user_sql="SELECT h5, val FROM src",
                finest_res=5, min_res=2, agg="MEDIAN",
                value_columns=["val"], h3_column="h5",
                output_uri="s3://public-output/hex/abc/",
            )

    def test_non_count_requires_value_columns(self):
        # Empty value_columns list is only valid for COUNT (which uses
        # a synthetic ["count"]). Other aggs must reject it.
        import pytest
        with pytest.raises(ValueError, match="value column"):
            build_pyramid_statements(
                user_sql="SELECT h5 FROM src",
                finest_res=5, min_res=2, agg="SUM",
                value_columns=[], h3_column="h5",
                output_uri="s3://public-output/hex/abc/",
            )
```

- [ ] **Step 2: Run new tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_tile_pyramid.py::TestBuildPyramidStatements -v
```

Expected: All FAIL with `ImportError` or `AttributeError: build_pyramid_statements`.

- [ ] **Step 3: Implement `build_pyramid_statements` in `tiles/pyramid.py`**

Replace the entire `build_pyramid_sql` function (lines 21-96) with the following. Keep the imports (`json`, `os`, `Decimal`, `List`, `duckdb`, `content_hash`) and the `MVT_LAYER_NAME` constant unchanged.

```python
def build_pyramid_statements(
    user_sql: str,
    finest_res: int,
    min_res: int,
    agg: str,
    value_columns: List[str],
    h3_column: str,
    output_uri: str,
) -> List[str]:
    """Return the ordered list of COPY statements that build a partitioned pyramid.

    Two-phase design:
      Phase 1 (first statement): one COPY reads user_sql, aggregates by (h, h0),
        and writes only res=finest_res partitioned by (res, h0).
      Phase 2 (remaining statements): for res = finest_res - 1 down to min_res,
        each COPY reads the previously written res+1 partition (already aggregated,
        small) and writes res. Working set per Phase 2 step is bounded by the
        cardinality of the previous res, which shrinks ~7x per step.

    AVG mode stores an internal `count` column alongside the aggregate so
    parent rollups produce correctly weighted averages instead of an
    unweighted mean-of-means.
    """
    _VALID_AGG = {"AVG", "SUM", "MIN", "MAX", "COUNT"}
    agg_upper = agg.upper()
    if agg_upper not in _VALID_AGG:
        raise ValueError(f"agg must be one of {_VALID_AGG}, got {agg!r}")
    if agg_upper != "COUNT" and not value_columns:
        raise ValueError(
            "user SQL must return at least one value column after the H3 index "
            "(or use agg='COUNT')"
        )

    qh = f'"{h3_column}"'

    # Per-agg expressions at finest (Phase 1, aggregating raw source rows)
    # and at parent levels (Phase 2, rolling up the previous resolution).
    if agg_upper == "COUNT":
        phase1_values = "COUNT(*) AS count"
        phase2_values = "SUM(count) AS count"
    elif agg_upper == "SUM":
        phase1_values = ", ".join(f'SUM("{c}") AS "{c}"' for c in value_columns)
        phase2_values = ", ".join(f'SUM("{c}") AS "{c}"' for c in value_columns)
    elif agg_upper == "MIN":
        phase1_values = ", ".join(f'MIN("{c}") AS "{c}"' for c in value_columns)
        phase2_values = ", ".join(f'MIN("{c}") AS "{c}"' for c in value_columns)
    elif agg_upper == "MAX":
        phase1_values = ", ".join(f'MAX("{c}") AS "{c}"' for c in value_columns)
        phase2_values = ", ".join(f'MAX("{c}") AS "{c}"' for c in value_columns)
    else:  # AVG
        # Phase 1: average raw source rows + carry COUNT for weighted parents.
        phase1_values = (
            ", ".join(f'AVG("{c}") AS "{c}"' for c in value_columns)
            + ", COUNT(*) AS count"
        )
        # Phase 2: weighted average = SUM(v*count) / SUM(count). Propagate count.
        phase2_values = (
            ", ".join(
                f'SUM("{c}" * count) / SUM(count) AS "{c}"' for c in value_columns
            )
            + ", SUM(count) AS count"
        )

    # Phase 1: scan user_sql, derive h0 once in the src CTE, aggregate at finest.
    phase_1 = (
        "COPY (\n"
        f"  WITH src AS (\n"
        f"    SELECT *, CAST(h3_cell_to_parent({qh}, 0) AS BIGINT) AS h0\n"
        f"    FROM (\n{user_sql}\n    )\n"
        f"  )\n"
        f"  SELECT {qh} AS h,\n"
        f"         h0,\n"
        f"         {phase1_values},\n"
        f"         {finest_res} AS res\n"
        f"  FROM src\n"
        f"  GROUP BY 1, 2\n"
        f") TO '{output_uri}' "
        f"(FORMAT PARQUET, PARTITION_BY (res, h0), OVERWRITE_OR_IGNORE)"
    )

    statements = [phase_1]

    # Phase 2: each parent res reads from the previously written res+1.
    for res in range(finest_res - 1, min_res - 1, -1):
        src_uri = f"{output_uri}res={res + 1}/**/*.parquet"
        stmt = (
            "COPY (\n"
            f"  SELECT h3_cell_to_parent(h, {res}) AS h,\n"
            f"         h0,\n"
            f"         {phase2_values},\n"
            f"         {res} AS res\n"
            f"  FROM read_parquet('{src_uri}', hive_partitioning=true)\n"
            f"  GROUP BY 1, 2\n"
            f") TO '{output_uri}' "
            f"(FORMAT PARQUET, PARTITION_BY (res, h0), OVERWRITE_OR_IGNORE)"
        )
        statements.append(stmt)

    return statements
```

- [ ] **Step 4: Update `register_hex_tiles` to loop over the new statement list**

In `tiles/pyramid.py`, replace lines 223-234 (the block that calls `build_pyramid_sql` and runs it once) with:

```python
    statements = build_pyramid_statements(
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
    for stmt in statements:
        con.sql(stmt)
```

Also remove the `build_pyramid_sql` import / reference from the top of the file if any remain after replacing the function.

- [ ] **Step 5: Run the new test class to verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_tile_pyramid.py::TestBuildPyramidStatements -v
```

Expected: all pass (13 tests).

- [ ] **Step 6: Update or remove obsolete tests in `TestBuildPyramidSQL`**

The existing class `TestBuildPyramidSQL` in `tests/test_tile_pyramid.py` asserts the old single-string return type and the UNION ALL shape. Delete it entirely (and any `from tiles.pyramid import build_pyramid_sql` import that becomes unused) — `TestBuildPyramidStatements` covers the same surface in a way that's consistent with the new return type.

After deletion, run:

```bash
.venv/bin/python -m pytest tests/test_tile_pyramid.py -v
```

Expected: All remaining `TestBuildPyramidStatements`, `TestInspectUserSQL`, `TestRegisterHexTiles`, `TestTilesetMetadata`, and `TestBuildTileConnection` tests pass. If any `TestRegisterHexTiles` tests fail, that signals an actual regression in `register_hex_tiles` from the loop change — debug those (most likely a path string / S3 URL issue) before continuing.

- [ ] **Step 7: Run full test suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "pyramid: two-phase iterative build (finest from source, then rollups)"
```

---

## Task 3: End-to-end mathematical correctness tests

**Files:**
- Test: `tests/test_tile_pyramid.py`

The unit tests in Task 2 cover SQL shape. This task adds tests that build an actual pyramid against synthetic data and assert the aggregations are mathematically correct.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tile_pyramid.py` after the existing `TestRegisterHexTiles` class:

```python
class TestPyramidMathCorrectness:
    """Assert mathematical invariants on built pyramids — independent of
    SQL shape. Each test registers a small multi-h0 synthetic dataset and
    checks that parent values are correct rollups of child values.
    """

    def _seed_sql(self, points):
        """Build a user_sql VALUES clause from a list of (lat, lng, val) tuples."""
        rows = ", ".join(f"({lat}, {lng}, {val})" for lat, lng, val in points)
        return f"""
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES {rows}) t(lat, lng, val)
        """

    def test_count_total_at_parent_equals_total_at_finest(self, local_bucket, h3_conn):
        # COUNT mode: SUM of all count values at any res equals the total
        # number of source rows (the dataset's true count).
        points = [
            (37.8, -122.3, 1.0), (37.80001, -122.30001, 2.0),  # same cluster
            (38.0, -122.5, 3.0),
            (40.0, -100.0, 4.0),
            (-30.0, 140.0, 5.0),  # different h0
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=2, agg="COUNT", zoom_offset=-1,
        )
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "**" / "*.parquet")
            total = h3_conn.sql(
                f"SELECT SUM(count) FROM read_parquet('{uri}', hive_partitioning=true)"
            ).fetchone()[0]
            assert total == len(points), f"res={res}: SUM(count)={total}, expected {len(points)}"

    def test_sum_total_at_parent_equals_total_at_finest(self, local_bucket, h3_conn):
        # SUM mode: conservation across resolutions.
        points = [
            (37.8, -122.3, 10.0), (37.80001, -122.30001, 5.0),
            (38.0, -122.5, 7.0),
            (40.0, -100.0, 3.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=2, agg="SUM", zoom_offset=-1,
        )
        for res in (2, 3, 4, 5):
            uri = str(local_bucket / "hex" / result["hash"] / f"res={res}" / "**" / "*.parquet")
            total = h3_conn.sql(
                f'SELECT SUM("val") FROM read_parquet(\'{uri}\', hive_partitioning=true)'
            ).fetchone()[0]
            assert total == 25.0, f"res={res}: SUM(val)={total}, expected 25.0"

    def test_min_at_parent_is_min_of_children(self, local_bucket, h3_conn):
        # MIN mode: per-cell parent.MIN equals MIN over its res+1 children.
        points = [
            (37.8, -122.3, 10.0), (37.80001, -122.30001, 2.0),
            (37.80002, -122.30002, 7.0),
            (40.0, -100.0, 3.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=3, agg="MIN", zoom_offset=-1,
        )
        # For each (h@res=4) cell, the parent MIN should equal MIN of children.
        child_uri = str(local_bucket / "hex" / result["hash"] / "res=5" / "**" / "*.parquet")
        parent_uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        children_min_by_parent = h3_conn.sql(
            f"""
            SELECT h3_cell_to_parent(h, 4) AS ph, MIN("val") AS min_v
            FROM read_parquet('{child_uri}', hive_partitioning=true)
            GROUP BY 1
            """
        ).fetchdf()
        parents = h3_conn.sql(
            f'SELECT h AS ph, "val" AS min_v FROM read_parquet(\'{parent_uri}\', hive_partitioning=true)'
        ).fetchdf()
        merged = parents.merge(children_min_by_parent, on="ph", suffixes=("_p", "_c"))
        assert (merged["min_v_p"] == merged["min_v_c"]).all()

    def test_max_at_parent_is_max_of_children(self, local_bucket, h3_conn):
        # Mirror of the MIN test.
        points = [
            (37.8, -122.3, 10.0), (37.80001, -122.30001, 2.0),
            (37.80002, -122.30002, 7.0),
            (40.0, -100.0, 3.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=3, agg="MAX", zoom_offset=-1,
        )
        child_uri = str(local_bucket / "hex" / result["hash"] / "res=5" / "**" / "*.parquet")
        parent_uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        children_max_by_parent = h3_conn.sql(
            f"""
            SELECT h3_cell_to_parent(h, 4) AS ph, MAX("val") AS max_v
            FROM read_parquet('{child_uri}', hive_partitioning=true)
            GROUP BY 1
            """
        ).fetchdf()
        parents = h3_conn.sql(
            f'SELECT h AS ph, "val" AS max_v FROM read_parquet(\'{parent_uri}\', hive_partitioning=true)'
        ).fetchdf()
        merged = parents.merge(children_max_by_parent, on="ph", suffixes=("_p", "_c"))
        assert (merged["max_v_p"] == merged["max_v_c"]).all()

    def test_avg_at_parent_is_weighted_source_mean(self, local_bucket, h3_conn):
        # AVG mode: parent.val equals the row-weighted mean of source rows
        # falling under that parent. Two cells with different cardinalities
        # produce different parent means under correct weighting; an
        # unweighted mean-of-means would give a wrong answer here.
        # Cluster A: 4 rows mapping to one h5 cell, values [10, 20, 30, 40] (mean 25).
        # Cluster B: 1 row mapping to a sibling h5 cell, value [100] (mean 100).
        # Both share the same h4 parent.
        # Unweighted mean-of-means = (25 + 100) / 2 = 62.5  ← WRONG
        # Correctly weighted mean = (10+20+30+40+100) / 5 = 40.0
        points = [
            (37.80000, -122.30000, 10.0),
            (37.80001, -122.30001, 20.0),
            (37.80002, -122.30002, 30.0),
            (37.80003, -122.30003, 40.0),
            (37.80100, -122.30100, 100.0),
        ]
        result = register_hex_tiles(
            con=h3_conn, sql=self._seed_sql(points),
            finest_res=5, min_res=4, agg="AVG", zoom_offset=-1,
        )
        # All 5 points share the same h4 parent — find it and check its AVG.
        parent_uri = str(local_bucket / "hex" / result["hash"] / "res=4" / "**" / "*.parquet")
        rows = h3_conn.sql(
            f'SELECT h, "val" FROM read_parquet(\'{parent_uri}\', hive_partitioning=true)'
        ).fetchall()
        # All five points map to the same (lat, lng) area at h4 → one parent row.
        assert len(rows) == 1, f"expected 1 parent cell, got {len(rows)}: {rows}"
        parent_val = rows[0][1]
        assert abs(parent_val - 40.0) < 1e-9, (
            f"parent AVG = {parent_val}, expected weighted mean 40.0 "
            f"(unweighted mean-of-means would be 62.5)"
        )
```

- [ ] **Step 2: Run the new tests**

```bash
.venv/bin/python -m pytest tests/test_tile_pyramid.py::TestPyramidMathCorrectness -v
```

Expected: all pass (5 tests). If `test_avg_at_parent_is_weighted_source_mean` returns 62.5 instead of 40.0, the Phase 2 AVG SQL is using `AVG(child.val)` instead of `SUM(val*count)/SUM(count)` — re-check Task 2 Step 3.

If any MIN/MAX test fails on a `merged["min_v_p"] == merged["min_v_c"]` comparison due to floating-point not exactly matching, that means parent isn't reading purely from children — it's a structural bug, not a numerical one. Fix at the source rather than relaxing the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/test_tile_pyramid.py
git commit -m "test: end-to-end mathematical invariants for two-phase pyramid"
```

---

## Task 4: Run full suite, push, open PR

- [ ] **Step 1: Full test run**

```bash
.venv/bin/python -m pytest -q
```

Expected: all pass. Note the total count for the PR description.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin worktree-hex-pyramid-two-phase
```

(The current branch is `worktree-hex-pyramid-two-phase`. Substitute if you renamed it.)

- [ ] **Step 3: Open the PR**

```bash
gh pr create --title "pyramid: two-phase iterative build (finest from source, then rollups)" --body "$(cat <<'EOF'
## Summary

Replaces \`build_pyramid_sql\`'s single COPY-with-7-branch-UNION-ALL with a two-phase build:

1. **Phase 1** — one COPY scans \`user_sql\`, aggregates by \`(h, h0)\`, writes only \`res=finest_res\` partitioned by \`(res, h0)\`.
2. **Phase 2** — for \`res = finest_res - 1\` down to \`min_res\`, each COPY reads the previously written \`res+1\` partition (already aggregated, small) and writes the next level.

Each step's working set is bounded by its **output** cardinality, not by the full source. Global high-cardinality datasets (GBIF-scale: billions of points → ~50M h8 cells) no longer have to hold seven simultaneous aggregations plus the source CTE in memory at once.

Spec: \`docs/superpowers/specs/2026-05-11-hex-pyramid-two-phase-design.md\`.

## Behavior changes

- **Finest level for non-COUNT modes is now aggregated** (was raw passthrough). One MVT feature per hex cell at every res, in every mode — fixes the overlapping-features bug at finest for SUM/MIN/MAX/AVG.
- **AVG mode carries an internal \`count\` column** at every resolution so parent rollups produce correctly weighted averages. Stored alongside the AVG value in parquet; not exposed in \`value_stats\` or to the LLM (\`value_columns\` is unchanged).
- **Layout version bumped to \`v3-iterative\`.** Old v2-h0 pyramids on S3 retain their hashes; new registrations write to fresh content addresses.

## Public API

Unchanged: \`register_hex_tiles\` MCP tool signature, the on-disk pyramid layout (\`res=N/h0=X/\`), the tile-serving SQL, the \`metadata.json\` schema, the cache short-circuit.

## Verification

- \`pytest\` — full suite passing (count: TBD — fill in from Step 1).
- Math correctness end-to-end tests assert: COUNT total is conserved across resolutions; SUM total is conserved; parent MIN/MAX equals MIN/MAX of children; **parent AVG equals the source-row weighted mean** (the unweighted mean-of-means would give a wrong answer on the test fixture).

## Out of scope

- Per-h0 outer loop at Phase 1. If GBIF still OOMs after this change, chunk Phase 1 by h0.
- Geo-agent client timeout bump (boettiger-lab/geo-agent#205, already open).

## Test plan

- [x] All unit + integration tests passing locally.
- [ ] Deploy to dev, re-register Irrecoverable Carbon, confirm metadata.json appears and the URL renders.
- [ ] Same for a GBIF-scale dataset that previously OOMed.
- [ ] After dev sanity check, roll out to prod.
EOF
)"
```

- [ ] **Step 4: Report PR URL**

Return the URL printed by `gh pr create` so the user can review.

---

## Self-Review Notes

Coverage check against the spec:
- ✅ Phase 1 finest-from-source COPY → Task 2 Step 3
- ✅ Phase 2 iterative parent rollups → Task 2 Step 3
- ✅ AVG with shared count column → Task 2 Steps 1, 3 and Task 3 Step 1 (`test_avg_at_parent_is_weighted_source_mean`)
- ✅ Layout version bump → Task 1
- ✅ Behavior change at finest level documented → covered by `test_sum_mode_uses_sum_at_every_level` (which asserts SUM at finest where the old code did raw passthrough) and by Task 3's invariant tests
- ✅ `metadata.json` + cache short-circuit unchanged → Task 2 Step 4 (only the COPY loop changes; stats/bounds/metadata code below it is untouched)
- ✅ End-to-end math invariants for each agg mode → Task 3 Step 1

No placeholder text, no unreferenced types, no "similar to Task N" cross-references. Function name `build_pyramid_statements` used consistently. Test class name `TestBuildPyramidStatements` used consistently. The pre-bump v2-h0 hash is filled in at Task 1 Step 2 from a real `content_hash` invocation rather than being left as a placeholder.
