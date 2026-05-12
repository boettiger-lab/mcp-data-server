# H3 Geospatial Indexing

**Most datasets have H3 hex versions.** Always use them for spatial operations instead of GeoParquet geometry columns.

**Always use H3 hex datasets for filtering and joining — never spatial predicates on GeoParquet.**
When a dataset appears in the STAC catalog as GeoParquet, a hex-indexed version almost always exists alongside it. Find and use the hex version. Never use `ST_Within`, `ST_Intersects`, `ST_Contains`, or similar predicates to filter or join large datasets — on global data these run 10+ minutes and return nothing useful.

If you browse the catalog and only find a GeoParquet with no hex equivalent, **say so** rather than falling back to spatial predicates. A missing hex version is a data pipeline gap (not something to work around silently).

## The H3 Data Model

All datasets are already stored as H3 hex parquet in the STAC catalog — no conversion is needed. Understanding the origin of each dataset explains the structure you will encounter when you query it.

**Vector datasets** (protected areas, districts, parcels) were built by tiling each source polygon into the H3 cells it covers — one row per (feature, hex-cell) pair. A single protected area covering 500 cells has 500 rows, all sharing the same `_cng_fid` and identical feature-level attributes. `_cng_fid` is the universal feature identifier added by CNG processing and is present on all vector hex datasets.

**Raster datasets** (land cover, elevation, biomass) were built by assigning each pixel to its H3 cell — one row per pixel, with no aggregation during processing. These datasets have no `_cng_fid`. When the raster resolution is finer than the H3 resolution, many pixels share the same hex cell, producing multiple rows per hex with different values.

All spatial operations are hex joins — two datasets overlap wherever their hex IDs match. **Never use `ST_Within`, `ST_Intersects`, `ST_Centroid`, or any spatial function.** For coordinates (e.g. to supply a map zoom), use `h3_cell_to_lat(hN)` and `h3_cell_to_lng(hN)`.

## Resolution Direction

**Higher H3 resolution numbers are finer (smaller cells); lower numbers are coarser (larger cells).** h0 is the coarsest (~1000 km edge length); h15 is the finest. A higher-resolution cell is always a *child* of a lower-resolution cell — never the reverse.

- h8 cells are children of h6 cells, not parents
- If a dataset is indexed at h6, it has no h8 column and no h8_parent column
- Always check the dataset schema for available resolution columns before writing a join

## Key Facts

- Always report **areas** (km², acres, etc.), never raw hex counts
- For nationwide/global aggregates over millions of cells, `APPROX_COUNT_DISTINCT(hN)` is fast and accurate to ~1–2%. For per-group breakdowns (per-state, per-class, per-county, per-district) where each group has fewer than ~1M distinct cells, use `COUNT(DISTINCT hN)` instead — DuckDB's HLL error grows steeply at smaller cardinalities and compounds inside `GROUP BY` (real-world per-group errors of +30% have been observed). Total scan size matters less than per-group cell count.
- **Never SUM area columns** (ACRES, GIS_Acres, area_ha, etc.) on hex data. These store the source polygon's total area repeated on every hex row. `SUM(ACRES)` = polygon_area × num_hex_cells — wrong by 10³–10⁶×. Always compute area from hex cells instead. Note: `DISTINCT` deduplication removes duplicate rows for the same feature but does not resolve overlapping features — two features covering the same ground still sum their acreages independently. Counting distinct hex cells × `area_per_cell` is the only method immune to this, since it counts physical cells rather than feature declarations (see the previous bullet for `APPROX` vs exact `COUNT DISTINCT`).

## Area Conversion

| Resolution | km²/cell | acres/cell |
|---|---|---|
| h5 | 252.9 | 62,502 |
| h6 | 36.13 | 8,929 |
| h8 | 0.7373 | 182.2 |
| h10 | 0.01505 | 3.718 |

```sql
-- Global aggregate (millions of cells, ~1% error acceptable):
SELECT APPROX_COUNT_DISTINCT(h8) * 0.7373 AS area_km2 FROM ...

-- Per-group breakdown (per-state, per-class, etc.) — use exact COUNT DISTINCT:
SELECT state, COUNT(DISTINCT h8) * 0.7373 AS area_km2
FROM ...
GROUP BY state
```

## Coordinates from H3 Cells

To get latitude/longitude from a hex column (e.g. to supply a `fly_to` map
center), call `h3_cell_to_lat(hN)` / `h3_cell_to_lng(hN)` on the cell **column**.
For a feature spanning many hexes, average:

