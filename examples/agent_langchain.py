"""
LangChain example: let an LLM decide when to call the duckdb-geo MCP tools.

Uses langchain-mcp-adapters to expose every MCP tool to a LangGraph ReAct
agent. The model picks the tool, generates SQL, and summarises the result.

Install:
    pip install langchain-mcp-adapters langchain-openai langgraph

Set:
    export OPENAI_API_KEY=...            # or use any OpenAI-compatible endpoint
    export OPENAI_BASE_URL=...           # optional, defaults to OpenAI

Run:
    python agent_langchain.py
"""

import asyncio
import os

from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

MCP_URL = "https://duckdb-mcp.nrp-nautilus.io/mcp"
QUESTION = "What fraction of Australia is protected area?"


async def main() -> None:
    client = MultiServerMCPClient(
        {
            "duckdb-geo": {
                "url": MCP_URL,
                "transport": "streamable_http",
            }
        }
    )
    tools = await client.get_tools()

    model = ChatOpenAI(
        model=os.environ.get("MODEL", "gpt-4o"),
        max_tokens=4096,
    )
    agent = create_react_agent(model, tools)

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": QUESTION}]}
    )
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
