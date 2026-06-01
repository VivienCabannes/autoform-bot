"""Docs lookup MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import DocsLookup


def create_docs_lookup_server(*, timeout_s: int = 20) -> FastMCP:
    """Create an MCP server with library documentation lookup tools.

    Args:
        timeout_s: HTTP request timeout in seconds.

    Returns:
        FastMCP server with docs lookup tools.
    """
    ops = DocsLookup(timeout_s=timeout_s)
    server = FastMCP(name="docs-lookup")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def search_docs(library: str, query: str) -> str:
        """Search for library documentation on the web.

        Args:
            library: Library/package name (e.g. "react", "fastapi", "numpy").
            query: What you want to know (e.g. "how to use hooks", "async endpoints").

        Returns:
            Search results with links to relevant documentation.
        """
        return await ops.search_docs(library, query)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def fetch_doc_page(url: str, max_length: int = 50000) -> str:
        """Fetch and extract content from a documentation page.

        Args:
            url: URL of the documentation page.
            max_length: Maximum characters to return.

        Returns:
            Extracted text content from the page.
        """
        return await ops.fetch_doc_page(url, max_length=max_length)

    return server


def docs_lookup_server(*, timeout_s: int = 20) -> MCPServerConfig:
    """Create a docs lookup MCP server config."""
    return MCPServerConfig(
        server_key="docs-lookup",
        description="Documentation search and lookup",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_docs_lookup_server(timeout_s=timeout_s),
    )
