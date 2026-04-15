# ellmer + mcptools example: let an LLM decide when to call the duckdb-geo
# MCP tools.
#
# mcptools is an MCP *client* for R that plugs MCP tools into ellmer chats.
# It only speaks stdio, so we bridge to the remote HTTP server through
# `mcp-remote` (an npx-based stdio <-> HTTP proxy).
#
# Requirements:
#   - Node.js / npx on PATH (npx fetches mcp-remote on first use)
#   - install.packages(c("ellmer", "mcptools"))
#
# Set:
#   export OPENAI_API_KEY=...            # or any OpenAI-compatible key
#   export OPENAI_BASE_URL=...           # optional, defaults to OpenAI
#
# Run:
#   Rscript agent_ellmer.R

library(mcptools)
library(ellmer)
library(jsonlite)

mcp_url <- "https://duckdb-mcp.nrp-nautilus.io/mcp"

# Build a Claude-Desktop-style config for mcptools.
# mcp-remote bridges stdio <-> streamable-HTTP so mcptools can connect
# to the remote MCP server.
config_file <- tempfile(fileext = ".json")
write_json(
  list(
    mcpServers = list(
      `duckdb-geo` = list(
        command = "npx",
        args = list("-y", "mcp-remote", mcp_url)
      )
    )
  ),
  config_file,
  auto_unbox = TRUE,
  pretty = TRUE
)

# Fetch the remote server's tools as ellmer-compatible tool definitions.
tools <- mcp_tools(config = config_file)
cat("Available tools:", vapply(tools, \(t) t@name, character(1)), "\n")

# Create a chat session with any OpenAI-compatible model.
chat <- chat_openai(
  model = Sys.getenv("MODEL", "gpt-4o"),
  echo = "output"
)
chat$set_tools(tools)

# Ask a question — the model will call browse_stac_catalog, get_stac_details,
# and query as needed.
chat$chat("What fraction of Australia is protected area?")
