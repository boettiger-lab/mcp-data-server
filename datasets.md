# Available Datasets

**IMPORTANT** You must read remote parquet datasets with read_parquet()

## Choosing the right format

Most datasets are available in multiple formats. **Pick the right one for the task:**

| Format | Path pattern | When to use |
|--------|-------------|-------------|
| **H3 hex parquet** | `…/hex/**` | Spatial joins, overlap analysis, area calculations, cross-dataset queries. **Always prefer this for any spatial operation.** |
| **Flat parquet** | `….parquet` | Single-dataset filtering, column value lookups, aggregations, checking unique values. No geometry overhead. |
| **GeoParquet** (flat parquet that happens to contain a `geometry`/`geom` column) | same as flat | **Almost never use the geometry column.** Scanning polygon geometries is slow and unnecessary — H3 hex joins are faster, simpler, and already set up for all datasets. Only read the geometry column if the user explicitly asks for WKT output or polygon shapes. |

**Rule of thumb:** If your query involves two or more datasets, or any concept of "overlap", "within", "intersection", or "area of X inside Y" → use the H3 hex paths and join on `h8` (+ `h0` for partition pruning). Do NOT use `ST_Intersects`, `ST_Area`, or any geometry functions when an H3 join will answer the question.

---

**1. GLWD (Global Lakes and Wetlands)**
- Partitioned parquet files at `s3://public-wetlands/glwd/hex/**`
- Columns: `Z` (type 0-33), `h8`, `h0`
- **Raster-derived**: multiple pixel rows per hex, each with a wetland type code. For dominant class use `MODE(Z)`; for presence/absence use `COUNT(DISTINCT Z) > 0`. Always use `APPROX_COUNT_DISTINCT(h8)` for area.
- **Categories CSV**: `s3://public-wetlands/glwd/category_codes.csv` (Z, name, description, category)
  - Open Water (1-7), Lacustrine (8-9), Riverine (10-15), Palustrine (16-19)
  - Ephemeral (20-21), Peatlands (22-27), Coastal (28-33)
  - Open Water (1-7), Lacustrine (8-9), Riverine (10-15), Palustrine (16-19)
  - Ephemeral (20-21), Peatlands (22-27), Coastal (28-33)

**2. Vulnerable Carbon**
- Partitioned parquet files at `s3://public-carbon/hex/vulnerable-carbon/**`
- Columns: `carbon`, `h8`, `h0`
- Conservation International 2018 - carbon vulnerable to development
- **Raster-derived**: multiple pixel rows per hex — always `GROUP BY h8, h0` and use `SUM(carbon)` or `AVG(carbon)`

**3. NCP (Nature Contributions to People)**
- Partitioned parquet files at `s3://public-ncp/hex/ncp_biod_nathab/**`
- Columns: `ncp` (0-1 score), `h8`, `h0`
- **Raster-derived**: multiple pixel rows per hex — always `GROUP BY h8, h0` and use `AVG(ncp)`

**4. Countries**
- Parquet files at `s3://public-overturemaps/hex/countries.parquet`
- Columns: `id`, `country` (ISO alpha-2: 'US', 'CA'), `name`, `h8`, `h0`

**5. Regions**
- Partitioned parquet files at `s3://public-overturemaps/hex/regions/**`
- Columns: `id`, `country`, `region` (ISO: 'US-CA'), `name`, `h8`, `h0`
- Use only when user explicitly requests regional breakdown

**6. WDPA (Protected Areas)**
- Partitioned parquet files at: `s3://public-wdpa/hex/**`
- Columns: `NAME_ENG`, `DESIG_ENG`, `IUCN_CAT`, `STATUS`, `GIS_AREA` (km²), `ISO3`, `h8`, `h0`
- **CRITICAL**: Multiple protected areas can cover the same hex. MUST use `SELECT DISTINCT h8, h0` before joining to other datasets to avoid double-counting. Direct joins will overcount areas and attributes.
- **IUCN**: Ia/Ib (Reserve), II (Park), III (Monument), IV (Habitat), V (Landscape), VI (Sustainable)

**7. Ramsar Sites**
- Partitioned parquet files at `s3://public-wetlands/ramsar/hex/**`
- Columns: `Site name`, `Country`, `Area (ha)`, `ramsarid`, `Criterion1-9`, `h8`, `h0`

**8. HydroBASINS**
- Levels 3-6 are available.  Level 3 parquet partition is: `s3://public-hydrobasins/level_03/hexes/**` and so forth, eg. level 6 just change the part of the path to `level_06`
- Columns: `id`, `PFAF_ID`, `UP_AREA` (upstream km²), `SUB_AREA`, `h8`, `h0`

