"""Task tracker — ItemTracker flavor for orchestration with DAGRunner integration."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig
from core.tracker import ItemTracker
from tools.workspace.tracker import create_tracker_server, tracker_server

from .core import get_state, task_summary


def task_tracker_server(
    tracker: ItemTracker,
    extra_tools: list[Callable[[FastMCP], None]] | None = None,
) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for task tracker tools."""
    return tracker_server(tracker, extra_tools=extra_tools)


__all__ = [
    "create_tracker_server",
    "get_state",
    "task_summary",
    "task_tracker_server",
]
