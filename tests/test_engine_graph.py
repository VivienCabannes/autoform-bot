from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.engine_graph import ProjectionError, project_engine_graph, write_engine_graph


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    sources = project / "blueprint" / "sources"
    (roadmap / "foundations").mkdir(parents=True)
    (roadmap / "results").mkdir(parents=True)
    sources.mkdir(parents=True)
    (roadmap / "foundations" / "README.md").write_text("# Foundations\n", encoding="utf-8")
    (roadmap / "results" / "README.md").write_text("# Main results\n", encoding="utf-8")
    (sources / "paper.md").write_text("# Source paper\n\n## Main theorem\n", encoding="utf-8")
    (roadmap / "foundations" / "base.md").write_text(
        """---
kind: node
declaration: def
statement: formalized
lean: Demo.base
origin: background
---

# Base definition
""",
        encoding="utf-8",
    )
    (roadmap / "results" / "main.md").write_text(
        """---
kind: node
declaration: theorem
origin: cited
---

# Main theorem

## Depends on

- [Base definition](../foundations/base.md)

## Sources

- [Main theorem](../../sources/paper.md#main-theorem)
""",
        encoding="utf-8",
    )
    (project / "Demo.lean").write_text(
        "namespace Demo\n\ndef base : Nat := 0\n\nend Demo\n",
        encoding="utf-8",
    )
    return project


def test_projects_markdown_into_deterministic_worker_schema(tmp_path: Path) -> None:
    project = _project(tmp_path)

    projected = project_engine_graph(
        project / "blueprint", project_root=project, lean_root=project
    )

    assert projected["metadata"] == {
        "generated_by": "autoform-blueprint",
        "source": "blueprint",
        "source_revision": projected["metadata"]["source_revision"],
        "sources": [
            {"file": "blueprint/sources/paper.md", "title": "Source paper", "format": "markdown"}
        ],
    }
    assert list(projected["nodes"]) == [
        "chapter:foundations",
        "chapter:results",
        "foundations/base",
        "results/main",
    ]
    assert projected["nodes"]["chapter:results"]["depends_on"] == ["chapter:foundations"]
    assert projected["nodes"]["foundations/base"]["lean_file"] == "Demo.lean"
    assert projected["nodes"]["results/main"]["depends_on"] == ["foundations/base"]
    assert projected["nodes"]["results/main"]["source_refs"] == [
        {"file": "blueprint/sources/paper.md", "location": "main-theorem"}
    ]


def test_projection_write_and_check_are_byte_stable(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = project / "graph.json"

    assert write_engine_graph(project / "blueprint", output, lean_root=project)
    first = output.read_bytes()
    assert write_engine_graph(project / "blueprint", output, lean_root=project)
    assert output.read_bytes() == first
    assert write_engine_graph(project / "blueprint", output, lean_root=project, check=True)

    data = json.loads(output.read_text(encoding="utf-8"))
    data["nodes"].pop("results/main")
    output.write_text(json.dumps(data), encoding="utf-8")
    assert not write_engine_graph(project / "blueprint", output, lean_root=project, check=True)


def test_projection_rejects_blueprint_outside_project(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with pytest.raises(ProjectionError, match="outside the project root"):
        project_engine_graph(project / "blueprint", project_root=tmp_path / "elsewhere")