**9. iNaturalist Species**
- Partitioned parquet files at `s3://public-inat/range-maps/hex/**`
- Columns: `taxon_id`, `name`, `rank`, `h0-h4` (NO h8 - use h3_cell_to_parent!)
- **Taxonomy**: `s3://public-inat/taxonomy/taxa_and_common.parquet`
  - Join: taxonomy.`id` = range.`taxon_id`
  - Columns: `class`, `order`, `family`, `scientificName`, `vernacularName`
  - Filter by class: 'Aves' (birds), 'Mammalia' (mammals), etc.

**10. Corruption Index 2024**
- CSV data at `s3://public-wetlands/other/cpi_2024_data.csv`
- Columns: `Country`, `ISO2`, `Score` (0-100), `Rank`
- Join to countries using ISO2 for spatial analysis

**11. PAD-US 4.1 — Protected Areas Database of the United States**
- H3-indexed parquet: `s3://public-padus/padus-4-1/combined/hex/**` (partitioned by `h0`, keyed on `h8`)
- Flat parquet: `s3://public-padus/padus-4-1/combined.parquet`
- 656,986 protected area records across the US
- Columns: `FeatClass` (Fee/Easement/Designation/Marine/Proclamation), `Mang_Type` (FED/STAT/LOC/PVT/JNT/TRIB/NGO), `Mang_Name` (managing agency), `Own_Type`, `Own_Name`, `Unit_Nm` (unit name), `State_Nm` (state abbreviation), `GIS_Acres`, `GAP_Sts` (1–4, 1=highest protection), `IUCN_Cat`, `Pub_Access` (OA/RA/XA/UK), `Category`
- **CRITICAL**: Like WDPA, multiple protected areas can cover the same hex. Use `SELECT DISTINCT h8, h0` before joining to avoid double-counting.

**12. WGFD Wildlife Seasonal & Crucial Ranges (Elk, Mule Deer, Pronghorn)**

All six datasets share the same schema and H3 resolution. Use the `RANGE` column to filter by range type.

| Species | Seasonal hex path | Crucial hex path |
|---------|-------------------|------------------|
| Elk | `s3://public-wyoming/wgfd-elk-seasonal/hex/**` | `s3://public-wyoming/wgfd-elk-crucial/hex/**` |
| Mule Deer | `s3://public-wyoming/wgfd-mule-deer-seasonal/hex/**` | `s3://public-wyoming/wgfd-mule-deer-crucial/hex/**` |
| Pronghorn | `s3://public-wyoming/wgfd-pronghorn-seasonal/hex/**` | `s3://public-wyoming/wgfd-pronghorn-crucial/hex/**` |

All partitioned by `h0`, with H3 columns `h8`, `h9`, `h10`.
Columns: `RANGE` (VARCHAR — range type code, e.g. CRUWIN, CRUSWR, CRUWYL, WIN, SWR, WYL, SSF), `Acres` (DOUBLE), `SQMiles` (DOUBLE)

**RANGE codes:** `WIN` = Winter, `SWR` = Severe Winter Relief, `WYL` = Winter/Yearlong, `SSF` = Spring/Summer/Fall. Prefix `CRU` = Crucial (population-determining). `CRUSWR` = Crucial Severe Winter Relief (NOT summer range).

**13. Greater Sage-Grouse Priority Habitat**
- H3-indexed parquet: `s3://public-wyoming/sage-grouse-priority/hex/**` (partitioned by `h0`, keyed on `h8`/`h9`/`h10`)
- Columns: `NAME` (VARCHAR — area name), `Acres` (DOUBLE)

**14. Wyoming Counties**
- H3-indexed parquet: `s3://public-wyoming/wy-counties/hex/**` (partitioned by `h0`, H3 columns `h9`, `h10`, `h11`)
- Columns: `COUNTYNAME` (VARCHAR)

**15. Raster-derived Wyoming H3 hex datasets**

| Dataset | Hex path | Value column | H3 resolution |
|---------|----------|-------------|---------------|
| NLCD 2024 Land Cover | `s3://public-wyoming/nlcd-2024/hex/**` | `nlcd` (FLOAT — NLCD class code) | h8 |
| Sagebrush Conservation Design | `s3://public-wyoming/sagebrush-design/hex/**` | `sagebrush` (FLOAT — 1=Core, 2=Growth, 3=Other) | h8 |
| Perennial Forb & Grass Cover (RAP) | `s3://public-wyoming/rap-pfg-biomass/hex/**` | `pfg` (FLOAT — % cover) | h10 |

**Note:** NLCD and sagebrush use `h8`; RAP PFG uses `h10`. When joining with h8 datasets, convert with `h3_cell_to_parent(h10, 8)`.
**Raster-derived**: multiple pixel rows per hex — always `GROUP BY` and aggregate (see H3 guide).
