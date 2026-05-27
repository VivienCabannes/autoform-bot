# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for MCPClientManager.call_tool() — reconnect retry logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types

from core.mcp import MCPServerConfig, TransportMethod
from core.mcp.manager import MCPClientManager


def _make_tool_result(text: str = "ok") -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _make_manager(*, reconnect=None) -> MCPClientManager:
    """Create an MCPClientManager with a single fake server config."""
    cfg = MCPServerConfig(
        server_key="test-server",
        description="test server",
        transport=TransportMethod.STREAMABLE_HTTP,
        url="http://localhost:9999",
        reconnect=reconnect,
    )
    manager = MCPClientManager([cfg])
    # Mark as discovered with one tool mapped to our server.
    manager._discovered = True
    manager._tool_to_server = {"my_tool": "test-server"}
    manager._server_to_tools = {"test-server": {"my_tool"}}
    manager._tools_by_name = {"my_tool": MagicMock()}
    return manager


@pytest.mark.asyncio
async def test_call_tool_reconnect_retries_on_failure():
    """First call fails, reconnect provides new config, retry succeeds."""
    new_cfg = MCPServerConfig(
        server_key="test-server",
        description="test server",
        transport=TransportMethod.STREAMABLE_HTTP,
        url="http://localhost:8888",
    )
    reconnect = MagicMock(return_value=new_cfg)
    manager = _make_manager(reconnect=reconnect)

    failing_client = AsyncMock()
    failing_client.call_tool.side_effect = ConnectionError("server down")
    failing_client.close = AsyncMock()

    success_client = AsyncMock()
    success_client.call_tool.return_value = _make_tool_result("recovered")

    with patch.object(manager, "_get_session", side_effect=[failing_client, success_client]):
        result = await manager.call_tool("my_tool", {"code": "test"})

    reconnect.assert_called_once()
    assert result.content[0].text == "recovered"


@pytest.mark.asyncio
async def test_call_tool_no_reconnect_raises():
    """Without reconnect callback, failure propagates immediately."""
    manager = _make_manager(reconnect=None)

    failing_client = AsyncMock()
    failing_client.call_tool.side_effect = ConnectionError("server down")
    failing_client.close = AsyncMock()

    with patch.object(manager, "_get_session", return_value=failing_client):
        with pytest.raises(ConnectionError, match="server down"):
            await manager.call_tool("my_tool", {"code": "test"})


@pytest.mark.asyncio
async def test_call_tool_reconnect_failure_raises():
    """If reconnect itself fails, the error propagates."""
    reconnect = MagicMock(side_effect=RuntimeError("cannot restart"))
    manager = _make_manager(reconnect=reconnect)

    failing_client = AsyncMock()
    failing_client.call_tool.side_effect = ConnectionError("server down")
    failing_client.close = AsyncMock()

    with patch.object(manager, "_get_session", return_value=failing_client):
        with pytest.raises(RuntimeError, match="cannot restart"):
            await manager.call_tool("my_tool", {"code": "test"})


def test_reconnect_config_field_default_none():
    """MCPServerConfig.reconnect defaults to None."""
    cfg = MCPServerConfig(server_key="test", description="test")
    assert cfg.reconnect is None
