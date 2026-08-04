#!/usr/bin/env python3
"""Fail closed unless every explicit formalization target is complete.

A target is a graph node carrying ``proof_status``. Completion is deliberately
small and mechanical: confirmed scope exactly matches the proved targets, their
spec/Lean fingerprints and clean AI reviews are current, and the shared queue
has no queued or running work.

Exit codes: 0 complete, 1 incomplete, 2 unreadable or malformed durable state.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dispatch_queue as dq
import target_state


PROOF_STATUSES = frozenset({"pending", "proved", "blocked"})
SPEC_STATUSES = frozenset({"draft", "ready"})
OPEN_TASK_STATUSES = frozenset({"queued", "running"})
TASK_STATUSES = OPEN_TASK_STATUSES | {"done", "failed"}


class CompletionStateError(ValueError):
    """Durable completion state is missing or malformed."""


@dataclass(frozen=True)
class CompletionReport:
    target_count: int
    issues: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.issues


def _load_object(path: Path, *, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists() and missing is not None:
        return missing
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompletionStateError(f"cannot read valid JSON at {path}: {error}") from error
    if not isinstance(data, dict):
        raise CompletionStateError(f"{path}: root must be a JSON object")
    return data


def _lean_root(project: Path, graph: dict[str, Any]) -> Path:
    metadata = graph.get("metadata")
    if not isinstance(metadata, dict):
        raise CompletionStateError("graph.json metadata must be an object")
    raw = metadata.get("lean_root")
    if not isinstance(raw, str) or not raw.strip():
        raise CompletionStateError("graph.json metadata.lean_root must be a non-empty path")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = project / root
    return root.resolve()


def _target_file(root: Path, node_id: str, node: dict[str, Any]) -> Path:
    try:
        return target_state.target_lean_file(root, node_id, node)
    except target_state.TargetStateError as error:
        raise CompletionStateError(str(error)) from error


def check_completion(project: Path) -> CompletionReport:
    """Evaluate the durable completion contract for one dispatch project."""
    project = project.resolve()
    graph = _load_object(project / "graph.json")
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise CompletionStateError("graph.json nodes must be an object")

    targets: list[tuple[str, dict[str, Any]]] = []
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise CompletionStateError("graph.json nodes must map string ids to objects")
        if "proof_status" in node:
            targets.append((node_id, node))
    targets.sort(key=lambda item: item[0])

    issues: list[str] = []
    if not targets:
        issues.append("no explicit targets (nodes carrying proof_status)")

    metadata = graph.get("metadata")
    if not isinstance(metadata, dict):
        raise CompletionStateError("graph.json metadata must be an object")
    scoped_ids = metadata.get("confirmed_scope")
    if targets and (
        not isinstance(scoped_ids, list)
        or not scoped_ids
        or not all(isinstance(roadmap_id, str) and roadmap_id for roadmap_id in scoped_ids)
    ):
        raise CompletionStateError("graph metadata.confirmed_scope must be a non-empty string list")
    scoped_ids = scoped_ids if isinstance(scoped_ids, list) else []
    if len(scoped_ids) != len(set(scoped_ids)):
        raise CompletionStateError("graph metadata.confirmed_scope contains duplicate ids")
    target_ids: dict[str, str] = {}
    for node_id, node in targets:
        roadmap_id = node.get("roadmap_id")
        if not isinstance(roadmap_id, str) or not roadmap_id:
            raise CompletionStateError(f"target {node_id!r} has invalid roadmap_id")
        if roadmap_id in target_ids:
            raise CompletionStateError(
                f"roadmap_id {roadmap_id!r} is used by targets "
                f"{target_ids[roadmap_id]!r} and {node_id!r}"
            )
        target_ids[roadmap_id] = node_id
    missing_scope = [roadmap_id for roadmap_id in scoped_ids if roadmap_id not in target_ids]
    if missing_scope:
        issues.append("roadmap targets are absent from the proof graph: " + ", ".join(missing_scope))
    extra_scope = [roadmap_id for roadmap_id in target_ids if roadmap_id not in scoped_ids]
    if extra_scope:
        issues.append("proof targets are absent from confirmed scope: " + ", ".join(extra_scope))

    root = _lean_root(project, graph) if targets else None
    sidecar = _load_object(project / "review_status.json", missing={"reviews": {}})
    reviews = sidecar.get("reviews")
    if not isinstance(reviews, dict):
        raise CompletionStateError("review_status.json reviews must be an object")

    for node_id, node in targets:
        spec_status = node.get("spec_status")
        if spec_status not in SPEC_STATUSES:
            raise CompletionStateError(f"target {node_id!r} has invalid spec_status {spec_status!r}")
        if spec_status != "ready":
            issues.append(f"target {node_id!r} spec_status is {spec_status!r}, not 'ready'")

        declarations = node.get("lean_declarations")
        if (
            not isinstance(declarations, list)
            or not declarations
            or not all(isinstance(name, str) and name.strip() for name in declarations)
        ):
            raise CompletionStateError(f"target {node_id!r} lean_declarations must be a non-empty string list")

        status = node.get("proof_status")
        if status not in PROOF_STATUSES:
            raise CompletionStateError(f"target {node_id!r} has invalid proof_status {status!r}")
        if status != "proved":
            issues.append(f"target {node_id!r} proof_status is {status!r}, not 'proved'")

        assert root is not None
        lean_path = _target_file(root, node_id, node)
        if not lean_path.is_file():
            issues.append(f"target {node_id!r} Lean file is missing: {lean_path}")
            artifact = None
        else:
            try:
                artifact = target_state.artifact_fingerprint(project, root, node_id, node)
            except target_state.TargetStateError as error:
                raise CompletionStateError(str(error)) from error

        if artifact is not None and node.get("proof_fingerprint") != artifact:
            issues.append(f"target {node_id!r} proof fingerprint is missing or stale")

        record = reviews.get(node_id)
        ai = record.get("ai") if isinstance(record, dict) else None
        verdict = ai.get("verdict") if isinstance(ai, dict) else None
        if verdict != "clean":
            issues.append(f"target {node_id!r} AI review is {verdict!r}, not 'clean'")
        if artifact is not None and (not isinstance(ai, dict) or ai.get("fingerprint") != artifact):
            issues.append(f"target {node_id!r} AI review fingerprint is missing or stale")

    queue_path = project / "task_queue.json"
    try:
        queue = dq.load_queue(queue_path)
    except dq.QueueStateError as error:
        raise CompletionStateError(str(error)) from error
    for task in queue:
        task_status = task.get("status")
        if task_status not in TASK_STATUSES:
            raise CompletionStateError(f"queue task {task['id']!r} has invalid status {task_status!r}")
        if task_status in OPEN_TASK_STATUSES:
            issues.append(f"queue task {task['id']!r} is still {task_status!r}")

    return CompletionReport(len(targets), tuple(issues))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="directory holding graph.json")
    args = parser.parse_args(argv)
    try:
        report = check_completion(args.project)
    except CompletionStateError as error:
        print(f"completion state error: {error}", file=sys.stderr)
        return 2

    if not report.complete:
        print(f"INCOMPLETE ({report.target_count} target(s))")
        for issue in report.issues:
            print(f"- {issue}")
        return 1
    print(f"COMPLETE ({report.target_count} target(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
