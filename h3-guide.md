# H3 Geospatial Indexing

**Most datasets have H3 hex versions.** Always use them for spatial operations instead of GeoParquet geometry columns.

**Always use H3 hex datasets for filtering and joining — never spatial predicates on GeoParquet.**
When a dataset appears in the STAC catalog as GeoParquet, a hex-indexed version almost always exists alongside it. Find and use the hex version. Never use `ST_Within`, `ST_Intersects`, `ST_Contains`, or similar predicates to filter or join large datasets — on global data these run 10+ minutes and return nothing useful. (Narrow exception: line datasets where exact mileage at AOI boundaries is required — see Problem 4.)

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
- **Never SUM area columns** (ACRES, GIS_Acres, area_ha, etc.) on hex data. These store the source polygon's total area repeated on every hex row. `SUM(ACRES)` = polygon_area × num_hex_cells — wrong by 10³–10⁶×. Always compute area from hex cells instead. Note: `DISTINCT` deduplication removes duplicate rows for the same feature but does not resolve overlapping features — two features covering the same ground still sum their acreages independently. Counting distinct hex cells × `area_per_cell` is the only method immune to this, since it counts physical cells rather than feature declarations (see the previous bullet for `APPROX` vs exact `COUNT DISTINCT`). The same row-replication problem applies to `length_*` columns on line hex at smaller scale — see Problem 4.

## Area Conversion

H3 cells are not equal-area — true area varies with latitude and icosahedral distortion (res-8 cells span ~0.55–0.82 km²). Pick the method by scope:

- **Scoped to a region, feature, or group** (a `WHERE`/mask bounds it to roughly ≤ a few million cells) — the common case, and the accuracy-critical one: sum exact `h3_cell_area()` over distinct cells.
- **Unscoped global coverage** (millions of cells, no region filter): multiply an approximate count by the rough per-cell constant.

For a region or feature area, and for per-group breakdowns, sum `h3_cell_area()` over distinct cells (exact at any resolution):

```sql
-- Region / feature area:
SELECT SUM(h3_cell_area(h8, 'km^2')) AS area_km2
FROM (SELECT DISTINCT h8, h0 FROM read_parquet('<hex>') WHERE <scope>);

-- Per-group breakdown (per-state, per-class, etc.):
SELECT state, SUM(h3_cell_area(h8, 'km^2')) AS area_km2
FROM (SELECT DISTINCT state, h8, h0 FROM read_parquet('<hex>'))
GROUP BY state;
```

When the scope is a name or feature id on a global `h0=*` dataset, restrict `h0` first — see *Scoping by name or feature id* in query-optimization.md.

`h3_cell_area()` takes a resolution column (`h3_cell_area(h6, 'km^2')`) and exactly one of three units: `'km^2'`, `'m^2'`, `'rads^2'`. It has no acre unit; any other unit string returns `NaN`, not an error. For acres, compute in `km^2` and multiply by 247.105 (or `m^2` by 0.000247105):

```sql
SELECT SUM(h3_cell_area(h8, 'km^2')) * 247.105 AS area_acres
FROM (SELECT DISTINCT h8, h0 FROM read_parquet('<hex>') WHERE <scope>);
```

The `acres/cell (rough)` column in the table below already includes this factor, so a count × constant path needs no conversion.

For unscoped global aggregates over millions of cells, multiplying an approximate count by the rough per-cell constant is faster (within ~1–2%). Exact area would force materializing every distinct cell, defeating the approximate path:

```sql
SELECT APPROX_COUNT_DISTINCT(h8) * 0.7373 AS area_km2 FROM ...
```

The constants below are latitude/distortion-averaged, not true per-cell values (res-8 cells range ~0.55–0.82 km²):

| Resolution | km²/cell (rough) | acres/cell (rough) |
|---|---|---|
| h5 | 252.9 | 62,502 |
| h6 | 36.13 | 8,929 |
| h7 | 5.161 | 1,275 |
| h8 | 0.7373 | 182.2 |
| h9 | 0.1053 | 26.02 |
| h10 | 0.01505 | 3.718 |

Use the constant for the dataset's **native** resolution — the column it is actually indexed on (check with `DESCRIBE`). Multiplying a cell count by the constant for a different resolution is off by ~7× per level.

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

## Distance Between Hexes

