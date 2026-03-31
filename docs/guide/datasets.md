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

```sql
-- Area in km² using H3 hex counts
SELECT APPROX_COUNT_DISTINCT(h8) * 0.737327598 AS area_km2
FROM read_parquet('s3://...')
WHERE ...
```

### Cross-dataset joins

Always include `h0` in join conditions to enable partition pruning:

```sql
SELECT a.h8, a.value, b.other_value
FROM read_parquet('s3://dataset-a/**') a
JOIN read_parquet('s3://dataset-b/**') b
  ON a.h8 = b.h8 AND a.h0 = b.h0   -- h0 required for pruning
```

Omitting `h0` forces a full scan of both datasets and is 5–20× slower.
