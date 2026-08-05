#!/usr/bin/env python3
"""Initialize or explicitly reset Autoform's durable planning state.

Normal invocation is non-destructive: it creates ``graph.json`` when missing,
or adds a missing ``metadata.lean_root`` to an empty/existing graph.  A
different existing Lean root is an error.

``--reset-plan`` is intentionally separate.  It snapshots the graph, prose,
queue, reviews, and activity status before replacing planning state with an
empty graph.  Workflow skills must obtain explicit user confirmation before
using that flag.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from .wiki_blueprint import SCHEMA_VERSION, ensure_layout
except ImportError:  # direct script execution
    from wiki_blueprint import SCHEMA_VERSION, ensure_layout

_SIDECARS = ("task_queue.json", "review_status.json", "agents_status.json")


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _load_graph(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid graph JSON at {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("nodes", {}), dict):
        raise ValueError(f"{path}: graph must be an object with a nodes object")
    metadata = data.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: metadata must be an object")
    metadata.setdefault("sources", [])
    data.setdefault("version", 2)
    data.setdefault("nodes", {})
    return data


def _snapshot(project: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshots_root = (project / ".autoform" / "snapshots").resolve(strict=False)
    try:
        snapshots_root.relative_to(project)
    except ValueError as error:
        raise ValueError("snapshot directory escapes the planning project") from error
    snapshot = snapshots_root / f"plan-{stamp}-{os.getpid()}"
    snapshot.mkdir(parents=True, exist_ok=False)
    for name in ("graph.json", *_SIDECARS):
        source = project / name
        if source.is_file():
            shutil.copy2(source, snapshot / name)
    for directory in ("wiki", "informal_content"):
        content = project / directory
        if content.is_dir():
            shutil.copytree(content, snapshot / directory, symlinks=True)
    return snapshot


def initialize(project: Path, lean_root: Path, *, reset: bool = False) -> tuple[str, Path | None]:
    project = project.resolve()
    lean_root = lean_root.resolve()
    if not project.is_dir():
        raise ValueError(f"planning project directory does not exist: {project}")
    if not lean_root.is_dir():
        raise ValueError(f"Lean project directory does not exist: {lean_root}")

    graph_path = project / "graph.json"
    snapshot: Path | None = None
    if reset:
        content_dirs = (project / "wiki", project / "informal_content")
        for managed in (graph_path, *content_dirs, *(project / name for name in _SIDECARS)):
            if managed.is_symlink():
                raise ValueError(f"refusing to reset symlinked plan state: {managed}")
        for content in content_dirs:
            if content.exists() and not content.is_dir():
                raise ValueError(f"expected directory at {content}")
        for name in _SIDECARS:
            path = project / name
            if path.exists() and not path.is_file():
                raise ValueError(f"expected file at {path}")
        snapshot = _snapshot(project)
        for content in content_dirs:
            if content.exists():
                shutil.rmtree(content)
        for name in _SIDECARS:
            path = project / name
            if path.exists():
                path.unlink()
        data = {
            "version": SCHEMA_VERSION,
            "metadata": {"sources": [], "lean_root": str(lean_root)},
            "nodes": {},
        }
        _write_json_atomic(graph_path, data)
        ensure_layout(project)
        return "reset", snapshot

    if graph_path.exists():
        data = _load_graph(graph_path)
        existing = data["metadata"].get("lean_root")
        if existing:
            existing_path = Path(str(existing)).expanduser()
            if not existing_path.is_absolute():
                existing_path = (project / existing_path).resolve()
            else:
                existing_path = existing_path.resolve()
            if existing_path != lean_root:
                raise ValueError(
                    "graph metadata.lean_root points elsewhere: "
                    f"{existing_path} (requested {lean_root})"
                )
            ensure_layout(project)
            return "unchanged", None
        data["metadata"]["lean_root"] = str(lean_root)
        _write_json_atomic(graph_path, data)
        ensure_layout(project)
        return "updated", None

    data = {
        "version": SCHEMA_VERSION,
        "metadata": {"sources": [], "lean_root": str(lean_root)},
        "nodes": {},
    }
    _write_json_atomic(graph_path, data)
    ensure_layout(project)
    return "created", None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--lean-root", type=Path, required=True)
    parser.add_argument("--reset-plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        action, snapshot = initialize(
            args.project, args.lean_root, reset=args.reset_plan
        )
    except (ValueError, OSError) as error:
        print(f"plan initialization failed: {error}", file=sys.stderr)
        return 2
    suffix = f"; snapshot={snapshot}" if snapshot else ""
    print(f"plan state {action}: {(args.project.resolve() / 'graph.json')}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
