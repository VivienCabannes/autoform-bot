"""Workspace inspection MCP server — scan a Lean project without modifying it."""

from __future__ import annotations

import json
import os

from fastmcp.server import FastMCP

from .core import inspect_workspace, list_targets, search_lean, list_lean_declarations


def create_workspace_server() -> FastMCP:
    """Create a FastMCP server for workspace inspection."""
    server = FastMCP(name="autoform-workspace")

    @server.tool
    def workspace_inspect(path: str = "") -> str:
        """Scan a Lean workspace and return a structured summary.

        Reports: lakefile location, toolchain version, targets file, book file,
        Lean file count, declaration count, sorry/axiom counts, available tools
        (lake, lean, rg), and recommended next steps.

        Args:
            path: Workspace or file path. Defaults to LEAN_PROJECT_DIR or cwd.
        """
        result = inspect_workspace(path or None)
        return json.dumps(result, indent=2)

    @server.tool
    def workspace_targets(path: str = "", limit: int = 50) -> str:
        """Read formalization targets from targets.yaml/yml/json.

        Args:
            path: Workspace or targets file path.
            limit: Maximum number of targets to return.
        """
        result = list_targets(path or None, limit=limit)
        return json.dumps(result, indent=2)

    @server.tool
    def workspace_search(pattern: str, path: str = "", limit: int = 50) -> str:
        """Search .lean files for a literal string.

        Uses ripgrep when available, falls back to pure Python.

        Args:
            pattern: Literal string to search for.
            path: Workspace path. Defaults to LEAN_PROJECT_DIR or cwd.
            limit: Maximum number of matches to return.
        """
        result = search_lean(pattern, path or None, limit=limit)
        return json.dumps(result, indent=2)

    @server.tool
    def workspace_declarations(path: str = "", limit: int = 200) -> str:
        """List Lean declarations found by lightweight source scanning.

        Finds theorem, lemma, def, abbrev, axiom, constant, inductive,
        structure, class, and instance declarations.

        Args:
            path: Workspace path. Defaults to LEAN_PROJECT_DIR or cwd.
            limit: Maximum number of declarations to return.
        """
        result = list_lean_declarations(path or None, limit=limit)
        return json.dumps(result, indent=2)

    return server


if __name__ == "__main__":
    server = create_workspace_server()
    server.run(transport="stdio")
