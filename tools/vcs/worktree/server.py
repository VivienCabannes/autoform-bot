"""Worktree MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import WorktreeSession


def create_worktree_server(repo_root: str) -> FastMCP:
    """Create an MCP server with worktree management tools."""
    session = WorktreeSession(repo_root)
    server = FastMCP(name="worktree")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def worktree_create(branch_name: str) -> str:
        """Create a new git worktree with a detached HEAD from main."""
        return session.create(branch_name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def worktree_sync(branch_name: str) -> str:
        """Reset a worktree to the latest main (before starting work)."""
        return session.sync(branch_name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def worktree_has_commits(branch_name: str) -> str:
        """Check if a worktree has commits beyond main."""
        return session.check_commits(branch_name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def worktree_merge(branch_name: str) -> str:
        """Merge a worktree's changes into main."""
        return session.merge(branch_name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def worktree_cleanup(branch_name: str) -> str:
        """Remove a worktree."""
        return session.cleanup(branch_name)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def worktree_list() -> str:
        """List all active worktrees tracked in this session."""
        return session.list_active()

    return server


def worktree_server(repo_root: str) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for worktree management."""
    mcp_instance = create_worktree_server(repo_root)
    return MCPServerConfig(
        server_key="worktree",
        description="Git worktree creation and management",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
