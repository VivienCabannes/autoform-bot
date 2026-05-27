# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Read-only MCP tool server for the goal tracker."""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec
from core.tracker import ItemTracker


def create_goal_tracker_server(tracker: ItemTracker) -> FastMCP:
    """Create an inprocess FastMCP server exposing read-only goal tools."""
    server = FastMCP(name="goal-tracker")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_goals(
        status: str | None = None,
        query: str | None = None,
    ) -> str:
        """List formalization goals with their status and score.

        Returns a compact view: status and score only, no feedback or
        descriptions. Use get_goal() to retrieve full feedback for a
        specific goal.

        Args:
            status: Filter by status (pending/completed/failed). Returns all if omitted.
            query: Text search in goal ID, title, and description (case-insensitive).
        """
        items = tracker.list(status=status, query=query)
        if not items:
            return "No goals found."
        # Compact view: strip description and bulky metadata fields.
        # Keep score for quick triage; use get_goal() for full feedback.
        _COMPACT_METADATA_KEYS = {"score"}
        compact = []
        for item in items:
            row = {k: v for k, v in item.items() if k != "description"}
            meta = item.get("metadata")
            if meta:
                row["metadata"] = {k: v for k, v in meta.items() if k in _COMPACT_METADATA_KEYS}
            compact.append(row)
        return json.dumps(compact, indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_goal(goal_id: str) -> str:
        """Get full details for a specific goal, including feedback and description.

        Use this after list_goals() to inspect why a specific goal failed.
        Returns the full metadata: score, feedback, lean_declaration, lean_file.

        Args:
            goal_id: The goal ID to inspect.
        """
        item = tracker.get(goal_id)
        if item is None:
            return f"Error: goal {goal_id} not found."
        return json.dumps(item, indent=2)

    return server


def goal_tracker_server(tracker: ItemTracker) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for the read-only goal tracker."""
    return MCPServerConfig(
        server_key="goal-tracker",
        description="Read-only goal tracker: list and inspect formalization targets",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_goal_tracker_server(tracker),
    )
