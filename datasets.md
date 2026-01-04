# Available Datasets

All datasets are H3-indexed for efficient spatial joins. Always use h8 and h0 columns for joining.

## Core Wetlands

**1. Global Lakes & Wetlands (GLWD)** - `s3://public-wetlands/glwd/hex/**`
- Columns: `Z` (type 0-33), `h8`, `h0`
- **Critical**: One hex can have multiple Z values. Always use `APPROX_COUNT_DISTINCT(h8)` for area
- **Categories CSV**: `s3://public-wetlands/glwd/category_codes.csv` (Z, name, description, category)
  - Open Water (1-7), Lacustrine (8-9), Riverine (10-15), Palustrine (16-19)
  - Ephemeral (20-21), Peatlands (22-27), Coastal (28-33)

## Environmental Data

**2. Vulnerable Carbon** - `s3://public-carbon/hex/vulnerable-carbon/**`
- Columns: `carbon`, `h8`, `h0`
- Conservation International 2018 - carbon vulnerable to development

**3. Nature's Contributions (NCP)** - `s3://public-ncp/hex/ncp_biod_nathab/**`
- Columns: `ncp` (0-1 score), `h8`, `h0`

## Geographic Boundaries

**4. Countries** - `s3://public-overturemaps/hex/countries.parquet`
- Columns: `id`, `country` (ISO alpha-2: 'US', 'CA'), `name`, `h8`, `h0`

**5. Regions** - `s3://public-overturemaps/hex/regions/**`
- Columns: `id`, `country`, `region` (ISO: 'US-CA'), `name`, `h8`, `h0`
- Use only when user explicitly requests regional breakdown

## Protected Areas

**6. Protected Areas (WDPA)** - `s3://public-wdpa/hex/**`
- Columns: `NAME_ENG`, `DESIG_ENG`, `IUCN_CAT`, `STATUS`, `GIS_AREA` (km²), `ISO3`, `h8`, `h0`
- **Note**: Overlapping areas → use `APPROX_COUNT_DISTINCT(h8)`
- **IUCN**: Ia/Ib (Reserve), II (Park), III (Monument), IV (Habitat), V (Landscape), VI (Sustainable)

**7. Ramsar Sites** - `s3://public-wetlands/ramsar/hex/**`
- Columns: `Site name`, `Country`, `Area (ha)`, `ramsarid`, `Criterion1-9`, `h8`, `h0`

## Watersheds

**8. HydroBASINS** - Level 3: `s3://public-hydrobasins/level_03/hexes/**` / Level 6: `level_06`
- Columns: `id`, `PFAF_ID`, `UP_AREA` (upstream km²), `SUB_AREA`, `h8`, `h0`

## Biodiversity

**9. iNaturalist Species** - `s3://public-inat/range-maps/hex/**`
- Columns: `taxon_id`, `name`, `rank`, `h0-h4` (NO h8 - use h3_cell_to_parent!)
- **Taxonomy**: `s3://public-inat/taxonomy/taxa_and_common.parquet`
  - Join: taxonomy.`id` = range.`taxon_id`
  - Columns: `class`, `order`, `family`, `scientificName`, `vernacularName`
  - Filter by class: 'Aves' (birds), 'Mammalia' (mammals), etc.

## Socioeconomic

**10. Corruption Index 2024** - `s3://public-wetlands/other/cpi_2024_data.csv`
- Columns: `Country`, `ISO2`, `Score` (0-100), `Rank`
- Join to countries using ISO2 for spatial analysis
