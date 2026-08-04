"""Export a Markdown dependency graph as a self-contained HTML/SVG file."""

from __future__ import annotations

import argparse
import html
import os
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from urllib.parse import quote

from autoform.graph import GraphValidationError, load_graph

if TYPE_CHECKING:
    from autoform.graph import Graph, Node


BOX_WIDTH = 240
BOX_HEIGHT = 64
X_GAP = 120
Y_GAP = 32
MARGIN = 48


def _ranks(graph: Graph) -> dict[str, int]:
    """Assign each node to the first column after all of its prerequisites."""
    ranks: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(node_id: str) -> int:
        if node_id in ranks:
            return ranks[node_id]
        if node_id in visiting:
            # ``load_graph`` normally rejects cycles. This fallback keeps direct
            # callers from recursing forever if they construct a Graph manually.
            return 0
        visiting.add(node_id)
        node = graph.nodes[node_id]
        rank = max((visit(dependency) + 1 for dependency in node.dependencies), default=0)
        visiting.remove(node_id)
        ranks[node_id] = rank
        return rank

    for node_id in sorted(graph.nodes):
        visit(node_id)
    return ranks


def _positions(graph: Graph) -> tuple[dict[str, tuple[int, int]], int, int]:
    ranks = _ranks(graph)
    columns: dict[int, list[Node]] = defaultdict(list)
    for node in graph.nodes.values():
        columns[ranks[node.id]].append(node)

    for nodes in columns.values():
        nodes.sort(key=lambda node: (node.title.casefold(), node.id))

    column_count = max(columns, default=0) + 1
    row_count = max((len(nodes) for nodes in columns.values()), default=1)
    width = 2 * MARGIN + column_count * BOX_WIDTH + max(column_count - 1, 0) * X_GAP
    height = 2 * MARGIN + row_count * BOX_HEIGHT + max(row_count - 1, 0) * Y_GAP

    positions: dict[str, tuple[int, int]] = {}
    for rank, nodes in columns.items():
        column_height = len(nodes) * BOX_HEIGHT + max(len(nodes) - 1, 0) * Y_GAP
        y = (height - column_height) // 2
        x = MARGIN + rank * (BOX_WIDTH + X_GAP)
        for node in nodes:
            positions[node.id] = (x, y)
            y += BOX_HEIGHT + Y_GAP
    return positions, width, height


def _link_to(node: Node, output: Path) -> str:
    relative = os.path.relpath(node.path.resolve(), output.resolve().parent)
    return quote(Path(relative).as_posix(), safe="/:")


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_graph(graph: Graph, output: Path) -> str:
    """Return a complete HTML document whose nodes link to their Markdown files."""
    positions, width, height = _positions(graph)
    edges: list[str] = []
    nodes: list[str] = []

    for dependent in sorted(graph.nodes.values(), key=lambda node: node.id):
        end_x, end_y = positions[dependent.id]
        for prerequisite_id in dependent.dependencies:
            start_x, start_y = positions[prerequisite_id]
            x1 = start_x + BOX_WIDTH
            y1 = start_y + BOX_HEIGHT // 2
            x2 = end_x
            y2 = end_y + BOX_HEIGHT // 2
            midpoint = (x1 + x2) // 2
            edges.append(
                f'<path class="edge" d="M {x1} {y1} C {midpoint} {y1}, {midpoint} {y2}, {x2} {y2}" '
                f'data-prerequisite="{html.escape(prerequisite_id, quote=True)}" '
                f'data-dependent="{html.escape(dependent.id, quote=True)}" />'
            )

    for index, node in enumerate(sorted(graph.nodes.values(), key=lambda node: node.id)):
        x, y = positions[node.id]
        title = html.escape(node.title)
        label = html.escape(_shorten(node.title, 34))
        identifier = html.escape(_shorten(node.id, 38))
        href = html.escape(_link_to(node, output), quote=True)
        nodes.append(
            f'<a class="node" id="node-{index}" href="{href}" '
            f'aria-label="Open {html.escape(node.title, quote=True)}">'
            f'<title>{title} ({html.escape(node.id)})</title>'
            f'<rect x="{x}" y="{y}" width="{BOX_WIDTH}" height="{BOX_HEIGHT}" rx="10" />'
            f'<text class="title" x="{x + 16}" y="{y + 27}">{label}</text>'
            f'<text class="identifier" x="{x + 16}" y="{y + 49}">{identifier}</text>'
            "</a>"
        )

    if not nodes:
        nodes.append(f'<text class="empty" x="{width // 2}" y="{height // 2}">No nodes</text>')

    project_name = html.escape(graph.blueprint_dir.name or "Blueprint")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{project_name} dependency graph</title>
  <style>
    :root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f6f7f9; color: #172033; }}
    header {{ padding: 1rem 1.5rem; border-bottom: 1px solid #d8dee9; background: #fff; }}
    h1 {{ margin: 0; font-size: 1.15rem; }}
    p {{ margin: .35rem 0 0; color: #596579; font-size: .9rem; }}
    .canvas {{ overflow: auto; padding: 1rem; }}
    svg {{ display: block; min-width: 100%; height: auto; }}
    .edge {{ fill: none; stroke: #8290a5; stroke-width: 2; marker-end: url(#arrow); }}
    .node rect {{ fill: #fff; stroke: #526a93; stroke-width: 2; }}
    .node:hover rect, .node:focus rect {{ fill: #edf4ff; stroke: #175dcc; stroke-width: 3; }}
    .node text {{ pointer-events: none; }}
    .title {{ fill: #172033; font-size: 14px; font-weight: 650; }}
    .identifier {{ fill: #69758a; font: 12px ui-monospace, monospace; }}
    .empty {{ fill: #69758a; text-anchor: middle; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111827; color: #e5e7eb; }}
      header {{ background: #182132; border-color: #364156; }}
      p, .identifier {{ color: #aeb8ca; }}
      .node rect {{ fill: #182132; stroke: #7890ba; }}
      .node:hover rect, .node:focus rect {{ fill: #20304a; stroke: #75a7ff; }}
      .title {{ fill: #f3f4f6; }}
      .identifier, .empty {{ fill: #aeb8ca; }}
      .edge {{ stroke: #71809a; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{project_name}</h1>
    <p>{len(graph.nodes)} nodes · {graph.edge_count} dependencies · arrows point from prerequisite to dependent</p>
  </header>
  <main class="canvas">
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="Dependency graph">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
        </marker>
      </defs>
      <g class="edges">{"".join(edges)}</g>
      <g class="nodes">{"".join(nodes)}</g>
    </svg>
  </main>
</body>
</html>
"""


def export_graph(blueprint_dir: Path, output: Path | None = None) -> Path:
    """Load and export ``blueprint_dir``; return the written HTML path."""
    blueprint_dir = blueprint_dir.resolve()
    destination = (output or blueprint_dir / "graph.html").resolve()
    graph = load_graph(blueprint_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_graph(graph, destination), encoding="utf-8")
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blueprint_dir", type=Path, help="directory containing nodes/**/*.md")
    parser.add_argument("-o", "--output", type=Path, help="output HTML (default: <blueprint-dir>/graph.html)")
    args = parser.parse_args(argv)
    try:
        output = export_graph(args.blueprint_dir, args.output)
    except GraphValidationError as error:
        parser.exit(2, f"error: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
