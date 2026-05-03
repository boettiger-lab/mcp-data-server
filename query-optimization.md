# Query Optimization Essentials

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

**Aggregate after the join, never before.** When joining a small scope to a large hex dataset, do the join against the raw `read_parquet(...)` of the large side. Wrapping the large side in a pre-aggregation CTE (`GROUP BY`, `MODE`, `SUM`) before the join forces DuckDB to scan every h0 partition of it — the small scope's h0 values cannot prune through an aggregation operator.

```sql
-- ❌ WRONG: large side aggregated first → scans all h0 partitions
WITH lc_agg AS (
  SELECT h8, h0, MODE(lc_class) AS dominant
  FROM read_parquet('<large_hex>') GROUP BY h8, h0
)
SELECT s.h8, a.dominant
FROM scope s JOIN lc_agg a USING (h8, h0);

-- ✅ RIGHT: join first, aggregate after → only scope's h0 partitions opened
WITH lc_on_scope AS (
  SELECT s.h8, s.h0, l.lc_class
  FROM scope s
  JOIN read_parquet('<large_hex>') l USING (h8, h0)
  WHERE l.lc_class IS NOT NULL
)
SELECT h8, h0, MODE(lc_class) AS dominant
FROM lc_on_scope GROUP BY h8, h0;
```

## 3. Trust your schema — don't grep with DESCRIBE+LIKE

For datasets already loaded in your app, the column list returned by `get_schema`
(or `get_stac_details`) is canonical: every column, with type and description.
Running follow-up queries like

```sql
SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('…') LIMIT 1)
WHERE column_name LIKE '%corridor%'
```

to grep for specific columns is wasteful — the same data is already in your
context. Search the existing schema response instead. `DESCRIBE` is appropriate
only for arbitrary parquet files not represented in any STAC collection.

## 4. Case-insensitive text search

DuckDB `LIKE` is case-sensitive by default. Name/label fields (site names, owner names,
program names) are often stored in uppercase or mixed case. Always normalize both sides:

```sql
WHERE lower(site) LIKE '%' || lower('user input') || '%'
```

## 5. GeoParquet geometry columns

GeoParquet files contain a geometry column (usually `geom`) typed as `GEOMETRY('OGC:CRS84')`.
This type cannot be displayed in tabular output — the server drops it automatically.
Avoid `SELECT *` on GeoParquet files; select only the columns you need. If you need
coordinates, cast explicitly: `ST_AsText(geom) AS geom_wkt`.

## 6. Apostrophes in string literals

Site names and owner names can contain apostrophes (e.g. `O'Brien Ranch`). Double any
single quote inside a SQL string literal — do not use a backslash:

```sql
WHERE site = 'O''Brien Ranch'   -- correct
WHERE site = 'O'Brien Ranch'    -- parse error
```
