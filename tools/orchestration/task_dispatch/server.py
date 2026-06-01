"""Task dispatch MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import TaskDispatcher


def create_task_dispatch_server(dispatcher: TaskDispatcher) -> FastMCP:
    """Create in-process MCP tools for task dispatch.

    Args:
        dispatcher: TaskDispatcher instance (caller retains reference
            to read completed_results, running_tasks, done).
    """
    server = FastMCP(name=f"task-dispatch-{dispatcher.task_id}")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE)
    async def submit_task(prompt: str, num_agents: int = 1) -> str:
        """Launch agents to work on a task. Returns immediately."""
        return await dispatcher.submit_task(prompt, num_agents=num_agents)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def show_agents() -> str:
        """Show agent pool status: total, available, busy."""
        return dispatcher.show_agents()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def show_completed() -> str:
        """Show results of completed tasks."""
        return dispatcher.show_completed()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def mark_done() -> str:
        """Signal that this task is complete."""
        return dispatcher.mark_done()

    return server


def task_dispatch_server(dispatcher: TaskDispatcher) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for task dispatch."""
    mcp_instance = create_task_dispatch_server(dispatcher)
    return MCPServerConfig(
        server_key=f"task-dispatch-{dispatcher.task_id}",
        description="Task dispatch and result reporting",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