```sql
SELECT AVG(h3_cell_to_lat(h8)) AS lat,
       AVG(h3_cell_to_lng(h8)) AS lng
FROM read_parquet('<hex parquet path>')
WHERE <feature filter>;
```

**Always pass the column** (`h8`, `h0`) — never copy a displayed h3 ID as a
literal. Markdown table output may render the BIGINT in scientific notation
(e.g. `6.15323e+17`), which DuckDB parses as DOUBLE (function rejects it) and
which loses precision even when accepted. Accepted argument types are
`VARCHAR`, `UBIGINT`, and `BIGINT`.

## Joining Different Resolutions

**Always join by converting the finer (higher-numbered) dataset to the coarser resolution — never look for child columns on the coarser dataset.**

### Step 1: Check for pre-computed parent columns (preferred)

Many fine-resolution datasets (e.g. GEBCO h8) already carry pre-computed parent columns (`h7`, `h6`, `h5`, ...). Use these directly — they are faster than calling `h3_cell_to_parent()` on every row. Check the schema first:

```sql
-- Check what resolution columns exist
DESCRIBE SELECT * FROM read_parquet('<STAC_PATH>') LIMIT 1;
```

If the finer dataset has the target parent column, use it directly:

```sql
-- GEBCO (h8-indexed, has h6 column) joined to geomorphology (h6-indexed)
WITH gebco_by_h6 AS (
  SELECT h6, h0, AVG(elevation) AS avg_elevation
  FROM read_parquet('<GEBCO_PATH>')
  GROUP BY h6, h0
)
SELECT s.feature_type, g.avg_elevation
FROM read_parquet('<GEOMORPHOLOGY_PATH>') s
JOIN gebco_by_h6 g ON s.h6 = g.h6 AND s.h0 = g.h0
```

### Step 2: Fall back to h3_cell_to_parent() when no pre-computed column exists

Use `h3_cell_to_parent()` — not `h3_cell_to_children()` — when the pre-computed parent column is absent:

```sql
-- dataset_a has h8, dataset_b has h4: convert h8 → h4
JOIN dataset_b b
    ON h3_cell_to_parent(a.h8, 4) = b.h4
    AND a.h0 = b.h0  -- include h0 when both sides have it

-- WDPA (h8) + GFW fishing effort (h6): convert h8 → h6
JOIN gfw ON h3_cell_to_parent(wdpa.h8, 6) = gfw.h6
         AND wdpa.h0 = gfw.h0
```

When one side lacks h0, omit it from that side. Prefer hex-partitioned variants (with h0) when available for partition pruning.

## Multiple Rows per Hex: Three Different Problems

There are **three distinct reasons** a dataset can have multiple rows with the same `h8` value, and they require different fixes:

---

### Problem 1 — Tiling: same feature repeated across many hexes

Every vector polygon is tiled into N hex rows — one per H3 cell it covers — all sharing the same `_cng_fid` and identical feature-level attributes (name, declared acres, funding amount). Summing or counting without deduplicating by feature multiplies attribute values by N.

**❌ WRONG: sums amount N times (once per hex row)**
```sql
SELECT SUM(amount) FROM read_parquet('<hex>') WHERE state_id = 'CA'
```

**✅ CORRECT: one amount per feature**
```sql
SELECT SUM(amount) FROM (
  SELECT DISTINCT _cng_fid, amount
  FROM read_parquet('<hex>')
  WHERE state_id = 'CA'
)
```

`_cng_fid` is the universal feature ID on all CNG-processed vector hex datasets. Some datasets also carry a source-specific ID (e.g. `tpl_id`, `GEOID`) for cross-collection joins — check `get_schema`.

**Cross-collection case: flat table joined to a hex table for spatial assignment**

When the aggregate value lives in a flat (non-hex) table, joining it to a hex table replicates it across N hex rows. Apply the same principle — deduplicate by feature ID — but the `DISTINCT` must be on `(feature_id, geography_id)`, not on hex coordinates:

```sql
-- ❌ WRONG: DISTINCT on hex coords doesn't help — they're already unique per row
tx_sites_hex AS (
  SELECT DISTINCT s.h10, s.h0, f.total_federal   -- still N rows per tpl_id
  FROM flat_funding f
  JOIN read_parquet('<sites_hex>') s USING (tpl_id)
)

-- ✅ CORRECT: DISTINCT on (feature_id, geography_id) gives one row per assignment
site_district AS (
  SELECT DISTINCT s.tpl_id, c.GEOID
  FROM read_parquet('<sites_hex>') s
  JOIN read_parquet('<cd_hex>') c ON s.h10 = c.h10 AND s.h0 = c.h0
)
SELECT sd.GEOID, SUM(f.total_federal) AS total
FROM site_district sd
JOIN flat_funding f USING (tpl_id)
GROUP BY sd.GEOID
```

