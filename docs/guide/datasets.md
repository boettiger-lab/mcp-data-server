# Available Datasets

The catalog is **live and growing** — it is the source of truth, not this page.
At the time of writing it holds **70 top-level collections spanning ~325
datasets**: protected areas, biodiversity, carbon, hydrology, land cover,
census and demography, hazards, marine and ocean governance, and more. Any
hardcoded list here would be out of date within a week.

## Browse the catalog

Explore everything interactively in the STAC browser:

**➡️ [browser.moregeo.it — Boettiger Lab Geospatial Datasets](https://browser.moregeo.it/external/s3-west.nrp-nautilus.io/public-data/stac/catalog.json)**

The underlying root catalog document is:

```
https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json
```

Each collection carries its own STAC metadata — asset hrefs (Parquet, PMTiles,
COG), spatial extent, column schemas, licensing, and dataset-specific
aggregation guidance.

## A few examples

Representative top-level collections, to give a sense of the range. This is a
sample, **not** an inventory — browse the catalog for the full list.

| `collection_id` | Description |
|---|---|
| `protected-planet` | UNEP-WCMC WDPA + WD-OECM global protected & conserved areas |
| `pad-us-4.1` | PAD-US 4.1 — national inventory of US protected areas |
| `iucn-richness-2025` | IUCN Red List species richness & range-weighted richness |
| `gbif-derived` | GBIF occurrences hexed at H3 res 0–10, plus derived products |
| `ncp-biodiversity` | Nature's Contributions to People biodiversity indicators |
| `irrecoverable-carbon` | Irrecoverable, manageable, and vulnerable carbon stocks |
| `wetlands-global-unified` | Ramsar, GLWD, and NWI wetlands |
| `hydrobasins-v1c` | HydroSHEDS HydroBasins global watershed boundaries |
| `overture-divisions` | Overture Maps administrative boundaries (country/region/county) |
| `us-census` | TIGER/Line boundaries and American Community Survey tables |
| `land-cover` | National and global land-cover classifications |
| `dem` | Digital elevation models (Copernicus GLO-30 and others) |
| `high-seas` | Areas beyond national jurisdiction & ocean governance layers |
| `kba` | World Database of Key Biodiversity Areas |
| `ca30x30` | California 30x30 conservation-initiative datasets |

## Let the agent discover them

Never hardcode or guess an S3 path. The workflow is always:

1. **`browse_stac_catalog`** — list the collections currently in the catalog.
2. **`get_stac_details(dataset_id)`** — resolve Parquet paths, column schemas,
   and any dataset-specific aggregation rules.
3. **`query(sql)`** — run DuckDB SQL over the paths returned in step 2, copied
   verbatim.

Because discovery is dynamic, newly published datasets become usable with no
change to the server, the client, or this page.

## H3 spatial indexing

Most tabular datasets are indexed on [Uber's H3 hexagonal grid](https://h3geo.org),
which makes cross-dataset joins a cheap key equality instead of a spatial predicate.

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

See [`h3-guide.md`](https://github.com/boettiger-lab/mcp-data-server/blob/main/h3-guide.md) for the full area guidance.

### Cross-dataset joins

Always include `h0` in join conditions to enable partition pruning:

```sql
SELECT a.h8, a.value, b.other_value
FROM read_parquet('s3://dataset-a/**') a
JOIN read_parquet('s3://dataset-b/**') b
  ON a.h8 = b.h8 AND a.h0 = b.h0   -- h0 required for pruning
```

Omitting `h0` forces a full scan of both datasets and is 5–20× slower.

## Adding a dataset

Datasets are published by the companion
[**data-workflows**](https://boettiger-lab.github.io/data-workflows/) project,
which converts legacy and proprietary sources into cloud-native Parquet/PMTiles/COG
with rich STAC metadata. To propose a dataset, open an issue there.
