"""Aristotle (Harmonic) MCP server — delegate formalization tasks to an autonomous prover.

Aristotle is not a chat LLM. It is an autonomous formal-reasoning agent that
takes a prompt (and optionally a Lean project directory), runs its own internal
tools (proof search, Lean builds, file edits), and returns finished Lean files
plus a natural-language summary.

This MCP server wraps ``aristotlelib`` to let any coding assistant delegate
formalization tasks to Aristotle as a tool call. The assistant submits a task,
polls for completion, retrieves results, and optionally steers a running task.
The ``aristotle_delegate_node`` tool is the **prover-backend entry** — it takes a
plan node + its spec and writes the proof back into the node (see core.py).

HARD CONSTRAINT: Aristotle ONLY produces a proof into a node; the landed proof
feeds the SAME jury / ``review_status.json`` / review surface as the in-session
worker. This server never reviews, scores, or touches the sidecar.

Aristotle is a **FREE** external API. It is OPT-IN and default-off: it needs the
``aristotle`` extra (``aristotlelib``) installed, plus ``ARISTOTLE_API_KEY`` and
network access. ``aristotlelib`` is imported lazily, so this module — and the
``create_aristotle_server()`` factory — import cleanly without the extra.

Dependency: ``aristotlelib`` (the ``aristotle`` extra in pyproject.toml).
API key: ARISTOTLE_API_KEY env var (mint at https://aristotle.harmonic.fun/dashboard/keys)
"""

from __future__ import annotations

import asyncio
import json
import os

from fastmcp.server import FastMCP

from .core import AristotleManager, delegate_to_node


def create_aristotle_server(manager: AristotleManager | None = None) -> FastMCP:
    """Create a FastMCP server for delegating formalization tasks to Aristotle.

    Args:
        manager: An :class:`AristotleManager`. When ``None`` (the default, used by
            the import smoke test and the stdio entrypoint), one is constructed
            lazily from ``ARISTOTLE_DOWNLOAD_DIR``. Constructing the manager does
            NOT import ``aristotlelib`` — that happens on the first real call — so
            the factory stays importable without the opt-in extra.
    """
    if manager is None:
        manager = AristotleManager(download_dir=os.environ.get("ARISTOTLE_DOWNLOAD_DIR", "./aristotle-output"))

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
        result = asyncio.run(manager.submit(
            session_id=session_id,
            prompt=prompt,
            project_dir=project_dir or None,
        ))
        return json.dumps(result, indent=2)

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
        result = asyncio.run(manager.wait(
            session_id=session_id,
            max_wait_seconds=max_wait_seconds,
        ))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_poll(session_id: str) -> str:
        """Check the status of an Aristotle task without blocking.

        Use this for non-blocking status checks (e.g., while doing
        other work in parallel).

        Args:
            session_id: The session to check.
        """
        result = asyncio.run(manager.poll(session_id=session_id))
        return json.dumps(result, indent=2)

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
        result = asyncio.run(manager.steer(session_id=session_id, prompt=prompt))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_events(session_id: str, limit: int = 20) -> str:
        """Fetch recent events from a running Aristotle task.

        Shows what Aristotle is doing: proof attempts, file edits,
        Lean builds, etc. Useful for monitoring progress.

        Args:
            session_id: The session to inspect.
            limit: Maximum number of events to return.
        """
        result = asyncio.run(manager.get_events(session_id=session_id, limit=limit))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_sessions() -> str:
        """List all active Aristotle sessions with their current status."""
        result = manager.list_sessions()
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_delegate_node(
        graph_path: str,
        node_id: str,
        project_dir: str,
        extra_instructions: str = "",
        max_wait_seconds: float = 5400,
    ) -> str:
        """Delegate ONE plan node to Aristotle: spec in, proof written back to the node.

        This is the prover-backend entry. It reads the target node's spec from
        the plan (its informal statement + source_refs + mathlib_declarations +
        in-tier depends_on), hands the whole Lean project to Aristotle, blocks
        until a terminal status, lands the returned Lean files into the project,
        records the proof in the node's prose file, and returns a `merge_node.py`
        payload that links the node's `content` (apply it through the single
        locked graph writer).

        Aristotle ONLY produces the proof into the node — it does not review,
        score, or touch review_status.json. The landed proof feeds the SAME
        jury/sidecar/review pipeline as the in-session worker.

        Args:
            graph_path: Path to the plan's graph.json.
            node_id: The target node id (verbatim, e.g. "Chernoff bound").
            project_dir: The Lean project directory (where files are landed and
                         informal_content/ lives).
            extra_instructions: Optional extra steering appended to the spec prompt.
            max_wait_seconds: Ceiling on how long to wait (default: 90 minutes).
        """
        result = asyncio.run(delegate_to_node(
            graph_path=graph_path,
            node_id=node_id,
            project_dir=project_dir,
            manager=manager,
            extra_instructions=extra_instructions,
            max_wait_seconds=max_wait_seconds,
        ))
        return json.dumps(
            {
                "node_id": result.node_id,
                "status": result.status,
                "ok": result.ok,
                "landed_files": result.landed_files,
                "content": result.content,
                "project_id": result.project_id,
                "output_summary": result.output_summary,
                "merge_payload": result.merge_payload,
            },
            indent=2,
        )

    return server


if __name__ == "__main__":
    server = create_aristotle_server()
    server.run(transport="stdio")
