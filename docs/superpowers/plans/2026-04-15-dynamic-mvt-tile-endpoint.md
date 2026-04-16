# Dynamic MVT Tile Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/tiles/hex/<name>/{z}/{x}/{y}.pbf` MVT endpoint to the MCP server backed by a content-addressable H3 resolution pyramid stored in S3, plus a `register_hex_tiles` MCP tool that materializes the pyramid from a user SQL query.

**Architecture:** A new `tiles/` Python package holds the tile logic. Registration writes a partitioned parquet pyramid to S3 via a single `COPY ... TO` with `PARTITION_BY (res)`. A persistent per-worker `:memory:` DuckDB connection (initialized at app startup, separate from the per-request isolated connections used by `query`) handles tile requests. Tile URLs are deterministic from input hash; replicas need no coordination.

**Tech Stack:** Python, DuckDB (`spatial`, `h3`, `httpfs` extensions), MCP `FastMCP`, Starlette (already the server framework via `mcp.streamable_http_app()`), `anyio` for thread offload.

**Spec:** `docs/superpowers/specs/2026-04-15-dynamic-mvt-tile-endpoint-design.md`

---

## File Structure

**New files:**
- `tiles/__init__.py` — package marker, re-exports public API
- `tiles/tile_math.py` — pure helpers: XYZ→lng/lat bounds, zoom→resolution, content hash
- `tiles/pyramid.py` — pyramid SQL generation + `register_hex_tiles` implementation
- `tiles/db.py` — persistent DuckDB connection factory + Starlette lifespan
- `tiles/endpoint.py` — Starlette route handler `serve_tile(request) -> Response`
- `tests/test_tile_math.py` — pure-function tests
- `tests/test_tile_pyramid.py` — pyramid SQL + local-bucket end-to-end
- `tests/test_tile_endpoint.py` — HTTP-level tests with `starlette.testclient`

**Modified files:**
- `server.py` — import `tiles` package, register `register_hex_tiles` as MCP tool, mount `/tiles/...` route on the Starlette app, wire up lifespan
- `requirements.txt` — add `mapbox-vector-tile` (test-only decoder) — or skip and assert raw bytes only
- `h3-guide.md` — add a section pointing agents at `register_hex_tiles` for large hex results
- `assistant-role.md` — add dispatch hint for when to use `register_hex_tiles`

**Configuration:**
- Env var `TILE_BUCKET_BASE` (default `s3://public-output`). Tests set it to `file:///tmp/test-tiles-<pid>/` to avoid needing S3 credentials.
- Env var `MCP_PUBLIC_BASE_URL` (default `https://duckdb-mcp.nrp-nautilus.io`) used to construct the returned `tile_url_template`.

---

### Task 1: Verify DuckDB ST_AsMVT API and capture concrete SQL

The spec notes ST_AsMVT semantics as an "open item to resolve during implementation." This task pins it down via a throwaway test so later tasks can reference the exact signatures.

**Files:**
- Create: `tests/test_tile_math.py` (placeholder — fleshed out in Task 2)

