"""Lean REPL MCP server — run Lean code and check compilation."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

from fastmcp.server import FastMCP

from servers import resolve_lean_project_dir

from .core import format_repl_response
from .pool import LeanReplPool, LeanReplPoolConfig


class LeanReplProjects:
    """Lazily keep one REPL pool per explicit Lean project."""

    def __init__(self, pool_factory: Callable[[Path], LeanReplPool]) -> None:
        self._pool_factory = pool_factory
        self._pools: dict[Path, LeanReplPool] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, project_dir: str) -> LeanReplPool:
        """Return the pool for a validated absolute Lake project."""
        root = resolve_lean_project_dir(project_dir)
        with self._lock:
            if self._closed:
                raise RuntimeError("Lean REPL project router is closed")
            pool = self._pools.get(root)
            if pool is None:
                pool = self._pool_factory(root)
                self._pools[root] = pool
            return pool

    def shutdown(self) -> None:
        """Shut down all pools created by this router."""
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
            self._closed = True
        for pool in pools:
            pool.shutdown()


def create_repl_server(projects: LeanReplProjects) -> FastMCP:
    """Create a FastMCP server routing calls to explicit Lean projects.

    Exposes two tools:
    - run_lean_code: Send Lean code to the REPL pool
    - get_repl_status: Check pool health and memory usage
    """
    server = FastMCP(name="autoform-repl")

    @server.tool
    def run_lean_code(project_dir: str, code: str, timeout: float | None = None) -> str:
        """Send Lean code to the REPL and return formatted diagnostics.

        Imports are cached automatically — repeated calls with the same
        imports reuse the cached environment for speed.

        Args:
            project_dir: Absolute path to the Lake project whose environment should be used.
            code: Lean code to execute (imports + body).
            timeout: Optional timeout in seconds (overrides the default).

        Returns:
            Formatted diagnostic output: compilation status, errors,
            sorries with goals, and warnings.
        """
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = projects.get(project_dir).run(code, **kwargs)
        return format_repl_response(result)

    @server.tool
    def get_repl_status(project_dir: str) -> str:
        """Check the REPL pool's health and memory usage.

        Args:
            project_dir: Absolute path to the Lake project whose pool should be inspected.

        Returns:
            JSON string with capacity, memory_usage_gb, and shutdown status.
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


if __name__ == "__main__":
    repl_cmd = os.environ.get("LEAN_REPL_CMD", "lake exe repl").split()
    num_repls = int(os.environ.get("LEAN_NUM_REPLS", "0")) or None

    def create_pool(project_dir: Path) -> LeanReplPool:
        config = LeanReplPoolConfig(cwd=str(project_dir), repl_command=repl_cmd, num_repls=num_repls)
        return LeanReplPool(config)

    projects = LeanReplProjects(create_pool)

    try:
        server = create_repl_server(projects)
        server.run(transport="stdio")
    finally:
        projects.shutdown()
