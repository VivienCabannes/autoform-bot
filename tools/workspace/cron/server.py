"""Cron MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import CronScheduler


def create_cron_server(scheduler: CronScheduler | None = None) -> FastMCP:
    """Create an MCP server with cron scheduling tools.

    Args:
        scheduler: Optional CronScheduler instance. If None, a new one is created.

    Returns:
        FastMCP server with cron tools.
    """
    if scheduler is None:
        scheduler = CronScheduler()

    server = FastMCP(name="cron")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def cron_create(cron: str, prompt: str, recurring: bool = True) -> str:
        """Schedule a prompt on a cron schedule.

        Args:
            cron: 5-field cron expression (minute hour dom month dow).
            prompt: The prompt to enqueue at each fire time.
            recurring: If True, fires on every match. If False, fires once then deletes.
        """
        return scheduler.create(cron, prompt, recurring=recurring)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def cron_delete(job_id: str) -> str:
        """Delete a scheduled cron job."""
        return scheduler.delete(job_id)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def cron_list() -> str:
        """List all scheduled cron jobs."""
        return scheduler.list_jobs()

    return server


def cron_server(scheduler: CronScheduler | None = None) -> MCPServerConfig:
    """Create a cron MCP server config."""
    if scheduler is None:
        scheduler = CronScheduler()
    return MCPServerConfig(
        server_key="cron",
        description="Scheduled task execution via cron expressions",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_cron_server(scheduler),
    )
