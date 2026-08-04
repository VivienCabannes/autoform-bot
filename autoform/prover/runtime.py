"""Unified prover runtime shared by the deterministic dispatcher.

``run_prove_node(node_id, backend, project_dir, max_steers=3)`` proves one plan node
with the chosen backend and writes the proof into the node. Supported adapters
are Claude, Aristotle, Codex, OpenAI-compatible, and Avocado. The **driver,
event contract, and verification gate are the same**; only the adapter differs.

The runtime only writes a proof into a node. It does not
review, score, taint, or touch ``review_status.json`` — the jury (PR E) and the
review surface (PR A) consume the proof downstream. Nothing here imports any
review/sidecar machinery.

``aristotlelib`` is imported lazily (only when ``backend="aristotle"`` is actually
used), so this module imports cleanly without the opt-in extra.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .aristotle import build_node_spec
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
        # Lazy import: the generic OpenAI-compatible HTTP backend. Avocado's
        # private endpoint/model/auth must be configured explicitly.
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
    judge_policy: str = "never",
    allow_api_egress: bool = False,
) -> ProofResult:
    """Build the node spec, run the unified driver with the chosen adapter.

    This is the importable entry point used by the dispatcher and tests.
    """
    if backend in {"openai", "avocado"} and not allow_api_egress:
        raise ValueError(
            f"{backend}: explicit project-data egress consent is required; "
            "set allow_api_egress=true only after the provider, base URL, and "
            "project data scope were shown to the user"
        )
    spec = build_node_spec(Path(graph_path), node_id, project_dir=Path(project_dir))
    adapter = _make_adapter(backend, graph_path, max_wait_seconds, extra_args, mcp_config)
    result = prove(
        adapter,
        node_id,
        spec,
        project_dir,
        max_steers=max_steers,
        judge_policy=judge_policy,
    )
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
