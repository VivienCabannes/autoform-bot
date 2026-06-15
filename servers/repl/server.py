"""Lean REPL MCP server — run Lean code and check compilation."""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

_NOT_IMPLEMENTED = "Not yet implemented. See examples/servers/repl/ for reference implementation."


def create_repl_server() -> FastMCP:
    """Create a FastMCP server with Lean REPL tools.

    Exposes two tools:
    - run_lean_code: Send Lean code to the REPL pool
    - get_repl_status: Check pool health and memory usage
    """
    server = FastMCP(name="autoform-repl")

    @server.tool
    def run_lean_code(code: str, timeout: float | None = None) -> str:
        """Send Lean code to the REPL and return formatted diagnostics.

        Imports are cached automatically — repeated calls with the same
        imports reuse the cached environment for speed.

        Args:
            code: Lean code to execute (imports + body).
            timeout: Optional timeout in seconds (overrides the default).

        Returns:
            Formatted diagnostic output: compilation status, errors,
            sorries with goals, and warnings.
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def get_repl_status() -> str:
        """Check the REPL pool's health and memory usage.

        Returns:
            JSON string with capacity, memory_usage_gb, and shutdown status.
        """
        return _NOT_IMPLEMENTED

    return server


if __name__ == "__main__":
    server = create_repl_server()
    server.run(transport="stdio")
