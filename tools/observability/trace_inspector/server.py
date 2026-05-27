# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Trace inspector MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from pathlib import Path

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import TraceInspector


def create_trace_inspector_server(inspector: TraceInspector) -> FastMCP:
    """Create an inprocess FastMCP server wrapping a TraceInspector."""
    server = FastMCP(name="trace-inspector")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_attempts() -> str:
        """List all attempts for this task with status, winner, and step counts."""
        return inspector.list_attempts()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_step_timeline(attempt_number: int | None = None) -> str:
        """Ordered build/rebase/merge/review steps for an attempt (default: latest).

        Args:
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_step_timeline(attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_build_errors(attempt_number: int | None = None) -> str:
        """All failed build steps with error text (default: latest attempt).

        Args:
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_build_errors(attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_review_feedback(attempt_number: int | None = None) -> str:
        """All review rejections with feedback text (default: latest attempt).

        Args:
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_review_feedback(attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_agents(attempt_number: int | None = None) -> str:
        """List agent IDs that ran in an attempt (default: latest).

        Args:
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.list_agents(attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_agent_stats(agent_id: str, attempt_number: int | None = None) -> str:
        """Summary stats for one agent: turns, tool counts, cost (default: latest attempt).

        Args:
            agent_id: Agent ID from list_agents.
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_agent_stats(agent_id, attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_tool_stats(agent_id: str, attempt_number: int | None = None) -> str:
        """Per-tool breakdown: count, success/fail, avg duration (default: latest attempt).

        Args:
            agent_id: Agent ID from list_agents.
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_tool_stats(agent_id, attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_failed_tools(agent_id: str, attempt_number: int | None = None) -> str:
        """All failed tool calls with error messages (default: latest attempt).

        Args:
            agent_id: Agent ID from list_agents.
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_failed_tools(agent_id, attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_messages(agent_id: str, last_n: int = 10, offset: int = 0, attempt_number: int | None = None) -> str:
        """Messages from the agent's conversation (default: latest attempt).

        Returns all roles (user, assistant, tool results) for full context.
        Use offset to paginate from the end (offset=0 is most recent).

        Args:
            agent_id: Agent ID from list_agents.
            last_n: Number of messages to return (default 10).
            offset: Skip this many messages from the end (default 0).
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_messages(agent_id, last_n, offset, attempt_number)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_tool_call(agent_id: str, call_index: int, attempt_number: int | None = None) -> str:
        """Full arguments and result of one tool call by index (default: latest attempt).

        Use get_tool_stats to find call indexes.

        Args:
            agent_id: Agent ID from list_agents.
            call_index: 0-based index from the tool call list.
            attempt_number: Attempt to inspect. Defaults to latest.
        """
        return inspector.get_tool_call(agent_id, call_index, attempt_number)

    return server


def trace_inspector_server(
    traces_dir: Path | str,
    task_id: str,
) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for trace inspection of a single task.

    Args:
        traces_dir: Root traces directory containing per-task subdirectories.
        task_id: The task whose attempts to inspect.
    """
    inspector = TraceInspector(traces_dir, task_id)
    return MCPServerConfig(
        server_key="trace-inspector",
        description="Inspect and analyze agent execution traces",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_trace_inspector_server(inspector),
    )
