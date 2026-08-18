"""Unified prover backend — one driver + one steerer + thin per-backend adapters.

This package makes "model-agnostic" real: the prover **contract** (``base.py``),
the **driver** (``driver.py``), and the **steering judge** (``steerer.py``) are
identical regardless of which backend proves the node. Only a small adapter
differs:

* :mod:`servers.prover.claude_adapter` — drives a headless ``claude -p`` worker
  on the Claude Max subscription (``ANTHROPIC_API_KEY`` scrubbed).
* :mod:`servers.prover.aristotle_adapter` — drives Harmonic's Aristotle via the
  existing :mod:`servers.aristotle.core` integration (steers via ``project.ask``).

The MCP server (:mod:`servers.prover.server`) exposes one tool,
``prove_node(node_id, backend, project_dir, ...)``, that picks the adapter from
``backend`` and runs the SAME driver for either.

Nothing here reviews, scores, taints, or touches ``review_status.json`` — a
backend ONLY writes a proof into a node (the jury / review surface consume it
downstream).
"""

from __future__ import annotations

from .base import Event, EventKind, ProofResult, ProverAdapter, Run

__all__ = [
    "Event",
    "EventKind",
    "ProofResult",
    "ProverAdapter",
    "Run",
]
