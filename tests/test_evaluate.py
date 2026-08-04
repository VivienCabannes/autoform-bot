from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import evaluate


def _task_project(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project = tmp_path / "project"
    corpus = project / "corpus"
    task = corpus / "case-a"
    task.mkdir(parents=True)
    (project / "lakefile.toml").write_text('name = "EvaluateFixture"\n')
    natural = task / "natural_language_statement.md"
    natural.write_text("Prove that True holds.\n")
    statement = task / "formalized_statement.lean"
    statement.write_text("theorem task_one : True := by\n  sorry\n")
    proof = task / "formalized_proof.lean"
    proof.write_text("theorem reference : True := by\n  trivial\n")
    return project, corpus, statement, proof


def test_discover_legacy_cases(tmp_path: Path):
    project, corpus, statement, proof = _task_project(tmp_path)

    cases = evaluate.discover_cases(corpus, project)

    assert [(case.case_id, case.statement_path, case.proof_path) for case in cases] == [
        ("case-a", statement, proof)
    ]


def test_discover_graph_allows_separate_dispatch_project(tmp_path: Path):
    project, _corpus, statement, _proof = _task_project(tmp_path)
    dispatch = tmp_path / "dispatch"
    content = dispatch / "informal_content" / "target.md"
    content.parent.mkdir(parents=True)
    content.write_text("Prove that True holds.\n")
    graph = dispatch / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "metadata": {"lean_root": str(project)},
                "nodes": {
                    "target": {
                        "tier": 2,
                        "lean_file": statement.relative_to(project).as_posix(),
                        "content": "informal_content/target.md",
                    }
                },
            }
        )
    )

    cases = evaluate.discover_cases(graph)

    assert cases[0].repo_root == project
    assert cases[0].statement_path == statement
    assert cases[0].natural_path == content


def test_static_audit_rejects_unsafe_and_opaque_inputs(tmp_path: Path):
    project, corpus, statement, _proof = _task_project(tmp_path)
    statement.write_text(
        "axiom unsupported : True\n"
        "def hidden : True := by sorry\n"
        "theorem target : True := by\n"
        "  run_tac Lean.Elab.Tactic.closeMainGoalUsing `True.intro\n"
    )

    report = evaluate.audit(corpus, project_root=project)

    assert report["summary"] == {"total": 1, "clean": 0, "flagged": 0, "rejected": 1}
    assert {finding["kind"] for finding in report["cases"][0]["findings"]} >= {
        "raw_axiom",
        "sorry_definition",
        "unsafe_elaboration",
    }


def test_model_audit_requires_separate_model_and_api_approvals(tmp_path: Path):
    project, corpus, _statement, _proof = _task_project(tmp_path)

    with pytest.raises(evaluate.EvaluationError, match="confirm-model-use"):
        evaluate.audit(corpus, project_root=project, judge_backend="codex")
    with pytest.raises(evaluate.EvaluationError, match="allow-api-egress"):
        evaluate.audit(
            corpus,
            project_root=project,
            judge_backend="openai",
            confirm_model_use=True,
        )


def test_model_audit_reuses_structured_faithfulness_judge(tmp_path: Path, monkeypatch):
    project, corpus, _statement, _proof = _task_project(tmp_path)
    calls: list[tuple] = []

    def fake_judge(*args, **kwargs):
        calls.append((args, kwargs))
        return {"score": 5, "reasoning": "matches"}

    monkeypatch.setattr(evaluate.judge_runtime, "run_judge", fake_judge)
    report = evaluate.audit(
        corpus,
        project_root=project,
        judge_backend="codex",
        confirm_model_use=True,
    )

    assert report["summary"]["clean"] == 1
    assert calls[0][0][0] == "faithfulness"
    assert calls[0][1]["backend"] == "codex"


