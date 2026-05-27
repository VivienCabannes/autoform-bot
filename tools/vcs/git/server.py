# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Git MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import GitOps


@dataclass(frozen=True)
class GitConfig:
    """Configuration for the git tool."""

    repo_root: str


def create_git_server(repo_dir: str) -> FastMCP:
    """Create a FastMCP server with git tools."""
    ops = GitOps(repo_dir)
    server = FastMCP(name="git")

    # Read operations

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_status() -> str:
        """Show the working tree status (modified, staged, untracked files)."""
        return ops.status()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_diff(ref: str = "") -> str:
        """Show changes in the working tree or between refs.

        Args:
            ref: Optional ref to diff against (e.g. 'HEAD', 'main', 'HEAD~3').
                 Empty string shows unstaged changes.
        """
        return ops.diff(ref=ref)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_log(max_count: int = 20, oneline: bool = True) -> str:
        """Show recent commit history.

        Args:
            max_count: Number of commits to show.
            oneline: If True, show one line per commit.
        """
        return ops.log(max_count=max_count, oneline=oneline)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_show(ref: str = "HEAD") -> str:
        """Show the contents of a commit.

        Args:
            ref: Commit ref to show (default: HEAD).
        """
        return ops.show(ref=ref)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_branch() -> str:
        """List all branches and show the current branch."""
        return ops.branch()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_show_file(path: str, ref: str = "HEAD") -> str:
        """Show a file's contents at a specific git ref.

        Args:
            path: File path relative to the repo root.
            ref: Git ref (branch, tag, or commit). Default: HEAD.
        """
        return ops.show_file(path, ref=ref)

    # Write operations

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_add(paths: str = ".") -> str:
        """Stage files for commit.

        Args:
            paths: Space-separated file paths to stage, or '.' for all.
        """
        return ops.add(paths=paths)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_commit(message: str) -> str:
        """Create a commit with the staged changes.

        Args:
            message: Commit message.
        """
        return ops.commit(message)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_checkout(ref: str) -> str:
        """Switch branches or restore working tree files.

        Args:
            ref: Branch name, tag, or commit to checkout.
        """
        return ops.checkout(ref)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_restore(paths: str, staged: bool = False) -> str:
        """Restore working tree files.

        Args:
            paths: Space-separated file paths to restore.
            staged: If True, unstage files (restore from HEAD to index).
        """
        return ops.restore(paths, staged=staged)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_reset(ref: str = "HEAD", paths: str = "") -> str:
        """Reset current HEAD to the specified state.

        Args:
            ref: Ref to reset to (default: HEAD).
            paths: Optional space-separated paths to reset.
        """
        return ops.reset(ref=ref, paths=paths)

    # Rebase workflow

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_rebase(branch: str = "main") -> str:
        """Rebase current branch onto another branch.

        Args:
            branch: Branch to rebase onto (default: main).
        """
        return ops.rebase(branch=branch)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_rebase_continue() -> str:
        """Continue a paused rebase after resolving conflicts."""
        return ops.rebase_continue()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_rebase_abort() -> str:
        """Abort a rebase in progress and return to original state."""
        return ops.rebase_abort()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def git_rebase_skip() -> str:
        """Skip the current commit during a rebase."""
        return ops.rebase_skip()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def git_conflicts() -> str:
        """Show conflict markers and their line numbers."""
        return ops.conflicts()

    return server


def git_server(config: GitConfig) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for git operations."""
    mcp_instance = create_git_server(config.repo_root)
    return MCPServerConfig(
        server_key="git",
        description="Git version control operations",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
