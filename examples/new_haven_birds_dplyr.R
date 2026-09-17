#!/usr/bin/env Rscript
# Bird observations vs. median household income, by census tract, New Haven CT.
#
# Runs entirely on YOUR machine: a local DuckDB streams Parquet from the
# source.coop mirror. No MCP server, no API key, no data downloaded to disk.
# See new_haven_birds_mcp.R for the same pipeline executed on the server.

library(duckdbfs)
library(dplyr)
library(ggplot2)

# --- connection -------------------------------------------------------------
# Anonymous reads from the source.coop mirror on AWS us-west-2. The bucket name
# contains dots, so path-style addressing is required for the TLS cert to match.
duckdb_s3_config(
  s3_endpoint  = "s3.us-west-2.amazonaws.com",
  s3_region    = "us-west-2",
  s3_url_style = "path",
  s3_use_ssl   = TRUE,
  anonymous    = TRUE
)

# Keep a laptop-sized footprint: DuckDB streams and spills to disk rather than
# trying to hold the join in RAM.
#
# Spill to node-local scratch, not the working directory. On JupyterHub, HPC or
# any cluster, $HOME is networked storage and spilling there is far slower than
# the local ephemeral disk that tempdir() resolves to.
duckdb_config(
  memory_limit   = "3GB",
  temp_directory = file.path(tempdir(), "duckdb")
)

# --- datasets ---------------------------------------------------------------
# open_dataset() globs the h0=* partitions under each prefix and hands back a
# lazy dplyr table. The h3 extension is loaded for us by duckdbfs.
base <- "s3://us-west-2.opendata.source.coop/cboettig"

places      <- open_dataset(paste0(base, "/census/census-2024/place/hex"))
blockgroups <- open_dataset(paste0(base, "/census/acs-2020-2024/blockgroup/hex"))
gbif        <- open_dataset(paste0(base, "/gbif/2026-06/hex"))

# --- the pipeline -----------------------------------------------------------
# Every dataset is indexed on the same H3 grid, so "which tract is this bird
# observation in?" is a key equality, not a point-in-polygon test. h10 cells are
# ~0.015 km2, fine enough that one rarely straddles two city block groups.
#
# Join on both h0 and h10. h0 is the partition column, and including it lets
# DuckDB skip all but one file per dataset.

# 1. The res-10 cells that make up the city of New Haven.
nh_cells <- places |>
  filter(STATEFP == "09", NAME == "New Haven") |>
  distinct(h0, h10)

# 2. Connecticut block groups falling inside those cells, tagged with their
#    11-digit tract id (state + county + tract).
nh_bg <- blockgroups |>
  filter(STATEFP == "09") |>
  semi_join(nh_cells, by = c("h0", "h10")) |>
  mutate(tract = paste0(STATEFP, COUNTYFP, TRACTCE))

# 3. A cell -> tract lookup. min() breaks ties so a cell straddling a boundary
#    is counted once rather than in both tracts.
cell_tract <- nh_bg |>
  group_by(h0, h10) |>
  summarise(tract = min(tract), .groups = "drop")

# 4. Income and population per tract. Both are block-group attributes repeated
#    on every hex row, so deduplicate by block group first.
tract_stats <- nh_bg |>
  distinct(tract, GEOID, TotPop, MHHI) |>
  group_by(tract) |>
  summarise(
    population = sum(TotPop, na.rm = TRUE),
    # A median is not a sum: to roll block groups up to a tract, take the
    # population-weighted mean of their medians. Block groups whose income
    # estimate is suppressed drop out of both halves of the ratio.
    median_income = sum(MHHI * TotPop, na.rm = TRUE) /
      sum(ifelse(is.na(MHHI), 0, TotPop), na.rm = TRUE),
    .groups = "drop"
  )

# 5. Bird observations per tract.
birds <- gbif |>
  filter(class == "Aves") |>
  inner_join(cell_tract, by = c("h0", "h10")) |>
  count(tract, name = "bird_observations")

result <- tract_stats |>
  inner_join(birds, by = "tract") |>
  arrange(desc(bird_observations))

# Nothing has touched the network yet -- `result` is a lazy query.
result |> show_query()

# --- run it -----------------------------------------------------------------
tracts <- collect(result)   # ~50 s; only these 32 rows come back to R
print(tracts)

ggplot(tracts, aes(median_income, bird_observations)) +
  geom_point(size = 2.6, alpha = 0.85, colour = "#1b7837") +
  scale_y_log10(labels = scales::comma) +
  scale_x_continuous(labels = scales::dollar) +
  labs(
    title = "Bird observations vs. median income, New Haven CT tracts",
    subtitle = "GBIF Aves joined to ACS block groups on H3 res-10 cells",
    x = "Median household income (ACS 2020-2024)",
    y = "GBIF bird observations (log scale)"
  ) +
  theme_minimal(base_size = 12)
