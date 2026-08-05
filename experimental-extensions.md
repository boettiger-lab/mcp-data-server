
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

For area, do not multiply a pixel count by a constant in a geographic CRS: pixel
ground area varies with latitude. Assign cells with `h3_latlng_to_cell(y, x, res)`
and sum `h3_cell_area()`, or transform to a projected CRS with
`always_xy := true` (see §5).

#### `zarr` — read Zarr stores

`read_zarr(path)`, plus `read_zarr_groups(path)` and `read_zarr_metadata(path)` for
discovery. Call the metadata/groups functions first to learn array names, shapes and
chunking; the same size discipline as `RT_ReadCells` applies — a Zarr array is
typically a full datacube, so slice on its coordinate columns before aggregating.

**Report which extension produced a number** when you use either one, so the user
knows the result came from the experimental path.
