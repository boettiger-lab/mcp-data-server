# Query Optimization Essentials

## 0. NEVER use DuckDB spatial operations — use H3 hash joins instead

**Spatial predicates (`ST_Within`, `ST_Intersects`, `ST_Contains`, `ST_Distance`, etc.)
are always too slow for global datasets and must never be used for filtering or joining.**

This entire workflow exists to avoid row-by-row geometry evaluation. Every dataset is
pre-indexed with H3 cell IDs precisely so that geographic filtering and joining is a
pure hash join on integer keys — no geometry loading, no spatial extension, no per-row
predicate evaluation. Spatial operations on global data run for 10+ minutes and return
nothing useful.

**Wrong — never do this:**
```sql
-- Loads full geometry, evaluates ST_Within for every row in the global dataset
WHERE ST_Within(ST_Point(h3_cell_to_lng(h8), h3_cell_to_lat(h8)), country_geom)
```

**Correct — always do this:**
```sql
-- Hash join on H3 keys: uses partition pruning, no geometry, fast
JOIN read_parquet('<STAC_BOUNDARY_HEX_PATH>') boundary
  ON data.h8 = boundary.h8 AND data.h0 = boundary.h0
WHERE boundary.country = 'PE'
```

For country/region filtering, use the H3-indexed boundary datasets (Overture Maps
divisions hex, regions hex, countries hex) available in the STAC catalog. These cover
all countries and admin boundaries and are pre-joined to the same H3 resolution as
the thematic datasets.

If no H3-indexed boundary dataset exists for the required geography, say so — do not
fall back to spatial predicates.

## 1. Always include h0 in joins

Most datasets are hive-partitioned by h0. When both sides of a join have h0, always include it in the join condition:

```sql
JOIN table2 ON table1.hX = table2.hX AND table1.h0 = table2.h0
```

where `hX` is the finest resolution shared by both datasets (h8, h9, etc. — check the
schema). Omitting `AND t1.h0 = t2.h0` causes DuckDB to open every partition file on S3
instead of only the matching ones (10-100x slower).

## 2. Start with a small geographic reference dataset

Use `regions/hex/**` or `countries.parquet` as the first CTE to establish geographic
scope before joining large thematic datasets (PADUS, carbon, wetlands, species).

```sql
WITH scope AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('<STAC_REGIONS_HEX_PATH>')
  WHERE region = 'US-CA'
),
parks AS (
  SELECT DISTINCT p.h8, p.h0
  FROM scope s
  JOIN read_parquet('<STAC_PADUS_HEX_PATH>') p
    ON s.h8 = p.h8 AND s.h0 = p.h0
  WHERE p.Des_Tp = 'NP'
)
SELECT SUM(c.carbon)/1e6
FROM parks p
JOIN read_parquet('<STAC_CARBON_HEX_PATH>') c
  ON p.h8 = c.h8 AND p.h0 = c.h0
```

**Note:** `rook-ceph-rgw-nautiluss3.rook` is an internal endpoint only accessible from k8s. Always use it — not the public endpoint — to run queries.

You must read parquet datasets from S3 using read_parquet(). There are no local tables.

## 3. Case-insensitive text search

DuckDB `LIKE` is case-sensitive by default. Name/label fields (site names, owner names,
program names) are often stored in uppercase or mixed case. Always normalize both sides:

```sql
WHERE lower(site) LIKE '%' || lower('user input') || '%'
```

## 4. GeoParquet geometry columns

GeoParquet files contain a geometry column (usually `geom`) typed as `GEOMETRY('OGC:CRS84')`.
This type cannot be displayed in tabular output — the server drops it automatically.
Avoid `SELECT *` on GeoParquet files; select only the columns you need. If you need
coordinates, cast explicitly: `ST_AsText(geom) AS geom_wkt`.

## 5. Apostrophes in string literals

Site names and owner names can contain apostrophes (e.g. `O'Brien Ranch`). Double any
single quote inside a SQL string literal — do not use a backslash:

```sql
WHERE site = 'O''Brien Ranch'   -- correct
WHERE site = 'O'Brien Ranch'    -- parse error
```
