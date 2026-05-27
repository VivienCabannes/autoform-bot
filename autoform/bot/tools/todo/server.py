# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool server for the orchestrator's personal TODO list.

Thin wrapper around ItemTracker with todo_-prefixed tool names,
a 30-item cap, and no dependencies/dispatch (flat list).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec
from core.tracker import ItemTracker

MAX_TODOS = 30


def create_todo_server(tracker: ItemTracker) -> FastMCP:
    server = FastMCP(name="todo")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def todo_add(note: str) -> str:
        """Add a personal TODO reminder.

        Args:
            note: What you want to remember to do.
        """
        active = [i for i in tracker.list() if i["status"] != "deleted"]
        if len(active) >= MAX_TODOS:
            return f"Error: TODO list is full ({MAX_TODOS}/{MAX_TODOS}). Delete or complete existing TODOs first."
        todo_id = tracker.add(title=note)
        remaining = MAX_TODOS - len(active) - 1
        return f"TODO {todo_id} added. ({remaining} slots remaining)"

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def todo_list() -> str:
        """List all active TODOs with their status."""
        items = [i for i in tracker.list() if i["status"] != "deleted"]
        if not items:
            return "No TODOs."
        rows = [{"id": i["id"], "status": i["status"], "note": i["title"]} for i in items]
        active = len(rows)
        result = json.dumps(rows, indent=2)
        return f"{result}\n\n({active}/{MAX_TODOS} slots used)"

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def todo_update(todo_id: str, note: str) -> str:
        """Rewrite a TODO's text.

        Args:
            todo_id: The TODO to update.
            note: The new text.
        """
        return tracker.update(todo_id, title=note)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def todo_set_status(todo_id: str, status: str) -> str:
        """Set a TODO's status.

        Args:
            todo_id: The TODO to update.
            status: One of: pending, in_progress, done.
        """
        mapped = "completed" if status == "done" else status
        return tracker.update(todo_id, status=mapped)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def todo_delete(todo_id: str) -> str:
        """Permanently remove a TODO from the list.

        Args:
            todo_id: The TODO to remove.
        """
        item = tracker.get(todo_id)
        if item is None:
            return f"Error: TODO {todo_id} not found."
        tracker.update(todo_id, status="deleted")
        # Purge from internal storage so it doesn't count toward the cap
        tracker._items = {k: v for k, v in tracker._items.items() if k != todo_id}
        tracker._save()
        return f"TODO {todo_id} removed."

    return server


def todo_server(persist_path: Path) -> MCPServerConfig:
    """Create a TODO tracker MCP server config.

    Args:
        persist_path: Path to the JSON file for persistence.
    """
    tracker = ItemTracker(persist_path)
    return MCPServerConfig(
        server_key="todo",
        description="Personal TODO list for tracking reminders across rounds",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_todo_server(tracker),
    )
