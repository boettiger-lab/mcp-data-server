# Architecture

## Runtime prompt files

The `.md` files in this repo are **not documentation** — they are curated prompt content loaded by `server.py` at startup and injected directly into MCP tool descriptions and prompts. The agent (LLM) reads them as instructions.

| File | How it is used |
|---|---|
| `query-setup.md` | SQL extracted and executed in every fresh DuckDB connection before a query runs |
| `query-optimization.md` | Injected verbatim into the `query` tool description |
| `h3-guide.md` | Injected verbatim into the `query` tool description |
| `assistant-role.md` | Served as the `geospatial-analyst` MCP prompt (role and response style) |

Editing these files changes what the agent is told to do. They must be written for a stateless LLM — short, concrete, and unambiguous. See `AGENTS.md` for editing rules.

## Two-process design

The server is designed around two distinct agent processes. See `AGENTS.md` for the full details.

**Process 1 — Real-time MCP tool (small LLM)**

Handles user requests in real time. Has no memory between requests. Its only context is what is injected at call time from the prompt files above.

**Process 2 — Asynchronous evaluation (Claude)**

Reviews logs from real user sessions, identifies slow or incorrect queries, diagnoses root causes, and updates the small LLM's prompt files. This separation prevents misdiagnoses (e.g. blaming DuckDB internals when the real cause was a query violating a rule in `query-optimization.md`) from corrupting the prompt files.

## Isolation engine

Each query runs in a fresh `duckdb.connect(":memory:")`:

```python
@contextmanager
def get_isolated_db(...):
    conn = duckdb.connect(database=":memory:")
    try:
        # Run setup SQL from query-setup.md
        # Inject per-request S3 credentials if provided
        yield conn
    finally:
        conn.close()
```

No state, credentials, or query results persist between requests.

## Context injection

Prompt content is embedded into tool descriptions so that MCP clients which don't support `prompts/list` (e.g. VS Code) still receive the guidance. The `query` tool description includes the full text of `query-optimization.md` and `h3-guide.md`.

## STAC catalog integration

Dataset discovery is handled by `stac.py`. The agent calls `browse_stac_catalog` to list available datasets, then `get_stac_details` to resolve S3 paths and column schemas. S3 paths are never hardcoded in the server or guessed by the agent.
