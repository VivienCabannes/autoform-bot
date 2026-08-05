"""Render a blueprint DAG as Mermaid inside a Markdown page.

Mermaid was chosen over a bespoke SVG because the same fenced block renders in
Obsidian, on GitHub, and on the published site. Colours follow
:mod:`autoform_cli.status`: fill tracks proof progress, stroke tracks statement
progress, exactly as ``leanblueprint`` draws its dependency graph.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .status import STATES, is_definition

if TYPE_CHECKING:
    from .graph import Graph, Node
    from .status import NodeStatus


def node_link(node: Node, output: Path, link_extension: str) -> str:
    """Relative link from the page at *output* to *node*'s source file."""
    return relative_link(node.path, output, link_extension)


def relative_link(target: Path, output: Path, link_extension: str) -> str:
    """Relative link from the page at *output* to *target*, with its suffix swapped."""
    relative = os.path.relpath(target.resolve(), output.resolve().parent)
    return Path(relative).with_suffix(link_extension).as_posix()


def source_links(graph: Graph, output: Path, link_extension: str) -> dict[str, str]:
    """Link every node to its own Markdown file, as the vault sees it."""
    return {
        node_id: node_link(node, output, link_extension)
        for node_id, node in graph.nodes.items()
    }


def _escape(text: str) -> str:
    """Make *text* safe inside a quoted Mermaid label."""
    return text.replace('"', "#quot;").replace("`", "#96;")


def render_diagram(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    output: Path,
    *,
    link_extension: str = ".md",
    links: dict[str, str] | None = None,
    include_classdefs: bool = True,
) -> str:
    """Return the ```mermaid fenced block for *graph*.

    *links* maps node ids to hrefs; without it, nodes link to their own
    Markdown files relative to *output*.

    Set *include_classdefs* to ``False`` for the published site, where the
    init script supplies the palette so it can be swapped for dark mode.
    Mermaid scopes its own styles to the SVG id with ``!important``, so a
    stylesheet cannot recolour a rendered diagram; only re-rendering works.
    Obsidian has no such script, so the vault copy keeps its colours inline.
    """
    ordered = sorted(graph.nodes.values(), key=lambda node: node.id)
    if not ordered:
        return '```mermaid\ngraph LR\n  empty["No nodes yet"]\n```'
    if links is None:
        links = source_links(graph, output, link_extension)

    handles = {node.id: f"n{index}" for index, node in enumerate(ordered)}
    lines = ["```mermaid", "graph LR"]

    for node in ordered:
        handle = handles[node.id]
        label = _escape(node.title)
        # Rectangles introduce data, rounded boxes assert something.
        shape = f'["{label}"]' if is_definition(node) else f'("{label}")'
        lines.append(f"  {handle}{shape}:::{statuses[node.id].key}")

    for node in ordered:
        for dependency in node.statement_dependencies:
            lines.append(f"  {handles[dependency]} --> {handles[node.id]}")
        for dependency in node.proof_dependencies:
            if dependency not in node.statement_dependencies:
                # Dashed: needed only to prove the node, not to state it.
                lines.append(f"  {handles[dependency]} -.-> {handles[node.id]}")

    for node in ordered:
        tooltip = _escape(f"{node.title} — {statuses[node.id].label}")
        lines.append(f'  click {handles[node.id]} "{links[node.id]}" "{tooltip}"')

    if include_classdefs:
        lines.extend(f"  {line}" for line in classdef_lines())
    lines.append("```")
    return "\n".join(lines)


def classdef_lines(*, dark: bool = False) -> list[str]:
    """Mermaid ``classDef`` declarations for every state, in one palette."""
    return [
        f"classDef {state.key} "
        f"fill:{state.dark_fill if dark else state.fill},"
        f"stroke:{state.dark_stroke if dark else state.stroke},"
        f"color:{state.dark_text if dark else state.text},stroke-width:2px"
        for state in STATES
    ]


def render_legend(statuses: dict[str, NodeStatus]) -> str:
    """Return a Markdown legend covering the states actually in use."""
    used = {status.key for status in statuses.values()}
    rows = [state for state in STATES if state.key in used]
    if not rows:
        return ""
    counts = {state.key: 0 for state in STATES}
    for status in statuses.values():
        counts[status.key] += 1
    lines = ["| | State | Nodes | Meaning |", "| --- | --- | --- | --- |"]
    for state in rows:
        # Colour comes from the stylesheet, so the legend follows the theme.
        swatch = f'<span class="bp-swatch bp-swatch-{state.key}"></span>'
        lines.append(f"| {swatch} | {state.label} | {counts[state.key]} | {_MEANINGS[state.key]} |")
    return "\n".join(lines)


_MEANINGS = {
    "mathlib": "Upstreamed into Mathlib.",
    "fully_proved": "Proved, and every prerequisite is fully proved too.",
    "proved": "Proof compiles, but something it rests on is not finished.",
    "defined": "Definition is written in Lean.",
    "can_prove": "Statement is in Lean and every prerequisite is proved — ready to work.",
    "stated": "Statement is in Lean; the proof is not.",
    "can_state": "Prerequisites are stated, so this can be written down.",
    "not_ready": "Needs more blueprint work before it can be attempted.",
    "planned": "Described in the blueprint only.",
}


def render_page(
    graph: Graph,
    statuses: dict[str, NodeStatus],
    output: Path,
    *,
    link_extension: str = ".md",
    title: str = "Dependency graph",
    links: dict[str, str] | None = None,
    include_classdefs: bool = True,
) -> str:
    """Return a complete Markdown page holding the diagram and its legend."""
    counts = f"{len(graph.nodes)} nodes · {graph.edge_count} dependencies"
    diagram = render_diagram(
        graph,
        statuses,
        output,
        link_extension=link_extension,
        links=links,
        include_classdefs=include_classdefs,
    )
    legend = render_legend(statuses)
    sections = [
        "---",
        "kind: graph",
        "---",
        "",
        f"# {title}",
        "",
        f"{counts}. Arrows point from a prerequisite to what depends on it; a dashed",
        "arrow marks a prerequisite that only the proof needs. Select a node to open it.",
        "",
        diagram,
        "",
    ]
    if legend:
        sections.extend(["## Legend", "", legend, ""])
    return "\n".join(sections)


__all__ = [
    "node_link",
    "relative_link",
    "render_diagram",
    "render_legend",
    "render_page",
    "source_links",
]
