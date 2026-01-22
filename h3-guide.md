# H3 Geospatial Indexing

**Most of these data uses H3 hexagons** (https://h3geo.org) - uniform hexagonal grid covering Earth

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

## Avoiding Double-Counting in Overlapping Datasets

**Critical:** Some datasets (WDPA, Ramsar, GLWD) have multiple records per hex. Joining directly will overcount.

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

**Validation:** Protected percentages must be ≤ 100%. If you see >100%, you're double-counting.

**Datasets requiring deduplication:**
- WDPA (overlapping protected areas)
- Ramsar (can overlap with WDPA)
- GLWD (multiple wetland types per hex)

## Generating Output Files

```sql
COPY (SELECT ...) TO 's3://public-output/unique-file-name.csv' (FORMAT CSV, HEADER, OVERWRITE_OR_IGNORE);
```

Then tell the user the *public https* address (note the use of the public, not private endpoint): it should have the format like: `https://s3-west.nrp-nautilus.io/public-output/unique-file-name.csv` (adjust `unique-file-name.csv` part appropriately.)

**Note:** s3://public-output has a 30-day expiration and 1 Gb object size limit. CORS headers will permit files to be placed here and rendered by other tools.
