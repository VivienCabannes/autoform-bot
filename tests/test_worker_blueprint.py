from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.engine_graph import write_engine_graph
from autoform_worker.config import resolve_config
from autoform_worker.errors import Die


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap" / "chapter"
    roadmap.mkdir(parents=True)
    (roadmap / "node.md").write_text(
        "---\nkind: node\ndeclaration: theorem\norigin: background\n---\n\n# Node\n",
        encoding="utf-8",
    )
    write_engine_graph(
        project / "blueprint",
        project / "graph.json",
        project_root=project,
        lean_root=project,
    )
    return project


def test_worker_accepts_current_markdown_projection(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))

    config = resolve_config(project=project, worker_id="worker")

    assert config.graph_path == project / "graph.json"


def test_worker_rejects_stale_markdown_projection(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    page = project / "blueprint" / "roadmap" / "chapter" / "node.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")

    with pytest.raises(Die, match="stale generated projection"):
        resolve_config(project=project, worker_id="worker")
