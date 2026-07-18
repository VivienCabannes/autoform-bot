"""Unified prover MCP server — ONE tool, the backend is a parameter.

``prove_node(node_id, backend, project_dir, max_steers=3)`` proves one plan node
with the chosen backend and writes the proof into the node. ``backend`` selects
the adapter — ``"claude"`` (default, Claude-on-Max, free) or ``"aristotle"``
(opt-in, free, needs the ``aristotle`` extra + ``ARISTOTLE_API_KEY`` + network) —
but the **driver and steerer are the SAME for every backend**: only the adapter differs.

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


def _make_adapter(
    backend: str,
    graph_path: str,
    max_wait_seconds: float,
    extra_args: list[str] | None = None,
    mcp_config: str | None = None,
) -> ProverAdapter:
    """Construct the adapter for ``backend``. Aristotle/Codex are imported lazily here."""
    if backend == "claude":
        return ClaudeAdapter(extra_args=extra_args, mcp_config=mcp_config,
                             max_wait_seconds=max_wait_seconds)
    if backend == "aristotle":
        # Lazy import: only pulled in when the Aristotle backend is actually
        # selected, so the server imports without the ``aristotle`` extra.
        from .aristotle_adapter import AristotleAdapter

        return AristotleAdapter(graph_path=graph_path, max_wait_seconds=max_wait_seconds)
    if backend == "codex":
        # Lazy import: the OpenAI ``codex`` CLI backend (its own auth, not Max).
        from .codex_adapter import CodexAdapter

        return CodexAdapter(extra_args=extra_args, max_wait_seconds=max_wait_seconds)
    if backend in ("openai", "avocado"):
        # Lazy import: the generic OpenAI-compatible HTTP backend. "avocado" is
        # the Meta-internal preset (Avocado = the internal codename of Muse
        # Spark; see docs/avocado-handoff.md for the work-laptop fill-ins).
        from .openai_adapter import OpenAICompatAdapter

        return OpenAICompatAdapter(graph_path=graph_path, preset=backend,
                                   max_wait_seconds=max_wait_seconds)
    raise ValueError(
        f"unknown backend {backend!r}; expected 'claude', 'aristotle', 'codex', "
        "'openai', or 'avocado'"
    )


def run_prove_node(
    *,
    graph_path: str,
    node_id: str,
    project_dir: str,
    backend: str = "claude",
    max_steers: int = 3,
    max_wait_seconds: float = 5400,
    extra_args: list[str] | None = None,
    mcp_config: str | None = None,
) -> ProofResult:
    """Build the node spec, run the unified driver with the chosen adapter.

    This is the importable core of the MCP tool (so tests drive it directly with
    a FAKE adapter). It returns the :class:`ProofResult`; the MCP tool serializes
    it to ``{status, reason, backend, ...}``.
    """
    spec = build_node_spec(Path(graph_path), node_id, project_dir=Path(project_dir))
    adapter = _make_adapter(backend, graph_path, max_wait_seconds, extra_args, mcp_config)
    result = prove(adapter, node_id, spec, project_dir, max_steers=max_steers)
    _record_usage(project_dir, node_id, backend, result)
    return result


#: How each backend is billed — recorded per ledger entry so the manifest's
#: spend line stays honest ("subscription" spend is notional, not dollars).
_BILLING = {"claude": "subscription", "aristotle": "external-compute",
            "codex": "external", "openai": "api", "avocado": "api"}


def _record_usage(project_dir: str, node_id: str, backend: str, result: ProofResult) -> None:
    """Append this run to the project's usage ledger and refresh formalization.yaml.

    Best-effort by design: accounting must never break a proof result. The
    ledger is the source of truth (append-only JSONL); the yaml refresh is a
    derived view and a no-op when the project never opted into the manifest.
    The formalization module lives in the plugin's ``scripts/`` (a standalone
    CLI, not a package member), so it is loaded by path.
    """
    try:
        import importlib.util
        import time as _time

        mod_path = Path(__file__).resolve().parents[2] / "scripts" / "formalization.py"
        spec = importlib.util.spec_from_file_location("autoform_formalization", mod_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {mod_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        meta = result.meta or {}
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        entry = {
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "node": node_id,
            "backend": backend,
            "model": (meta.get("model") or ""),
            "status": result.status,
            "billing": _BILLING.get(backend, "unknown"),
            "wall_seconds": usage.get("wall_seconds", 0),
            "usage": {"worker": usage.get("worker") or {},
                      "judge": usage.get("judge") or {}},
        }
        mod.record_run(project_dir, entry)
        mod.update_formalization(project_dir)
    except Exception as err:  # noqa: BLE001 — accounting is never fatal
        logger.warning("usage ledger update failed (non-fatal): %s", err)


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
        extra_args: list[str] | None = None,
        mcp_config: str | None = None,
    ) -> str:
        """Prove ONE plan node with a swappable backend; write the proof into the node.

        The backend is a PARAMETER — ``"claude"`` (default, runs on the Claude Max
        subscription, free) or ``"aristotle"`` (opt-in, free, needs the aristotle
        extra + ARISTOTLE_API_KEY + network). The driver and the live-steering
        judge are IDENTICAL across backends; only the thin adapter differs. The shared
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
            backend: "claude" (default), "aristotle", or "codex".
            max_steers: Cap on in-flight steers for this run (default 3).
            max_wait_seconds: Wall-clock ceiling for the run — honored by every
                backend (Aristotle stops polling; claude/codex kill the worker
                process group and fail with sub-status "timeout").
            extra_args: Extra CLI args threaded through to the claude/codex
                worker invocation.
            mcp_config: MCP config path for the claude worker's --mcp-config.
                Default (None) auto-discovers: the AUTOFORM_MCP_CONFIG env var if
                set, else the plugin's own .mcp.json next to this package; the
                headless worker also runs with --dangerously-skip-permissions by
                default so it can actually Edit/Write/Bash.

        Returns:
            JSON ``{node_id, backend, status, reason, landed_files, proof_text,
            steering, verify, gate_folds}`` where ``status`` is "proved" or
            "failed", ``steering`` is the run's telemetry ({capability, policy,
            steers, signals}), ``verify`` the honesty gate's checks, and
            ``gate_folds`` how many times a rejected claim was folded back as a
            corrective turn (absent when zero).
        """
        try:
            result = run_prove_node(
                graph_path=graph_path,
                node_id=node_id,
                project_dir=project_dir,
                backend=backend,
                max_steers=max_steers,
                max_wait_seconds=max_wait_seconds,
                extra_args=extra_args,
                mcp_config=mcp_config,
            )
        except Exception as err:
            return json.dumps(
                {"node_id": node_id, "backend": backend, "status": "failed", "reason": str(err)},
                indent=2,
            )
        payload = {
            "node_id": node_id,
            "backend": result.backend,
            "status": result.status,
            "reason": result.reason,
            "landed_files": result.landed_files,
            "proof_text": result.proof_text[:4000],
        }
        # Surface the driver's audit trail: steering telemetry, the honesty
        # gate's checks, fold count, dropped steers — so the orchestrator (and a
        # human reading the tool output) sees WHY a verdict is what it is.
        meta = result.meta or {}
        for key in ("steering", "verify", "gate_folds", "dropped_steers", "sub_status", "usage"):
            if key in meta:
                payload[key] = meta[key]
        return json.dumps(payload, indent=2)

    return server


if __name__ == "__main__":
    create_prover_server().run(transport="stdio")
