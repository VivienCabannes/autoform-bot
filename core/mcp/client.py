# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""FastMCPClient — thin async client for MCP using fastmcp.Client."""

import asyncio
import logging
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import (
    FastMCPTransport,
    NpxStdioTransport,
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)
from mcp import types

from .manager import TransportMethod

logger = logging.getLogger(__name__)


class FastMCPClient:
    """Thin async client for MCP using fastmcp.Client."""

    def __init__(
        self,
        *,
        url: str = "",
        transport: TransportMethod = TransportMethod.STREAMABLE_HTTP,
        auth_token: str | None = None,
        headers: dict[str, str] = {},
        command: list[str] | None = None,
        env: dict[str, str] | None = None,
        mcp_instance: Any = None,
    ):
        request_headers = dict(headers or {})
        if auth_token:
            request_headers["Authorization"] = f"Bearer {auth_token}"

        self._transport_type = transport
        self._command = command

        match transport:
            case TransportMethod.INPROCESS:
                if not mcp_instance:
                    raise ValueError("mcp_instance is required for inprocess transport")
                transport_obj = FastMCPTransport(mcp_instance)
            case TransportMethod.STDIO:
                if not command:
                    raise ValueError("command is required for stdio transport")
                transport_obj = StdioTransport(
                    command=command[0],
                    args=command[1:] if len(command) > 1 else [],
                    env=env,
                )
            case TransportMethod.NPX:
                if not command:
                    raise ValueError("command (package and args) is required for npx transport")
                transport_obj = NpxStdioTransport(
                    package=command[0],
                    args=command[1:] if len(command) > 1 else [],
                )
            case TransportMethod.STREAMABLE_HTTP:
                transport_obj = StreamableHttpTransport(
                    url=url,
                    headers=request_headers or None,
                )
            case TransportMethod.SSE:
                transport_obj = SSETransport(
                    url=url,
                    headers=request_headers or None,
                )

        self._client = Client(transport_obj)
        self._connect_lock = asyncio.Lock()
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            await self._client.__aenter__()
            self._connected = True

    async def close(self) -> None:
        if not self._connected:
            return
        try:
            await self._client.__aexit__(None, None, None)
        except (asyncio.CancelledError, Exception):
            logger.warning("Error during MCP client __aexit__", exc_info=True)
        finally:
            self._connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def list_tools(self) -> list[types.Tool]:
        await self.connect()
        tools_result = await self._client.list_tools_mcp()
        return tools_result.tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> types.CallToolResult:
        await self.connect()
        call = self._client.call_tool_mcp(tool_name, arguments)
        if timeout_s is None:
            return await call
        return await asyncio.wait_for(call, timeout=timeout_s)
