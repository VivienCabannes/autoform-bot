"""Read-only worker compatibility view derived from the Markdown runtime graph.

This module is the only bridge between ``autoform_worker`` and roadmap data.
It never reads or writes ``graph.json`` and does not expose persistence APIs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from autoform_cli.runtime import RuntimeGraph, RuntimeNode, model_kind


def legacy_node(node: RuntimeNode) -> dict[str, object]:
    """Project one immutable runtime node into the mature prover prompt shape."""
    lean_file = next(
        (target.source_file for target in node.lean_targets if target.source_file),
        None,
    )
    return {
        "id": node.id,
        "kind": model_kind(node.declaration),
        "description": node.title,
        "depends_on": list(node.dependencies),
        "statement_dependencies": list(node.statement_dependencies),
        "proof_dependencies": list(node.proof_dependencies),
        "mathlib_status": "in-mathlib" if node.mathlib else "missing",
        "mathlib_declarations": list(node.mathlib_declarations),
        "mathlib_file": node.mathlib_file,
        "source_refs": [{"file": target} for target in node.source_targets],
        "origin": node.origin,
        "content": node.article_path,
        "lean_file": lean_file,
        "statement_formalized": node.assertions.statement_formalized,
        "proof_formalized": node.assertions.proof_formalized,
        "not_ready": node.assertions.not_ready,
        "dispatchable": node.dispatchable,
        "runtime_status": node.status.state,
    }


def legacy_nodes(runtime: RuntimeGraph) -> dict[str, dict[str, object]]:
    """Return deterministic mutable copies for legacy read-only consumers."""
    return {node.id: legacy_node(node) for node in runtime.nodes}


def write_private_snapshot(runtime: RuntimeGraph, destination: Path, lean_root: Path) -> Path:
    """Atomically refresh a private legacy snapshot for unported prover adapters.

    The file lives under worker state, never in the project. It is a disposable
    cache keyed by the runtime revision, not an authored or synchronized graph.
    """
    payload = {
        "version": 2,
        "metadata": {
            "authority": runtime.authority,
            "generated_by": "autoform-runtime/v1",
            "source_revision": runtime.source_revision,
            "lean_root": str(lean_root),
        },
        "nodes": legacy_nodes(runtime),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if destination.read_text(encoding="utf-8") == encoded:
            return destination
    except OSError:
        pass
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def eligible_prove_nodes(runtime: RuntimeGraph) -> list[tuple[str, dict[str, object], str]]:
    """Return dispatchable, not-yet-proved formalizable leaves.

    Eligibility comes entirely from the derived runtime contract. Containers,
    non-formalizable articles, Mathlib results, and blocked prerequisites never
    enter the worker queue.
    """
    eligible: list[tuple[str, dict[str, object], str]] = []
    for node in runtime.nodes:
        if not node.dispatchable or node.mathlib or node.status.proved:
            continue
        if not node.status.can_prove:
            continue
        reason = "statement formalized; proof ready" if node.status.stated else "ready to formalize"
        eligible.append((node.id, legacy_node(node), reason))
    return sorted(eligible, key=lambda item: item[0])


__all__ = ["eligible_prove_nodes", "legacy_node", "legacy_nodes", "write_private_snapshot"]
