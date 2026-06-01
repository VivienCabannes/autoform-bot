"""Google Chat MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import GChatClient


def create_gchat_server(
    *,
    base_url: str = "http://localhost:8000",
    secret: str = "",
    timeout_s: int = 30,
) -> FastMCP:
    """Create an MCP server with Google Chat tools."""
    client = GChatClient(base_url=base_url, secret=secret, timeout_s=timeout_s)
    server = FastMCP(name="gchat")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def gchat_list_spaces(limit: int = 20) -> str:
        """List Google Chat spaces you have access to.

        Args:
            limit: Maximum number of spaces to return.
        """
        return await client.list_spaces(limit=limit)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def gchat_list_messages(space: str, limit: int = 25) -> str:
        """List recent messages from a Google Chat space.

        Args:
            space: Space resource name (e.g. 'spaces/AAQAH-isw2k').
            limit: Maximum number of messages to return.
        """
        return await client.list_messages(space, limit=limit)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    async def gchat_send_message(space: str, message: str) -> str:
        """Send a message to a Google Chat space.

        Args:
            space: Space resource name (e.g. 'spaces/AAQAH-isw2k').
            message: The message text to send.
        """
        return await client.send_message(space, message)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def gchat_get_message(message_name: str) -> str:
        """Get a specific message by its resource name.

        Args:
            message_name: Full message resource name (e.g. 'spaces/XXXX/messages/YYYY').
        """
        return await client.get_message(message_name)

    return server


def gchat_server(
    *,
    base_url: str = "",
    secret: str = "",
) -> MCPServerConfig:
    """Create a Google Chat MCP server config.

    Reads from environment variables if not provided:
        GCHAT_BRIDGE_URL — defaults to http://localhost:8000
        GCHAT_BRIDGE_SECRET — bearer token for the bridge
    """
    if not base_url:
        base_url = os.environ.get("GCHAT_BRIDGE_URL", "http://localhost:8000")
    if not secret:
        secret = os.environ.get("GCHAT_BRIDGE_SECRET", "")

    mcp_instance = create_gchat_server(base_url=base_url, secret=secret)
    return MCPServerConfig(
        server_key="gchat",
        description="Google Chat messaging",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
