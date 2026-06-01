"""Grep MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import GrepSearch


def create_grep_server(allowed_dirs: list[str]) -> FastMCP:
    """Create an MCP server with grep/content-search tools."""
    ops = GrepSearch(allowed_dirs)
    server = FastMCP(name="grep")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def grep(
        pattern: str,
        path: str = "",
        glob: str = "",
        file_type: str = "",
        context: int = 0,
        max_results: int = 200,
        case_insensitive: bool = False,
        multiline: bool = False,
        literal: bool = False,
        invert_match: bool = False,
        word_boundary: bool = False,
        count_only: bool = False,
        json_output: bool = False,
    ) -> str:
        """Search file contents with a regex pattern.

        Uses ripgrep if available, otherwise falls back to pure-Python.

        Args:
            pattern: Regex pattern to search for.
            path: File or directory to search in. Defaults to first allowed dir.
            glob: Glob pattern to filter filenames (e.g. "*.py", "*.lean").
            file_type: File type filter (e.g. "py", "lean"). Ripgrep built-in types.
            context: Number of context lines before and after each match.
            max_results: Maximum number of matching lines to return.
            case_insensitive: Case-insensitive search.
            multiline: Enable multiline matching (ripgrep only).
            literal: Treat pattern as literal string, not regex.
            invert_match: Return lines that do NOT match the pattern.
            word_boundary: Match whole words only.
            count_only: Return match counts per file instead of matched lines.
            json_output: Return results in JSON format (ripgrep only).
        """
        return ops.grep(
            pattern,
            path=path,
            glob=glob,
            file_type=file_type,
            context=context,
            max_results=max_results,
            case_insensitive=case_insensitive,
            multiline=multiline,
            literal=literal,
            invert_match=invert_match,
            word_boundary=word_boundary,
            count_only=count_only,
            json_output=json_output,
        )

    return server


def grep_server(allowed_dirs: list[str]) -> MCPServerConfig:
    """Create a grep MCP server config."""
    return MCPServerConfig(
        server_key="grep",
        description="Regex content search across files",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_grep_server(allowed_dirs),
    )
