"""Adapt the Markdown blueprint to Autoform's in-process worker model.

This module deliberately returns Python objects only. Markdown articles are the
durable graph; workers and dashboards may normalize them in memory, but there
is no graph file to generate, commit, synchronize, or become stale.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

from .graph import Node, load_graph
from .lean import build_linker, declaration_names


class RuntimeModelError(ValueError):
    """A blueprint cannot be safely resolved inside its project."""


def load_runtime_model(
    blueprint_dir: str | Path,
    *,
    project_root: str | Path | None = None,
    lean_root: str | Path | None = None,
) -> tuple[dict[str, dict], dict]:
    """Load Markdown articles as the normalized dictionaries used internally."""
    graph = load_graph(blueprint_dir)
    blueprint = graph.blueprint_dir
    project = Path(project_root or blueprint.parent).expanduser().resolve()
    lean = Path(lean_root or project).expanduser().resolve()
    _relative(blueprint, project, "blueprint")
    linker = build_linker(lean)

    nodes: dict[str, dict] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        project_names = declaration_names(node.lean or "")
        location = next(
            (linker.location(name) for name in project_names if linker.location(name)),
            None,
        )
        nodes[node_id] = {
            "id": node_id,
            "tier": node.depth + 1,
            "depth": node.depth,
            "parent": node.parent,
            "kind": _runtime_kind(node),
            "description": node.title,
            "depends_on": list(node.dependencies),
            "statement_depends_on": list(node.statement_dependencies),
            "proof_depends_on": list(node.proof_dependencies),
            "mathlib_status": "in-mathlib" if node.mathlib else "missing",
            "mathlib_declarations": (
                declaration_names(node.mathlib_declaration or "") if node.mathlib else []
            ),
            "mathlib_file": node.mathlib_file if node.mathlib else None,
            "source_refs": _source_refs(node, project),
            "origin": node.origin or ("cited" if node.sources else "background"),
            "content": _relative(node.path, project, f"article {node_id!r}"),
            "lean_file": location.path.as_posix() if location else None,
            "declaration": node.declaration,
            "formalizable": node.formalizable,
            "blueprint_article": True,
            "statement_formalized": node.statement_formalized,
            "proof_formalized": node.proof_formalized,
            "not_ready": node.not_ready,
        }

    metadata = {
        "source": _relative(blueprint, project, "blueprint"),
        "sources": _project_sources(blueprint, project),
        "authority": "markdown-articles",
    }
    return nodes, metadata


def resolve_blueprint(path: str | Path) -> tuple[Path, Path]:
    """Return ``(blueprint, project)`` for a project or blueprint path."""
    candidate = Path(path).expanduser().resolve()
    if (candidate / "roadmap").is_dir():
        return candidate, candidate.parent
    if (candidate / "blueprint" / "roadmap").is_dir():
        return candidate / "blueprint", candidate
    raise RuntimeModelError(f"no blueprint/roadmap found at {candidate}")


def load_runtime_node(
    blueprint_dir: str | Path,
    node_id: str,
    *,
    project_root: str | Path | None = None,
) -> dict:
    """Load one article for a prover adapter without any durable projection."""
    blueprint, inferred_project = resolve_blueprint(blueprint_dir)
    nodes, _metadata = load_runtime_model(
        blueprint,
        project_root=project_root or inferred_project,
        lean_root=project_root or inferred_project,
    )
    try:
        return nodes[node_id]
    except KeyError as error:
        raise KeyError(f"article {node_id!r} not found in {blueprint}") from error


def _runtime_kind(node: Node) -> str:
    if not node.formalizable:
        return "article"
    declaration = (node.declaration or "theorem").casefold()
    if declaration in {"abbrev", "class", "def", "definition", "inductive", "instance", "structure"}:
        return "definition"
    if declaration in {"lemma", "proposition", "corollary", "example"}:
        return declaration
    return "theorem"


def _source_refs(node: Node, project: Path) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for target in node.sources:
        split = urlsplit(target)
        source = target if split.scheme or split.netloc else _relative(
            (node.path.parent / unquote(split.path)).resolve(), project, "source link"
        )
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
        {"file": _relative(path, project, "source page"), "title": _title(path), "format": "markdown"}
        for path in sorted(root.rglob("*.md"))
    ]


def _title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip().rstrip("#").strip()
    return path.stem.replace("-", " ").title()


def _relative(path: Path, project: Path, label: str) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError as error:
        raise RuntimeModelError(f"{label} is outside the project root: {path}") from error


__all__ = ["RuntimeModelError", "load_runtime_model", "load_runtime_node", "resolve_blueprint"]
