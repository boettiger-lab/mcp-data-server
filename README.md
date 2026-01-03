# Wetlands MCP Server

An MCP server specialized for analyzing wetlands datasets using DuckDB. This server provides AI assistants and IDEs with the ability to query and analyze comprehensive wetlands data, including spatial information, temporal trends, and ecological metrics.

This server is based on [MotherDuck's DuckDB MCP Server](https://github.com/motherduckdb/mcp-server-motherduck) and extends it with wetlands-specific prompts and context.

## Features

- **Wetlands-specific**: Pre-configured prompts and context for wetlands data analysis
- **Hybrid execution**: Query data from local DuckDB or cloud-based MotherDuck databases
- **Cloud storage integration**: Access wetlands data stored in Amazon S3 or other cloud storage
- **Spatial analytics**: Leverage DuckDB's spatial extensions for geospatial wetlands analysis
- **SQL analytics**: Use DuckDB's SQL dialect to query wetlands datasets directly from your AI Assistant or IDE
- **Serverless architecture**: Run analytics without needing to configure instances or clusters

## Components

### Prompts

The server accepts custom prompts with --custom-prompt argument.  

### Tools

The server offers one tool:

- `query`: Execute a SQL query on the DuckDB or MotherDuck database
  - **Inputs**:
    - `query` (string, required): The SQL query to execute

All interactions with both DuckDB and MotherDuck are done through writing SQL queries.

**Result Limiting**: Query results are automatically limited to prevent using up too much context:
- Maximum 1024 rows by default (configurable with `--max-rows`)
- Maximum 50,000 characters by default (configurable with `--max-chars`)
- Truncated responses include a note about truncation

## Command Line Parameters

The MCP server supports the following parameters:

| Parameter | Type | Default | Description                                                                                                                                                                                                                                                    |
|-----------|------|---------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--transport` | Choice | `stdio` | Transport type. Options: `stdio`, `sse`, `stream`                                                                                                                                                                                                              |
| `--port` | Integer | `8000` | Port to listen on for sse and stream transport mode                                                                                                                                                                                                            |
| `--host` | String | `127.0.0.1` | Host to bind the MCP server for sse and stream transport mode                                                                                                                                                                                                  |
| `--db-path` | String | `md:` | Path to local DuckDB database file, MotherDuck database, or S3 URL (e.g., `s3://bucket/path/to/db.duckdb`)                                                                                                                                                     |
| `--read-only` | Flag | `False` | Flag for connecting to DuckDB or MotherDuck in read-only mode. For DuckDB it uses short-lived connections to enable concurrent access                                                                                                                          |
| `--home-dir` | String | `None` | Home directory for DuckDB (uses `HOME` env var by default)                                                                                                                                                
| `--json-response` | Flag | `False` | Enable JSON responses for HTTP stream. Only supported for `stream` transport                                                                                                                                                                                   |
| `--max-rows` | Integer | `1024` | Maximum number of rows to return from queries.                                                                                                                                                                    |
| `--max-chars` | Integer | `50000` | Maximum number of characters in query results.                                                                                                                                                          |
| `--query-timeout` | Integer | `-1` | Query execution timeout in seconds. Set to -1 to disable timeout (default).                                                                                                                                                          |
| `--custom-prompt` | String | `None` | Path to a custom prompt markdown file. Use this to provide domain-specific context instead of the built-in wetlands data prompt. The custom prompt will be combined with the DuckDB prompt.                                           |

### Quick Usage Examples

```bash
# Connect to local DuckDB file in read-only mode
uvx mcp-data-server --db-path /path/to/local.db --read-only

# Connect to local DuckDB file in read-only mode
uvx mcp-data-server --db-path /path/to/local.db --read-only

# Customize result truncation limits for large wetlands queries
uvx mcp-data-server --max-rows 2048 --max-chars 100000

# Enable query timeout (5 minutes) for complex spatial queries
uvx mcp-data-server --query-timeout 300

# Use a custom prompt file for different datasets
uvx mcp-data-server --custom-prompt /path/to/custom-prompt.md
```

## Getting Started

### General Prerequisites

- `uv` installed, you can install it using `pip install uv` or `brew install uv`

If you plan to use the MCP with Claude Desktop or any other MCP compatible client, the client needs to be installed.

### Development Setup

1. Clone the repository
2. Create and activate a virtual environment:
```bash
uv venv
source .venv/bin/activate  # On Linux/Mac
```

3. Install the package in development mode:
```bash
uv pip install -e .
```

4. Install the MCP Python SDK for testing:
```bash
uv pip install mcp
```

5. Run the test suite:
```bash
python3 test_server.py
```

### Prerequisites for DuckDB

- No prerequisites. The MCP server can create an in-memory database on-the-fly
- Or connect to an existing local DuckDB database file, or one stored on remote object storage (e.g., AWS S3)
- Wetlands datasets can be loaded from S3 or other sources as needed


#### Manual Installation


Optionally, you can add it to a file called `.vscode/mcp.json` in your workspace. This will allow you to share the configuration with others.

```json
{
  "servers": {
    "wetlands": {
      "command": "uvx",
      "args": [
        "mcp-data-server"
      ]
    }
  }
}
```

### Usage with Claude Code

Claude Code supports MCP servers through CLI commands or JSON configuration. Add the server using a JSON configuration:

```bash
claude mcp add-json mcp-data-server '{
  "command": "uvx",
  "args": [
    "mcp-data-server",
    "--db-path"
  ]
}'
```

**Scoping Options**:
- Use `--local` (default) for project-specific configuration
- Use `--project` to share the configuration with your team via `.mcp.json`
- Use `--user` to make the server available across all your projects



## Running in SSE mode

The server can run in SSE mode in two ways:

### Direct SSE mode

Run the server directly in SSE mode using the `--transport sse` flag:

```bash
uvx mcp-data-server --transport sse --port 8000 --db-path md: --motherduck-token <your_motherduck_token>
```

This will start the server listening on the specified port (default 8000) and you can point your clients directly to this endpoint.

### Using supergateway

Alternatively, you can run SSE mode using `supergateway`:

```bash
npx -y supergateway --stdio "uvx mcp-data-server --db-path md: --motherduck-token <your_motherduck_token>"
```

Both methods allow you to point your clients such as Claude Desktop, Cursor to the SSE endpoint.

## Development configuration

To run the server from a local development environment, use the following configuration:

```json
 {
  "mcpServers": {
    "mcp-data-server": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/your/local/mcp-data-server",
        "run",
        "mcp-data-server",
        "--db-path",
        "md:",
        "--motherduck-token",
        "<YOUR_MOTHERDUCK_TOKEN_HERE>"
      ]
    }
  }
}
```

## Troubleshooting

- If you encounter connection issues, verify your MotherDuck token is correct
- For local file access problems, ensure the `--home-dir` parameter is set correctly
- Check that the `uvx` command is available in your PATH
- If you encounter `spawn uvx ENOENT` errors, try specifying the full path to `uvx` (output of `which uvx`)
- In version previous for v0.4.0 we used environment variables, now we use parameters

## License

This MCP server is licensed under the MIT License. This means you are free to use, modify, and distribute the software, subject to the terms and conditions of the MIT License. For more details, please see the LICENSE file in the project repository.

##
mcp-name: io.github.boettiger-lab/mcp-data-server
