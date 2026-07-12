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

No R MCP client speaks HTTP directly ([`mcptools`](https://posit-dev.github.io/mcptools/) is stdio-only — see the [ellmer + mcptools](#r-ellmer-mcptools) section below). The server runs in stateless mode, so you can hit the JSON-RPC endpoint directly with `httr2`. Responses arrive as server-sent events (SSE).

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

### R — dplyr / dbplyr

If you'd rather write queries in `dplyr` than SQL, you don't need the MCP server at all: point a **local** DuckDB at the public [source.coop](https://source.coop) mirror of the same data and let `dbplyr` compile your `dplyr` verbs to DuckDB SQL. [`duckdbfs`](https://cboettig.github.io/duckdbfs/) handles the connection mechanics — `open_dataset()` returns a lazy `dplyr` table straight from an `s3://` path, no `DBI` connection, secret, or view to manage. The mirror lives on AWS `us-west-2` (anonymous reads, reader doesn't pay egress), so it works from anywhere. Use the STAC catalog to *discover* paths and columns; read the Parquet directly here.

```r
library(duckdbfs)
library(dplyr)

# Anonymous reads from the source.coop mirror (AWS us-west-2). The bucket name
# has dots, so path-style addressing is required for the TLS certificate to match.
duckdb_s3_config(
  s3_endpoint  = "s3.us-west-2.amazonaws.com",
  s3_region    = "us-west-2",
  s3_url_style = "path",
  s3_use_ssl   = TRUE,
  anonymous    = TRUE
)

# open_dataset() returns a lazy dplyr table backed by DuckDB. recursive = FALSE
# because this is a single file; drop it (the default globs a directory prefix,
# e.g. the per-h0 files of a hex dataset) to read many Parquet as one table.
path <- "s3://us-west-2.opendata.source.coop/cboettig/overturemaps/2026-02-18.0/countries.parquet"
countries <- open_dataset(path, recursive = FALSE)

q <- countries |>
  filter(subtype == "country", is_land) |>
  select(country, name_en, subtype) |>
  arrange(name_en) |>
  head(10)

q |> show_query()   # inspect the DuckDB SQL dbplyr generated
q |> collect()      # pull the result into a tibble
```

`dbplyr` pushes `filter`/`select`/`arrange`/`summarise`/joins down into DuckDB, so column and row pruning happen in the engine — only the final `collect()` pulls data into R.

::: tip
The STAC catalog publishes NRP paths (`s3://public-<name>/…`); the source.coop mirror maps them to `s3://us-west-2.opendata.source.coop/cboettig/<name>/…`. Discover paths and columns via `get_stac_details` (see the [`httr2` example above](#r)) or the [web catalog](https://beta.source.coop), then translate the prefix. The usual DuckDB read rule still applies: always `read_parquet('s3://…')`, never a bare table name.
:::

### Python — ibis

The Python parallel to `dbplyr`: [ibis](https://ibis-project.org) drives the same local DuckDB against the same source.coop mirror, and its deferred expressions compile to DuckDB SQL — nothing runs until `.execute()`.

```bash
pip install 'ibis-framework[duckdb]'
```

```python
import ibis

con = ibis.duckdb.connect()
con.raw_sql("INSTALL httpfs; LOAD httpfs;")

# Anonymous reads from the source.coop mirror (AWS us-west-2). The bucket name
# has dots, so URL_STYLE 'path' is required for the TLS certificate to match.
con.raw_sql("""
  CREATE SECRET source_coop (
    TYPE S3, KEY_ID '', SECRET '',
    ENDPOINT 's3.us-west-2.amazonaws.com', REGION 'us-west-2',
    URL_STYLE 'path', USE_SSL 'true',
    SCOPE 's3://us-west-2.opendata.source.coop'
  )""")

path = "s3://us-west-2.opendata.source.coop/cboettig/overturemaps/2026-02-18.0/countries.parquet"
# EXCLUDE the GEOMETRY column: ibis's type mapper can't represent DuckDB's
# GEOMETRY type during schema inference. (The attribute columns are all we need.)
con.raw_sql(f"CREATE VIEW countries AS SELECT * EXCLUDE (geometry) FROM read_parquet('{path}')")

countries = con.table("countries")

expr = (
    countries
    .filter((countries.subtype == "country") & countries.is_land)
    .select("country", "name_en", "subtype")
    .order_by("name_en")
    .limit(10)
)

print(ibis.to_sql(expr))   # inspect the DuckDB SQL ibis generated
print(expr.execute())      # run it, returns a pandas DataFrame
```

Like `dbplyr`, ibis pushes `filter`/`select`/`order_by`/aggregations/joins down into DuckDB, so only the final `.execute()` pulls rows into Python. The same path mapping and `read_parquet` rules from the R tip above apply.

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

[`mcptools`](https://posit-dev.github.io/mcptools/) is an MCP client for R that plugs MCP tools into `ellmer` chats. Its client (`mcp_tools()`) speaks **stdio only** — direct HTTP-transport support was proposed in [posit-dev/mcptools#88](https://github.com/posit-dev/mcptools/issues/88) but deliberately deferred (the maintainers still [recommend `mcp-remote`](https://posit-dev.github.io/mcptools/reference/client.html#connecting-to-remote-http-servers)). So we bridge to the remote HTTP server with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) (requires Node.js on PATH).

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
