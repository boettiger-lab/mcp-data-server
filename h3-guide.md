# H3 Geospatial Indexing

**Most datasets have H3 hex versions.** Always use them for spatial operations instead of GeoParquet geometry columns.

**H3 hex joins replace geometry functions.** Do NOT use `ST_Intersects`, `ST_Contains`, `ST_Area`, or `ST_Within` — these require scanning full polygon geometries and are orders of magnitude slower. Instead, join datasets on their shared H3 index (`h8`, `h0`) to compute overlaps, areas, and containment. If two datasets use different H3 resolutions, convert with `h3_cell_to_parent()`.

## Key Facts

- Always report **areas** (km², acres, etc.), never raw hex counts
- Use `APPROX_COUNT_DISTINCT(hN)` when counting hexes — avoids double-counting
- **Never SUM area columns** (ACRES, GIS_Acres, area_ha, etc.) on hex data. These store the source polygon's total area repeated on every hex row. `SUM(ACRES)` = polygon_area × num_hex_cells — wrong by 10³–10⁶×. Always compute area from hex cells instead.

## Area Conversion

| Resolution | km²/cell | acres/cell |
|---|---|---|
| h5 | 252.9 | 62,502 |
| h6 | 36.13 | 8,929 |
| h8 | 0.7373 | 182.2 |
| h10 | 0.01505 | 3.718 |

```sql
-- Example: area from h8 hexes
SELECT APPROX_COUNT_DISTINCT(h8) * 0.7373 AS area_km2 FROM ...

-- Example: area from h5 hexes
SELECT APPROX_COUNT_DISTINCT(h5) * 252.9 AS area_km2 FROM ...
```

## Joining Different Resolutions

Datasets use different H3 resolutions. Use `h3_cell_to_parent()` to convert the **finer** resolution up to the **coarser** one before joining:

```sql
-- dataset_a has h8, dataset_b has h4: convert h8 → h4
JOIN dataset_b b
    ON h3_cell_to_parent(a.h8, 4) = b.h4
    AND a.h0 = b.h0  -- include h0 when both sides have it
```

When one side lacks h0, omit it from that side. Prefer hex-partitioned variants (with h0) when available for partition pruning.

## Multiple Rows per Hex: Two Different Problems

There are **two distinct reasons** a dataset can have multiple rows with the same `h8` value, and they require different fixes:

---

### Problem 1 — Overlapping polygons (vector datasets)

Some vector datasets store one row per *feature* (e.g. each protected area). Multiple features can cover the same hex, producing duplicate `h8` values. Fix: **deduplicate with DISTINCT** before joining.

**❌ WRONG:** Joining directly multiplies rows
```sql
-- If 2 features cover hex ABC, this counts carbon twice
JOIN read_parquet('s3://bucket/dataset/hex/**') d ON c.h8 = d.h8
```

**✅ CORRECT:** Deduplicate first with DISTINCT
```sql
unique_hexes AS (
  SELECT DISTINCT h8, h0 FROM read_parquet('s3://bucket/dataset/hex/**')
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

### Problem 2 — Raster pixels (raster-derived datasets)

Raster datasets are converted to hex by assigning each **pixel** its H3 cell — no aggregation is applied during processing. When the raster resolution is finer than the H3 resolution, many pixels map to the same hex cell, producing many rows with the same `h8`, all with different values.

- At H3 resolution 8 (edge ~531m) with 30m pixels: ~300 pixel rows per hex
- At H3 resolution 8 with 1km pixels: ~1 row per hex (ratio near 1)

**DISTINCT does not help here** — you genuinely need to aggregate the values.

**✅ CORRECT: Always GROUP BY and aggregate raster datasets**
```sql
-- Continuous values (carbon, biomass, etc.) → SUM or AVG
SELECT h8, h0, SUM(value) as total
FROM read_parquet('s3://bucket/dataset/hex/**')
GROUP BY h8, h0

-- Categorical values (land cover, etc.) → use MODE (most frequent class)
SELECT h8, h0, MODE(class) as dominant_class
FROM read_parquet('s3://bucket/dataset/hex/**')
GROUP BY h8, h0
```

Check the dataset's STAC description — it will note when aggregation is required and which method (SUM, AVG, or MODE) to use.

---

### Diagnostic: check rows-per-hex before writing queries

When uncertain, run this check on a single h0 partition first:

```sql
SELECT
  COUNT(*)                        AS total_rows,
  APPROX_COUNT_DISTINCT(h8)       AS unique_hexes,
  COUNT(*) * 1.0 / APPROX_COUNT_DISTINCT(h8) AS avg_rows_per_hex
FROM read_parquet('s3://bucket/dataset/hex/h0=8001fffffffffff/data_0.parquet');
```

| avg_rows_per_hex | Meaning |
|---|---|
| ≈ 1 | One row per hex — no aggregation needed |
| > 1, integer-ish | Overlapping polygons — use DISTINCT |
| >> 1, non-integer | Raster pixels — use GROUP BY + SUM/AVG/MODE |

## Generating Output Files

```sql
COPY (SELECT ...) TO 's3://public-output/unique-file-name.csv' (FORMAT CSV, HEADER, OVERWRITE_OR_IGNORE);
```

Then tell the user the *public https* address (note the use of the public, not private endpoint): it should have the format like: `https://s3-west.nrp-nautilus.io/public-output/unique-file-name.csv` (adjust `unique-file-name.csv` part appropriately.)

**Note:** s3://public-output has a 30-day expiration and 1 Gb object size limit. CORS headers will permit files to be placed here and rendered by other tools.
