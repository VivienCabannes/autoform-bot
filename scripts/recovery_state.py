#!/usr/bin/env python3
"""Deterministic state fingerprints for proof-recovery retries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _node(graph: dict[str, Any], node_id: str) -> dict[str, Any]:
    nodes = graph.get("nodes", {})
    if isinstance(nodes, dict):
        value = nodes.get(node_id, {})
        return value if isinstance(value, dict) else {}
    if isinstance(nodes, list):
        for value in nodes:
            if isinstance(value, dict) and value.get("id") == node_id:
                return value
    return {}


def _safe_bytes(base: Path, relative: str | None) -> bytes:
    if not relative:
        return b"<unspecified>"
    try:
        path = (base / relative).resolve()
        path.relative_to(base.resolve())
        return path.read_bytes() if path.is_file() else b"<missing>"
    except (OSError, ValueError):
        return b"<unreadable>"


def proof_fingerprint(
    blueprint_path: Path,
    node_id: str,
    lean_root: Path,
    backend: str,
) -> str:
    """Hash the durable inputs that can justify another prover attempt.

    Machine-specific absolute paths and timestamps are deliberately excluded.
    A graph edit, prose strategy update, Lean edit, or backend change produces a
    new fingerprint; merely re-enqueueing the same work does not.
    """
    blueprint_path = blueprint_path.resolve()
    if blueprint_path.is_dir():
        try:
            from autoform_cli.runtime import load_runtime_model, resolve_blueprint
            blueprint, project = resolve_blueprint(blueprint_path)
            nodes, _metadata = load_runtime_model(
                blueprint, project_root=project, lean_root=lean_root
            )
            node = nodes.get(node_id, {})
        except (OSError, ValueError):
            project, node = blueprint_path.parent, {}
    else:  # compatibility for old recovery ledgers and isolated fixtures
        try:
            graph = json.loads(blueprint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            graph = {}
        node = _node(graph, node_id)
        project = blueprint_path.parent
    content = node.get("content") if isinstance(node.get("content"), str) else None
    if content is None:
        content = (
            f"blueprint/roadmap/{node_id}.md"
            if blueprint_path.is_dir()
            else f"informal_content/{node_id}.md"
        )
    lean_file = node.get("lean_file") if isinstance(node.get("lean_file"), str) else None

    digest = hashlib.sha256()
    parts = (
        ("schema", b"autoform-proof-recovery/v1"),
        ("node", json.dumps(node, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False).encode("utf-8")),
        ("content", _safe_bytes(project, content)),
        ("lean", _safe_bytes(lean_root, lean_file)),
        ("backend", backend.encode("utf-8")),
    )
    for label, value in parts:
        digest.update(label.encode("ascii") + b"\0")
        digest.update(value + b"\0")
    return digest.hexdigest()


def latest_recovery(tasks: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
    """Return the newest recovery task carrying fingerprint metadata."""
    for task in reversed(tasks):
        if (task.get("agent") == "escalation" and task.get("node") == node_id
                and isinstance(task.get("recovery"), dict)):
            return task
    return None


def unchanged_recovery(
    tasks: list[dict[str, Any]],
    node_id: str,
    graph_path: Path,
    lean_root: Path,
    backend: str,
) -> bool:
    """Whether the latest completed recovery produced no new prover input."""
    task = latest_recovery(tasks, node_id)
    if task is None or task.get("status") not in {"done", "parked"}:
        return False
    previous = task.get("recovery", {}).get("fingerprint")
    return bool(previous) and previous == proof_fingerprint(
        graph_path, node_id, lean_root, backend
    )


def resumable_park(
    tasks: list[dict[str, Any]],
    node_id: str,
    graph_path: Path,
    lean_root: Path,
    backend: str,
) -> dict[str, Any] | None:
    """A parked recovery whose durable inputs have since CHANGED, or None.

    Parking is a resting state, not a grave. A node parks because its own
    recovery produced no new prover input — but the inputs can still move for
    reasons outside that node: a sibling proof merges and changes a dependency,
    a Mathlib bump lands, a cluster is re-planned, a human edits the prose. The
    evidence gate is symmetric: the same fingerprint that justifies refusing a
    retry justifies granting one the moment it differs.

    Without this, a parked node is unreachable forever and an unattended fleet
    silently loses it. Returns the parked task so the caller can resume it.
    """
    task = latest_recovery(tasks, node_id)
    if task is None or task.get("status") != "parked":
        return None
    previous = task.get("recovery", {}).get("fingerprint")
    if not previous:
        return None
    current = proof_fingerprint(graph_path, node_id, lean_root, backend)
    return task if current != previous else None
