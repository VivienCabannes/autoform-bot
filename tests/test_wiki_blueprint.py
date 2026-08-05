from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.graph_contract import edge_id
from scripts.wiki_blueprint import (
    WikiError,
    build,
    ensure_layout,
    migrate,
    query_cell,
    render,
    search_cells,
)


def write_graph(project: Path, graph: dict) -> None:
    (project / "graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")


def v4_graph(project: Path) -> dict:
    return {
        "version": 4,
        "metadata": {
            "lean_root": str(project / "machine-specific-lean"),
            "created_at": "2026-01-01T00:00:00Z",
            "targets": ["goal"],
            "sources": [
                {
                    "id": "paper",
                    "title": "A useful paper",
                    "url": "https://example.test/paper",
                    "wiki": "wiki/sources/paper.md",
                }
            ],
        },
        "nodes": {
            "base": {
                "id": "base",
                "name": "Base lemma",
                "tier": 2,
                "parent": None,
                "kind": "lemma",
                "statement_depends_on": [],
                "proof_depends_on": [],
                "depends_on": [],
                "related": ["goal"],
                "mathlib_status": "in-mathlib",
                "content": "wiki/nodes/base.md",
                "source_refs": [],
                "aliases": ["foundation"],
            },
            "goal": {
                "id": "goal",
                "name": "Main theorem",
                "tier": 2,
                "parent": None,
                "kind": "theorem",
                "statement_depends_on": ["base"],
                "proof_depends_on": [],
                "depends_on": ["base"],
                "related": [],
                "mathlib_status": "missing",
                "content": "wiki/nodes/goal.md",
                "source_refs": [{"source": "paper", "locator": "Theorem 4.2", "role": "statement"}],
                "aliases": ["main result"],
            },
        },
        "edges": [
            {
                "id": edge_id("goal", "base", "statement-requires"),
                "source": "goal",
                "target": "base",
                "kind": "statement-requires",
                "confidence": "high",
                "provenance": {"source": "paper", "locator": "Theorem 4.2"},
            },
            {
                "id": edge_id("base", "goal", "related"),
                "source": "base",
                "target": "goal",
                "kind": "related",
                "confidence": "medium",
                "provenance": {"kind": "graph-review"},
            },
        ],
    }


