# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP manager — server configuration and multi-server client manager."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from mcp import types

if TYPE_CHECKING:
    from .client import FastMCPClient

logger = logging.getLogger(__name__)


class TransportMethod(Enum):
    """MCP transport methods.

    STREAMABLE_HTTP — HTTP transport using the MCP Streamable HTTP protocol.
        Suitable for remote servers accessible over HTTP/HTTPS. Supports
        bidirectional streaming. This is the default and most common remote transport.

    SSE — HTTP transport using Server-Sent Events.
        Legacy HTTP-based transport. Use streamable-http for new servers;
        SSE is supported for backward compatibility with older MCP servers.

    STDIO — Subprocess transport communicating over stdin/stdout.
        Launches a local command as a child process and exchanges MCP
        messages via its standard I/O streams. Use when the server is a
        local CLI tool.

    NPX — Subprocess transport that runs an npm package via npx.
        Convenience wrapper around stdio that invokes `npx <package>`.
        Use for MCP servers distributed as npm packages.

    INPROCESS — In-process transport using a FastMCP server object.
        No subprocess or network — calls go directly to a FastMCP server
        instance in the same Python process. Used for co-located tool servers.
    """

    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"
    STDIO = "stdio"
    NPX = "npx"
    INPROCESS = "inprocess"


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for an MCP server connection."""

    server_key: str
    description: str
    url: str = ""
    transport: TransportMethod = TransportMethod.STREAMABLE_HTTP
    auth_token: str | None = None
    headers: dict[str, str] | None = None
    command: tuple[str, ...] | None = None  # For stdio transport
    env: dict[str, str] | None = None  # Environment variables for stdio
    mcp_instance: Any = None  # FastMCP server instance for in-process transport
    reconnect: Callable[[], MCPServerConfig] | None = None


class MCPClientManager:
    """Multi-server MCP client manager.

    - Maintains one session per server
    - Discovers tools and maps tool_name -> server_key
    - Routes tool calls to the correct server
    """

    def __init__(self, server_configs: list[MCPServerConfig]):
        if not server_configs:
            raise ValueError("server_configs cannot be empty")

        self._server_configs = {cfg.server_key: cfg for cfg in server_configs}
        self._tool_to_server: dict[str, str] = {}
        self._server_to_tools: dict[str, set[str]] = {}
        self._tools_by_name: dict[str, types.Tool] = {}
        self._sessions: dict[str, FastMCPClient] = {}
        self._discovered = False

    async def discover_tools(self) -> dict[str, str]:
        """Discover tools from all configured servers.

        Returns:
            Mapping tool_name -> server_key
        """
        tool_map: dict[str, str] = {}
        server_to_tools: dict[str, set[str]] = {key: set() for key in self._server_configs}
        tools_by_name: dict[str, types.Tool] = {}

        for server_key in self._server_configs:
            try:
                client = await self._get_session(server_key)
            except Exception:
                logger.warning("Failed to connect to MCP server '%s', skipping", server_key, exc_info=True)
                continue
            tools = await client.list_tools()
            for tool in tools:
                name = str(tool.name)
                if name in tool_map:
                    raise ValueError(f"Duplicate tool '{name}' from server '{server_key}'.")
                tool_map[name] = server_key
                server_to_tools[server_key].add(name)
                tools_by_name[name] = tool

        self._tool_to_server = tool_map
        self._server_to_tools = server_to_tools
        self._tools_by_name = tools_by_name
        self._discovered = True
        return self._tool_to_server

    async def _ensure_discovered(self) -> None:
        """Call discover_tools() if not yet called."""
        if not self._discovered:
            await self.discover_tools()

    def _get_discovered_tools(self, *, tool_names: set[str] | None = None) -> list[types.Tool]:
        if tool_names is None:
            return [self._tools_by_name[name] for name in sorted(self._tools_by_name)]
        names = [name for name in sorted(tool_names) if name in self._tools_by_name]
        return [self._tools_by_name[name] for name in names]

    async def _get_session(self, server_key: str) -> FastMCPClient:
        if server_key in self._sessions:
            return self._sessions[server_key]

        from .client import FastMCPClient

        cfg = self._server_configs[server_key]
        client = FastMCPClient(
            url=cfg.url,
            transport=cfg.transport,
            auth_token=cfg.auth_token,
            headers=cfg.headers,
            command=list(cfg.command) if cfg.command else None,
            env=dict(cfg.env) if cfg.env else None,
            mcp_instance=cfg.mcp_instance,
        )
        await client.connect()
        self._sessions[server_key] = client
        return client

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_s: float | None = None,
    ) -> types.CallToolResult:
        await self._ensure_discovered()
        if tool_name not in self._tool_to_server:
            raise KeyError(f"Unknown tool '{tool_name}'")
        server_key = self._tool_to_server[tool_name]
        client = await self._get_session(server_key)
        try:
            return await client.call_tool(tool_name, arguments, timeout_s=timeout_s)
        except Exception:
            # Evict the session so the next call reconnects fresh.
            self._sessions.pop(server_key, None)
            try:
                await client.close()
            except Exception:
                pass

            # If the server provides a reconnect callback, use it to
            # restart the server and retry the call once.
            cfg = self._server_configs[server_key]
            if cfg.reconnect is not None:
                try:
                    new_cfg = await asyncio.to_thread(cfg.reconnect)
                    self._server_configs[server_key] = new_cfg
                    client = await self._get_session(server_key)
                    return await client.call_tool(tool_name, arguments, timeout_s=timeout_s)
                except Exception:
                    failed_session = self._sessions.pop(server_key, None)
                    if failed_session:
                        try:
                            await failed_session.close()
                        except Exception:
                            pass
                    raise

            raise

    async def close_all(self) -> None:
        for client in self._sessions.values():
            await client.close()
        self._sessions.clear()

    @property
    def tool_to_server(self) -> dict[str, str]:
        return self._tool_to_server

    @property
    def server_to_tools(self) -> dict[str, set[str]]:
        return self._server_to_tools

    @property
    def server_configs(self) -> dict[str, MCPServerConfig]:
        return self._server_configs

    @property
    def tools_by_name(self) -> dict[str, types.Tool]:
        return self._tools_by_name
