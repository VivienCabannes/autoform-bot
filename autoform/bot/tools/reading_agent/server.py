# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Reading agent MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import ReadingAgent


def reading_agent_server(allowed_dirs: tuple[str, ...]) -> tuple[MCPServerConfig, ReadingAgent]:
    """Create an MCP server that delegates file reading to a sub-agent.

    Returns the server config and the ReadingAgent instance.
    Set ``ops.trace_store`` on the returned instance to enable cost tracking.
    """
    ops = ReadingAgent(allowed_dirs)
    server = FastMCP(name="reading-agent")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    async def read_and_summarize(path: str, instructions: str = "") -> str:
        """Read a file using a lightweight reading agent and return a summary.

        The reading agent is a small, fast model (Haiku). Give it clear,
        specific instructions about what to look for — it works best with
        targeted questions rather than open-ended "summarize everything" requests.

        Use this instead of read_text_file for large files that would consume
        too much context.

        Args:
            path: Absolute path to the file to read.
            instructions: What to look for or extract from the file.
                Be specific — e.g. "find the definition of X" or "what imports are used".
                If empty, the agent returns a general summary.
        """
        return await ops.read_and_summarize(path, instructions)

    config = MCPServerConfig(
        server_key="reading-agent",
        description="Delegate file reading to a sub-agent that summarizes content",
        transport=TransportMethod.INPROCESS,
        mcp_instance=server,
    )
    return config, ops