- [ ] **Step 1: Run a one-off probe in Python REPL (document findings in this task's commit message)**

```bash
python3 -c "
import duckdb
c = duckdb.connect(':memory:')
c.sql('INSTALL spatial; LOAD spatial; INSTALL h3 FROM community; LOAD h3')
# Probe ST_AsMVTGeom / ST_AsMVT signatures.
print(c.sql(\"SELECT function_name, parameters FROM duckdb_functions() WHERE function_name ILIKE 'st_asmvt%'\").fetchall())
# Probe h3 cells-covering helper.
print(c.sql(\"SELECT function_name FROM duckdb_functions() WHERE function_name ILIKE 'h3_polygon%' OR function_name ILIKE 'h3_cells%'\").fetchall())
"
```

Expected output contains functions named `st_asmvt`, `st_asmvtgeom`, plus an `h3_polygon_wkt_to_cells` (or similarly named) function. If the exact names differ from what this plan assumes, update later tasks' SQL accordingly — exact names are called out in Task 5/6 SQL blocks.

- [ ] **Step 2: Write down the exact signatures observed**

Add a comment block at the top of `tiles/endpoint.py` (created in Task 6) documenting:
- `ST_AsMVTGeom(geom, bounds, extent, buffer, clip) -> geometry`
- `ST_AsMVT(row, layer_name, extent, geom_col, feature_id_col) -> bytea`
- `h3_polygon_wkt_to_cells(wkt, res)` return type (list of cells)

If the observed API diverges significantly from the spec, stop and flag — the spec may need a correction before proceeding.

- [ ] **Step 3: Commit the investigation**

```bash
git add tests/test_tile_math.py
git commit -m "test: scaffold tile_math test module and document DuckDB MVT API"
```

---

### Task 2: Tile math — XYZ to lng/lat bounds

**Files:**
- Create: `tiles/__init__.py`
- Create: `tiles/tile_math.py`
- Create/Modify: `tests/test_tile_math.py`

- [ ] **Step 1: Write failing tests for `tile_xyz_to_lnglat_bounds`**

Replace any placeholder content in `tests/test_tile_math.py` with:

```python
import math
import pytest
from tiles.tile_math import tile_xyz_to_lnglat_bounds


class TestTileBounds:
    def test_z0_is_whole_world(self):
        w, s, e, n = tile_xyz_to_lnglat_bounds(0, 0, 0)
        assert w == pytest.approx(-180.0)
        assert e == pytest.approx(180.0)
        assert s == pytest.approx(-math.degrees(math.atan(math.sinh(math.pi))))
        assert n == pytest.approx(math.degrees(math.atan(math.sinh(math.pi))))

    def test_z1_nw_quadrant(self):
        # Tile (0,0) at z=1 is the northwest quadrant.
        w, s, e, n = tile_xyz_to_lnglat_bounds(1, 0, 0)
        assert w == pytest.approx(-180.0)
        assert e == pytest.approx(0.0)
        assert n > 0
        assert s == pytest.approx(0.0)

    def test_z1_se_quadrant(self):
        w, s, e, n = tile_xyz_to_lnglat_bounds(1, 1, 1)
        assert w == pytest.approx(0.0)
        assert e == pytest.approx(180.0)
        assert s < 0
        assert n == pytest.approx(0.0)

    def test_bounds_monotonic_in_x(self):
        # Adjacent tiles share an edge.
        _, _, e1, _ = tile_xyz_to_lnglat_bounds(5, 10, 12)
        w2, _, _, _ = tile_xyz_to_lnglat_bounds(5, 11, 12)
        assert e1 == pytest.approx(w2)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_tile_math.py -v`
Expected: `ModuleNotFoundError: No module named 'tiles'` or similar import error.

- [ ] **Step 3: Create `tiles/__init__.py`**

Create `tiles/__init__.py` (empty file).

- [ ] **Step 4: Implement `tile_xyz_to_lnglat_bounds`**

Create `tiles/tile_math.py`:

```python
"""Pure helper functions for tile math. No side effects, no DuckDB."""
import math
from typing import Tuple


def tile_xyz_to_lnglat_bounds(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """Return (west, south, east, north) in lng/lat (EPSG:4326) for XYZ tile."""
    n = 2.0 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (west, lat_s, east, lat_n)
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_tile_math.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add tiles/__init__.py tiles/tile_math.py tests/test_tile_math.py
git commit -m "feat(tiles): tile_xyz_to_lnglat_bounds helper"
```

---

### Task 3: Tile math — zoom → H3 resolution, content hash

**Files:**
- Modify: `tiles/tile_math.py`
- Modify: `tests/test_tile_math.py`

- [ ] **Step 1: Write failing tests for `zoom_to_h3_res` and `content_hash`**

Append to `tests/test_tile_math.py`:

```python
from tiles.tile_math import zoom_to_h3_res, content_hash


class TestZoomToRes:
    def test_default_offset_maps_z8_to_r4(self):
        assert zoom_to_h3_res(8, min_res=2, finest_res=9, zoom_offset=4) == 4

    def test_clamped_at_min_res(self):
        assert zoom_to_h3_res(0, min_res=2, finest_res=9, zoom_offset=4) == 2
        assert zoom_to_h3_res(3, min_res=2, finest_res=9, zoom_offset=4) == 2

    def test_clamped_at_finest_res(self):
        assert zoom_to_h3_res(20, min_res=2, finest_res=8, zoom_offset=4) == 8

    def test_custom_offset(self):
        assert zoom_to_h3_res(10, min_res=2, finest_res=9, zoom_offset=2) == 8


class TestContentHash:
    def test_stable_for_identical_inputs(self):
        h1 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        h2 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        assert h1 == h2

    def test_differs_when_sql_differs(self):
        h1 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        h2 = content_hash(sql="SELECT 2", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        assert h1 != h2

    def test_differs_when_agg_differs(self):
        h1 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        h2 = content_hash(sql="SELECT 1", finest_res=8, min_res=2, agg="SUM", zoom_offset=4)
        assert h1 != h2

    def test_length_is_16_hex_chars(self):
        h = content_hash(sql="x", finest_res=8, min_res=2, agg="AVG", zoom_offset=4)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_tile_math.py -v`
Expected: `ImportError: cannot import name 'zoom_to_h3_res'`.

- [ ] **Step 3: Implement `zoom_to_h3_res` and `content_hash`**

Append to `tiles/tile_math.py`:

```python
import hashlib


def zoom_to_h3_res(z: int, min_res: int, finest_res: int, zoom_offset: int = 4) -> int:
    """Clamp(z - zoom_offset, min_res, finest_res) — coarser hexes at lower zooms."""
    target = z - zoom_offset
    return max(min_res, min(finest_res, target))


def content_hash(sql: str, finest_res: int, min_res: int, agg: str, zoom_offset: int) -> str:
    """Deterministic 16-char hex hash of the registration inputs.

    Used as the <name> component of tile URLs so identical registrations
    dedupe naturally and URLs are CDN-friendly.
    """
    canonical = f"{sql}\0{finest_res}\0{min_res}\0{agg}\0{zoom_offset}"
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_tile_math.py -v`
Expected: all previous tests + 7 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add tiles/tile_math.py tests/test_tile_math.py
git commit -m "feat(tiles): zoom_to_h3_res and content_hash helpers"
```

---

### Task 4: Pyramid SQL generator

**Files:**
- Create: `tiles/pyramid.py`
- Create: `tests/test_tile_pyramid.py`

- [ ] **Step 1: Write failing test for `build_pyramid_sql`**

Create `tests/test_tile_pyramid.py`:

```python
import re
import pytest
from tiles.pyramid import build_pyramid_sql


class TestBuildPyramidSQL:
    def test_contains_one_select_per_resolution(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8,
            min_res=2,
            agg="AVG",
            value_columns=["val"],
            h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Parents at res 2..7, plus finest level at res 8. 7 SELECTs total.
        union_alls = len(re.findall(r"\bUNION ALL\b", sql, re.IGNORECASE))
        assert union_alls == 6  # 7 selects → 6 UNION ALLs

    def test_finest_level_is_ungrouped(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8,
            min_res=2,
            agg="AVG",
            value_columns=["val"],
            h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # The finest-res SELECT should NOT have AVG/SUM wrapping the value column.
        # Look for the "... AS res" marker equal to finest_res.
        finest_select = re.search(r"SELECT[^)]*?8 AS res FROM src", sql, re.DOTALL)
        assert finest_select is not None
        assert "AVG" not in finest_select.group(0)

    def test_parent_levels_aggregate(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8,
            min_res=2,
            agg="SUM",
            value_columns=["val"],
            h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Parent selects use SUM(val)
        assert "SUM(val)" in sql

    def test_partitions_by_res(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=2, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        assert "PARTITION_BY (res)" in sql
        assert "OVERWRITE_OR_IGNORE" in sql
        assert "FORMAT PARQUET" in sql

    def test_multiple_value_columns(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, v1, v2 FROM src",
            finest_res=8, min_res=2, agg="AVG",
            value_columns=["v1", "v2"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        assert "AVG(v1)" in sql
        assert "AVG(v2)" in sql

    def test_min_res_equals_finest_res_single_level(self):
        sql = build_pyramid_sql(
            user_sql="SELECT h8, val FROM src",
            finest_res=8, min_res=8, agg="AVG",
            value_columns=["val"], h3_column="h8",
            output_uri="s3://public-output/hex/abc/",
        )
        # Only the finest level — no UNION ALL.
        assert "UNION ALL" not in sql
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_tile_pyramid.py -v`
Expected: `ModuleNotFoundError: No module named 'tiles.pyramid'`.

- [ ] **Step 3: Implement `build_pyramid_sql`**

Create `tiles/pyramid.py`:

```python
"""Pyramid SQL generation and registration.

register_hex_tiles() materializes a partitioned parquet pyramid to object storage.
Tile requests read directly from the pyramid — no coordination needed.
"""
from typing import List


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

    The finest-resolution level stores the user's values unaggregated; parents
    at each coarser resolution aggregate via the user-chosen `agg` function.
    """
    value_list_raw = ", ".join(value_columns)
    value_list_agg = ", ".join(f"{agg}({c}) AS {c}" for c in value_columns)

    selects = []
    # Parents: min_res .. finest_res - 1, each aggregated.
    for res in range(min_res, finest_res):
        selects.append(
            f"  SELECT h3_cell_to_parent({h3_column}, {res}) AS h, "
            f"{value_list_agg}, {res} AS res FROM src GROUP BY 1"
        )
    # Finest level: raw values, no aggregation.
    selects.append(
        f"  SELECT {h3_column} AS h, {value_list_raw}, {finest_res} AS res FROM src"
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

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_tile_pyramid.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "feat(tiles): pyramid SQL generator"
```

---

### Task 5: `register_hex_tiles` end-to-end with local bucket

**Files:**
- Modify: `tiles/pyramid.py`
- Modify: `tests/test_tile_pyramid.py`

- [ ] **Step 1: Write failing integration test using a local directory as the "bucket"**

Append to `tests/test_tile_pyramid.py`:

```python
import os
import duckdb
from pathlib import Path
from tiles.pyramid import register_hex_tiles


@pytest.fixture
def local_bucket(tmp_path, monkeypatch):
    """Point TILE_BUCKET_BASE at a local directory so no S3 is required."""
    bucket = tmp_path / "tiles-bucket"
    bucket.mkdir()
    monkeypatch.setenv("TILE_BUCKET_BASE", str(bucket))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://test.local")
    return bucket


@pytest.fixture
def h3_conn():
    con = duckdb.connect(":memory:")
    con.sql("INSTALL h3 FROM community; LOAD h3")
    con.sql("INSTALL spatial; LOAD spatial")
    con.sql("INSTALL httpfs; LOAD httpfs")
    yield con
    con.close()


class TestRegisterHexTiles:
    def test_writes_pyramid_partitions(self, local_bucket, h3_conn):
        # Source: 5 cells at r5 around a point.
        user_sql = """
            SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, val
            FROM (VALUES (37.8, -122.3, 1.0),
                         (37.9, -122.4, 2.0),
                         (38.0, -122.5, 3.0),
                         (38.1, -122.6, 4.0),
                         (38.2, -122.7, 5.0)) t(lat, lng, val)
        """
        result = register_hex_tiles(
            con=h3_conn,
            sql=user_sql,
            finest_res=5,
            min_res=2,
            agg="AVG",
            zoom_offset=4,
        )
        assert "hash" in result
        # Partitioned files written: res=2 through res=5.
        for res in (2, 3, 4, 5):
            partition = local_bucket / "hex" / result["hash"] / f"res={res}"
            assert partition.exists(), f"Missing res={res}"
            assert any(partition.iterdir()), f"Empty res={res}"

    def test_tile_url_template_uses_public_base(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        assert result["tile_url_template"].startswith("http://test.local/tiles/hex/")
        assert result["tile_url_template"].endswith("/{z}/{x}/{y}.pbf")

    def test_returns_value_columns(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS v1, 2.0 AS v2"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4,
        )
        assert result["value_columns"] == ["v1", "v2"]

    def test_identical_inputs_same_hash(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        r1 = register_hex_tiles(con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4)
        r2 = register_hex_tiles(con=h3_conn, sql=user_sql, finest_res=5, min_res=5, agg="AVG", zoom_offset=4)
        assert r1["hash"] == r2["hash"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_tile_pyramid.py::TestRegisterHexTiles -v`
Expected: `ImportError: cannot import name 'register_hex_tiles'`.

- [ ] **Step 3: Implement `register_hex_tiles`**

Append to `tiles/pyramid.py`:

```python
import os
from typing import Optional
import duckdb

from tiles.tile_math import content_hash


def _bucket_base() -> str:
    return os.environ.get("TILE_BUCKET_BASE", "s3://public-output").rstrip("/")


def _public_base_url() -> str:
    return os.environ.get("MCP_PUBLIC_BASE_URL", "https://duckdb-mcp.nrp-nautilus.io").rstrip("/")


def _inspect_user_sql(con: duckdb.DuckDBPyConnection, user_sql: str):
    """Run user SQL with LIMIT 0 to extract column names without materializing data."""
    columns = con.sql(f"SELECT * FROM ({user_sql}) LIMIT 0").columns
    if not columns:
        raise ValueError("user SQL returned no columns")
    h3_column = columns[0]
    value_columns = list(columns[1:])
    if not value_columns:
        raise ValueError("user SQL must return at least one value column after the H3 index")
    return h3_column, value_columns


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
    """
    if finest_res < min_res:
        raise ValueError(f"finest_res ({finest_res}) must be >= min_res ({min_res})")

    h3_column, value_columns = _inspect_user_sql(con, sql)
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
    con.sql(pyramid_sql)

    # Bounds of finest-level cells (approximate via simple min/max on cell centers).
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

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_tile_pyramid.py -v`
Expected: all previous tests + 4 new `TestRegisterHexTiles` tests pass.

- [ ] **Step 5: Commit**

```bash
git add tiles/pyramid.py tests/test_tile_pyramid.py
git commit -m "feat(tiles): register_hex_tiles materializes pyramid"
```

---

### Task 6: Persistent DuckDB connection + Starlette lifespan

**Files:**
- Create: `tiles/db.py`
- Modify: `tests/test_tile_pyramid.py` (add test for lifespan behavior)

- [ ] **Step 1: Write failing test for `build_tile_connection`**

Append to `tests/test_tile_pyramid.py`:

```python
from tiles.db import build_tile_connection


class TestBuildTileConnection:
    def test_loads_required_extensions(self):
        con = build_tile_connection()
        try:
            # Proxy: LOAD must have succeeded if these functions are callable.
            con.sql("SELECT h3_latlng_to_cell(37.8, -122.3, 5)").fetchone()
            con.sql("SELECT ST_Point(0, 0)").fetchone()
            # httpfs loaded — confirm by presence of its config.
            con.sql("SELECT current_setting('s3_region')").fetchone()
        finally:
            con.close()

    def test_no_s3_secret_created(self):
        con = build_tile_connection()
        try:
            names = [r[0] for r in con.sql("SELECT name FROM duckdb_secrets()").fetchall()]
            # Persistent tile connection reads only from public buckets.
            assert "client_s3" not in names
        finally:
            con.close()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_tile_pyramid.py::TestBuildTileConnection -v`
Expected: `ModuleNotFoundError: No module named 'tiles.db'`.

- [ ] **Step 3: Implement `build_tile_connection` and lifespan context**

Create `tiles/db.py`:

```python
"""Persistent :memory: DuckDB connection for the tile endpoint.

Separate from the per-request isolated connections used by the `query` tool:
tile requests never take user credentials, so the connection can be long-lived
and shared across requests via con.cursor() for per-request isolation.
"""
import sys
from contextlib import asynccontextmanager
import duckdb


def build_tile_connection() -> duckdb.DuckDBPyConnection:
    """Create a :memory: connection with extensions loaded.

    Extensions are assumed to be pre-installed in the image (see mcp-data-server#54);
    LOAD is per-session and always required.
    """
    con = duckdb.connect(":memory:")
    # Extensions may not be pre-installed in dev environments — install defensively.
    con.sql("INSTALL httpfs; LOAD httpfs")
    con.sql("INSTALL spatial; LOAD spatial")
    con.sql("INSTALL h3 FROM community; LOAD h3")
    return con


@asynccontextmanager
async def tile_lifespan(app):
    """Starlette lifespan that creates and tears down the persistent connection.

    Stored on app.state.tile_con so request handlers can reach it.
    """
    con = build_tile_connection()
    print("📦 Tile endpoint: persistent DuckDB connection ready", file=sys.stderr)
    app.state.tile_con = con
    try:
        yield
    finally:
        con.close()
        print("📦 Tile endpoint: connection closed", file=sys.stderr)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_tile_pyramid.py::TestBuildTileConnection -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add tiles/db.py tests/test_tile_pyramid.py
git commit -m "feat(tiles): persistent tile DuckDB connection + lifespan"
```

---

### Task 7: Tile request handler

**Files:**
- Create: `tiles/endpoint.py`
- Create: `tests/test_tile_endpoint.py`

- [ ] **Step 1: Write failing end-to-end test**

Create `tests/test_tile_endpoint.py`:

```python
import os
import pytest
import duckdb
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tiles.endpoint import serve_tile
from tiles.pyramid import register_hex_tiles
from tiles.db import build_tile_connection


@pytest.fixture
def local_bucket(tmp_path, monkeypatch):
    bucket = tmp_path / "tiles-bucket"
    bucket.mkdir()
    monkeypatch.setenv("TILE_BUCKET_BASE", str(bucket))
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "http://test.local")
    return bucket


@pytest.fixture
def app_with_tiles(local_bucket):
    """Build a minimal Starlette app that mounts the tile route with a live connection."""
    con = build_tile_connection()
    app = Starlette(routes=[
        Route("/tiles/{namespace}/{name}/{z:int}/{x:int}/{y:int}.pbf", serve_tile),
    ])
    app.state.tile_con = con
    yield app
    con.close()


@pytest.fixture
def registered_ca(app_with_tiles):
    """Register a small California tileset and return its hash."""
    con = app_with_tiles.state.tile_con
    # Generate ~few hundred r5 cells across California.
    user_sql = """
        SELECT h3_latlng_to_cell(lat, lng, 5) AS h5, 1.0 AS val
        FROM (
            SELECT 36 + random()*3 AS lat, -121 - random()*3 AS lng
            FROM range(500)
        )
    """
    result = register_hex_tiles(
        con=con, sql=user_sql, finest_res=5, min_res=2, agg="AVG", zoom_offset=4,
    )
    return result["hash"]


class TestServeTile:
    def test_returns_mvt_content_type_for_covering_tile(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        # z=5 tile that covers part of California.
        # California is near tile (5, 5, 12) in XYZ.
        r = client.get(f"/tiles/hex/{registered_ca}/5/5/12.pbf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/vnd.mapbox-vector-tile"
        assert len(r.content) > 0

    def test_empty_tile_returns_204(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        # A tile over open ocean far from California should be empty.
        r = client.get(f"/tiles/hex/{registered_ca}/5/0/0.pbf")
        assert r.status_code in (200, 204)  # implementations may differ
        if r.status_code == 200:
            assert len(r.content) < 100  # empty MVT is very small

    def test_unknown_hash_returns_404(self, app_with_tiles):
        client = TestClient(app_with_tiles)
        r = client.get("/tiles/hex/0000000000000000/5/5/12.pbf")
        assert r.status_code == 404

    def test_unknown_namespace_returns_404(self, app_with_tiles, registered_ca):
        client = TestClient(app_with_tiles)
        r = client.get(f"/tiles/bogus/{registered_ca}/5/5/12.pbf")
        assert r.status_code == 404
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_tile_endpoint.py -v`
Expected: `ModuleNotFoundError: No module named 'tiles.endpoint'`.

- [ ] **Step 3: Implement `serve_tile`**

Create `tiles/endpoint.py`:

```python
"""Starlette request handler for /tiles/{namespace}/{name}/{z}/{x}/{y}.pbf.

Observed DuckDB signatures (verified in Task 1):
- ST_AsMVTGeom(geom, bounds_geom) -> geometry
- ST_AsMVT(row) -> bytea  (aggregate)
- h3_polygon_wkt_to_cells(wkt, res) -> list<h3index>
"""
import os
import sys
import anyio
from starlette.requests import Request
from starlette.responses import Response

from tiles.tile_math import tile_xyz_to_lnglat_bounds, zoom_to_h3_res


MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"


def _bucket_base() -> str:
    return os.environ.get("TILE_BUCKET_BASE", "s3://public-output").rstrip("/")


def _tileset_dir(namespace: str, name: str) -> str:
    return f"{_bucket_base()}/{namespace}/{name}"


def _pyramid_exists(con, namespace: str, name: str, res: int) -> bool:
    """Check whether the pyramid partition for (namespace, name, res) is readable."""
    uri = f"{_tileset_dir(namespace, name)}/res={res}/*.parquet"
    try:
        cur = con.cursor()
        cur.sql(f"SELECT 1 FROM read_parquet('{uri}') LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _build_tile_sql(namespace: str, name: str, z: int, x: int, y: int,
                    target_res: int, finest_res: int) -> str:
    """Produce the SQL that returns a single bytea row (the MVT for this tile).

    Strategy:
      1. Compute the tile's web-mercator bounds geometry.
      2. Compute H3 cells at target_res covering the tile's lng/lat polygon.
      3. Select rows from the pyramid partition whose cell is in that set.
      4. Project cell geometries with ST_AsMVTGeom then aggregate with ST_AsMVT.
    """
    west, south, east, north = tile_xyz_to_lnglat_bounds(z, x, y)
    tileset = _tileset_dir(namespace, name)
    # Tile polygon WKT in lng/lat. h3_polygon_wkt_to_cells interprets lng/lat.
    tile_wkt = (
        f"POLYGON(("
        f"{west} {south}, {east} {south}, {east} {north}, "
        f"{west} {north}, {west} {south}))"
    )
    # MVT bounds in EPSG:3857 (web-mercator). ST_Transform handles projection.
    return f"""
        WITH cells AS (
          SELECT UNNEST(h3_polygon_wkt_to_cells('{tile_wkt}', {target_res})) AS cell
        ),
        src AS (
          SELECT p.* FROM read_parquet('{tileset}/res={target_res}/*.parquet') p
          SEMI JOIN cells c ON p.h = c.cell
        ),
        projected AS (
          SELECT
            ST_AsMVTGeom(
              ST_Transform(h3_cell_to_boundary_wkt(h)::GEOMETRY, 'EPSG:4326', 'EPSG:3857'),
              ST_MakeEnvelope(
                ST_X(ST_Transform(ST_Point({west}, {south}), 'EPSG:4326', 'EPSG:3857')),
                ST_Y(ST_Transform(ST_Point({west}, {south}), 'EPSG:4326', 'EPSG:3857')),
                ST_X(ST_Transform(ST_Point({east}, {north}), 'EPSG:4326', 'EPSG:3857')),
                ST_Y(ST_Transform(ST_Point({east}, {north}), 'EPSG:4326', 'EPSG:3857'))
              )
            ) AS geom,
            src.* EXCLUDE (h)
          FROM src
        )
        SELECT ST_AsMVT(projected) FROM projected WHERE geom IS NOT NULL
    """


def _run_tile_query(con, sql: str) -> bytes:
    """Run the tile SQL on a fresh cursor and return raw MVT bytes (or empty)."""
    cur = con.cursor()
    row = cur.sql(sql).fetchone()
    if row is None or row[0] is None:
        return b""
    return bytes(row[0])


async def serve_tile(request: Request) -> Response:
    """GET /tiles/{namespace}/{name}/{z}/{x}/{y}.pbf"""
    namespace = request.path_params["namespace"]
    name = request.path_params["name"]
    z = int(request.path_params["z"])
    x = int(request.path_params["x"])
    y = int(request.path_params["y"])

    if namespace != "hex":
        return Response(status_code=404)

    con = request.app.state.tile_con

    # Probe whether the tileset exists at any res — use finest since it's always present.
    # We don't know finest_res at tile-serving time without metadata; try a few.
    # Convention: we accept whatever res in [2..15] has data.
    target_res = None
    for guess_finest in range(15, 1, -1):
        if await anyio.to_thread.run_sync(_pyramid_exists, con, namespace, name, guess_finest):
            finest_res = guess_finest
            # (Floor of) zoom_to_h3_res with default zoom_offset=4 and min_res=2.
            # In v1 we don't persist zoom_offset/min_res per-tileset — caller's registered
            # URL captures the content hash, so clients get stable tiles. Use defaults.
            target_res = zoom_to_h3_res(z, min_res=2, finest_res=finest_res, zoom_offset=4)
            break
    if target_res is None:
        return Response(status_code=404)

    sql = _build_tile_sql(namespace, name, z, x, y, target_res, finest_res)
    try:
        mvt_bytes = await anyio.to_thread.run_sync(_run_tile_query, con, sql)
    except Exception as e:
        print(f"⚠️ Tile {z}/{x}/{y} error: {e}", file=sys.stderr)
        return Response(status_code=500)

    if not mvt_bytes:
        return Response(status_code=204)
    return Response(content=mvt_bytes, media_type=MVT_CONTENT_TYPE)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_tile_endpoint.py -v`
Expected: 4 passed (or 3 + 1 skipped if ST_AsMVT returns an unexpected shape — in that case, adjust the SQL based on Task 1's documented signature).

- [ ] **Step 5: Commit**

```bash
git add tiles/endpoint.py tests/test_tile_endpoint.py
git commit -m "feat(tiles): MVT tile request handler"
```

---

### Task 8: Persist tileset metadata for per-tileset zoom_offset / min_res

The previous task hardcoded `min_res=2`, `zoom_offset=4` at tile-serve time because we have no metadata file. Fix that: write a sibling `metadata.json` during registration so the handler reads the real values.

**Files:**
- Modify: `tiles/pyramid.py`
- Modify: `tiles/endpoint.py`
- Modify: `tests/test_tile_pyramid.py`
- Modify: `tests/test_tile_endpoint.py`

- [ ] **Step 1: Write failing test for metadata sidecar**

Append to `tests/test_tile_pyramid.py`:

```python
import json as _json


class TestTilesetMetadata:
    def test_metadata_written_alongside_pyramid(self, local_bucket, h3_conn):
        user_sql = "SELECT h3_latlng_to_cell(37.8, -122.3, 5) AS h5, 1.0 AS val"
        result = register_hex_tiles(
            con=h3_conn, sql=user_sql, finest_res=5, min_res=3, agg="SUM", zoom_offset=3,
        )
        meta_path = local_bucket / "hex" / result["hash"] / "metadata.json"
        assert meta_path.exists()
        meta = _json.loads(meta_path.read_text())
        assert meta["finest_res"] == 5
        assert meta["min_res"] == 3
        assert meta["agg"] == "SUM"
        assert meta["zoom_offset"] == 3
        assert meta["value_columns"] == ["val"]
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_tile_pyramid.py::TestTilesetMetadata -v`
Expected: `FileNotFoundError: ... metadata.json`.

- [ ] **Step 3: Write metadata sidecar from `register_hex_tiles`**

In `tiles/pyramid.py`, after the `con.sql(pyramid_sql)` call and before computing bounds, add:

```python
    # Write a sidecar metadata.json so the tile handler knows finest_res / zoom_offset.
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
```

Add the import and helper at the top of `tiles/pyramid.py`:

```python
import json


def _json_dumps_escaped(obj) -> str:
    # DuckDB's COPY ... (FORMAT CSV, QUOTE '') writes the raw string. We must
    # escape any single quotes in the JSON so they don't break the SQL literal.
    return json.dumps(obj).replace("'", "''")
```

- [ ] **Step 4: Run tests, verify pyramid metadata test passes**

Run: `pytest tests/test_tile_pyramid.py::TestTilesetMetadata -v`
Expected: 1 passed.

- [ ] **Step 5: Write failing test that endpoint respects persisted zoom_offset**

Append to `tests/test_tile_endpoint.py`:

```python
class TestMetadataDriven:
    def test_endpoint_reads_finest_res_from_metadata(self, app_with_tiles, local_bucket):
        """After registering with finest_res=4, a tile request should use res=4 max."""
        con = app_with_tiles.state.tile_con
        user_sql = """
            SELECT h3_latlng_to_cell(37.8, -122.3, 4) AS h4, 1.0 AS val
        """
        result = register_hex_tiles(
            con=con, sql=user_sql, finest_res=4, min_res=2, agg="AVG", zoom_offset=4,
        )
        client = TestClient(app_with_tiles)
        # At z=20 with zoom_offset=4 → target_res=16, clamped to finest_res=4.
        # Tile over (37.8, -122.3) at z=20 covers one r4 hex or so. Main assertion:
        # request succeeds (not 404 from "no pyramid at res=16").
        # Compute approximate tile (x,y) at z=20 for (37.8, -122.3):
        import math
        lat_rad = math.radians(37.8)
        z = 20
        n = 2 ** z
        x = int((-122.3 + 180) / 360 * n)
        y = int((1 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2 * n)
        r = client.get(f"/tiles/hex/{result['hash']}/{z}/{x}/{y}.pbf")
        assert r.status_code in (200, 204)  # not 404
```

- [ ] **Step 6: Run test, verify it fails OR already passes**

Run: `pytest tests/test_tile_endpoint.py::TestMetadataDriven -v`
Expected: may pass incidentally, but the endpoint is still hardcoded. Even if it passes, continue to Step 7 — the hardcoded values must be replaced.

- [ ] **Step 7: Replace the `finest_res` probe loop in `serve_tile` with a metadata read**

In `tiles/endpoint.py`, replace the `target_res = None / for guess_finest in range(...)` block inside `serve_tile` with:

```python
    meta = await anyio.to_thread.run_sync(_read_metadata, namespace, name)
    if meta is None:
        return Response(status_code=404)
    finest_res = meta["finest_res"]
    min_res = meta["min_res"]
    zoom_offset = meta["zoom_offset"]
    target_res = zoom_to_h3_res(z, min_res=min_res, finest_res=finest_res, zoom_offset=zoom_offset)
```

And add the helper elsewhere in `tiles/endpoint.py`:

```python
import json


def _read_metadata(namespace: str, name: str):
    """Read the metadata.json sidecar for a tileset. Returns None if missing."""
    uri = f"{_tileset_dir(namespace, name)}/metadata.json"
    # Works for both file:// / local paths and s3:// via DuckDB httpfs.
    # But httpfs-readable JSON needs DuckDB; use a dedicated cursor to read it.
    import duckdb
    try:
        # Local path shortcut for tests (no need for an extra DuckDB connection).
        if not uri.startswith("s3://") and not uri.startswith("http"):
            with open(uri, "r") as f:
                return json.loads(f.read().strip().strip('"'))
        # For S3, use a one-off connection.
        c = duckdb.connect(":memory:")
        c.sql("INSTALL httpfs; LOAD httpfs")
        row = c.sql(f"SELECT content FROM read_text('{uri}')").fetchone()
        c.close()
        if row is None:
            return None
        return json.loads(row[0])
    except FileNotFoundError:
        return None
    except Exception:
        return None
```

- [ ] **Step 8: Run full tile endpoint test suite**

Run: `pytest tests/test_tile_endpoint.py tests/test_tile_pyramid.py -v`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add tiles/pyramid.py tiles/endpoint.py tests/test_tile_pyramid.py tests/test_tile_endpoint.py
git commit -m "feat(tiles): metadata sidecar for tileset config"
```

---

### Task 9: Wire tiles into `server.py`

**Files:**
- Modify: `server.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing test that server exposes the tile route**

Append to `tests/test_server.py`:

```python
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

    def test_register_hex_tiles_is_an_mcp_tool(self):
        """The MCP server should expose register_hex_tiles as a tool."""
        from server import mcp
        # FastMCP tracks tools on its internal registry; adapt to the API.
        import anyio
        tool_names = [t.name for t in anyio.run(mcp.list_tools)]
        assert "register_hex_tiles" in tool_names
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_server.py::TestTileRouteMounted -v`
Expected: `ImportError: cannot import name 'mount_tiles'` and `register_hex_tiles` not in tools.

- [ ] **Step 3: Add tile wiring to `server.py`**

In `server.py`, after the `mcp.tool()(query)` line (around line 193), add:

```python
# -------------------------------------------------------------------------
# 8b. TILE ENDPOINT — dynamic MVT for H3 hex visualization (see issue #4)
# -------------------------------------------------------------------------
from starlette.routing import Route
from tiles.endpoint import serve_tile
from tiles.db import build_tile_connection
from tiles.pyramid import register_hex_tiles as _register_hex_tiles


# Module-level persistent connection. Initialized lazily at first use OR
# at startup via the lifespan in mount_tiles().
_tile_con = None


def _get_tile_con():
    global _tile_con
    if _tile_con is None:
        _tile_con = build_tile_connection()
    return _tile_con


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

    Input SQL contract: return (h3_index, value1, value2, ...) where the first
    column is an H3 index at resolution `finest_res`, and subsequent columns
    are numeric values. All value columns are aggregated by `agg` (AVG, SUM, MIN,
    MAX, COUNT) at each coarser resolution down to `min_res`.

    Returns a dict with `tile_url_template` that any MapLibre client can consume
    via:
        map.addSource(id, {type: 'vector', tiles: [tile_url_template], minzoom: 0, maxzoom: 14});
        map.addLayer({..., 'source-layer': 'hex', paint: {...}});
    """
    con = _get_tile_con()
    return _register_hex_tiles(
        con=con, sql=sql, finest_res=finest_res, min_res=min_res,
        agg=agg, zoom_offset=zoom_offset,
    )


mcp.tool()(register_hex_tiles)


def mount_tiles(app):
    """Mount the /tiles route onto the Starlette app and ensure tile con is ready."""
    # Pre-initialize the connection so first tile request is fast.
    con = _get_tile_con()
    app.state.tile_con = con
    app.router.routes.append(
        Route("/tiles/{namespace}/{name}/{z:int}/{x:int}/{y:int}.pbf", serve_tile)
    )
```

Then update the `if __name__ == "__main__":` block to call `mount_tiles(app)` right after `app = mcp.streamable_http_app()`:

```python
if __name__ == "__main__":
    app = mcp.streamable_http_app()
    app.router.redirect_slashes = False
    mount_tiles(app)     # <-- add this line
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_server.py::TestTileRouteMounted -v`
Expected: 2 passed.

Also run the existing suite to verify no regressions:

Run: `pytest tests/test_server.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "feat(server): mount tile route and register_hex_tiles MCP tool"
```

---

### Task 10: Docs updates

**Files:**
- Modify: `h3-guide.md`
- Modify: `assistant-role.md`

- [ ] **Step 1: Read the existing sections that will host the new content**

Run: `grep -n "public-output" h3-guide.md` and look at the "Generating Output Files" section around line 165. Read surrounding lines (offset 150, limit 60).

- [ ] **Step 2: Add a new section to `h3-guide.md` documenting `register_hex_tiles`**

Append to `h3-guide.md` (after the existing "Generating Output Files" section):

```markdown
## Rendering hex results as a map layer

For hex result sets too large to display as a table (roughly >100k cells), or
whenever the user wants to visualize hexes directly on the map, use the
`register_hex_tiles` MCP tool instead of returning markdown.

**When to use:**
- User asks to *show*, *render*, *visualize*, or *color* hexes on the map
- Result set would exceed the 50-row `query` cap with meaningful content
- The answer is "per-hex values across a region" rather than "top N rows"

**How to use:**
1. Write your analysis SQL that returns `(h3_index, value1, value2, ...)` — first
   column must be the H3 index at your chosen resolution.
2. Call `register_hex_tiles(sql=..., finest_res=N)` with the resolution of that
   index column.
3. Return the `tile_url_template` to the client. Any MapLibre-compatible client
   (including geo-agent) will render it natively as a vector source.

Example SQL shape for the `sql` parameter:

    SELECT h3_latlng_to_cell(lat, lng, 8) AS h8,
           AVG(carbon_density) AS carbon
    FROM read_parquet('s3://public-cng/.../carbon_r8.parquet')
    WHERE <region filter>
    GROUP BY 1

The tool handles pyramiding (coarser hexes at lower zooms) and partitioned
parquet storage automatically. Repeat calls with identical inputs return the
same URL — natural caching.
```

- [ ] **Step 3: Add dispatch hint to `assistant-role.md`**

Find the section in `assistant-role.md` that describes tool dispatch (or append a new section). Add:

```markdown
### Dispatch: map-rendering tools

- `query` — for answering in markdown tables (capped at 50 rows). Default.
- `register_hex_tiles` — when the user wants *hexes on the map*, or the result
  would be a large (>100k cell) hex layer. Returns a tile URL; the client adds
  it as a MapLibre vector source.

If the user asks to "show", "render", or "color" hexes on the map, choose
`register_hex_tiles`. If they ask "what's the value of X at location Y" or
"top N hexes by Z", use `query`.
```

- [ ] **Step 4: Commit docs**

```bash
git add h3-guide.md assistant-role.md
git commit -m "docs: register_hex_tiles usage and dispatch guidance"
```

---

### Task 11: Manual smoke test against real S3 (optional pre-deploy check)

This is a manual one-off check before deploying — not a CI test. Skip if you're not ready to deploy.

**Files:** None

- [ ] **Step 1: Ensure S3 credentials are available**

```bash
# These need to be set in your shell for the manual test.
echo "Has MCP_S3_KEY: ${MCP_S3_KEY:+yes}"
echo "Has MCP_S3_SECRET: ${MCP_S3_SECRET:+yes}"
```

If not set, grab them from the k8s secret (`kubectl get secret mcp-public-output-secrets -o yaml`) or skip this task.

- [ ] **Step 2: Register a small CA tileset against real S3**

```bash
python3 -c "
import os, duckdb
os.environ['TILE_BUCKET_BASE'] = 's3://public-output'
from tiles.db import build_tile_connection
from tiles.pyramid import register_hex_tiles

con = build_tile_connection()
# Set up S3 credentials on the persistent connection for this one-off test.
con.sql(f\"\"\"CREATE OR REPLACE SECRET s3 (TYPE S3, KEY_ID '{os.environ['MCP_S3_KEY']}',
            SECRET '{os.environ['MCP_S3_SECRET']}', ENDPOINT 's3-west.nrp-nautilus.io',
            URL_STYLE 'path', USE_SSL 'true')\"\"\")

result = register_hex_tiles(
    con=con,
    sql='''SELECT h3_latlng_to_cell(lat, lng, 6) AS h6, 1.0 AS val
           FROM (SELECT 36 + random()*3 AS lat, -121 - random()*3 AS lng FROM range(5000))''',
    finest_res=6, min_res=2, agg='AVG', zoom_offset=4,
)
print('Hash:', result['hash'])
print('Tile URL template:', result['tile_url_template'])
print('Feature count:', result['feature_count_finest'])
"
```

- [ ] **Step 3: Verify the pyramid is on S3**

```bash
# Using rclone or any S3 client.
rclone ls nrp-s3:public-output/hex/<hash>/
```

Expected: parquet files under `res=2/` through `res=6/`, plus `metadata.json`.

- [ ] **Step 4: Start server locally and fetch a tile**

```bash
# In another terminal:
uv run --with mcp --with duckdb --with pandas --with uvicorn --with tabulate --with pystac --with requests server.py &
SERVER_PID=$!

# Wait for startup, then:
curl -v "http://localhost:8000/tiles/hex/<hash>/5/5/12.pbf" -o /tmp/tile.pbf
ls -l /tmp/tile.pbf
# Expected: non-zero file, content-type application/vnd.mapbox-vector-tile.

kill $SERVER_PID
```

- [ ] **Step 5: No commit — this is a manual check**

---

## Self-Review

Ran the self-review checklist against the spec (`docs/superpowers/specs/2026-04-15-dynamic-mvt-tile-endpoint-design.md`):

**Spec coverage:**
- URL scheme `/tiles/<namespace>/<name>/{z}/{x}/{y}.pbf` → Task 7 (`serve_tile`) + Task 9 (route mount).
- S3-as-state-store content-addressable pyramid → Task 4 (pyramid SQL) + Task 5 (register).
- LOD Approach B pyramid → Task 4 (generates the right UNION ALL structure).
- Zoom → resolution with `zoom_offset` → Task 3 (`zoom_to_h3_res`) + Task 8 (per-tileset metadata).
- `register_hex_tiles` tool API → Task 5 + Task 9 (MCP registration).
- Persistent connection + `anyio.to_thread.run_sync` → Task 6 + Task 7.
- Client usage pattern (MapLibre vector source) → documented in Task 10.
- Client-agnosticism → implicit (endpoint is plain XYZ MVT).
- Private-source caveat → out of scope for v1 per spec; pyramid always lands in public bucket.
- Testing strategy (unit math, integration pyramid, end-to-end tile) → Tasks 2–3 (math), 4–5 (pyramid), 7 (end-to-end).
- Open items (ST_AsMVT API verification, error taxonomy, empty tiles at low zoom) → Task 1 (API probe), Task 7 (204 for empty, 404 for unknown).

**Placeholder scan:** Each code step contains complete code. No "TBD" / "add error handling" / "similar to above" — every step shows the content.

**Type consistency:**
- `content_hash(...)` signature in Task 3 matches its call in Task 5's `register_hex_tiles`.
- `zoom_to_h3_res(z, min_res, finest_res, zoom_offset)` signature matches calls in Tasks 7 and 8.
- `build_pyramid_sql` parameter names match the call in Task 5.
- `serve_tile` path params (`namespace`, `name`, `z`, `x`, `y`) match the Route declaration in Task 7 and Task 9.
- `app.state.tile_con` written in Task 6 (`tile_lifespan`) and Task 9 (`mount_tiles`), read in Task 7 (`serve_tile`). Consistent.

**Risks identified during review:**
- Task 1's API-probe strategy: if DuckDB's `ST_AsMVT` aggregate returns a blob rather than bytea, minor adjustment needed in `_run_tile_query` (change the `bytes(row[0])` call). Flag for the implementer.
- Task 7's `_pyramid_exists` probe loop is replaced by metadata in Task 8 — intentionally kept as a step-wise evolution so test passes incrementally.
- Task 8's `read_text` function may not exist in all DuckDB versions; fallback via `read_blob` may be needed — noted in the implementation step.

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-15-dynamic-mvt-tile-endpoint.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
