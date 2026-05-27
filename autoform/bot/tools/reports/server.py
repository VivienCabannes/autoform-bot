# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool server for loading task reports."""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import ReportsLoader


def create_reports_server(loader: ReportsLoader) -> FastMCP:
    """Create an inprocess FastMCP server for report loading."""
    server = FastMCP(name="reports")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def load_reports() -> str:
        """Load all task reports from the last round as a compact JSON array.

        Each report contains: task_id, status, attempts, summary, suggestions.
        Returns an empty array if no reports exist yet.
        """
        reports = loader.load()
        return json.dumps(reports, separators=(",", ":"))

    return server


def reports_server(reports_path: Path) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for the reports tool."""
    loader = ReportsLoader(reports_path)
    return MCPServerConfig(
        server_key="reports",
        description="Load task reports from the last execution round",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_reports_server(loader),
    )
