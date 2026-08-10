"""Test-only conversion of legacy worker fixture dictionaries to Markdown roadmaps."""

from __future__ import annotations

from pathlib import Path


def write_markdown_roadmap(
    project: Path,
    nodes: dict[str, dict],
    *,
    lean_root: Path | None = None,
) -> None:
    """Author a minimal canonical roadmap equivalent to a legacy node fixture."""
    roadmap = project / "blueprint" / "roadmap"
    roadmap.mkdir(parents=True, exist_ok=True)
    (roadmap / "README.md").write_text("---\n---\n\n# Roadmap\n", encoding="utf-8")

    for node_id, node in nodes.items():
        if node_id == "roadmap":
            continue
        path = roadmap / f"{node_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = []
        tier = int(node.get("tier", 2))
        formalizable = tier == 2 and node.get("kind") not in {"chapter", "section"}
        if formalizable:
            kind = str(node.get("kind") or "theorem")
            declaration = "def" if kind in {"definition", "def"} else kind
            metadata.append(f"declaration: {declaration}")
            metadata.append("statement: formalized")
        status = str(node.get("mathlib_status") or "").lower()
        if status in {"exists", "in-mathlib", "in_mathlib", "mathlib"}:
            if formalizable:
                metadata.append("mathlib: true")
            else:
                metadata.extend(["statement: formalized", "proof: formalized"])
        if node.get("not_ready"):
            metadata.append("not_ready: true")
        lean_file = node.get("lean_file")
        if formalizable and lean_file and lean_root is not None:
            declaration_name = _first_declaration(lean_root / str(lean_file))
            if declaration_name:
                metadata.append(f"lean: {declaration_name}")
        body = ["---", *metadata, "---", "", f"# {node.get('description') or node_id}"]
        dependencies = [str(value) for value in node.get("depends_on") or () if value in nodes]
        if dependencies:
            body.extend(["", "## Proof depends on", ""])
            for dependency in dependencies:
                target = _relative_article(path, roadmap / f"{dependency}.md")
                body.append(f"- [{dependency}]({target})")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _relative_article(source: Path, target: Path) -> str:
    import os

    return Path(os.path.relpath(target, source.parent)).as_posix()


def _first_declaration(path: Path) -> str | None:
    if not path.is_file():
        return None
    import re

    pattern = re.compile(r"^\s*(?:theorem|lemma|def|abbrev|instance|structure|class)\s+([A-Za-z0-9_'.]+)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None
