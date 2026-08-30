from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import autoform_cli.execution_input as execution_module
from autoform_cli.execution_input import (
    EXECUTION_INPUT_SCHEMA,
    ExecutionInputError,
    load_execution_input,
)
from autoform_cli.runtime import RUNTIME_SCHEMA, load_runtime_graph


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project(tmp_path: Path, *, v2: bool = True) -> Path:
    project = tmp_path / "project"
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text("# Roadmap\n", encoding="utf-8")
    (roadmap / "result.md").write_text(
        "---\n"
        "declaration: theorem\n"
        "source_units: [result]\n"
        "---\n\n"
        "# Result\n\nA precise result.\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage" / "README.md"
    coverage.parent.mkdir(parents=True)
    if not v2:
        coverage.write_text(
            "# Coverage\n\n"
            "| Area | Coverage | Evidence |\n"
            "| --- | --- | --- |\n"
            "| Result | OUT | Explicitly outside scope |\n",
            encoding="utf-8",
        )
        return project
    artifact = b"The source result.\n"
    source = blueprint / "sources" / "book.md"
    source.parent.mkdir()
    source.write_bytes(artifact)
    coverage.write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        "artifact: sources/book.md\n"
        f"artifact_sha256: {_digest(artifact)}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| result | Main result | 1-1 | Theorem 1 | {_digest(artifact)} | "
        "DECOMPOSED | [Result](../roadmap/result.md) |\n",
        encoding="utf-8",
    )
    return project


def test_builds_deterministic_deeply_immutable_execution_input(tmp_path: Path) -> None:
    project = _project(tmp_path)

    first = load_execution_input(project)
    second = load_execution_input(project)

    assert first == second
    assert first.schema == EXECUTION_INPUT_SCHEMA
    assert first.runtime.schema == RUNTIME_SCHEMA
    assert first.coverage_schema == "autoform-coverage/v2"
    assert first.artifact_path == "sources/book.md"
    assert first.units[0].roadmap_nodes == ("result",)
    assert json.loads(first.to_json()) == first.as_dict()
    assert first.sha256 == second.sha256
    assert str(tmp_path) not in first.to_json()
    with pytest.raises(FrozenInstanceError):
        first.artifact_path = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.units[0].unit = "changed"  # type: ignore[misc]


def test_runtime_v1_shape_does_not_gain_coverage_fields(tmp_path: Path) -> None:
    runtime = load_runtime_graph(_project(tmp_path))
    node = runtime.as_dict()["nodes"][1]

    assert "source_units" not in node
    assert set(runtime.as_dict()) == {
        "article_count",
        "authority",
        "blueprint_path",
        "dependency_count",
        "dispatchable_count",
        "formalizable_count",
        "maximum_depth",
        "nodes",
        "schema",
        "source_revision",
    }


def test_v1_coverage_is_explicitly_refused_for_execution(tmp_path: Path) -> None:
    project = _project(tmp_path, v2=False)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["coverage-v2-required"]


def test_invalid_roadmap_is_reported_through_execution_input_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    article.write_text(article.read_text(encoding="utf-8") + "\n# Second title\n", encoding="utf-8")

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["runtime-invalid"]


def test_concurrent_authority_change_is_not_snapshotted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    article = project / "blueprint/roadmap/result.md"
    original = execution_module.load_coverage
    calls = 0

    def mutate_after_first(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            article.write_text(article.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
        return result

    monkeypatch.setattr(execution_module, "load_coverage", mutate_after_first)

    with pytest.raises(ExecutionInputError) as raised:
        load_execution_input(project)

    assert [issue.code for issue in raised.value.issues] == ["execution-input-changed"]
