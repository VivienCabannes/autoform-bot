"""Web fetch MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import WebFetcher


def create_web_fetch_server(
    *,
    timeout_s: int = 30,
    max_content_length: int = 50_000,
) -> FastMCP:
    """Create an MCP server with web fetch tools.

    Args:
        timeout_s: HTTP request timeout in seconds.
        max_content_length: Max bytes to read from a response body.

    Returns:
        FastMCP server with web_fetch tool.
    """
    ops = WebFetcher(timeout_s=timeout_s, max_content_length=max_content_length)
    server = FastMCP(name="web-fetch")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def web_fetch(url: str) -> str:
        """Fetch a URL and return its content as markdown.

        HTTP URLs are auto-upgraded to HTTPS. Returns the page content
        converted from HTML to markdown.
        """
        return await ops.fetch(url)

    return server


def web_fetch_server(
    *,
    timeout_s: int = 30,
    max_content_length: int = 50_000,
) -> MCPServerConfig:
    """Create a web fetch MCP server config."""
    return MCPServerConfig(
        server_key="web-fetch",
        description="Fetch and extract content from web pages",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_web_fetch_server(
            timeout_s=timeout_s,
            max_content_length=max_content_length,
        ),
    )
