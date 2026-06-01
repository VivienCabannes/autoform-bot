"""Zulip MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import ZulipClient


def create_zulip_server(*, config_file: str) -> FastMCP:
    """Create an MCP server with Zulip tools."""
    client = ZulipClient(config_file=config_file)
    server = FastMCP(name="zulip")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def zulip_list_streams(subscribed_only: bool = False) -> str:
        """List Zulip streams (channels).

        Args:
            subscribed_only: If True, list only streams the user is subscribed to.
                Defaults to False (all public streams).
        """
        return client.list_streams(subscribed_only=subscribed_only)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def zulip_get_topics(stream: str) -> str:
        """List topics in a Zulip stream.

        Args:
            stream: Stream name (from zulip_list_streams).
        """
        return client.get_topics(stream)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def zulip_get_messages(stream: str, topic: str = "", limit: int = 20) -> str:
        """Fetch recent messages from a Zulip stream, optionally filtered by topic.

        Args:
            stream: Stream name.
            topic: Optional topic name to narrow results.
            limit: Maximum number of messages to return (default 20).
        """
        return client.get_messages(stream, topic=topic, limit=limit)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def zulip_search_messages(query: str, limit: int = 20) -> str:
        """Search Zulip messages by keyword.

        Args:
            query: Search terms.
            limit: Maximum number of results (default 20).
        """
        return client.search_messages(query, limit=limit)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def zulip_get_direct_messages(limit: int = 20) -> str:
        """Fetch recent direct (private) messages.

        Args:
            limit: Maximum number of messages to return (default 20).
        """
        return client.get_direct_messages(limit=limit)

    # @server.tool
    # @ToolSpec.define(autonomy=Autonomy.WRITE)
    # async def zulip_send_message(stream: str, topic: str, content: str) -> str:
    #     """Send a message to a Zulip stream and topic.
    #
    #     Args:
    #         stream: Stream name to post to.
    #         topic: Topic name within the stream.
    #         content: Message content (Zulip markdown supported).
    #     """
    #     return client.send_message(stream, topic=topic, content=content)

    return server


def zulip_server(*, config_file: str = "") -> MCPServerConfig:
    """Create a Zulip MCP server config.

    Reads config file path from environment if not provided:
        ZULIP_RC_PATH — path to zuliprc file (defaults to ~/.zuliprc)
    """
    if not config_file:
        config_file = os.environ.get("ZULIP_RC_PATH", os.path.expanduser("~/.zuliprc"))

    mcp_instance = create_zulip_server(config_file=config_file)
    return MCPServerConfig(
        server_key="zulip",
        description="Zulip messaging",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
