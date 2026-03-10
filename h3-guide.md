# H3 Geospatial Indexing

**Most datasets have H3 hex versions.** Always use them for spatial operations instead of GeoParquet geometry columns.

**H3 hex joins replace geometry functions.** Do NOT use `ST_Intersects`, `ST_Contains`, `ST_Area`, or `ST_Within` — these require scanning full polygon geometries and are orders of magnitude slower. Instead, join datasets on their shared H3 index (`h8`, `h0`) to compute overlaps, areas, and containment. If two datasets use different H3 resolutions, convert with `h3_cell_to_parent()`.

## Key Facts

- Each h8 hexagon = **73.7327598 hectares** (≈ 0.737 km²)
- Always report AREAS, not hex counts
- **Use** `APPROX_COUNT_DISTINCT(h8)` when counting hexes to compute areas -- this avoids double-counting and is reasonably fast enough.

## Area Conversion

```sql
SELECT APPROX_COUNT_DISTINCT(h8) * 0.737327598 as area_km2 FROM ...
```

## Joining Different Resolutions

Some datasets use different H3 resolutions (h8 vs h0-h4). Use `h3_cell_to_parent()` to convert:

```sql
-- iNaturalist has h4, wetlands has h8 → convert h8 to h4
JOIN read_parquet('s3://public-inat/range-maps/hex/**') pos 
    ON h3_cell_to_parent(wetlands.h8, 4) = pos.h4 
    AND wetlands.h0 = pos.h0  -- Always include h0 for partition pruning!
```

## Multiple Rows per Hex: Two Different Problems

There are **two distinct reasons** a dataset can have multiple rows with the same `h8` value, and they require different fixes:

---

### Problem 1 — Overlapping polygons (vector datasets)

Datasets like WDPA store one row per *feature* (protected area). Multiple protected areas can cover the same hex, producing duplicate `h8` values. Fix: **deduplicate with DISTINCT** before joining.

**❌ WRONG:** Joining WDPA directly multiplies rows
```sql
-- If 2 protected areas cover hex ABC, this counts carbon twice
JOIN read_parquet('s3://public-wdpa/hex/**') w ON c.h8 = w.h8
```

**✅ CORRECT:** Deduplicate first with DISTINCT
```sql
protected_hexes AS (
  SELECT DISTINCT h8, h0 FROM read_parquet('s3://public-wdpa/hex/**')
),
protected_carbon AS (
  SELECT country, SUM(carbon) as protected
  FROM countries c
  JOIN protected_hexes p ON c.h8 = p.h8 AND c.h0 = p.h0
  JOIN carbon_data USING (h8, h0)
  GROUP BY country
)
```

**Datasets requiring DISTINCT deduplication:**
- WDPA (overlapping protected areas)
- Ramsar (can overlap with WDPA)
- GLWD (multiple wetland types per hex)

**Validation:** Protected percentages must be ≤ 100%. If you see >100%, you're double-counting.

---

### Problem 2 — Raster pixels (raster-derived datasets)

Raster datasets are converted to hex by assigning each **pixel** its H3 cell — no aggregation is applied during processing. When the raster resolution is finer than the H3 resolution, many pixels map to the same hex cell, producing many rows with the same `h8`, all with different values.

- At H3 resolution 8 (edge ~531m) with 30m pixels: ~300 pixel rows per hex
- At H3 resolution 8 with 1km pixels: ~1 row per hex (ratio near 1)

**DISTINCT does not help here** — you genuinely need to aggregate the values.

**✅ CORRECT: Always GROUP BY and aggregate raster datasets**
```sql
-- Continuous values (carbon, biomass, etc.) → SUM or AVG
SELECT h8, h0, SUM(carbon) as total_carbon
FROM read_parquet('s3://public-carbon/.../hex/**')
GROUP BY h8, h0

-- Categorical values (land cover, etc.) → use MODE (most frequent class)
SELECT h8, h0, MODE(Z) as dominant_class
FROM read_parquet('s3://public-wetlands/glwd/hex/**')
GROUP BY h8, h0
```

**Raster-derived datasets (always aggregate before joining):**
- Vulnerable Carbon, Irrecoverable Carbon (SUM or AVG)
- NCP (AVG)
- GLWD / land cover (MODE for dominant class)

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
