"""Orchestration MCP tool servers."""

from __future__ import annotations

from core.mcp import MCPServerConfig, TransportMethod


def inprocess_server(mcp_instance) -> MCPServerConfig:
    """Server config for an in-process MCP server.

    Args:
        mcp_instance: A FastMCP server instance.
    """
    return MCPServerConfig(
        server_key="inprocess",
        description="Sub-agent spawning and orchestration",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
