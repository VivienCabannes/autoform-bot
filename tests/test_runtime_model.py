from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.runtime import RuntimeModelError, load_runtime_model


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap"
    sources = project / "blueprint" / "sources"
    (roadmap / "foundations").mkdir(parents=True)
    (roadmap / "results").mkdir(parents=True)
    sources.mkdir(parents=True)
    (roadmap / "README.md").write_text("# Roadmap\n")
    (roadmap / "foundations" / "README.md").write_text("# Foundations\n")
    (roadmap / "results" / "README.md").write_text("# Main results\n")
    (sources / "paper.md").write_text("# Source paper\n\n## Main theorem\n")
    (roadmap / "foundations" / "base.md").write_text(
        """---
kind: article
declaration: def
statement: formalized
lean: Demo.base
origin: background
---

# Base definition
"""
    )
    (roadmap / "results" / "main.md").write_text(
        """---
kind: article
declaration: theorem
origin: cited
---

# Main theorem

## Depends on

- [Base definition](../foundations/base.md)

## Sources

- [Main theorem](../../sources/paper.md#main-theorem)
"""
    )
    (project / "Demo.lean").write_text("namespace Demo\n\ndef base : Nat := 0\n\nend Demo\n")
    return project


def test_runtime_model_is_derived_in_memory_from_articles(tmp_path: Path) -> None:
    project = _project(tmp_path)
    nodes, metadata = load_runtime_model(
        project / "blueprint", project_root=project, lean_root=project
    )

    assert metadata["authority"] == "markdown-articles"
    assert nodes["roadmap"]["parent"] is None
    assert nodes["foundations"]["parent"] == "roadmap"
    assert nodes["foundations/base"]["parent"] == "foundations"
    assert nodes["foundations/base"]["lean_file"] == "Demo.lean"
    assert nodes["results/main"]["depends_on"] == ["foundations/base"]
    assert nodes["results/main"]["source_refs"] == [
        {"file": "blueprint/sources/paper.md", "location": "main-theorem"}
    ]
    assert not (project / "graph.json").exists()


def test_runtime_model_rejects_blueprint_outside_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    with pytest.raises(RuntimeModelError, match="outside the project root"):
        load_runtime_model(project / "blueprint", project_root=tmp_path / "elsewhere")
