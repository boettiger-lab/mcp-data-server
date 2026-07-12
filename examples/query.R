# Minimal R example: call the duckdb-geo MCP `query` tool directly.
#
# No LLM involved -- this just speaks MCP over streamable HTTP and runs SQL.
# No R MCP client speaks HTTP directly (mcptools is stdio-only), so we hit the
# JSON-RPC endpoint via httr2. If you only want to *read* the data with dplyr and
# don't need the MCP tools, see query_dbplyr.R instead.
# The server runs in stateless mode, so no session handshake is required.
#
# Install:
#   install.packages(c("httr2", "jsonlite"))
#
# Run:
#   Rscript query.R

library(httr2)
library(jsonlite)

mcp_url <- "https://duckdb-mcp.nrp-nautilus.io/mcp"

sql <- "
SELECT country, name_en, subtype
FROM read_parquet('s3://public-overturemaps/2026-02-18.0/countries.parquet')
WHERE subtype = 'country' AND is_land
ORDER BY name_en
LIMIT 10
"

# MCP streamable-HTTP responses come back as text/event-stream (SSE) by
# default. Each event is a `data: {...}\n\n` block whose payload is JSON-RPC.
parse_sse <- function(body) {
  lines <- strsplit(body, "\n", fixed = TRUE)[[1]]
  data_lines <- sub("^data: ", "", lines[grepl("^data: ", lines)])
  lapply(data_lines, fromJSON, simplifyVector = FALSE)
}

mcp_call <- function(method, params, id = 1L) {
  resp <- request(mcp_url) |>
    req_headers(
      Accept = "application/json, text/event-stream",
      `Content-Type` = "application/json"
    ) |>
    req_body_json(list(
      jsonrpc = "2.0",
      id = id,
      method = method,
      params = params
    )) |>
    req_perform()

  body <- resp_body_string(resp)
  ctype <- resp_content_type(resp)
  if (grepl("event-stream", ctype, fixed = TRUE)) {
    msgs <- parse_sse(body)
    msgs[[length(msgs)]]            # final message holds the result
  } else {
    fromJSON(body, simplifyVector = FALSE)
  }
}

# Call the `query` tool.
resp <- mcp_call("tools/call", list(
  name = "query",
  arguments = list(sql_query = sql)
))

# Each content block has a `text` field; print them all.
for (block in resp$result$content) {
  cat(block$text, "\n")
}
