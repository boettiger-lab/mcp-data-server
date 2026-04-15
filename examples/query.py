"""
Minimal Python example: call the duckdb-geo MCP `query` tool directly.

No LLM involved — this just speaks MCP over streamable HTTP and runs SQL.

Install:
    pip install mcp

Run:
    python query.py
"""

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


async def main() -> None:
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List the tools the server exposes.
            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])

            # Call the `query` tool with a SQL string.
            result = await session.call_tool("query", {"sql_query": SQL})
            for block in result.content:
                # Each content block has a `.text` field for text results.
                print(block.text)


if __name__ == "__main__":
    asyncio.run(main())
