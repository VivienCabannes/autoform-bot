"""Project the Markdown roadmap into the legacy worker graph schema.

The Markdown files remain authoritative. This module exists only while the
distributed worker and local dashboard consume ``graph.json``; its output is a
deterministic compatibility artifact, analogous to a lock file, and must never
be edited by agents or humans.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .graph import Node, load_graph
from .lean import build_linker, declaration_names

SCHEMA_VERSION = 2
GENERATOR = "autoform-blueprint"


class ProjectionError(ValueError):
    """The blueprint cannot be represented safely for the worker engine."""


def project_engine_graph(
    blueprint_dir: str | Path,
    *,
    project_root: str | Path | None = None,
    lean_root: str | Path | None = None,
) -> dict:
    """Return a deterministic worker/dashboard projection of a blueprint."""
    graph = load_graph(blueprint_dir)
    blueprint = graph.blueprint_dir
    project = Path(project_root or blueprint.parent).expanduser().resolve()
    lean = Path(lean_root or project).expanduser().resolve()
    _relative_path(blueprint, project, "blueprint")

    linker = build_linker(lean)
    chapter_ids = sorted({_chapter(node.id) for node in graph.nodes.values()})
    chapter_dependencies: dict[str, set[str]] = {chapter: set() for chapter in chapter_ids}
    children: dict[str, list[Node]] = {chapter: [] for chapter in chapter_ids}
    for node in graph.nodes.values():
        children[_chapter(node.id)].append(node)
        for dependency in node.dependencies:
            source = _chapter(dependency)
            target = _chapter(node.id)
            if source != target:
                chapter_dependencies[target].add(_chapter_id(source))

    nodes: dict[str, dict] = {}
    for chapter in chapter_ids:
        chapter_id = _chapter_id(chapter)
        members = sorted(children[chapter], key=lambda node: node.id)
        readme = (
            blueprint / "roadmap" / chapter / "README.md"
            if chapter
            else blueprint / "roadmap" / "README.md"
        )
        statuses = {_legacy_mathlib_status(node) for node in members}
        nodes[chapter_id] = {
            "id": chapter_id,
            "tier": 1,
            "parent": None,
            "kind": "definition",
            "description": _markdown_title(readme) or _humanize(chapter or "roadmap"),
            "depends_on": sorted(chapter_dependencies[chapter]),
            "mathlib_status": (
                "in-mathlib" if statuses == {"in-mathlib"}
                else "partial" if "in-mathlib" in statuses
                else "missing"
            ),
            "mathlib_declarations": [],
            "content": _optional_relative_path(readme, project),
            "generated": True,
        }

    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        project_names = declaration_names(node.lean or "")
        mathlib_names = declaration_names(node.mathlib_declaration or "")
        location = next(
            (linker.location(name) for name in project_names if linker.location(name)),
            None,
        )
        sources = _source_refs(node, project)
        nodes[node_id] = {
            "id": node_id,
            "tier": 2,
            "parent": _chapter_id(_chapter(node_id)),
            "kind": _legacy_kind(node.declaration),
            "description": node.title,
            "depends_on": list(node.dependencies),
            "mathlib_status": _legacy_mathlib_status(node),
            "mathlib_declarations": mathlib_names if node.mathlib else [],
            "mathlib_file": node.mathlib_file if node.mathlib else None,
            "source_refs": sources,
            "origin": node.origin or ("cited" if sources else "background"),
            "content": _relative_path(node.path, project, f"node {node_id!r}"),
            "lean_file": location.path.as_posix() if location else None,
            "statement_formalized": node.statement_formalized,
            "proof_formalized": node.proof_formalized,
            "not_ready": node.not_ready,
            "generated": True,
        }
        if node.mathlib:
            nodes[node_id]["mathlib_verified"] = {
                "method": "blueprint assertion",
                "declarations": len(mathlib_names),
            }

    return {
        "version": SCHEMA_VERSION,
        "metadata": {
            "generated_by": GENERATOR,
            "source": _relative_path(blueprint, project, "blueprint"),
            "source_revision": _blueprint_revision(blueprint),
            "sources": _project_sources(blueprint, project),
        },
        "nodes": nodes,
    }


def write_engine_graph(
    blueprint_dir: str | Path,
    output: str | Path,
    *,
    project_root: str | Path | None = None,
    lean_root: str | Path | None = None,
    check: bool = False,
) -> bool:
    """Write the projection atomically, or return whether it is current."""
    destination = Path(output).expanduser().resolve()
    data = project_engine_graph(
        blueprint_dir,
        project_root=project_root or destination.parent,
        lean_root=lean_root,
    )
    encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if check:
        try:
            return destination.read_text(encoding="utf-8") == encoded
        except OSError:
            return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _chapter(node_id: str) -> str:
    head, separator, _ = node_id.partition("/")
    return head if separator else ""


def _chapter_id(chapter: str) -> str:
    return f"chapter:{chapter or 'roadmap'}"


def _legacy_kind(declaration: str | None) -> str:
    folded = (declaration or "theorem").casefold()
    if folded in {"abbrev", "class", "def", "definition", "inductive", "instance", "structure"}:
        return "definition"
    if folded in {"lemma", "proposition", "corollary", "example"}:
        return folded
    return "theorem"


def _legacy_mathlib_status(node: Node) -> str:
    return "in-mathlib" if node.mathlib else "missing"


def _source_refs(node: Node, project: Path) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for target in node.sources:
        split = urlsplit(target)
        if split.scheme or split.netloc:
            source = target
        else:
            resolved = (node.path.parent / unquote(split.path)).resolve()
            source = _relative_path(resolved, project, f"source link from {node.id!r}")
        ref = {"file": source}
        if split.fragment:
            ref["location"] = unquote(split.fragment)
        refs.append(ref)
    return refs


def _project_sources(blueprint: Path, project: Path) -> list[dict[str, str]]:
    root = blueprint / "sources"
    if not root.is_dir():
        return []
    return [
        {
            "file": _relative_path(path, project, "source page"),
            "title": _markdown_title(path) or _humanize(path.stem),
            "format": "markdown",
        }
        for path in sorted(root.rglob("*.md"))
    ]


def _blueprint_revision(blueprint: Path) -> str:
    digest = hashlib.sha256(b"autoform-engine-projection/v1\0")
    for path in sorted(blueprint.rglob("*")):
        relative = path.relative_to(blueprint)
        if any(part.startswith(".") for part in relative.parts) or not path.is_file():
            continue
        if relative.as_posix() == "dependencies.md":
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _markdown_title(path: Path) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip().rstrip("#").strip()
    except OSError:
        return None
    return None


def _humanize(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").strip().title()


def _optional_relative_path(path: Path, project: Path) -> str | None:
    return _relative_path(path, project, "chapter page") if path.is_file() else None


def _relative_path(path: Path, project: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError as error:
        raise ProjectionError(f"{label} is outside the project root: {path}") from error


__all__ = [
    "GENERATOR",
    "ProjectionError",
    "project_engine_graph",
    "write_engine_graph",
]
