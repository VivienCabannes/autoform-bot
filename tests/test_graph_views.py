from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.graph import Graph, Node
from autoform_cli.graph_views import chapter_view, focus_view, full_view, project_view
from autoform_cli.status import derive


def _graph(tmp_path: Path) -> Graph:
    blueprint = tmp_path / "blueprint"
    roadmap = blueprint / "roadmap"
    for group, title in (("a", "Foundations"), ("b", "Main chapter"), ("c", "Applications")):
        page = roadmap / group / "README.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\nkind: roadmap\n---\n\n# {title}\n", encoding="utf-8")

    def node(node_id: str, title: str, **kwargs) -> Node:
        return Node(
            id=node_id,
            title=title,
            path=roadmap / f"{node_id}.md",
            dependencies=kwargs.pop("dependencies", ()),
            **kwargs,
        )

    nodes = {
        "a/base": node(
            "a/base",
            "Base object",
            declaration="def",
            statement_formalized=True,
        ),
        "b/bridge": node(
            "b/bridge",
            "Bridge lemma",
            dependencies=("a/base",),
            statement_dependencies=("a/base",),
            declaration="lemma",
        ),
        "b/top": node(
            "b/top",
            "Main theorem",
            dependencies=("b/bridge", "a/base"),
            statement_dependencies=("b/bridge",),
            proof_dependencies=("a/base",),
            declaration="theorem",
        ),
        "c/use": node(
            "c/use",
            "Application",
            dependencies=("b/top",),
            statement_dependencies=("b/top",),
            declaration="theorem",
        ),
    }
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def test_project_view_collapses_chapters_without_flattening_statuses(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = project_view(graph, derive(graph))

    assert view.kind == "project"
    assert [(node.id, node.title, node.members) for node in view.nodes] == [
        ("scope:a", "Foundations", ("a/base",)),
        ("scope:b", "Main chapter", ("b/bridge", "b/top")),
        ("scope:c", "Applications", ("c/use",)),
    ]
    main = view.nodes[1]
    assert main.status_counts == (("can_state", 1), ("planned", 1))
    assert [(edge.source, edge.target, edge.statement_count, edge.proof_count) for edge in view.edges] == [
        ("scope:a", "scope:b", 1, 1),
        ("scope:b", "scope:c", 1, 0),
    ]


def test_chapter_view_keeps_external_relations_as_boundaries(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = chapter_view(graph, derive(graph), "b")

    assert view.kind == "chapter"
    assert view.scope == "b"
    assert {node.id for node in view.nodes} == {
        "b/bridge",
        "b/top",
        "boundary:a",
        "boundary:c",
    }
    assert {(edge.source, edge.target, edge.statement_count, edge.proof_count) for edge in view.edges} == {
        ("boundary:a", "b/bridge", 1, 0),
        ("boundary:a", "b/top", 0, 1),
        ("b/bridge", "b/top", 1, 0),
        ("b/top", "boundary:c", 1, 0),
    }


def test_focus_view_uses_graph_distance_independently_of_chapter_scope(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    statuses = derive(graph)

    focused = focus_view(graph, statuses, "b/top", radius=1)
    assert focused.focus == "b/top"
    assert focused.radius == 1
    assert set(focused.member_ids) == {"a/base", "b/bridge", "b/top", "c/use"}
    assert [node.id for node in focused.nodes if node.focus] == ["b/top"]

    node_only = focus_view(graph, statuses, "b/top", radius=0)
    assert node_only.member_ids == ("b/top",)
    assert node_only.edges == ()

    with pytest.raises(ValueError, match="non-negative"):
        focus_view(graph, statuses, "b/top", radius=-1)


def test_full_view_preserves_every_fine_node_and_edge(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    view = full_view(graph, derive(graph))

    assert view.kind == "full"
    assert set(view.member_ids) == set(graph.nodes)
    assert sum(edge.dependency_count for edge in view.edges) == graph.edge_count
