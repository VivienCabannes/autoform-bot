# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""MCP tool servers for escalation — write (workers) and read (trace analyzer)."""

from __future__ import annotations

from pathlib import Path

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import EscalationLogger, EscalationReader


def _create_escalate_server(logger: EscalationLogger, agent_id: str) -> FastMCP:
    server = FastMCP(name="escalate")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def escalate(severity: str, message: str) -> str:
        """Escalate a critical issue to the human operator.

        Use this ONLY for situations that require human attention:
        - Infrastructure failures that block the entire pipeline
        - Unreasonable or contradictory constraints that prevent progress
        - Systemic errors that affect multiple tasks and cannot be resolved autonomously

        Do NOT use for routine failures, partial progress, or recoverable errors.

        Args:
            severity: One of "critical", "warning", or "decomposition".
                      critical      — pipeline is blocked, cannot continue.
                      warning       — can continue but outcome may be compromised.
                      decomposition — task should be split into sub-tasks (include concrete proposal).
            message: Clear description of the issue, what was tried, and why
                     human intervention is needed.
        """
        if severity not in ("critical", "warning", "decomposition"):
            return "Error: severity must be 'critical', 'warning', or 'decomposition'."

        logger.log(severity, message, agent_id)
        return f"Escalation recorded ({severity})."

    return server


def escalate_server(run_path: Path, agent_id: str) -> tuple[MCPServerConfig, EscalationLogger]:
    """Create an inprocess MCPServerConfig for the escalation tool.

    Returns the config and the underlying logger so callers can set
    ``logger.task_id`` at dispatch time.
    """
    logger = EscalationLogger(run_path / "escalations.jsonl")
    config = MCPServerConfig(
        server_key="escalate",
        description="Escalate critical issues to the human operator",
        transport=TransportMethod.INPROCESS,
        mcp_instance=_create_escalate_server(logger, agent_id),
    )
    return config, logger


def _create_escalation_reader_server(reader: EscalationReader) -> FastMCP:
    server = FastMCP(name="escalation-reader")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_escalations() -> str:
        """Get all escalations raised by workers on this task.

        Returns escalations logged by any agent that worked on this task,
        across all attempts. Includes severity, agent ID, timestamp, and message.
        """
        return reader.get_escalations()

    return server


def escalation_reader_server(
    run_path: Path,
    traces_dir: Path,
    task_id: str,
) -> MCPServerConfig:
    """Create an inprocess MCPServerConfig for reading task escalations."""
    reader = EscalationReader(
        escalations_path=run_path / "escalations.jsonl",
        traces_dir=traces_dir,
        task_id=task_id,
    )
    return MCPServerConfig(
        server_key="escalation-reader",
        description="Read escalations raised by workers on this task",
        transport=TransportMethod.INPROCESS,
        mcp_instance=_create_escalation_reader_server(reader),
    )
