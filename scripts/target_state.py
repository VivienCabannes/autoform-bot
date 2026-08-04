#!/usr/bin/env python3
"""Deterministic fingerprints for one explicit formalization target."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class TargetStateError(ValueError):
    """A target cannot be fingerprinted from its durable state."""


# Proof/review lifecycle fields are deliberately absent: these are the inputs that
# define what a worker must prove, not attestations about a previous attempt.
SPEC_FIELDS = (
    "id",
    "tier",
    "parent",
    "kind",
    "description",
    "depends_on",
    "mathlib_status",
    "mathlib_declarations",
    "mathlib_file",
    "mathlib_notes",
    "source_refs",
    "content",
    "spec_status",
    "lean_file",
    "lean_declarations",
    "roadmap_id",
)


def _safe_path(base: Path, raw: Any, *, field: str, node_id: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise TargetStateError(f"target {node_id!r} has no non-empty {field}")
    relative = Path(raw)
    if relative.is_absolute():
        raise TargetStateError(f"target {node_id!r} {field} must be relative to its root")
    root = base.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise TargetStateError(f"target {node_id!r} {field} escapes its root") from error
    return path


def _safe_file(base: Path, raw: Any, *, field: str, node_id: str) -> Path:
    path = _safe_path(base, raw, field=field, node_id=node_id)
    if not path.is_file():
        raise TargetStateError(f"target {node_id!r} {field} is missing: {path}")
    return path


def target_lean_file(lean_root: Path, node_id: str, node: Mapping[str, Any]) -> Path:
    """Resolve a target's Lean file while rejecting absolute and escaping paths."""
    return _safe_path(lean_root, node.get("lean_file"), field="lean_file", node_id=node_id)


def _read_bytes(path: Path, *, node_id: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise TargetStateError(f"cannot read target {node_id!r} file {path}: {error}") from error


def _part(digest: Any, label: bytes, payload: bytes) -> None:
    """Hash an unambiguous length-framed byte string."""
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def spec_fingerprint(project: Path, node_id: str, node: Mapping[str, Any]) -> str:
    """Hash canonical target fields and the exact linked informal-content bytes."""
    if not isinstance(node_id, str) or not node_id:
        raise TargetStateError("target id must be a non-empty string")
    if not isinstance(node, Mapping):
        raise TargetStateError(f"target {node_id!r} must be an object")

    fields = {field: node.get(field) for field in SPEC_FIELDS}
    try:
        canonical = json.dumps(
            {"node_id": node_id, "fields": fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TargetStateError(f"target {node_id!r} has non-canonical spec fields: {error}") from error

    content_raw = node.get("content")
    if content_raw is None:
        content = b""
    else:
        content_path = _safe_file(project, content_raw, field="content", node_id=node_id)
        content = _read_bytes(content_path, node_id=node_id)

    digest = hashlib.sha256()
    _part(digest, b"domain", b"autoform-target-spec-v1")
    _part(digest, b"fields", canonical)
    _part(digest, b"content", content)
    return digest.hexdigest()


def artifact_fingerprint(
    project: Path,
    lean_root: Path,
    node_id: str,
    node: Mapping[str, Any],
) -> str:
    """Hash the current target spec together with its exact safe Lean file bytes."""
    spec = spec_fingerprint(project, node_id, node).encode("ascii")
    lean_path = target_lean_file(lean_root, node_id, node)
    lean = _read_bytes(lean_path, node_id=node_id)

    digest = hashlib.sha256()
    _part(digest, b"domain", b"autoform-target-artifact-v1")
    _part(digest, b"spec-sha256", spec)
    _part(digest, b"lean", lean)
    return digest.hexdigest()
