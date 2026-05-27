# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for core.mcp module."""

from core.mcp import MCPServerConfig, TransportMethod, parse_tool_call_arguments


def test_server_config_creation():
    config = MCPServerConfig(
        server_key="test",
        description="test",
        transport=TransportMethod.STDIO,
        command=("echo", "hello"),
    )
    assert config.server_key == "test"
    assert config.transport == TransportMethod.STDIO
    assert config.command == ("echo", "hello")


def test_parse_tool_call_arguments():
    assert parse_tool_call_arguments(None) == {}
    assert parse_tool_call_arguments("") == {}
    assert parse_tool_call_arguments('{"key": "value"}') == {"key": "value"}
    assert parse_tool_call_arguments("not json")["_error"] == "invalid_json_arguments"
    assert parse_tool_call_arguments('"just a string"')["_error"] == "arguments_not_object"


def test_server_config_frozen():
    """MCPServerConfig is frozen (immutable)."""
    config = MCPServerConfig(server_key="test", description="test")
    try:
        config.server_key = "modified"
        assert False, "Should have raised"
    except AttributeError:
        pass
