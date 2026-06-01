"""Web search MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import WebSearcher


def create_web_search_server(
    *,
    timeout_s: int = 15,
    max_results: int = 10,
) -> FastMCP:
    """Create an MCP server with web search tools.

    Args:
        timeout_s: HTTP request timeout in seconds.
        max_results: Maximum number of search results to return.

    Returns:
        FastMCP server with web_search tool.
    """
    ops = WebSearcher(timeout_s=timeout_s, max_results=max_results)
    server = FastMCP(name="web-search")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def web_search(query: str) -> str:
        """Search the web and return results.

        Returns a list of results with title, URL, and snippet.
        """
        return await ops.search(query)

    return server


def web_search_server(
    *,
    timeout_s: int = 15,
    max_results: int = 10,
) -> MCPServerConfig:
    """Create a web search MCP server config."""
    return MCPServerConfig(
        server_key="web-search",
        description="Web search via search engine APIs",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_web_search_server(
            timeout_s=timeout_s,
            max_results=max_results,
        ),
    )
