import logging
from pydantic import AnyUrl
from typing import Literal
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from .configs import SERVER_VERSION
from .database import DatabaseClient
from .prompt import get_full_prompt


logger = logging.getLogger("mcp_data_server")


def build_application(
    db_path: str,
    motherduck_token: str | None = None,
    home_dir: str | None = None,
    saas_mode: bool = False,
    read_only: bool = False,
    max_rows: int = 1024,
    max_chars: int = 50000,
    query_timeout: int = -1,
    custom_prompt_path: str | None = None,
):
    logger.info("Starting MCP Data Server")
    if custom_prompt_path:
        logger.info(f"Using custom prompt from: {custom_prompt_path}")
    server = Server("mcp-data-server")
    
    # Generate the prompt template with custom prompt if provided
    prompt_template = get_full_prompt(custom_prompt_path)
    
    # Set prompt metadata based on whether custom prompt is used
    if custom_prompt_path:
        prompt_name = "data-analyst-prompt"
        prompt_description = "A prompt for analyzing data using DuckDB SQL queries"
        prompt_result_description = "Data analyst prompt for querying databases"
    else:
        prompt_name = "data-descriptions-prompt"
        prompt_description = "A prompt for analyzing global wetlands data including GLWD, protected areas, carbon storage, species ranges, and more using DuckDB"
        prompt_result_description = "Wetlands data analyst prompt for querying global wetlands datasets"
    db_client = DatabaseClient(
        db_path=db_path,
        motherduck_token=motherduck_token,
        home_dir=home_dir,
        saas_mode=saas_mode,
        read_only=read_only,
        max_rows=max_rows,
        max_chars=max_chars,
        query_timeout=query_timeout,
    )

    logger.info("Registering handlers")

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        """
        List available note resources.
        Each note is exposed as a resource with a custom note:// URI scheme.
        """
        logger.info("No resources available to list")
        return []

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        """
        Read a specific note's content by its URI.
        The note name is extracted from the URI host component.
        """
        logger.info(f"Reading resource: {uri}")
        raise ValueError(f"Unsupported URI scheme: {uri.scheme}")

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        """
        List available prompts.
        Each prompt can have optional arguments to customize its behavior.
        """
        logger.info("Listing prompts")
        return [
            types.Prompt(
                name=prompt_name,
                description=prompt_description,
            )
        ]

    @server.get_prompt()
    async def handle_get_prompt(
        name: str, arguments: dict[str, str] | None
    ) -> types.GetPromptResult:
        """
        Generate a prompt by combining arguments with server state.
        The prompt provides data analysis capabilities using DuckDB.
        """
        logger.info(f"Getting prompt: {name}::{arguments}")
        if name != prompt_name:
            raise ValueError(f"Unknown prompt: {name}")

        return types.GetPromptResult(
            description=prompt_result_description,
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt_template),
                )
            ],
        )

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """
        List available tools.
        Each tool specifies its arguments using JSON Schema validation.
        """
        logger.info("Listing tools")
        return [
            types.Tool(
                name="query",
                description="Use this to execute a query on the MotherDuck or DuckDB database",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "SQL query to execute that is a dialect of DuckDB SQL",
                        },
                    },
                    "required": ["query"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_tool_call(
        name: str, arguments: dict | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """
        Handle tool execution requests.
        Tools can modify server state and notify clients of changes.
        """
        logger.info(f"Calling tool: {name}::{arguments}")
        try:
            if name == "query":
                if arguments is None:
                    return [
                        types.TextContent(type="text", text="Error: No query provided")
                    ]
                tool_response = db_client.query(arguments["query"])
                return [types.TextContent(type="text", text=str(tool_response))]

            return [types.TextContent(type="text", text=f"Unsupported tool: {name}")]

        except Exception as e:
            logger.error(f"Error executing tool {name}: {e}")
            raise ValueError(f"Error executing tool {name}: {str(e)}")

    initialization_options = InitializationOptions(
        server_name="motherduck",
        server_version=SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )

    return server, initialization_options
