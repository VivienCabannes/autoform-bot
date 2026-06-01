"""Glob search MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import GlobSearch


def create_glob_server(allowed_dirs: list[str]) -> FastMCP:
    """Create an MCP server with glob file search tools.

    Args:
        allowed_dirs: Directories the server is allowed to search.

    Returns:
        FastMCP server with glob_search tool.
    """
    ops = GlobSearch(allowed_dirs)
    server = FastMCP(name="glob")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def glob_search(
        pattern: str,
        path: str = "",
        max_results: int = 500,
    ) -> str:
        """Find files matching a glob pattern.

        Supports patterns like "**/*.py", "src/**/*.ts", "*.json".
        Results are sorted by modification time (most recent first).

        Args:
            pattern: Glob pattern to match files against.
            path: Directory to search in. Defaults to first allowed dir.
            max_results: Maximum number of results to return.
        """
        return ops.search(pattern, path=path, max_results=max_results)

    return server


def glob_search_server(allowed_dirs: list[str]) -> MCPServerConfig:
    """Create a glob search MCP server config."""
    return MCPServerConfig(
        server_key="glob",
        description="File path search using glob patterns",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_glob_server(allowed_dirs),
    )
