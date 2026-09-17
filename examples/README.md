# Examples

## The worked example

One analysis — *bird observations vs. median household income, by census tract, in New Haven
CT* — written four ways. Each pair is the same pipeline run two different
places, so the diff between them is exactly the "local vs. server" trade.
Walked through in [Programmatic Access](https://boettiger-lab.github.io/mcp-data-server/guide/programmatic-access.html).

| File | Query language | Runs where |
|---|---|---|
| [new_haven_birds_dplyr.R](new_haven_birds_dplyr.R) | `dplyr` / `dbplyr` | local DuckDB, source.coop mirror |
| [new_haven_birds_mcp.R](new_haven_birds_mcp.R) | `dplyr` / `dbplyr` | MCP server (`sql_render()` → `query`) |
| [new_haven_birds_ibis.py](new_haven_birds_ibis.py) | ibis | local DuckDB, source.coop mirror |
| [new_haven_birds_mcp.py](new_haven_birds_mcp.py) | ibis | MCP server (`ibis.to_sql()` → `query`) |

You do not write SQL in any of them: `dbplyr` and ibis compile it, and the
server runs what they emit. The local scripts cap DuckDB at 3 GB and spill to
node-local scratch (not a networked `$HOME`), so they run on a laptop.

## Smaller examples

| File | What it does |
|---|---|
| [query.py](query.py) | Direct MCP `query` tool call from Python (no LLM) |
| [query.R](query.R) | Direct MCP `query` tool call from R via JSON-RPC over HTTP |
| [query_dbplyr.R](query_dbplyr.R) | Minimal dplyr-against-source.coop query (no MCP server) |
| [query_ibis.py](query_ibis.py) | Minimal ibis-against-source.coop query (no MCP server) |
| [agent_langchain.py](agent_langchain.py) | LangGraph ReAct agent that calls MCP tools via tool use |
| [agent_ellmer.R](agent_ellmer.R) | ellmer chat that calls MCP tools via tool use |

All scripts target the public endpoint `https://duckdb-mcp.nrp-nautilus.io/mcp`.
The agent examples use `langchain-openai` / `ellmer::chat_openai()` so they work with any OpenAI-compatible endpoint — set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` and `MODEL`) in your environment. The R agent example also requires Node.js (for `npx mcp-remote`).

The Python MCP examples target the `mcp` 2.x SDK, where the transport helper is
`streamable_http_client` and yields two streams; on the 1.x SDK it is
`streamablehttp_client` and yields a third element, the session id.
