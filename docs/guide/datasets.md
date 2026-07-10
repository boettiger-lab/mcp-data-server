# Available Datasets

Datasets are served from the public STAC catalog at:

```
https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json
```

The agent discovers dataset paths and schemas dynamically — use the `browse_stac_catalog` and `get_stac_details` tools rather than hardcoding any S3 paths.

## Current datasets

| Dataset | Description |
|---|---|
| **GLWD** | Global Lakes and Wetlands Database |
| **Vulnerable Carbon** | Conservation International carbon vulnerability data |
| **NCP** | Nature Contributions to People biodiversity scores |
| **Countries & Regions** | Global administrative boundaries (Overture Maps) |
| **WDPA** | World Database on Protected Areas |
| **Ramsar Sites** | Wetlands of International Importance |
| **HydroBASINS** | Global watershed boundaries (levels 3–6) |
| **iNaturalist** | Species occurrence range maps |
| **Corruption Index 2024** | Transparency International data |

## H3 spatial indexing

All datasets are indexed using [Uber's H3 hexagonal grid system](https://h3geo.org).

| Column | Resolution | Cell area |
|---|---|---|
| `h8` | Resolution 8 | ~0.74 km² |
| `h4` | Resolution 4 | ~1,771 km² |
| `h0` | Resolution 0 | ~4,357,449 km² |

### Area calculations

H3 cells are **not** equal-area — true cell area varies with latitude and
icosahedral distortion (res-8 cells range ~0.55–0.82 km²), so a nominal
per-resolution constant introduces a systematic error (~6% for California).
For a region, feature, or per-group area, sum the **exact** per-cell area over
**distinct** cells:

```sql
-- Exact area in km² (the proper method)
SELECT SUM(h3_cell_area(h8, 'km^2')) AS area_km2
FROM (SELECT DISTINCT h8, h0 FROM read_parquet('s3://...') WHERE ...);
```

Only for unscoped global aggregates over millions of cells — where
materializing every distinct cell would defeat the fast approximate path — fall
back to multiplying an approximate count by the nominal constant
(`APPROX_COUNT_DISTINCT(h8) * 0.737327598`, accurate to ~1–2% globally).

See [h3-guide.md](../../h3-guide.md) for the full area guidance.

### Cross-dataset joins

Always include `h0` in join conditions to enable partition pruning:

```sql
SELECT a.h8, a.value, b.other_value
FROM read_parquet('s3://dataset-a/**') a
JOIN read_parquet('s3://dataset-b/**') b
  ON a.h8 = b.h8 AND a.h0 = b.h0   -- h0 required for pruning
```

Omitting `h0` forces a full scan of both datasets and is 5–20× slower.
