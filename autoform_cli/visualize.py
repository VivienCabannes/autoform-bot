"""Export a blueprint dependency graph as a Mermaid Markdown page."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from . import mermaid, status
from .graph import GraphValidationError, load_graph


def export_graph(
    blueprint_dir: Path,
    output: Path | None = None,
    *,
    link_extension: str = ".md",
    title: str = "Dependency graph",
) -> Path:
    """Load and export ``blueprint_dir``; return the written Markdown path."""
    blueprint_dir = Path(blueprint_dir).resolve()
    destination = (output or blueprint_dir / "dependencies.md").resolve()
    graph = load_graph(blueprint_dir)
    statuses = status.derive(graph)
    page = mermaid.render_page(
        graph,
        statuses,
        destination,
        link_extension=link_extension,
        title=title,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    return destination


def export_structure(blueprint_dir: Path, output: Path | None = None) -> Path:
    """Write the vault's own structure page, for reading inside Obsidian.

    Obsidian's file explorer already shows the tree, so the part worth writing
    down is the part it cannot know: the derived state of each article, which
    comes from the dependency graph rather than from anything in the file. The
    flat-vault warning travels with it, because a vault with every article
    directly under ``roadmap/`` publishes a book with no chapters and looks
    perfectly ordinary in the explorer.

    Plain Markdown, no HTML: the site's stylesheet does not exist here.
    """
    blueprint_dir = Path(blueprint_dir).resolve()
    destination = (output or blueprint_dir / "structure.md").resolve()
    graph = load_graph(blueprint_dir)
    statuses = status.derive(graph)
    by_path = {node.path.resolve(): node for node in graph.nodes.values()}

    files = [
        path
        for path in sorted(blueprint_dir.rglob("*.md"))
        if not any(part.startswith(".") for part in path.relative_to(blueprint_dir).parts)
        and path.name not in {"dependencies.md", destination.name}
    ]
    directories: set[Path] = set()
    for path in files:
        for parent in path.relative_to(blueprint_dir).parents:
            if parent != Path("."):
                directories.add(parent)

    lines: list[str] = []
    for entry in sorted(directories | {p.relative_to(blueprint_dir) for p in files}):
        indent = "    " * (len(entry.parts) - 1)
        if entry in directories:
            lines.append(f"{indent}- **{entry.name}/**")
            continue
        node = by_path.get((blueprint_dir / entry).resolve())
        if node is None:
            lines.append(f"{indent}- [{entry.name}]({entry.as_posix()}) · prose")
            continue
        kind = node.declaration or node.kind
        lines.append(
            f"{indent}- [{node.title}]({entry.as_posix()}) · {kind} · {statuses[node.id].label}"
        )

    depths = {len(p.relative_to(blueprint_dir).parts) - 1 for p in by_path}
    warning = (
        "> [!warning] Every article sits directly under `roadmap/`.\n"
        "> Chapters come from directories, so this vault publishes as one\n"
        "> undivided list. Group the articles into subdirectories.\n\n"
        if len(by_path) > 3 and depths <= {1}
        else ""
    )
    page = (
        "---\nkind: structure\n---\n\n"
        "# Vault structure\n\n"
        "Every Markdown file in this vault, with the state the dependency graph\n"
        "derives for it. Chapters come from directories, so the shape of this\n"
        "tree is the shape of the published book.\n\n"
        f"{warning}"
        + "\n".join(lines)
        + "\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blueprint_dir", type=Path, help="directory containing roadmap Markdown nodes")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output Markdown (default: <blueprint-dir>/dependencies.md)",
    )
    parser.add_argument(
        "--link-extension",
        choices=(".md", ".html"),
        default=".md",
        help="node-link extension: .md for the vault or .html for a built site",
    )
    parser.add_argument("--title", default="Dependency graph", help="page heading")
    args = parser.parse_args(argv)
    try:
        output = export_graph(
            args.blueprint_dir,
            args.output,
            link_extension=args.link_extension,
            title=args.title,
        )
        structure = export_structure(args.blueprint_dir)
    except GraphValidationError as error:
        parser.exit(2, f"error: {error}\n")
    print(output)
    print(structure)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
