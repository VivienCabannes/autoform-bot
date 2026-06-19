"""Unified prover MCP server — ONE tool, the backend is a parameter.

``prove_node(node_id, backend, project_dir, max_steers=3)`` proves one plan node
with the chosen backend and writes the proof into the node. ``backend`` selects
the adapter — ``"claude"`` (default, Claude-on-Max, free) or ``"aristotle"``
(opt-in, free, needs the ``aristotle`` extra + ``ARISTOTLE_API_KEY`` + network) —
but the **driver and steerer are the SAME for both**: only the adapter differs.

This is the unified replacement for PR C's one-shot ``aristotle_delegate_node``
and PR D's in-session worker: both are now adapters behind one driver.

HARD CONSTRAINT: ``prove_node`` ONLY writes a proof into a node. It does not
review, score, taint, or touch ``review_status.json`` — the jury (PR E) and the
review surface (PR A) consume the proof downstream. Nothing here imports any
review/sidecar machinery.

``aristotlelib`` is imported lazily (only when ``backend="aristotle"`` is actually
used), so this server — and ``create_prover_server()`` — import cleanly without
the opt-in extra.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastmcp.server import FastMCP

from servers.aristotle.core import build_node_spec
from .base import ProofResult, ProverAdapter
from .claude_adapter import ClaudeAdapter
from .driver import prove

logger = logging.getLogger(__name__)


def _make_adapter(backend: str, graph_path: str, max_wait_seconds: float) -> ProverAdapter:
    """Construct the adapter for ``backend``. Aristotle is imported lazily here."""
    if backend == "claude":
        return ClaudeAdapter()
    if backend == "aristotle":
        # Lazy import: only pulled in when the Aristotle backend is actually
        # selected, so the server imports without the ``aristotle`` extra.
        from .aristotle_adapter import AristotleAdapter

        return AristotleAdapter(graph_path=graph_path, max_wait_seconds=max_wait_seconds)
    raise ValueError(f"unknown backend {backend!r}; expected 'claude' or 'aristotle'")


def run_prove_node(
    *,
    graph_path: str,
    node_id: str,
    project_dir: str,
    backend: str = "claude",
    max_steers: int = 3,
    max_wait_seconds: float = 5400,
) -> ProofResult:
    """Build the node spec, run the unified driver with the chosen adapter.

    This is the importable core of the MCP tool (so tests drive it directly with
    a FAKE adapter). It returns the :class:`ProofResult`; the MCP tool serializes
    it to ``{status, reason, backend, ...}``.
    """
    spec = build_node_spec(Path(graph_path), node_id, project_dir=Path(project_dir))
    adapter = _make_adapter(backend, graph_path, max_wait_seconds)
    return prove(adapter, node_id, spec, project_dir, max_steers=max_steers)


def create_prover_server() -> FastMCP:
    """Create the unified prover FastMCP server (the single ``prove_node`` tool)."""
    server = FastMCP(name="autoform-prover")

    @server.tool
    def prove_node(
        graph_path: str,
        node_id: str,
        project_dir: str,
        backend: str = "claude",
        max_steers: int = 3,
        max_wait_seconds: float = 5400,
    ) -> str:
        """Prove ONE plan node with a swappable backend; write the proof into the node.

        The backend is a PARAMETER — ``"claude"`` (default, runs on the Claude Max
        subscription, free) or ``"aristotle"`` (opt-in, free, needs the aristotle
        extra + ARISTOTLE_API_KEY + network). The driver and the live-steering
        judge are IDENTICAL for both; only the thin adapter differs. The shared
        steerer watches the run and injects a corrective instruction (in-flight
        for Aristotle via project.ask, turn-granular for Claude via --resume) only
        when the prover goes off-course, up to ``max_steers`` times.

        This tool ONLY writes a proof into the node. It does not review, score, or
        touch review_status.json — the jury and the review surface consume the
        proof downstream.

        Args:
            graph_path: Path to the plan's graph.json (the node spec source).
            node_id: The target node id (verbatim, e.g. "Chernoff bound").
            project_dir: The Lean project directory (where the proof is written
                and informal_content/ lives).
            backend: "claude" (default) or "aristotle".
            max_steers: Cap on in-flight steers for this run (default 3).
            max_wait_seconds: Ceiling on how long to wait (Aristotle backend).

        Returns:
            JSON ``{node_id, backend, status, reason, landed_files, proof_text}``
            where ``status`` is "proved" or "failed".
        """
        try:
            result = run_prove_node(
                graph_path=graph_path,
                node_id=node_id,
                project_dir=project_dir,
                backend=backend,
                max_steers=max_steers,
                max_wait_seconds=max_wait_seconds,
            )
        except Exception as err:
            return json.dumps(
                {"node_id": node_id, "backend": backend, "status": "failed", "reason": str(err)},
                indent=2,
            )
        return json.dumps(
            {
                "node_id": node_id,
                "backend": result.backend,
                "status": result.status,
                "reason": result.reason,
                "landed_files": result.landed_files,
                "proof_text": result.proof_text[:4000],
            },
            indent=2,
        )

    return server


if __name__ == "__main__":
    create_prover_server().run(transport="stdio")
