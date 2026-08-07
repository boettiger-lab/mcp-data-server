
### 🧪 EXPERIMENTAL EXTENSIONS (this deployment only)

This endpoint loads two community extensions beyond the stock set. They are **not**
available on the default endpoint — do not assume them elsewhere.

#### `raster` — read GeoTIFF/COG directly

`RT_ReadCells(path)` returns **one row per pixel**: `(id, x, y, geometry, col, row, band_1, …)`.
`RT_Read(path)` returns one row per tile with an `RT_DATACUBE` band, for band algebra
(`RT_CubeStats`, `RT_CubeClip`, `RT_CubeBurn`). Paths may be S3/HTTP via GDAL's
`/vsicurl/` or `/vsis3/` prefixes.

⛔ **Size guard — `RT_ReadCells` materializes every pixel.** A 92×81 fire-severity
raster is 7,452 rows and returns in seconds; a continental COG is billions and will
exhaust the pod. Before reading an unfamiliar raster, check its dimensions with
`SELECT cols, rows FROM RT_Read(path)` and stop if `cols * rows` exceeds ~10 million.
For anything larger, use the dataset's `…/hex/h0=*/…` asset — every raster collection
in the catalog is already aggregated to H3 by the ingest pipeline, and that path is
partition-pruned and far cheaper. `RT_ReadCells` is for small rasters not yet ingested.

⚠️ **CRS type mismatch.** `RT_ReadCells` emits `GEOMETRY('EPSG:4326')`; catalog
GeoParquet is `GEOMETRY('OGC:CRS84')`. Both hold lon/lat, but DuckDB rejects the join
on the type difference. Re-tag the raster side — no reprojection is involved:

```sql
WITH px AS (
  SELECT band_1 AS value, ST_SetCRS(geometry, 'OGC:CRS84') AS g
  FROM RT_ReadCells('/vsicurl/https://example.org/severity.tif')
  WHERE band_1 > 0                       -- drop nodata BEFORE the join
)
SELECT f.name, count(*) AS n_px, round(avg(px.value), 3) AS mean_value
FROM px LEFT JOIN read_parquet('<geoparquet>') f ON f.geom.ST_Contains(px.g)
GROUP BY 1;
```

Filter the raster's nodata sentinel in the CTE (see §7) — it is usually a large
negative value like `-9999` that will wreck any average.

⚠️ **Area: take the pixel size from the raster's own transform.** Do not sum
`h3_cell_area()` over pixel rows — that charges each pixel a whole H3 cell's area,
which is a different quantity and overcounts badly (it inflated this fire by ~29%).
H3 cell area is the right tool for *hex* rows, not raster rows. Do not use a
hardcoded constant either: in a geographic CRS, pixel ground area varies with
latitude.

`RT_Read(path).metadata` is JSON; `$.transform` is a **6-element GDAL geotransform
in the order `[originX, dx, rotX, originY, rotY, dy]`** — the pixel size lives at
indices **1 and 5**, not 0 and 4:

```sql
WITH hdr AS (
  SELECT abs(CAST(metadata->'$.transform'->>1 AS DOUBLE)) AS dlon,
         abs(CAST(metadata->'$.transform'->>5 AS DOUBLE)) AS dlat
  FROM RT_Read('<path>')
), px AS (
  SELECT band_1 AS value,
         ST_SetCRS(geometry, 'OGC:CRS84') AS g,
         h.dlon * 111320.0 * cos(radians(ST_Y(geometry)))   -- metres per degree lon
           * h.dlat * 110574.0                              -- metres per degree lat
           AS px_m2
  FROM RT_ReadCells('<path>'), hdr h
  WHERE band_1 > 0
)
SELECT round(sum(px_m2) / 10000, 2) AS hectares FROM px;
```

Other useful `metadata` keys: `crs`, `bounds`, `width`, `height`, `bands`. Note
`RT_Envelope` takes an `RT_DATACUBE`, not the `bbox` column — use `$.bounds`.

#### `zarr` — read Zarr stores

`read_zarr(path)`, plus `read_zarr_groups(path)` and `read_zarr_metadata(path)` for
discovery. Call the metadata/groups functions first to learn array names, shapes and
chunking; the same size discipline as `RT_ReadCells` applies — a Zarr array is
typically a full datacube, so slice on its coordinate columns before aggregating.

**Report which extension produced a number** when you use either one, so the user
knows the result came from the experimental path.
