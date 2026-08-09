from __future__ import annotations

from pathlib import Path

from autoform_worker.config import resolve_config


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    roadmap = project / "blueprint" / "roadmap" / "chapter"
    roadmap.mkdir(parents=True)
    (project / "blueprint" / "roadmap" / "README.md").write_text("# Roadmap\n")
    (roadmap / "README.md").write_text("# Chapter\n")
    (roadmap / "node.md").write_text(
        "---\nkind: article\ndeclaration: theorem\norigin: background\n---\n\n# Node\n",
        encoding="utf-8",
    )
    return project


def test_worker_reads_markdown_blueprint_directly(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))

    config = resolve_config(project=project, worker_id="worker")

    assert config.blueprint_path == project / "blueprint"


def test_worker_needs_no_generated_projection_after_markdown_edit(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    page = project / "blueprint" / "roadmap" / "chapter" / "node.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")

    config = resolve_config(project=project, worker_id="worker")
    assert config.blueprint_path == project / "blueprint"
    assert not (project / "graph.json").exists()
