"""Lean REPL MCP server — run Lean code and check compilation."""

from __future__ import annotations

import json
import os

from fastmcp.server import FastMCP

from .core import format_repl_response
from .pool import LeanReplPool, LeanReplPoolConfig


def create_repl_server(pool: LeanReplPool) -> FastMCP:
    """Create a FastMCP server wrapping a LeanReplPool.

    Exposes two tools:
    - run_lean_code: Send Lean code to the REPL pool
    - get_repl_status: Check pool health and memory usage
    """
    server = FastMCP(name="autoform-repl")

    @server.tool
    def run_lean_code(code: str, timeout: float | None = None) -> str:
        """Compile Lean code against a preloaded Mathlib environment.

        Each pooled REPL has already run ``import Mathlib``. Submitted
        ``import`` lines are stripped and the remaining code is compiled
        against that preloaded environment, so imports cost nothing —
        but only imports the preloaded environment transitively provides
        (Mathlib and its dependencies, e.g. Aesop, Batteries) are
        available. Any other import returns an error instead of being
        silently ignored.

        Args:
            code: Lean code to execute (optional imports + body).
            timeout: Optional timeout in seconds (overrides the default).

        Returns:
            Formatted diagnostic output: compilation status, errors,
            sorries with goals, and warnings.
        """
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = pool.run(code, **kwargs)
        return format_repl_response(result)

    @server.tool
    def get_repl_status() -> str:
        """Check the REPL pool's health and memory usage.

        Returns:
            JSON string with capacity, memory_usage_gb, and shutdown status.
        """
        return json.dumps(
            {
                "capacity": pool.capacity,
                "memory_usage_gb": round(pool.get_memory_usage(), 2),
                "shutdown": pool._shutdown,
            }
        )

    return server


if __name__ == "__main__":
    cwd = os.environ.get("LEAN_PROJECT_DIR", ".")
    repl_cmd = os.environ.get("LEAN_REPL_CMD", "lake exe repl").split()
    num_repls = int(os.environ.get("LEAN_NUM_REPLS", "0")) or None

    config = LeanReplPoolConfig(cwd=cwd, repl_command=repl_cmd, num_repls=num_repls)
    pool = LeanReplPool(config)

    try:
        server = create_repl_server(pool)
        server.run(transport="stdio")
    finally:
        pool.shutdown()
