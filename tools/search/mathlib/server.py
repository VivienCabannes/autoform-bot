# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Mathlib MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import grep_mathlib, find_name_in_mathlib, read_mathlib_file


@dataclass(frozen=True)
class MathlibConfig:
    """Configuration for the Mathlib search tool."""

    repo_root: str


def create_mathlib_server(repo_root: str | Path) -> FastMCP:
    """Create a FastMCP server with Mathlib source search tools."""
    server = FastMCP(name="mathlib")
    repo_root = Path(repo_root).resolve()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
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
        return grep_mathlib(
            repo_root,
            pattern,
            kind=kind,
            subdir=subdir,
            max_results=max_results,
            context_lines=context_lines,
            literal=literal,
        )

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
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
        return find_name_in_mathlib(repo_root, name, exact=exact, max_results=max_results)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ, max_result_chars=float("inf"))
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
        return read_mathlib_file(repo_root, file_path, start_line=start_line, end_line=end_line)

    return server


def mathlib_server(config: MathlibConfig) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for Mathlib search."""
    mcp_instance = create_mathlib_server(config.repo_root)
    return MCPServerConfig(
        server_key="mathlib",
        description="Mathlib theorem search and lookup",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