def test_layout_is_non_destructive(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    wiki = ensure_layout(project)
    custom = wiki / "README.md"
    custom.write_text("custom\n", encoding="utf-8")
    ensure_layout(project)
    assert custom.read_text(encoding="utf-8") == "custom\n"
    assert all(
        (wiki / name / "README.md").is_file()
        for name in ("nodes", "sources", "papers", "concepts", "audits", "decisions")
    )


def test_migrate_v2_moves_prose_and_types_edges_and_sources(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    prose = project / "informal_content"
    prose.mkdir()
    (prose / "goal.md").write_text("# Goal\n\nStatement.\n", encoding="utf-8")
    write_graph(
        project,
        {
            "version": 2,
            "metadata": {"sources": [{"file": "sources/paper.pdf", "title": "Paper"}]},
            "nodes": {
                "base": {"id": "base", "tier": 2, "parent": None, "depends_on": []},
                "goal": {
                    "id": "goal",
                    "tier": 2,
                    "parent": None,
                    "depends_on": ["base"],
                    "content": "informal_content/goal.md",
                    "source_refs": [{"file": "sources/paper.pdf", "location": "p. 7"}],
                },
            },
        },
    )

    result = migrate(project)
    graph = json.loads((project / "graph.json").read_text(encoding="utf-8"))
    goal = graph["nodes"]["goal"]
    assert result == {"nodes": 2, "moved": 1, "imported": 0, "sources": 0}
    assert graph["version"] == 4
    assert graph["metadata"]["sources"][0]["id"] == "paper"
    assert goal["statement_depends_on"] == []
    assert goal["proof_depends_on"] == ["base"]
    assert goal["depends_on"] == ["base"]
    assert goal["source_refs"][0]["source"] == "paper"
    assert goal["source_refs"][0]["locator"] == "p. 7"
    assert goal["content"] == "wiki/nodes/goal.md"
    assert (project / goal["content"]).is_file()
    assert graph["edges"][0]["kind"] == "proof-requires"
    assert graph["edges"][0]["provenance"]["kind"] == "legacy-node-field"


def test_migrate_imports_markdown_first_blueprint(tmp_path: Path):
    project = tmp_path / "project"
    source_dir = project / "blueprint" / "sources"
    source_dir.mkdir(parents=True)
    (source_dir / "thesis.md").write_text(
        "---\nkind: source\n---\n\n# Thesis\n\nStable record: [arXiv](https://arxiv.org/abs/1234.5678).\n",
        encoding="utf-8",
    )
    node_dir = project / "blueprint" / "roadmap" / "chapter" / "theorems"
    node_dir.mkdir(parents=True)
    (node_dir / "base.md").write_text(
        "---\nkind: node\ndeclaration: lemma\n---\n\n# Base\n",
        encoding="utf-8",
    )
    (node_dir / "goal.md").write_text(
        "---\nkind: node\ndeclaration: theorem\n---\n\n# Goal\n\n"
        "## Sources\n\n- [Thesis: Theorem 2](../../../sources/thesis.md)\n\n"
        "## Depends on\n\n- [Base](base.md)\n",
        encoding="utf-8",
    )
    write_graph(project, {"version": 2, "metadata": {"sources": []}, "nodes": {}})

    result = migrate(project)
    graph = json.loads((project / "graph.json").read_text(encoding="utf-8"))
    goal = graph["nodes"]["chapter/theorems/goal"]
    assert result["imported"] == 3
    assert result["sources"] == 1
    assert "cluster:chapter" in graph["nodes"]
    assert goal["parent"] == "cluster:chapter"
    assert goal["statement_depends_on"] == ["chapter/theorems/base"]
    assert goal["depends_on"] == ["chapter/theorems/base"]
    assert goal["source_refs"] == [{"source": "thesis", "locator": "Thesis: Theorem 2", "role": "statement"}]
    assert graph["metadata"]["sources"][0]["url"] == "https://arxiv.org/abs/1234.5678"
    assert (project / goal["content"]).read_text(encoding="utf-8").startswith("# Goal")
    assert (project / "wiki" / "sources" / "thesis.md").is_file()


def test_render_is_deterministic_and_excludes_machine_paths(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    ensure_layout(project)
    (project / "wiki" / "nodes" / "base.md").write_text("# Base lemma\n", encoding="utf-8")
    (project / "wiki" / "nodes" / "goal.md").write_text(
        "# Main theorem\n\nThe complete authored statement.\n", encoding="utf-8"
    )
    (project / "wiki" / "sources" / "paper.md").write_text("# Paper notes\n", encoding="utf-8")
    graph = v4_graph(project)
    write_graph(project, graph)

    first = render(project)
    graph["metadata"]["lean_root"] = "/another/machine/path"
    graph["metadata"]["created_at"] = "2099-12-31T00:00:00Z"
    write_graph(project, graph)
    second = render(project)

    assert first == second
    joined = "".join(first.values())
    assert str(project) not in joined
    assert "/another/machine/path" not in joined
    assert "Theorem 4.2" in first["cells/goal.md"]
    assert "https://example.test/paper" in first["cells/goal.md"]
    assert first["cells/goal.md"].count("# Main theorem") == 1
    assert "The complete authored statement." in first["cells/goal.md"]
    assert "Authored mathematics" in first["cells/goal.md"]
    assert "confidence: high" in first["cells/goal.md"]
    aliases = json.loads(first["aliases.json"])["aliases"]
    assert aliases["main result"] == ["goal"]
    assert aliases["paper:Theorem 4.2"] == ["goal"]
    assert "Base lemma" in first["targets/goal.md"]
    assert "Main theorem" in first["targets/goal.md"]


def test_build_check_detects_stale_generated_wiki(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    ensure_layout(project)
    (project / "wiki" / "nodes" / "base.md").write_text("# Base\n", encoding="utf-8")
    (project / "wiki" / "nodes" / "goal.md").write_text("# Goal\n", encoding="utf-8")
    write_graph(project, v4_graph(project))
    assert build(project)
    assert build(project, check=True)
    (project / "wiki" / "_generated" / "index.md").write_text("stale\n", encoding="utf-8")
    assert not build(project, check=True)


def test_migration_rejects_content_path_traversal(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    write_graph(
        project,
        {
            "version": 2,
            "metadata": {"sources": []},
            "nodes": {"n": {"id": "n", "depends_on": [], "content": "../secret.md"}},
        },
    )
    with pytest.raises(WikiError, match="escapes the project"):
        migrate(project)


def test_cell_query_resolves_alias_and_returns_local_organs(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    ensure_layout(project)
    (project / "wiki" / "nodes" / "base.md").write_text("# Base\n\nFoundation.\n", encoding="utf-8")
    (project / "wiki" / "nodes" / "goal.md").write_text("# Goal\n\nResult.\n", encoding="utf-8")
    write_graph(project, v4_graph(project))

    result = query_cell(project, "main result", depth=1)

    assert result["focus"] == "goal"
    assert set(result["cells"]) == {"base", "goal"}
    assert result["edges"][0]["confidence"] == "high"
    assert {organ["kind"] for organ in result["cells"]["goal"]["organs"]} >= {"wiki", "source"}
    assert search_cells(project, "foundation") == [{"id": "base", "name": "Base lemma"}]
    assert search_cells(project, "Theorem 4.2") == [{"id": "goal", "name": "Main theorem"}]
    assert query_cell(project, "paper:Theorem 4.2")["focus"] == "goal"


def test_schema_v4_requires_canonical_edge_table(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    graph = v4_graph(project)
    graph.pop("edges")
    write_graph(project, graph)
    with pytest.raises(ValueError, match="must contain an edges list"):
        render(project)


def test_tier_one_navigation_uses_supercell_pages(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    ensure_layout(project)
    (project / "wiki" / "nodes" / "child.md").write_text("# Child\n\nStatement.\n", encoding="utf-8")
    write_graph(
        project,
        {
            "version": 4,
            "metadata": {"sources": [], "targets": []},
            "nodes": {
                "cluster": {
                    "id": "cluster", "name": "Cluster", "tier": 1, "parent": None,
                    "statement_depends_on": [], "proof_depends_on": [], "depends_on": [],
                    "related": [], "aliases": [],
                },
                "child": {
                    "id": "child", "name": "Child", "tier": 2, "parent": "cluster",
                    "statement_depends_on": [], "proof_depends_on": [], "depends_on": [],
                    "related": [], "aliases": [], "content": "wiki/nodes/child.md",
                },
            },
            "edges": [],
        },
    )
    files = render(project)
    assert "(supercells/cluster.md)" in files["index.md"]
    assert "(../supercells/cluster.md)" in files["cells/child.md"]
    assert "cells/child.md" in files["supercells/cluster.md"]
