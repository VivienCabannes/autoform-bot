"""Mathlib MCP server — search Mathlib source by name, pattern, or file."""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

_NOT_IMPLEMENTED = "Not yet implemented. See examples/servers/mathlib/ for reference implementation."


def create_mathlib_server() -> FastMCP:
    """Create a FastMCP server with Mathlib source search tools."""
    server = FastMCP(name="autoform-mathlib")

    @server.tool
    def mathlib_grep(
        pattern: str,
        kind: str = "",
        subdir: str = "",
        max_results: int = 50,
        context_lines: int = 0,
        literal: bool = False,
    ) -> str:
        """Search Mathlib source code using ripgrep.

        Args:
            pattern: Search pattern (regex by default).
            kind: Filter by declaration kind (theorem, lemma, def, etc.).
            subdir: Subdirectory to search (e.g. Algebra, Analysis, Topology).
            max_results: Maximum results to return.
            context_lines: Lines of context around matches.
            literal: If true, treat pattern as literal string.
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def mathlib_find_name(
        name: str,
        exact: bool = False,
        max_results: int = 30,
    ) -> str:
        """Find a theorem, lemma, or definition by name in Mathlib.

        Args:
            name: Name to search for (e.g. sum_add_distrib, det_mul).
            exact: Match exact name only.
            max_results: Maximum results to return.
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def mathlib_read_file(
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a Mathlib source file.

        Args:
            file_path: Path relative to Mathlib root (e.g. Mathlib/LinearAlgebra/Matrix/Determinant.lean).
            start_line: Starting line (1-indexed, optional).
            end_line: Ending line (inclusive, optional).
        """
        return _NOT_IMPLEMENTED

    return server


if __name__ == "__main__":
    server = create_mathlib_server()
    server.run(transport="stdio")
