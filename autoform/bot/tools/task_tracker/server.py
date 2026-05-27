# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool server for the constrained task tracker."""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec
from core.tracker import ItemTracker

from .core import ConstrainedTracker


def create_constrained_tracker_server(ops: ConstrainedTracker) -> FastMCP:
    """Create an inprocess FastMCP server wrapping a ConstrainedTracker.

    Read tools delegate to the underlying tracker. Mutation tools
    enforce the pending/failed guard via ConstrainedTracker.
    """
    server = FastMCP(name="task-tracker")

    # ------------------------------------------------------------------
    # Read-only tools
    # ------------------------------------------------------------------

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_items(
        status: str | None = None,
        query: str | None = None,
        ready_only: bool = False,
    ) -> str:
        """List all tasks with their status and dependencies.

        Returns a compact view (no descriptions). Use ``get_item``
        for full details on a specific task.

        Args:
            status: Filter by status. Returns all if omitted.
            query: Text search in task ID, title, and description (case-insensitive).
            ready_only: If true, only return tasks whose dependencies are all satisfied.
        """
        if ready_only:
            items = ops.ready()
            if query is not None:
                q = query.lower()
                items = [i for i in items if q in f"{i['id']} {i.get('title', '')} {i.get('description', '')}".lower()]
        else:
            items = ops.list(status=status, query=query)
        if not items:
            return "No tasks found."
        compact = [{k: v for k, v in item.items() if k != "description"} for item in items]
        return json.dumps(compact, indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_item(item_id: str) -> str:
        """Get the full details for a specific task.

        Args:
            item_id: The task ID to inspect.
        """
        item = ops.get(item_id)
        if item is None:
            return f"Error: task {item_id} not found."
        return json.dumps(item, indent=2)

    # ------------------------------------------------------------------
    # Mutation tools — delegated to ConstrainedTracker
    # ------------------------------------------------------------------

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def add_item(
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
        metadata: dict | None = None,
        item_id: str | None = None,
    ) -> str:
        """Add a new task. It will be created with pending status.

        Dependencies must already exist — add parent tasks first.

        Args:
            title: Short summary of the task.
            description: Full details or instructions.
            depends_on: IDs of tasks that must complete before this one.
            metadata: Optional key-value pairs.
            item_id: Explicit ID slug. Auto-increments if omitted.
        """
        try:
            new_id = ops.add(
                title,
                description,
                depends_on,
                metadata=metadata,
                item_id=item_id,
            )
        except ValueError as e:
            return f"Error: {e}"
        return f"Task {new_id} added: {title}"

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def update_item(
        item_id: str,
        title: str | None = None,
        description: str | None = None,
        depends_on: list[str] | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Update a pending or failed task's fields.

        Cannot modify tasks that are in_progress, completed, or deleted.
        Cannot change task status — use delete_item to remove a task.

        Args:
            item_id: The task to update.
            title: New title (optional).
            description: New description (optional).
            depends_on: New dependency list (optional, replaces existing).
            metadata: Fields to merge into metadata (optional).
        """
        return ops.update(
            item_id,
            title=title,
            description=description,
            depends_on=depends_on,
            metadata=metadata,
        )

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def delete_item(item_id: str) -> str:
        """Delete a pending or failed task.

        Cannot delete tasks that are in_progress or completed.

        Args:
            item_id: The task to delete.
        """
        return ops.delete(item_id)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def dispatch_task(task_id: str) -> str:
        """Dispatch a ready task for immediate execution without waiting for the current turn to end.

        The task must be pending with all dependencies met. Use after adding
        a task to start its execution while you continue planning.

        Args:
            task_id: The task to dispatch.
        """
        if ops.dispatch_fn is None:
            return "Error: dispatch not available (DAGRunner not connected)."
        return ops.dispatch_fn(task_id)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def dispatch_ready() -> str:
        """Dispatch all ready tasks for immediate execution without waiting for the current turn to end.

        Dispatches every pending task whose dependencies are all met and
        there is available worker capacity. Use after adding a batch of
        tasks to start execution while you continue planning.
        """
        if ops.dispatch_ready_fn is None:
            return "Error: dispatch not available (DAGRunner not connected)."
        n = ops.dispatch_ready_fn()
        if n == 0:
            return "No tasks dispatched (none ready or no available slots)."
        return f"Dispatched {n} task(s)."

    return server


def constrained_tracker_server(tracker: ItemTracker | ConstrainedTracker) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for the constrained task tracker."""
    ops = tracker if isinstance(tracker, ConstrainedTracker) else ConstrainedTracker(tracker)
    return MCPServerConfig(
        server_key="task-tracker",
        description="Track tasks: add, update, delete, dispatch, and query tasks with DAG dependencies",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_constrained_tracker_server(ops),
    )