`h3_great_circle_distance` measures between two coordinate pairs, not cell
indices. To get the distance between two hex cells, convert each to its center
first with `h3_cell_to_lat` / `h3_cell_to_lng`:

```sql
SELECT h3_great_circle_distance(
  h3_cell_to_lat(a.h8), h3_cell_to_lng(a.h8),
  h3_cell_to_lat(b.h8), h3_cell_to_lng(b.h8),
  'km') AS dist_km
FROM ...
```

Units: `'km'`, `'m'`, or `'rads'`.

## Joining Different Resolutions

**Always join by converting the finer (higher-numbered) dataset to the coarser resolution — never look for child columns on the coarser dataset.**

Pick the reducer for that conversion by what the value means: a measured quantity per cell rolls up with `SUM` or `AVG`, but a **coverage fraction** (the share of a cell covered by something) rolls up as the mean over the parent's child cells — see *Feature coarser than the overlay layer* under Problem 3.

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

Some datasets carry NULL in their finest pre-computed parent column for very large features (e.g. WDPA's largest protected areas have h8 but NULL h9). Joining on that finer column silently drops those features and undercounts coverage. Join at the coarsest resolution both sides share, or fall back to `h3_cell_to_parent()` which is always populated.

## Subsetting a dataset to a region (state, county, district)

**`h0` is the res-0 partition key — a coarse *storage* key, never a spatial or boundary filter.** Each res-0 base cell spans ~4.35 **million** km² (larger than any US state), so `WHERE h0 = …` or `WHERE h0 IN (…)` selects whole base cells, not the region. Florida sits inside a single base cell, so `WHERE h0 = <fl_cell>` renders the entire continental US; California spans two base cells, so `WHERE h0 IN (<ca_cell_1>, <ca_cell_2>)` renders both, far larger than the state. Resolving a region to its base cells (`SELECT DISTINCT h0 … WHERE STUSPS='CA'`) and filtering the value dataset by that `h0` set **alone** is always wrong — it clips to nothing finer than the base cells.

To clip a value dataset (carbon, land cover, biomass — anything under a global `hex/h0=*/`) to a named region, **filter it by the region's hex mask at the finest resolution the two share, keyed on the mask's attribute.** The census state/county hexes are ordinary catalog datasets — find their exact path with `get_stac_details` like any other hex dataset.

The most robust form is an `IN` subquery: the attribute filter lives *inside* the subquery, so it can never be misplaced. Pair the `h8 IN (…)` boundary filter with a coarse `h0 IN (…)` prefilter to prune partitions:

```sql
SELECT c.h8, SUM(c.carbon) AS carbon
FROM read_parquet('<value_hex>', hive_partitioning = true) c
WHERE c.h0 IN (SELECT DISTINCT h0 FROM read_parquet('<census_state_hex>', hive_partitioning = true) WHERE STUSPS = 'CA')
  AND c.h8 IN (SELECT DISTINCT h8 FROM read_parquet('<census_state_hex>', hive_partitioning = true) WHERE STUSPS = 'CA')
GROUP BY c.h8;
```

Here `h8 IN (…)` is the real boundary (the finest shared resolution); `h0 IN (…)` only prunes which partition files are scanned — see the closing note.

A `SEMI JOIN` to a **pre-filtered mask CTE** is equivalent and also prunes partitions (the MASK BEFORE AGGREGATE rule). Filter the attribute *inside* the CTE and join with `USING` — **never reference the mask's columns in the outer query.** DuckDB `SEMI JOIN` tests row existence only; it does NOT bring the joined table's columns into the outer `SELECT`/`WHERE` scope, so `SEMI JOIN <mask> s … WHERE s.STUSPS='CA'` fails with `Binder Error: Referenced table "s" not found`:

```sql
WITH ca AS (
  SELECT h8, h0
  FROM read_parquet('<census_state_hex>', hive_partitioning = true)
  WHERE STUSPS = 'CA'          -- attribute filter lives HERE, inside the CTE
)
SELECT c.h8, SUM(c.carbon) AS carbon
FROM read_parquet('<value_hex>', hive_partitioning = true) c
SEMI JOIN ca USING (h8, h0)    -- do NOT reference ca's columns in SELECT/WHERE
GROUP BY c.h8;
```

Restricting `h0` is legitimate only as a **partition-pruning prefilter paired with a real boundary filter** — the `h8 IN`/mask join above, or an attribute filter on the value dataset itself (see *Scoping by name or feature id* in query-optimization.md). On its own, `h0` narrows which files are scanned; it never clips to the region.

## Multiple Rows per Hex: Four Different Problems

There are **four distinct reasons** a dataset can have multiple rows with the same `h8` value, and they require different fixes:

---

### Problem 1 — Tiling: same feature repeated across many hexes

Every vector polygon is tiled into N hex rows — one per H3 cell it covers — all sharing the same `_cng_fid` and identical feature-level attributes (name, declared acres, funding amount). Summing an attribute directly multiplies it by N; deduplicate to one row per feature first:

```sql
SELECT SUM(amount) FROM (
  SELECT DISTINCT _cng_fid, amount
  FROM read_parquet('<hex>')
  WHERE state_id = 'CA'
)
```

`_cng_fid` is the universal feature ID on all CNG-processed vector hex datasets. Some datasets also carry a source-specific ID (e.g. `tpl_id`, `GEOID`) for cross-collection joins — check `get_schema`.

**Cross-collection case: flat table joined to a hex table for spatial assignment**

When the aggregate value lives in a flat (non-hex) table, joining it to a hex table replicates it across N hex rows. Apply the same principle — deduplicate by feature ID — but the `DISTINCT` must be on `(feature_id, geography_id)`, **not on hex coordinates**: hex coordinates are already unique per row, so `DISTINCT (h10, h0, value)` doesn't collapse the per-feature replication. The flat table is joined **last**, after the spatial assignment is deduplicated:

```sql
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

---

### Problem 2 — Overlapping polygons (vector datasets)

Some vector datasets store one row per *feature* (e.g. each protected area). Multiple features can cover the same hex, producing duplicate `h8` values. Joining the raw hex table directly inflates downstream aggregates (two features over the same cell sum carbon twice). Deduplicate to unique `(h8, h0)` pairs first:

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

**Always GROUP BY and aggregate raster datasets.**
```sql
-- Continuous values (carbon, biomass, elevation) → SUM or AVG
SELECT h8, h0, SUM(value) AS total
FROM read_parquet('<hex>')
GROUP BY h8, h0

-- Categorical, dominant class per cell (map styling, "what's here") → MODE
SELECT h8, h0, MODE(class) AS dominant_class
FROM read_parquet('<mode hex>')
GROUP BY h8, h0
```

Check the dataset's STAC description — it notes when aggregation is required and which method (SUM, AVG, or MODE) to use.

For categorical **area or composition** ("how much of class X", "percent of the region that is X"), MODE is the wrong tool: a cell holds several classes and MODE keeps only the winner, biasing per-class areas in both directions. Categorical layers increasingly ship a companion **`*-hex-fractions`** asset — a long schema with one `(class, frac)` row per class present in a cell, where `frac` is that class's fractional coverage of the cell, in (0,1]. When it exists (check `get_schema`), use it and weight area by `frac`:

```sql
SELECT class, SUM(frac * h3_cell_area(h10, 'km^2')) AS area_km2
FROM read_parquet('<hex-fractions>')
WHERE class <> <nodata>          -- exclude the no-data class; get its code from get_schema
GROUP BY class;
```

`get_schema` names the fractions asset and the no-data code. Per cell `SUM(frac) <= 1`; the shortfall is outside-raster/quantization, so do not treat the layer as covering 100% of a cell.

**Overlaying two partial-coverage layers — multiply the coverage fractions, then weight by cell area; never count a cell as all-or-nothing.** *(Skip unless you are intersecting a fractional-coverage layer with another layer that only partly covers its cells — e.g. "what percent of each habitat class is conserved".)* Treating a cell as fully inside the other layer whenever it matches over-counts every partly-covered cell. Weight by the coverage fraction on **both** sides, and carry `h3_cell_area` through numerator and denominator. The area terms do **not** cancel: numerator and denominator run over the same cells, but those cells differ in area, so dropping the terms weights every cell equally and biases the share toward whichever latitudes hold more cells. Keep `h3_cell_area` in both sums even though it looks like it divides out.

The other layer's per-cell weight comes from a companion **per-cell weights asset** when one exists — `get_schema` names it (e.g. ca30x30 conserved-areas publishes `…-hex-weights` at res 10, with `w1`–`w4` giving the share of each cell in GAP status 1–4). Otherwise reduce the layer's features to one weight per cell first: `MAX` over the features on the cell of the per-feature coverage share its STAC documents.

A weights asset covers only its own footprint, so `LEFT JOIN` it and `COALESCE(…, 0)`. Bound the result with a dense land grid for the region (California: `ca30x30-ecoregion` at res 10) — without it the denominator picks up cells outside the region.

```sql
WITH land AS (   -- dense res-10 land grid bounding the region
  SELECT DISTINCT h10, h0 FROM read_parquet('<ecoregion hex>')
)
SELECT f.whr13num,
       100 * SUM(f.frac * COALESCE(c.w1 + c.w2, 0) * h3_cell_area(f.h10, 'km^2'))
           / SUM(f.frac * h3_cell_area(f.h10, 'km^2')) AS pct_conserved
FROM read_parquet('<cwhr13 hex-fractions>') f
JOIN land l ON f.h10 = l.h10 AND f.h0 = l.h0
LEFT JOIN read_parquet('<conserved-areas hex-weights>') c ON f.h10 = c.h10 AND f.h0 = c.h0
WHERE f.whr13num <> 0
GROUP BY f.whr13num
ORDER BY pct_conserved;
```

Asking about **one** class ("what percent of hardwood woodland is conserved") is the same query with `WHERE f.whr13num = <code>` — keep the `frac × weight × area` product. Joining to the *distinct conserved cells* instead (a `SEMI JOIN` on `(h10, h0)`) counts every partly-conserved cell as fully conserved and overstates the percentage.

**Feature coarser than the overlay layer — read the weights asset at the feature's own resolution.** *(Skip unless the feature's native resolution is coarser than the layer you are overlaying — e.g. a res-8 or res-9 feature against a res-10 coverage layer.)* Weights assets are published per resolution (`…-hex-weights-res9`, `…-hex-weights-res8`) and the coarser ones are already averaged over the fine cells inside each parent, so no rollup is needed. Weight each coarse cell by its **land** area — `nland * h3_cell_area(h8, 'km^2')`, where `nland` counts the fine land cells inside it — since coastal and border cells are only partly land.

```sql
WITH feat AS (
  SELECT DISTINCT h8, h0
  FROM read_parquet('<res-8 feature hex>')
  WHERE <feature filter>
)
SELECT 100 * SUM((p.w1 + p.w2) * p.nland * h3_cell_area(f.h8, 'km^2'))
           / SUM(p.nland * h3_cell_area(f.h8, 'km^2')) AS pct_conserved
FROM feat f
JOIN read_parquet('<conserved-areas hex-weights-res8>') p USING (h8, h0);
```

The res-9 and res-8 weights assets carry a row for every land cell in the region, so this join also bounds the result to land — no separate land grid is needed.

When no weights asset is published at the coarse resolution, roll the fine layer up in two reductions, in this order: `MAX` (or `LEAST(SUM(w), 1)`) across the units overlapping **one fine cell**, then the mean across the **child cells of a coarse parent** — `SUM(w) / <children per parent>`, `7` for one resolution step (res-10 → res-9) and `49` for two (res-10 → res-8). `MAX` is the wrong reducer across children: it scores a whole parent as covered whenever a single child is.

Group on `h3_cell_to_parent(h10, 8)` when the fine layer carries no `h8` column. When the coarse feature is itself a `hex-fractions` layer, keep its `frac` in the product as in the same-resolution case: `SUM(f.frac * p.w9 * p.nland * h3_cell_area(f.h9, 'km^2')) / SUM(f.frac * p.nland * h3_cell_area(f.h9, 'km^2'))`. Matching the coarse feature against the fine layer's *distinct cells* treats a parent as fully covered when any one child is; the inflation grows with the number of children per parent.

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

### Problem 4 — Lines: per-segment columns and AOI boundaries

*(Applies only to line-derived hex: source geometry is `LineString`/`MultiLineString`, columns like `length_miles`, `length_km`. Skip if your dataset has area/acres columns or is raster-derived.)*

Per-segment values are **replicated on every row of any JOIN that matches a segment to multiple things**. Two unrelated mechanisms produce that replication:

- **Hex tiling.** Each segment → 2–6 hex rows at h8.
- **AOI matching.** Joining segments to AOI polygons (states, counties, fire perimeters) by *any* predicate — hex SEMI/INNER JOIN, `ST_Intersects` on GeoParquet — emits one row per (segment, AOI) the segment touches. A trail crossing 3 states appears in 3 rows.

**Therefore `SUM(length_miles)` after such a JOIN over-counts.** Recipe by question type:

- **Total per agency / class / surface** (no AOI): dedup by feature first. `SELECT admin_agency, SUM(length_miles) FROM (SELECT DISTINCT _cng_fid, admin_agency, length_miles FROM <line_hex>) GROUP BY admin_agency`.
- **Presence / count** ("which trails cross this AOI"): hex SEMI JOIN + `COUNT(DISTINCT _cng_fid)`. No spatial functions.
- **Mileage *inside* an AOI** (per-state, per-county, per-perimeter): `length_miles` is the **wrong column** — it's the segment's *full* length, not the AOI-clipped length. Default pattern: hex SEMI JOIN to a candidate `(trail _cng_fid, aoi _cng_fid)` list, then `ST_Intersection` on the GeoParquets joined by `_cng_fid` (the per-feature key, deterministic — avoid joining on names which can repeat across admin levels).

```sql
WITH cand AS (
  SELECT DISTINCT t._cng_fid AS trail_fid, r._cng_fid AS aoi_fid
  FROM read_parquet('<line_hex>') t
  JOIN read_parquet('<aoi_hex>') r ON t.h8 = r.h8 AND t.h0 = r.h0
)
SELECT rg.name_en AS aoi,
       SUM(ST_Length(ST_Intersection(tg.geometry, rg.geometry)) / 1609.344) AS miles
FROM cand c
JOIN read_parquet('<line_geoparquet>') tg ON tg._cng_fid = c.trail_fid
JOIN read_parquet('<aoi_geoparquet>') rg ON rg._cng_fid = c.aoi_fid
GROUP BY rg.name_en ORDER BY miles DESC;
```

The geometry column is `geometry` on the GeoParquets — not `geom`.

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
| > 1, integer-ish | Overlapping polygons — use DISTINCT (or Problem 4 if `length_*` columns / no area columns) |
| >> 1, non-integer | Raster pixels — use GROUP BY + SUM/AVG/MODE |

## Generating Output Files

```sql
COPY (SELECT ...) TO 's3://public-output/unique-file-name.csv' (FORMAT CSV, HEADER, OVERWRITE_OR_IGNORE);
```

Then tell the user the *public https* address (note the use of the public, not private endpoint): it should have the format like: `https://s3-west.nrp-nautilus.io/public-output/unique-file-name.csv` (adjust `unique-file-name.csv` part appropriately.)

**Note:** s3://public-output has a 30-day expiration and 1 Gb object size limit. CORS headers will permit files to be placed here and rendered by other tools.

## Rendering hex results as a map layer

Use `register_hex_tiles` only to display a value your SQL computes that no
layer or COG already serves. Otherwise render the existing source:

| To display… | Use |
|---|---|
| A raster field (effort, elevation, SST) | the **COG via titiler** |
| A column already in a layer's PMTiles (e.g. county population) | **data-driven paint (set_style)** on that layer |
| A value your SQL computes that no layer/COG serves | **`register_hex_tiles`** |

Use `register_hex_tiles` only when all hold:
- The value is produced by your SQL, not already a servable field (a layer's PMTiles column, or a COG raster)
- It is shown as per-hex values across a region, not a top-N table
- The result set is large (would exceed the 50-row `query` cap)

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

**Species richness (distinct count per hex):** for a richness map — distinct
species per cell, e.g. from GBIF — use `agg="COUNT_DISTINCT"`, NOT `COUNT`.
Plain `COUNT` measures sampling effort (occurrence density), not richness.
The SQL returns the H3 index then the key to count distinctly:

    SELECT h5, specieskey
    FROM read_parquet('s3://public-gbif/.../hex/h0=*/data_0.parquet', hive_partitioning = true)
    WHERE <coordinate-quality filters>   -- see the gbif STAC data-quality note
    GROUP BY h5, specieskey

Call `register_hex_tiles(sql=..., agg="COUNT_DISTINCT")`. Distinct-count is not
pyramid-composable (siblings share species, so a parent's richness can't be
summed from its children), so it is **exact at the finest resolution** and rolls
up coarser levels with `MAX` — a lower bound that under-counts at low zoom but
never over-counts. The result carries a `rollup_note` spelling this out.
