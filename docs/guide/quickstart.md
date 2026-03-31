# Quick Start

The hosted MCP server is available at:

```
https://duckdb-mcp.nrp-nautilus.io/mcp
```

Add it to your LLM client and start asking questions about the data.

## VS Code

Create a `.vscode/mcp.json` in your project:

```json
{
  "servers": {
    "duckdb-geo": {
      "url": "https://duckdb-mcp.nrp-nautilus.io/mcp"
    }
  }
}
```

## Claude Desktop

Add to your Claude Desktop configuration file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "duckdb-geo": {
      "url": "https://duckdb-mcp.nrp-nautilus.io/mcp"
    }
  }
}
```

Restart Claude Desktop after saving.

## Example questions

Once connected, ask your LLM client questions like:

- *What fraction of Australia is protected area?*
- *How many km² of wetlands overlap with vulnerable carbon stores?*
- *Which watersheds have the highest biodiversity scores?*

The agent will call `browse_stac_catalog` and `get_stac_details` to discover dataset paths, then execute DuckDB SQL against S3.

## MCP tools

| Tool | Description |
|---|---|
| `browse_stac_catalog` | List all available datasets from the STAC catalog |
| `get_stac_details` | Get S3 paths and column schemas for a dataset |
| `query` | Execute DuckDB SQL against S3 Parquet files |

## MCP resources & prompts

Some clients (Claude Code, Continue.dev) also support MCP resources and prompts:

- `catalog://list` — list all datasets
- `catalog://{dataset_id}` — schema for a specific dataset
- `geospatial-analyst` prompt — load the full analyst persona and context

## Local development

Run the server locally against your own CPU and network:

```bash
pip install -r requirements.txt
python server.py
```

Then connect via `http://localhost:8000/mcp` (note: `http`, not `https`).

::: warning
Local queries use your machine's CPU and network. Large S3 scans will be significantly slower than the hosted k8s endpoint.
:::