The flat table is joined **last**, after the spatial assignment is deduplicated.

---

### Problem 2 — Overlapping polygons (vector datasets)

Some vector datasets store one row per *feature* (e.g. each protected area). Multiple features can cover the same hex, producing duplicate `h8` values. Fix: **deduplicate with DISTINCT** before joining.

**❌ WRONG:** Joining directly multiplies rows
```sql
-- If 2 features cover hex ABC, this counts carbon twice
JOIN read_parquet('<STAC_HEX_PATH>') d ON c.h8 = d.h8
```

**✅ CORRECT:** Deduplicate first with DISTINCT
```sql
unique_hexes AS (
  SELECT DISTINCT h8, h0 FROM read_parquet('<STAC_HEX_PATH>')
),
SELECT country, SUM(carbon) as total
FROM countries c
JOIN unique_hexes u ON c.h8 = u.h8 AND c.h0 = u.h0
JOIN carbon_data USING (h8, h0)
GROUP BY country
```

**Validation:** Protected percentages must be ≤ 100%. If you see >100%, you're double-counting.

Check the dataset's STAC description — it will note when DISTINCT is required.

---

### Problem 3 — Raster pixels (raster-derived datasets)

Raster datasets are converted to hex by assigning each **pixel** its H3 cell — no aggregation is applied during processing. When the raster resolution is finer than the H3 resolution, many pixels map to the same hex cell, producing many rows with the same `h8`, all with different values.

- At H3 resolution 8 (edge ~531m) with 30m pixels: ~300 pixel rows per hex
- At H3 resolution 8 with 1km pixels: ~1 row per hex (ratio near 1)

**DISTINCT does not help here** — you genuinely need to aggregate the values.

**✅ CORRECT: Always GROUP BY and aggregate raster datasets**
```sql
-- Continuous values (carbon, biomass, etc.) → SUM or AVG
SELECT h8, h0, SUM(value) as total
FROM read_parquet('<STAC_HEX_PATH>')
GROUP BY h8, h0

-- Categorical values (land cover, etc.) → use MODE (most frequent class)
SELECT h8, h0, MODE(class) as dominant_class
FROM read_parquet('<STAC_HEX_PATH>')
GROUP BY h8, h0
```

Check the dataset's STAC description — it will note when aggregation is required and which method (SUM, AVG, or MODE) to use.

**If you plan to mask this result against another hex dataset:** put the
`SEMI JOIN` on the raw `read_parquet(...)` *before* `GROUP BY`, not in a
CTE after it. Aggregation blocks DuckDB's dynamic partition pruning, so a
post-aggregation mask forces every h0 partition of the value dataset to
be scanned — turning a small masked query into a global one.

```sql
-- Mask first: only matching h0 files scanned
SELECT a.h8, MODE(a.lc_class) AS dominant
FROM read_parquet('<raster_hex>', hive_partitioning = true) a
SEMI JOIN mask m USING (h8, h0)
WHERE a.lc_class IS NOT NULL
GROUP BY a.h8;
```

---

### Diagnostic: check rows-per-hex before writing queries

When uncertain, run this check on a single h0 partition first:

```sql
SELECT
  COUNT(*)                        AS total_rows,
  APPROX_COUNT_DISTINCT(h8)       AS unique_hexes,
  COUNT(*) * 1.0 / APPROX_COUNT_DISTINCT(h8) AS avg_rows_per_hex
FROM read_parquet('<STAC_HEX_PATH_SINGLE_PARTITION>');
```

| avg_rows_per_hex | Meaning |
|---|---|
| ≈ 1 | One row per hex — check `_cng_fid` presence; if vector, tiling dedup still applies to attribute sums |
| > 1, integer-ish | Overlapping polygons — use DISTINCT |
| >> 1, non-integer | Raster pixels — use GROUP BY + SUM/AVG/MODE |

## Generating Output Files

```sql
COPY (SELECT ...) TO 's3://public-output/unique-file-name.csv' (FORMAT CSV, HEADER, OVERWRITE_OR_IGNORE);
```

Then tell the user the *public https* address (note the use of the public, not private endpoint): it should have the format like: `https://s3-west.nrp-nautilus.io/public-output/unique-file-name.csv` (adjust `unique-file-name.csv` part appropriately.)

**Note:** s3://public-output has a 30-day expiration and 1 Gb object size limit. CORS headers will permit files to be placed here and rendered by other tools.

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
