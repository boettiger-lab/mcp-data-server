# Programmatic Access from R & Python

The MCP server speaks [streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http), so you can call its tools from any language — no LLM client required. You can also wire the tools into an LLM agent so the model writes and runs queries on its own.

Full runnable scripts are in the [`examples/`](https://github.com/boettiger-lab/mcp-data-server/tree/main/examples) folder.

## Direct queries (no LLM)

Call the MCP `query` tool directly to run SQL against S3 Parquet files.

### Python

The official `mcp` SDK speaks streamable HTTP natively.

```bash
pip install mcp
```

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "https://duckdb-mcp.nrp-nautilus.io/mcp"

SQL = """
SELECT country, name_en, subtype
FROM read_parquet('s3://public-overturemaps/2026-02-18.0/countries.parquet')
WHERE subtype = 'country' AND is_land
ORDER BY name_en
LIMIT 10
"""

async def main():
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])

            result = await session.call_tool("query", {"sql_query": SQL})
            for block in result.content:
                print(block.text)

asyncio.run(main())
```

### R

There is no R MCP client library yet. The server runs in stateless mode, so you can hit the JSON-RPC endpoint directly with `httr2`. Responses arrive as server-sent events (SSE).

```r
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
    msgs[[length(msgs)]]
  } else {
    fromJSON(body, simplifyVector = FALSE)
  }
}

resp <- mcp_call("tools/call", list(
  name = "query",
  arguments = list(sql_query = sql)
))

for (block in resp$result$content) {
  cat(block$text, "\n")
}
```

## LLM tool use

Let the model discover datasets, write SQL, and interpret results autonomously. The MCP tools (`browse_stac_catalog`, `get_stac_details`, `query`) are registered as callable tools so the model decides when and how to use them.

Both examples below use `ChatOpenAI` / `chat_openai()` and work with any OpenAI-compatible endpoint. Set `OPENAI_API_KEY` and optionally `OPENAI_BASE_URL` in your environment.

### Python — LangChain + LangGraph

```bash
pip install langchain-mcp-adapters langchain-openai langgraph
```

```python
import asyncio
import os
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

MCP_URL = "https://duckdb-mcp.nrp-nautilus.io/mcp"

async def main():
    client = MultiServerMCPClient({
        "duckdb-geo": {
            "url": MCP_URL,
            "transport": "streamable_http",
        }
    })
    tools = await client.get_tools()

    model = ChatOpenAI(
        model=os.environ.get("MODEL", "gpt-4o"),
        max_tokens=4096,
    )
    agent = create_react_agent(model, tools)

    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "What fraction of Australia is protected area?"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

### R — ellmer + mcptools

[`mcptools`](https://github.com/tidyverse/mcptools) is an MCP client for R that plugs MCP tools into `ellmer` chats. It speaks stdio, so we bridge to the remote HTTP server with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) (requires Node.js on PATH).

```r
library(mcptools)
library(ellmer)
library(jsonlite)

mcp_url <- "https://duckdb-mcp.nrp-nautilus.io/mcp"

# Build a config pointing mcptools at the remote server via mcp-remote.
config_file <- tempfile(fileext = ".json")
write_json(
  list(mcpServers = list(
    `duckdb-geo` = list(
      command = "npx",
      args = list("-y", "mcp-remote", mcp_url)
    )
  )),
  config_file,
  auto_unbox = TRUE, pretty = TRUE
)

# Fetch MCP tools as ellmer-compatible tool definitions.
tools <- mcp_tools(config = config_file)

# Create a chat session and register the tools.
chat <- chat_openai(
  model = Sys.getenv("MODEL", "gpt-4o"),
  echo = "output"
)
chat$set_tools(tools)

chat$chat("What fraction of Australia is protected area?")
```

::: tip
You can use the same pattern to talk to a local dev server at `http://localhost:8000/mcp` — just change the URL.
:::
