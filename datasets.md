# Available Datasets

**IMPORTANT** You must read remote parquet datasets with read_parquet()

---

**1. GLWD (Global Lakes and Wetlands)**
- Partitioned parquet files at `s3://public-wetlands/glwd/hex/**`
- Columns: `Z` (type 0-33), `h8`, `h0`
- **Critical**: One hex can have multiple Z values. Always use `APPROX_COUNT_DISTINCT(h8)` for area
- **Categories CSV**: `s3://public-wetlands/glwd/category_codes.csv` (Z, name, description, category)
  - Open Water (1-7), Lacustrine (8-9), Riverine (10-15), Palustrine (16-19)
  - Ephemeral (20-21), Peatlands (22-27), Coastal (28-33)

**2. Vulnerable Carbon**
- Partitioned parquet files at `s3://public-carbon/hex/vulnerable-carbon/**`
- Columns: `carbon`, `h8`, `h0`
- Conservation International 2018 - carbon vulnerable to development

**3. NCP (Nature Contributions to People)**
- Partitioned parquet files at `s3://public-ncp/hex/ncp_biod_nathab/**`
- Columns: `ncp` (0-1 score), `h8`, `h0`

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
