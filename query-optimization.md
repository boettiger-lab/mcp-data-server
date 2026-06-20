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
-- Join first, aggregate after → only scope's h0 partitions opened
WITH lc_on_scope AS (
  SELECT s.h8, s.h0, l.lc_class
  FROM scope s
  JOIN read_parquet('<large_hex>') l USING (h8, h0)
  WHERE l.lc_class IS NOT NULL
)
SELECT h8, h0, MODE(lc_class) AS dominant
FROM lc_on_scope GROUP BY h8, h0;
```

### Clip to a named region by hex-join

For "X **in** \<country / state / county / region\>", clip X to the region's
boundary hexes with a `SEMI JOIN` against the region-hex on `(h8, h0)`. Name the
region on the small region-hex side; filter the large side by the join alone.
The region hexes supply the `h0` set that prunes the scan, and the boundary clip
keeps only on-land cells.

Region-hex sources: countries → `overture-divisions-countries-*` (`country` =
ISO alpha-2); states/regions and counties → the matching `overture-divisions-*`
or `census-2024-*` hex collection.

```sql
-- "GBIF richness in Peru"
WITH region AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://public-overturemaps/2026-02-18.0/countries/hex/h0=*/data_0.parquet')
  WHERE country = 'PE'
)
SELECT g.h8, COUNT(DISTINCT g.species) AS species_richness
FROM read_parquet('<gbif_hex>', hive_partitioning = true) g
SEMI JOIN region r USING (h8, h0)
WHERE g.species IS NOT NULL
GROUP BY g.h8;
```

Put the `SEMI JOIN` on the raw `read_parquet()` of the large side, before
`GROUP BY` (§2).

### Scoping by name or feature id (no region mask)

To filter a global `…/hex/h0=*/…` dataset by a name or `_cng_fid` and there is no region-mask hex to join, first restrict `h0` to the region, then apply the attribute filter:

- **Known coordinates** (island centroids, a city, bbox corners): build the `h0` set with `h3_latlng_to_cell(lat, lng, 0)` per point and `UNNEST(h3_grid_disk(h0, 1))` to include neighbouring res-0 cells. For large or multi-part features, supply every known point or the bbox corners — one centroid can miss partitions the feature reaches.
- **Known name only**: look the feature up in the dataset's GeoParquet asset (one row per feature) to read its centroid/bbox, convert to `h0`, then query the hex.

```sql
WITH h0s AS (
  SELECT DISTINCT d
  FROM (VALUES (19.282,166.647),(16.728,-169.534),(6.383,-162.417),
               (0.807,-176.617),(-0.374,-159.997)) AS i(lat,lng),
       UNNEST(h3_grid_disk(h3_latlng_to_cell(lat,lng,0),1)) AS t(d)
)
SELECT SUM(h3_cell_area(h8, 'km^2')) AS area_km2
FROM (
  SELECT DISTINCT h8, h0
  FROM read_parquet('s3://<bucket>/<dataset>/hex/h0=*/data_00.parquet')
  WHERE h0 IN (SELECT d FROM h0s) AND _cng_fid IN (201, 246, 243, 142, 90)
);
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

## 4. Text matching: fuzzy search vs exact keys

DuckDB `LIKE` is case-sensitive by default. For **fuzzy substring search** on a
free-text label the user typed (site, owner, program names), normalize both
sides:

```sql
WHERE lower(site) LIKE '%' || lower('user input') || '%'
```

For an **exact match on a known key** (`sci_name`, `name_en`, a country or
category code), match the stored case exactly — this keeps parquet row-group
pruning (~3× faster on large files). Take the spelling from a `get_stac_details`
example or a `SELECT DISTINCT` probe:

```sql
WHERE sci_name = 'Rana draytonii'
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
