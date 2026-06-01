"""ItemTracker MCP server — exposes item tracking as agent tools."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from core.tracker import ItemStatus, ItemTracker


def create_tracker_server(
    tracker: ItemTracker,
    extra_tools: list[Callable[[FastMCP], None]] | None = None,
) -> FastMCP:
    """Create an inprocess FastMCP server wrapping an ItemTracker.

    Tools are always named ``list_items``, ``get_item``, etc. Use the
    ``flavor`` parameter on ``list_items`` and ``add_item`` to distinguish
    item types within a single tracker.

    Args:
        tracker: The backing ItemTracker instance.
        extra_tools: Optional list of callables that receive the FastMCP
            server and register additional domain-specific tools on it.
    """
    server = FastMCP(name=f"{tracker.default_flavor}-tracker")

    statuses_str = ", ".join(ItemStatus)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_items(
        status: str | None = None,
        flavor: str | None = None,
        query: str | None = None,
        ready_only: bool = False,
    ) -> str:
        """List all items with their status and dependencies.

        Returns a compact view (no descriptions). Use ``get_item``
        for full details on a specific item.

        Args:
            status: Filter by status. Returns all if omitted.
            flavor: Filter by item type (e.g. "task", "statement").
            query: Text search in item ID, title, and description (case-insensitive).
            ready_only: If true, only return items whose dependencies are all satisfied.
        """
        if ready_only:
            items = tracker.ready()
            if flavor is not None:
                items = [i for i in items if i.get("flavor") == flavor]
            if query is not None:
                q = query.lower()
                items = [i for i in items if q in f"{i['id']} {i.get('title', '')} {i.get('description', '')}".lower()]
        else:
            items = tracker.list(status=status, flavor=flavor, query=query)
        if not items:
            return "No items found."
        compact = [{k: v for k, v in item.items() if k != "description"} for item in items]
        return json.dumps(compact, indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_item(item_id: str) -> str:
        """Get the full details for a specific item.

        Args:
            item_id: The item ID to inspect.
        """
        item = tracker.get(item_id)
        if item is None:
            return f"Error: item {item_id} not found."
        return json.dumps(item, indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def add_item(
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
        metadata: dict | None = None,
        item_id: str | None = None,
        owner: str | None = None,
        flavor: str | None = None,
    ) -> str:
        """Add a new item to the tracker.

        Dependencies must already exist — add parent items first.

        Args:
            title: Short summary of the item.
            description: Full details or instructions.
            depends_on: IDs of items that must complete before this one.
            metadata: Optional key-value pairs.
            item_id: Explicit ID slug. Auto-increments if omitted.
            owner: Agent identifier to assign ownership.
            flavor: Item type ("task", "issue", "goal"). Defaults to tracker default.
        """
        try:
            new_id = tracker.add(
                title,
                description,
                depends_on,
                metadata=metadata,
                item_id=item_id,
                owner=owner,
                flavor=flavor,
            )
        except ValueError as e:
            return f"Error: {e}"
        return f"Item {new_id} added: {title}"

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    def update_item(
        item_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        depends_on: list[str] | None = None,
        metadata: dict | None = None,
        owner: str | None = None,
    ) -> str:
        f"""Update an existing item.

        Combines field updates, status transitions, and claiming:
        - Set ``status`` to change the item's status ({statuses_str}).
        - Set ``owner`` on an unowned item to claim it (auto-transitions
          to in_progress).

        Args:
            item_id: The item to update.
            title: New title (optional).
            description: New description (optional).
            status: New status (optional).
            depends_on: New dependency list (optional, replaces existing).
            metadata: Fields to merge into metadata (optional).
            owner: Set or change the owner (optional).
        """
        updates = {
            "title": title,
            "description": description,
            "status": status,
            "depends_on": depends_on,
            "metadata": metadata,
            "owner": owner,
        }
        # Filter out None to avoid accidentally clearing fields like 'owner'
        # which use a sentinel in the underlying tracker.
        filtered_updates = {k: v for k, v in updates.items() if v is not None}
        return tracker.update(item_id, **filtered_updates)

    if extra_tools:
        for register_tool in extra_tools:
            register_tool(server)

    return server


def tracker_server(
    tracker: ItemTracker,
    extra_tools: list[Callable[[FastMCP], None]] | None = None,
) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for item tracking tools."""
    flavor = tracker.default_flavor
    return MCPServerConfig(
        server_key=f"{flavor}-tracker",
        description=f"Track {flavor}s: add, update, remove, and query items with DAG dependencies",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_tracker_server(tracker, extra_tools=extra_tools),
    )
