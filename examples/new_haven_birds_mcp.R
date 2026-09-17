#!/usr/bin/env Rscript
# The same analysis as new_haven_birds_dplyr.R, but executed on the MCP server.
#
# You still write dplyr. dbplyr compiles it to DuckDB SQL, and instead of
# running that SQL against a local DuckDB we hand it to the MCP server's
# `query` tool, which runs it next to the data on NRP. Only the 32-row result
# crosses the network.

library(duckdbfs)
library(dplyr)
library(dbplyr)
library(httr2)
library(jsonlite)

mcp_url <- "https://duckdb-mcp.nrp-nautilus.io/mcp"

# --- MCP plumbing -----------------------------------------------------------
# The server is stateless, so a single JSON-RPC POST is enough. Replies come
# back as server-sent events.
mcp_call <- function(name, arguments) {
  resp <- request(mcp_url) |>
    req_headers(Accept = "application/json, text/event-stream",
                `Content-Type` = "application/json") |>
    req_body_json(list(jsonrpc = "2.0", id = 1L, method = "tools/call",
                       params = list(name = name, arguments = arguments))) |>
    req_timeout(600) |>
    req_perform()

  body <- resp_body_string(resp)
  if (grepl("event-stream", resp_content_type(resp), fixed = TRUE)) {
    data <- sub("^data: ", "", grep("^data: ", strsplit(body, "\n")[[1]], value = TRUE))
    out <- fromJSON(data[length(data)], simplifyVector = FALSE)
  } else {
    out <- fromJSON(body, simplifyVector = FALSE)
  }
  if (!is.null(out$error)) stop(out$error$message)
  out$result$content[[1]]$text
}

# `query` answers with a markdown table. Everything stays character on the way
# back: tract ids are FIPS codes, and as.numeric() would eat their leading zero.
parse_md_table <- function(txt) {
  rows <- grep("^\\s*\\|", strsplit(txt, "\n")[[1]], value = TRUE)
  cells <- lapply(rows, \(r) trimws(strsplit(sub("^\\s*\\|", "", sub("\\|\\s*$", "", r)), "|", fixed = TRUE)[[1]]))
  header <- cells[[1]]
  body <- cells[-(1:2)]   # drop header and the |---| separator
  out <- as.data.frame(do.call(rbind, body), stringsAsFactors = FALSE)
  names(out) <- header
  tibble::as_tibble(out)
}

# --- datasets ---------------------------------------------------------------
# The server reads the NRP copy, so the paths here are the NRP ones the STAC
# catalog publishes.
#
# Build the handles from read_parquet() rather than open_dataset(), which would
# register a DuckDB view: dbplyr then renders "FROM <view name>", which means
# nothing to the server. Keeping the path inline makes the SQL portable.
#
# dbplyr only needs column NAMES to compile the query, and gets them by reading
# Parquet footers -- a fraction of a second, not a scan.
duckdb_s3_config(s3_endpoint = "s3-west.nrp-nautilus.io",
                 s3_url_style = "path", s3_use_ssl = TRUE, anonymous = TRUE)
con <- cached_connection()

hex <- function(path) {
  tbl(con, sql(paste0("SELECT * FROM read_parquet('s3://public", path, "')")))
}

places      <- hex("-census/census-2024/place/hex/h0=*/data_0.parquet")
blockgroups <- hex("-census/acs-2020-2024/blockgroup/hex/h0=*/data_0.parquet")
gbif        <- hex("-gbif/2026-06/hex/h0=*/data_0.parquet")

# --- the pipeline (identical to new_haven_birds_dplyr.R) --------------------
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


# --- compile locally, execute remotely --------------------------------------
sql <- as.character(sql_render(result))
cat(sql, "\n\n")

tracts <- mcp_call("query", list(sql_query = sql)) |>
  parse_md_table() |>
  mutate(across(c(population, median_income, bird_observations), as.numeric))

print(tracts)
