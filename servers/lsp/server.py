"""Lean LSP MCP server — diagnostics and type information via Language Server Protocol."""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

_NOT_IMPLEMENTED = "Not yet implemented. See examples/servers/lsp/ for reference implementation."


def create_lsp_server() -> FastMCP:
    """Create a FastMCP server with Lean LSP tools."""
    server = FastMCP(name="autoform-lsp")

    @server.tool
    def lean_diagnostic_messages(file_path: str) -> str:
        """Get compilation diagnostics for a Lean file.

        Returns errors, warnings, and info messages from the Lean language server.

        Args:
            file_path: Absolute path to the .lean file.
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def lean_hover(file_path: str, line: int, character: int) -> str:
        """Get type information at a position in a Lean file.

        Args:
            file_path: Absolute path to the .lean file.
            line: 0-indexed line number.
            character: 0-indexed character position.
        """
        return _NOT_IMPLEMENTED

    return server


if __name__ == "__main__":
    server = create_lsp_server()
    server.run(transport="stdio")
