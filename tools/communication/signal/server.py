"""Signal Messenger MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import SignalClient


def create_signal_server(
    *,
    base_url: str = "http://localhost:9922",
    sender_number: str = "",
    allowed_group_names: list[str] | None = None,
    timeout_s: int = 30,
) -> FastMCP:
    """Create an MCP server with Signal Messenger tools."""
    client = SignalClient(
        base_url=base_url,
        sender_number=sender_number,
        allowed_group_names=allowed_group_names,
        timeout_s=timeout_s,
    )
    server = FastMCP(name="signal")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def signal_list_groups() -> str:
        """List Signal groups available for messaging.

        Returns group names that this agent can send messages to.
        """
        return await client.list_groups()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    async def signal_send_message(group_name: str, message: str) -> str:
        """Send a text message to a Signal group.

        Args:
            group_name: The group name to send to (from signal_list_groups).
            message: The message text to send.
        """
        return await client.send_message(group_name, message)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def signal_receive_messages() -> str:
        """Fetch pending incoming messages from Signal.

        Returns messages with sender, timestamp, and group info.
        Note: calling this consumes the messages from the server.
        """
        return await client.receive_messages()

    return server


def signal_server(
    *,
    base_url: str = "",
    sender_number: str = "",
    allowed_group_names: list[str] | None = None,
) -> MCPServerConfig:
    """Create a Signal MCP server config.

    Reads from environment variables if not provided:
        SIGNAL_API_URL — defaults to http://localhost:9922
        SIGNAL_SENDER_NUMBER — your Signal number in international format
        SIGNAL_ALLOWED_GROUPS — comma-separated list of allowed group names
    """
    if not base_url:
        base_url = os.environ.get("SIGNAL_API_URL", "http://localhost:9922")
    if not sender_number:
        sender_number = os.environ.get("SIGNAL_SENDER_NUMBER", "")
    if allowed_group_names is None:
        env_groups = os.environ.get("SIGNAL_ALLOWED_GROUPS", "")
        allowed_group_names = [g.strip() for g in env_groups.split(",") if g.strip()] if env_groups else []

    mcp_instance = create_signal_server(
        base_url=base_url,
        sender_number=sender_number,
        allowed_group_names=allowed_group_names,
    )
    return MCPServerConfig(
        server_key="signal",
        description="Signal messaging",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
