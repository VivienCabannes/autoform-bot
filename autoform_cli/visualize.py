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
    except GraphValidationError as error:
        parser.exit(2, f"error: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
