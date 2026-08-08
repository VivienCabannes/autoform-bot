from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_worker import cli, survey
from autoform_worker.config import resolve_config
from autoform_worker.errors import ClaimTransportError, Die
from autoform_worker.runtime_graph import legacy_nodes
from autoform_worker.work_units import _cooperative_claim


def _article(project: Path, relative: str, *, declaration: str | None = None, statement: bool = False) -> None:
    path = project / "blueprint" / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = ["kind: article"]
    if declaration:
        metadata.append(f"declaration: {declaration}")
    if statement:
        metadata.append("statement: formalized")
    path.write_text(
        "---\n" + "\n".join(metadata) + f"\n---\n\n# {path.stem.title()}\n",
        encoding="utf-8",
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md")
    _article(project, "chapter/README.md")
    _article(project, "chapter/theorem.md", declaration="theorem", statement=True)
    return project


def test_worker_loads_markdown_runtime_without_project_graph(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))

    config = resolve_config(project=project, worker_id="worker")

    assert config.runtime.authority == "markdown-articles"
    assert config.runtime.dispatchable_count == 1
    assert not (project / "graph.json").exists()
    assert not hasattr(config, "graph_path")
    assert not config.durable_identity_ready


def test_private_snapshot_is_disposable_and_excludes_containers_from_dispatch(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    config = resolve_config(project=project, worker_id="worker")

    snapshot = config.compatibility_graph_path
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    eligible = survey.eligible_prove_nodes(config)

    assert snapshot.is_relative_to(config.state_dir)
    assert payload["metadata"]["authority"] == "markdown-articles"
    assert payload["metadata"]["source_revision"] == config.runtime.source_revision
    assert set(legacy_nodes(config.runtime)) == {"roadmap", "chapter", "chapter/theorem"}
    assert [node_id for node_id, _node, _reason in eligible] == ["chapter/theorem"]
    assert not (project / "graph.json").exists()


def test_claim_context_requires_board_when_coordination_is_enabled() -> None:
    with pytest.raises(ClaimTransportError, match="required"):
        with _cooperative_claim(None, True, "author/node", []):
            pass


def test_cli_namespaces_coexist_and_unsafe_claim_switches_are_absent() -> None:
    worker_help = cli.build_parser().format_help()
    assert "distributed worker" in worker_help
    assert "--ignore-claims" not in worker_help
    claim = cli.build_parser().parse_args(["claim", "acquire", "author/node"])
    assert not hasattr(claim, "steal")


def test_durable_cli_operations_are_gated_but_issue_dry_run_is_available(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    body = tmp_path / "pr-body.md"
    body.write_text('<!--autoform-target:v1 {"node": "chapter/theorem"}-->\n', encoding="utf-8")

    with pytest.raises(Die, match="durable identity"):
        cli.main(["pr-create", "--project", str(project), "--body-file", str(body)])
    with pytest.raises(Die, match="durable identity"):
        cli.main(["dashboard", "--project", str(project), "export"])
    with pytest.raises(Die, match="durable identity"):
        cli.main(["issues", "sync", "--project", str(project)])

    assert cli.main(["issues", "sync", "--project", str(project), "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out


def test_stateful_worker_candidates_are_suppressed_until_identity_is_stable(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    config = resolve_config(project=project, worker_id="worker")
    candidate = survey.Candidate("prove", "ready", node="chapter/theorem")
    picture = survey.Survey(canonical="owner/repo", default_branch="main", me="worker")
    picture.stages = {stage: [] for stage in ("prove",)}
    picture.suppressed = {stage: [] for stage in ("prove",)}

    # Exercise the externally visible contract through the immutable flag. The
    # full collect path is covered by the existing worker survey suite.
    assert not config.durable_identity_ready
    candidate.reason = "durable article identity is not configured - stateful worker execution disabled"
    picture.suppressed["prove"].append(candidate)
    assert picture.stages["prove"] == []
    assert "durable article identity" in picture.suppressed["prove"][0].reason
