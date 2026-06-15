"""Aristotle (Harmonic) MCP server — delegate formalization tasks to an autonomous prover.

Aristotle is not a chat LLM. It is an autonomous formal-reasoning agent that
takes a prompt (and optionally a Lean project directory), runs its own internal
tools (proof search, Lean builds, file edits), and returns finished Lean files
plus a natural-language summary.

This MCP server wraps ``aristotlelib`` to let any coding assistant delegate
formalization tasks to Aristotle as a tool call. The assistant submits a task,
polls for completion, retrieves results, and optionally steers a running task.

Dependency: ``aristotlelib`` (provided by the ``aristotle`` extra in pyproject.toml).
API key: ARISTOTLE_API_KEY env var (mint at https://aristotle.harmonic.fun/dashboard/keys)
"""

from __future__ import annotations

import os

from fastmcp.server import FastMCP

_NOT_IMPLEMENTED = "Not yet implemented. See examples/servers/aristotle/ for reference implementation."


def create_aristotle_server() -> FastMCP:
    """Create a FastMCP server for delegating formalization tasks to Aristotle."""
    server = FastMCP(name="autoform-aristotle")

    @server.tool
    def aristotle_submit(
        session_id: str,
        prompt: str,
        project_dir: str = "",
    ) -> str:
        """Submit a formalization task to Aristotle.

        Aristotle is an autonomous formal-reasoning agent. Give it a clear
        task description and it will search Mathlib, write Lean proofs, and
        return finished files. For follow-up turns on the same task, reuse
        the same session_id — Aristotle continues its server-side session.

        Args:
            session_id: Unique identifier for this task (e.g., "thm-2-3" or "convex-sets").
            prompt: The formalization task. Be specific: include the statement,
                    relevant definitions, and which file to write to.
            project_dir: Optional path to a Lean project directory. Aristotle
                         will use it as context (existing code, lakefile, etc.).
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def aristotle_wait(
        session_id: str,
        max_wait_seconds: float = 600,
    ) -> str:
        """Wait for an Aristotle task to complete and return the result.

        Polls until the task reaches a terminal status. Use this after
        aristotle_submit to block until Aristotle finishes.

        Args:
            session_id: The session to wait on.
            max_wait_seconds: Maximum time to wait (default: 10 minutes).
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def aristotle_poll(session_id: str) -> str:
        """Check the status of an Aristotle task without blocking.

        Use this for non-blocking status checks (e.g., while doing
        other work in parallel).

        Args:
            session_id: The session to check.
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def aristotle_steer(session_id: str, prompt: str) -> str:
        """Redirect a running Aristotle task with new instructions.

        Only works while the task is in-flight. Use this to correct
        Aristotle's approach or add constraints without restarting.

        Args:
            session_id: The session to steer.
            prompt: New instructions to inject (e.g., "Use Finset.sum_le_sum
                    instead of manual induction").
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def aristotle_events(session_id: str, limit: int = 20) -> str:
        """Fetch recent events from a running Aristotle task.

        Shows what Aristotle is doing: proof attempts, file edits,
        Lean builds, etc. Useful for monitoring progress.

        Args:
            session_id: The session to inspect.
            limit: Maximum number of events to return.
        """
        return _NOT_IMPLEMENTED

    @server.tool
    def aristotle_sessions() -> str:
        """List all active Aristotle sessions with their current status."""
        return _NOT_IMPLEMENTED

    return server


if __name__ == "__main__":
    server = create_aristotle_server()
    server.run(transport="stdio")
