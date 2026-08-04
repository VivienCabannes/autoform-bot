"""Lean REPL MCP server."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from fastmcp.server import FastMCP

from .core import format_repl_response
from .pool import LeanReplPool, LeanReplPoolConfig
from .projects import LeanReplProjects


def create_repl_server(projects: LeanReplProjects) -> FastMCP:
    """Create the REPL MCP server for explicit Lean projects."""
    server = FastMCP(name="autoform-repl")

    @server.tool
    def run_lean_code(project_dir: str, code: str, timeout: float | None = None) -> str:
        """Compile a Lean snippet in a project's persistent REPL.

        Args:
            project_dir: Absolute path to the Lake project root.
            code: Lean code to execute.
            timeout: Optional timeout in seconds.
        """
        kwargs = {} if timeout is None else {"timeout": timeout}
        return format_repl_response(projects.get(project_dir).run(code, **kwargs))

    @server.tool
    def get_repl_status(project_dir: str) -> str:
        """Return pool capacity, memory use, and shutdown state.

        Args:
            project_dir: Absolute path to the Lake project root.
        """
        pool = projects.get(project_dir)
        return json.dumps(
            {
                "capacity": pool.capacity,
                "memory_usage_gb": round(pool.get_memory_usage(), 2),
                "shutdown": pool._shutdown,
            }
        )

    return server


def main() -> None:
    command = shlex.split(os.environ.get("LEAN_REPL_CMD", "lake exe repl"))
    num_repls = int(os.environ.get("LEAN_NUM_REPLS", "0")) or None

    def create_pool(project_dir: Path) -> LeanReplPool:
        config = LeanReplPoolConfig(
            cwd=str(project_dir),
            repl_command=command,
            num_repls=num_repls,
        )
        return LeanReplPool(config)

    projects = LeanReplProjects(create_pool)
    try:
        create_repl_server(projects).run(transport="stdio")
    finally:
        projects.shutdown()


if __name__ == "__main__":
    main()