def test_benchmark_isolates_tasks_and_preserves_reference_files(tmp_path: Path):
    project, corpus, statement, proof = _task_project(tmp_path)
    output = tmp_path / "results"
    source_statement = statement.read_text()
    source_proof = proof.read_text()

    def fake_runner(**kwargs):
        isolated = Path(kwargs["project_dir"])
        graph = json.loads(Path(kwargs["graph_path"]).read_text())
        node = graph["nodes"][kwargs["node_id"]]
        isolated_statement = isolated / node["lean_file"]
        isolated_proof = isolated / "corpus" / "case-a" / "formalized_proof.lean"
        assert isolated != project
        assert isolated_proof.read_text() == ""
        isolated_statement.write_text(isolated_statement.read_text().replace("sorry", "trivial"))
        return {"status": "proved", "meta": {"usage": {"input_tokens": 7}}}

    report = evaluate.benchmark(
        corpus,
        project_root=project,
        output=output,
        backend="codex",
        confirm_model_use=True,
        runner=fake_runner,
    )

    assert report["summary"] == {"total": 1, "passed": 1, "failed": 0, "error": 0}
    assert statement.read_text() == source_statement
    assert proof.read_text() == source_proof
    row = json.loads((output / "results.jsonl").read_text())
    assert row["verification"] == "autoform-kernel-gate+immutable-theorem-headers"
    assert row["statement_headers_unchanged"] is True
    assert Path(row["artifact"]).read_text().endswith("  trivial\n")


def test_benchmark_rejects_changed_theorem_header(tmp_path: Path):
    project, corpus, statement, proof = _task_project(tmp_path)

    def changing_runner(**kwargs):
        isolated = Path(kwargs["project_dir"])
        graph = json.loads(Path(kwargs["graph_path"]).read_text())
        lean_file = graph["nodes"][kwargs["node_id"]]["lean_file"]
        (isolated / lean_file).write_text("theorem task_one : False := by\n  sorry\n")
        return {"status": "proved"}

    report = evaluate.benchmark(
        corpus,
        project_root=project,
        output=tmp_path / "changed-results",
        backend="codex",
        confirm_model_use=True,
        runner=changing_runner,
    )

    assert report["summary"]["failed"] == 1
    row = json.loads((tmp_path / "changed-results" / "results.jsonl").read_text())
    assert row["statement_headers_unchanged"] is False
    assert statement.read_text() == "theorem task_one : True := by\n  sorry\n"
    assert proof.read_text().startswith("theorem reference")


def test_benchmark_allows_new_helper_declarations(tmp_path: Path):
    project, corpus, _statement, _proof = _task_project(tmp_path)

    def helper_runner(**kwargs):
        isolated = Path(kwargs["project_dir"])
        graph = json.loads(Path(kwargs["graph_path"]).read_text())
        lean_file = graph["nodes"][kwargs["node_id"]]["lean_file"]
        path = isolated / lean_file
        path.write_text(
            path.read_text().replace("sorry", "trivial")
            + "\nlemma benchmark_helper : True := by trivial\n"
        )
        return {"status": "proved"}

    report = evaluate.benchmark(
        corpus,
        project_root=project,
        output=tmp_path / "helper-results",
        backend="codex",
        confirm_model_use=True,
        runner=helper_runner,
    )

    assert report["summary"]["passed"] == 1


def test_benchmark_refuses_symlinks_that_can_escape_isolation(tmp_path: Path):
    project, corpus, _statement, _proof = _task_project(tmp_path)
    outside = tmp_path / "outside.lean"
    outside.write_text("theorem outside : True := by trivial\n")
    (project / "Outside.lean").symlink_to(outside)

    with pytest.raises(evaluate.EvaluationError, match="absolute symlink"):
        evaluate.benchmark(
            corpus,
            project_root=project,
            output=tmp_path / "symlink-results",
            backend="codex",
            confirm_model_use=True,
        )


def test_benchmark_requires_model_and_api_approvals(tmp_path: Path):
    project, corpus, _statement, _proof = _task_project(tmp_path)

    with pytest.raises(evaluate.EvaluationError, match="confirm-model-use"):
        evaluate.benchmark(
            corpus,
            project_root=project,
            output=tmp_path / "unapproved",
            backend="codex",
            confirm_model_use=False,
        )
    with pytest.raises(evaluate.EvaluationError, match="allow-api-egress"):
        evaluate.benchmark(
            corpus,
            project_root=project,
            output=tmp_path / "api-unapproved",
            backend="openai",
            confirm_model_use=True,
        )
