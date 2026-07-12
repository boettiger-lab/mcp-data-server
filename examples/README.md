# Examples

Small scripts showing ways to talk to the duckdb-geo MCP server (plus one that
skips it and reads the same public data directly):

| File | What it does |
|---|---|
| [query.py](query.py) | Direct MCP `query` tool call from Python (no LLM) |
| [query.R](query.R) | Direct MCP `query` tool call from R via JSON-RPC over HTTP |
| [query_dbplyr.R](query_dbplyr.R) | Query the source.coop mirror with dplyr/dbplyr via a local DuckDB (no MCP server) |
| [query_ibis.py](query_ibis.py) | Query the source.coop mirror with ibis via a local DuckDB (no MCP server) |
| [agent_langchain.py](agent_langchain.py) | LangGraph ReAct agent that calls MCP tools via tool use |
| [agent_ellmer.R](agent_ellmer.R) | ellmer chat that calls MCP tools via tool use |

All scripts target the public endpoint `https://duckdb-mcp.nrp-nautilus.io/mcp`.
The agent examples use `langchain-openai` / `ellmer::chat_openai()` so they work with any OpenAI-compatible endpoint — set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` and `MODEL`) in your environment. The R agent example also requires Node.js (for `npx mcp-remote`).
