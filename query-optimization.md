# Query Optimization Essentials

## 1. Always include h0 in joins
<!-- prov: issue=#40 models=unrecorded added=2026-03-31 cell=hex-join-include-h0 tier=core -->

Most datasets are hive-partitioned by h0. When both sides of a join have h0, always include it in the join condition:

```sql
JOIN table2 ON table1.hX = table2.hX AND table1.h0 = table2.h0
```

where `hX` is the finest resolution shared by both datasets (h8, h9, etc. — check the
schema). Omitting `AND t1.h0 = t2.h0` causes DuckDB to open every partition file on S3
instead of only the matching ones (10-100x slower).

## 2. Start with a small geographic reference dataset
<!-- prov: issue=#83,#87 models=unrecorded added=2026-08-12 cell=mask-before-aggregate tier=core -->

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

The `WHERE l.lc_class IS NOT NULL` here keeps a no-data cell from poisoning the aggregate — but it also **changes which cells the answer describes**. When that column is only partly populated, see §9 before reporting the result as a share of the whole.

### Scoping by name or feature id (no region mask)
<!-- prov: issue=#163 models=unrecorded added=2026-06-07 cell=none tier=core -->

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
<!-- prov: issue=#108,#113 models=qwen3 added=2026-05-03 cell=none tier=core -->

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
<!-- prov: issue=#209,#220 models=unrecorded added=2026-06-20 cell=feature_type-exact,county-name-filter,program-like-lwcf tier=core -->

DuckDB `LIKE` is case-sensitive by default. For **fuzzy substring search** on a
free-text label the user typed (site, owner, program names), normalize both
sides:

```sql
WHERE lower(site) LIKE '%' || lower('user input') || '%'
```

For an **exact match on a known key** (`sci_name`, `name_en`, a country or
category code), match the stored case exactly instead of wrapping the column in
`lower()`. Where the file is sorted or clustered by that key, exact-case lets
DuckDB skip row groups by their stored min/max stats; `lower()` forces a full
column scan. Take the spelling from a `get_stac_details` example or a
`SELECT DISTINCT` probe:

```sql
WHERE sci_name = 'Rana draytonii'
```

## 5. GeoParquet geometry columns
<!-- prov: issue=#48 models=unrecorded added=2026-05-03 cell=none tier=core -->

GeoParquet files contain a geometry column (usually `geom`) typed as `GEOMETRY('OGC:CRS84')`.
This type cannot be displayed in tabular output — the server drops it automatically.
Avoid `SELECT *` on GeoParquet files; select only the columns you need. If you need
coordinates, cast explicitly: `ST_AsText(geom) AS geom_wkt`.

## 6. Apostrophes in string literals
<!-- prov: issue=unrecorded models=unrecorded added=2026-06-24 cell=none tier=core -->

Site names and owner names can contain apostrophes (e.g. `O'Brien Ranch`). Double any
single quote inside a SQL string literal — do not use a backslash:

```sql
WHERE site = 'O''Brien Ranch'   -- correct
WHERE site = 'O'Brien Ranch'    -- parse error
```

## 7. Filter no-data values before SUM / AVG
<!-- prov: issue=#243,#244 models=gemma,glm-5,kimi added=2026-08-12 cell=sum-amount-safe tier=core -->

A single `NaN` turns the entire aggregate into `NaN` — one no-data cell poisons a
whole `SUM` or `AVG`. No-data shows up as literal `NaN`, or as sentinel codes a
dataset reserves for "no data" (e.g. land-cover classes 0, 80, 200). Exclude both
before aggregating:

```sql
SELECT SUM(value) AS total
FROM read_parquet('<hex>')
WHERE value IS NOT NULL AND NOT isnan(value)
  AND lc_class NOT IN (0, 80, 200);   -- dataset's no-data sentinels
```

Take the sentinel codes from the dataset's STAC description. A `NaN` total or a
total far smaller than expected is the signature of this trap.

## 8. Total a partial-coverage feature by its own measure, not a hex count
<!-- prov: issue=#289 models=claude-sonnet-5,glm-5.2,kimi-k3,qwen,qwen3-small added=2026-08-12 cell=acres-from-geoparquet-not-hex tier=core -->

A vector feature's area or length is the value in its **own** column (`acres`,
`length_km`), summed over `DISTINCT` feature id — tiling replicates that value on
every cell the feature touches, so dedup by `_cng_fid` before summing.
`COUNT(DISTINCT hN) * h3_cell_area(...)` is a larger, different number: it counts
every partially-covered boundary cell as if the feature filled it. Use the hex
only to *locate* the feature (mask, join, overlay); take the magnitude from the
measured column. For the conserved or overlaid **share** of that total, weight by
the coverage fraction — area for polygons and rasters, length for lines — and keep
the weight inside the same `SUM()` as the measure, not derived in a subquery the
outer aggregate then ignores. (See the overlay rules in the H3 guide.)

## 9. Filtering no-data changes what you measured — say so
<!-- prov: issue=#359 models=unrecorded added=2026-08-12 cell=instance-only:streamorder-use-nhdplus-hr-not-base-nhd tier=core -->

`WHERE col IS NOT NULL` (or excluding sentinels) fixes the arithmetic but replaces
the population. Missing values are rarely missing at random: incomplete columns are
usually incomplete in patterns that track geography, feature type, or source
vintage, so the rows that survive are a biased sample, and any share computed from
them describes that sample, not the whole. Before using a partially-populated
column as a filter, a class selector, or a denominator:

1. **Quantify coverage** — what fraction of rows, and of the relevant measure
   (area, length, count), survive the filter?
2. **Check it against at least one independent grouping** — a partition key,
   region, category, or date. Uniform coverage is safe to filter on; coverage that
   is near 0% in some groups and near 100% in others means the filter is a group
   selector wearing an attribute's clothes.
3. **Confirm how absence is encoded.** `IS NOT NULL` misses sentinel values
   (`0`, `-9999`, `''`); take them from the STAC description and exclude them before
   measuring coverage — a sentinel can fake coverage a column does not have.
4. **Report it.** If coverage is materially incomplete, state the covered fraction
   and what it covers alongside the number. If coverage is concentrated in part of
   the study area, the honest answer is that the breakdown is unavailable for the
   whole — not a number with a caveat.
