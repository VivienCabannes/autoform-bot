# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool server for Lean code analysis."""

from __future__ import annotations

from pathlib import Path

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import find_sorries


def lean_analysis_server(code_path: Path) -> MCPServerConfig:
    """Create an MCP server exposing Lean code analysis tools."""
    server = FastMCP(name="lean-analysis")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def find_sorries_in_codebase() -> str:
        """Search all Lean files in the codebase for uses of sorry.

        Returns a list of files and line numbers containing sorry,
        or a confirmation that the codebase is sorry-free.
        """
        results = find_sorries(Path(code_path))
        if not results:
            return "No sorry found — codebase is clean."
        return f"{len(results)} sorry occurrence(s):\n" + "\n".join(results)

    return MCPServerConfig(
        server_key="lean-analysis",
        description="Lean code analysis: sorry detection and codebase inspection",
        transport=TransportMethod.INPROCESS,
        mcp_instance=server,
    )
