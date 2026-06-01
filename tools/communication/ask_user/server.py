"""Ask user MCP server — FastMCP tool definition and config factory."""

from __future__ import annotations

import asyncio
from typing import Callable

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import UserInteraction


def create_ask_user_server(
    *,
    interaction_handler: Callable[[dict], str] | None = None,
) -> FastMCP:
    """Create an MCP server with user interaction tools.

    Args:
        interaction_handler: Optional custom handler for user interactions.
            If None, uses stdin/stdout for CLI interaction.
    """
    ops = UserInteraction(handler=interaction_handler)
    server = FastMCP(name="ask-user")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def ask_user_question(
        question: str,
        options: str | list[dict] = "",
        multi_select: bool = False,
    ) -> str:
        """Ask the user a question and get their response.

        Args:
            question: The question to ask.
            options: Option objects as a list of dicts or a JSON string encoding
                one. Each object must have a "label" key (displayed and returned
                as the answer) and may have an optional "description" key (extra
                context shown to the user).
                Example: [{"label": "Option A", "description": "Does X"}, {"label": "Option B"}]
            multi_select: If True, allow selecting multiple options.
        """
        return await asyncio.to_thread(ops.ask, question, options=options, multi_select=multi_select)

    return server


def ask_user_server(
    *,
    interaction_handler: Callable[[dict], str] | None = None,
) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for user interaction."""
    mcp_instance = create_ask_user_server(interaction_handler=interaction_handler)
    return MCPServerConfig(
        server_key="ask-user",
        description="Interactive user communication and question asking",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
