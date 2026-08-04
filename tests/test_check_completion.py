"""Focused tests for the deterministic formalization completion gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts"))

import check_completion as cc  # noqa: E402
import target_state  # noqa: E402


def _project(
    tmp_path: Path,
    *,
    targets: dict | None = None,
    scope: list[str] | None = None,
) -> Path:
    lean = tmp_path / "lean"
    lean.mkdir()
    nodes = targets or {}
    if scope is None:
        scope = [
            node["roadmap_id"]
            for node in nodes.values()
            if isinstance(node, dict) and "proof_status" in node
        ]
    graph = {
        "version": 2,
        "metadata": {"lean_root": str(lean), "confirmed_scope": scope},
        "nodes": nodes,
    }
    (tmp_path / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return tmp_path


def _target(status: str = "proved", lean_file: str = "Concentration/Main.lean") -> dict:
    return {
        "spec_status": "ready",
        "proof_status": status,
        "lean_file": lean_file,
        "lean_declarations": ["Concentration.main"],
        "roadmap_id": "goal",
    }


def _stamp_current_state(project: Path, *node_ids: str) -> None:
    graph_path = project / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    root = Path(graph["metadata"]["lean_root"])
    reviews = {}
    for node_id in node_ids:
        fingerprint = target_state.artifact_fingerprint(project, root, node_id, graph["nodes"][node_id])
        graph["nodes"][node_id]["proof_fingerprint"] = fingerprint
        reviews[node_id] = {"ai": {"verdict": "clean", "fingerprint": fingerprint}}
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    (project / "review_status.json").write_text(json.dumps({"reviews": reviews}), encoding="utf-8")


def _write_clean_review(project: Path, *node_ids: str) -> None:
    reviews = {node_id: {"ai": {"verdict": "clean"}} for node_id in node_ids}
    (project / "review_status.json").write_text(json.dumps({"reviews": reviews}), encoding="utf-8")


def test_complete_when_every_target_is_proved_present_clean_and_queue_closed(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    lean_file = tmp_path / "lean" / "Concentration" / "Main.lean"
    lean_file.parent.mkdir()
    lean_file.write_text("theorem done : True := by trivial\n", encoding="utf-8")
    _stamp_current_state(project, "goal")
    (project / "task_queue.json").write_text(json.dumps([{"id": "worker:goal", "status": "done"}]), encoding="utf-8")

    report = cc.check_completion(project)

    assert report.complete
    assert report.target_count == 1
    assert report.issues == ()


def test_zero_explicit_targets_is_incomplete(tmp_path):
    report = cc.check_completion(_project(tmp_path))

    assert not report.complete
    assert report.target_count == 0
    assert "no explicit targets" in report.issues[0]


def test_missing_confirmed_roadmap_target_is_incomplete(tmp_path):
    target = _target()
    target["roadmap_id"] = "scalar.done"
    project = _project(
        tmp_path,
        targets={"goal": target},
        scope=["scalar.done", "scalar.missing"],
    )
    lean_file = tmp_path / "lean" / "Concentration" / "Main.lean"
    lean_file.parent.mkdir()
    lean_file.write_text("theorem done : True := by trivial\n", encoding="utf-8")
    _stamp_current_state(project, "goal")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("scalar.missing" in issue for issue in report.issues)


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_non_proved_target_is_incomplete(tmp_path, status):
    project = _project(tmp_path, targets={"goal": _target(status)})
    (tmp_path / "lean" / "Concentration").mkdir()
    (tmp_path / "lean" / "Concentration" / "Main.lean").touch()
    _write_clean_review(project, "goal")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("proof_status" in issue for issue in report.issues)


def test_missing_lean_file_is_incomplete(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    _write_clean_review(project, "goal")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("Lean file is missing" in issue for issue in report.issues)


def test_draft_spec_is_incomplete(tmp_path):
    target = _target()
    target["spec_status"] = "draft"
    project = _project(tmp_path, targets={"goal": target})
    (tmp_path / "lean" / "Concentration").mkdir()
    (tmp_path / "lean" / "Concentration" / "Main.lean").touch()
    _write_clean_review(project, "goal")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("spec_status" in issue for issue in report.issues)


def test_target_requires_expected_declarations(tmp_path):
    target = _target()
    target["lean_declarations"] = []
    project = _project(tmp_path, targets={"goal": target})

    with pytest.raises(cc.CompletionStateError, match="lean_declarations"):
        cc.check_completion(project)


def test_missing_proof_fingerprint_is_incomplete(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    lean_file = tmp_path / "lean" / "Concentration" / "Main.lean"
    lean_file.parent.mkdir()
    lean_file.write_text("theorem done : True := by trivial\n", encoding="utf-8")
    _stamp_current_state(project, "goal")
    graph = json.loads((project / "graph.json").read_text(encoding="utf-8"))
    del graph["nodes"]["goal"]["proof_fingerprint"]
    (project / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("proof fingerprint" in issue for issue in report.issues)


def test_missing_ai_review_fingerprint_is_incomplete(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    lean_file = tmp_path / "lean" / "Concentration" / "Main.lean"
    lean_file.parent.mkdir()
    lean_file.write_text("theorem done : True := by trivial\n", encoding="utf-8")
    _stamp_current_state(project, "goal")
    sidecar = json.loads((project / "review_status.json").read_text(encoding="utf-8"))
    del sidecar["reviews"]["goal"]["ai"]["fingerprint"]
    (project / "review_status.json").write_text(json.dumps(sidecar), encoding="utf-8")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("AI review fingerprint" in issue for issue in report.issues)


@pytest.mark.parametrize("changed", ["spec", "content", "lean"])
def test_changed_target_state_invalidates_proof_and_review(tmp_path, changed):
    target = _target()
    target["content"] = "informal_content/goal.md"
    project = _project(tmp_path, targets={"goal": target})
    prose = project / "informal_content" / "goal.md"
    prose.parent.mkdir()
    prose.write_text("Prove the stated tail bound.\n", encoding="utf-8")
    lean_file = tmp_path / "lean" / "Concentration" / "Main.lean"
    lean_file.parent.mkdir()
    lean_file.write_text("theorem done : True := by trivial\n", encoding="utf-8")
    _stamp_current_state(project, "goal")

    if changed == "spec":
        graph = json.loads((project / "graph.json").read_text(encoding="utf-8"))
        graph["nodes"]["goal"]["lean_declarations"] = ["Concentration.renamed"]
        (project / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    elif changed == "content":
        prose.write_text("Prove a different tail bound.\n", encoding="utf-8")
    else:
        lean_file.write_text("theorem changed : True := by trivial\n", encoding="utf-8")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("proof fingerprint" in issue for issue in report.issues)
    assert any("AI review fingerprint" in issue for issue in report.issues)


def test_spec_fingerprint_is_independent_of_json_key_order(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    node = json.loads((project / "graph.json").read_text())["nodes"]["goal"]

    forward = target_state.spec_fingerprint(project, "goal", node)
    reverse = target_state.spec_fingerprint(project, "goal", dict(reversed(list(node.items()))))

    assert forward == reverse


def test_fingerprint_rejects_content_outside_project(tmp_path):
    target = _target()
    target["content"] = "../outside.md"
    project = _project(tmp_path, targets={"goal": target})
    (tmp_path.parent / "outside.md").write_text("outside\n", encoding="utf-8")

    with pytest.raises(target_state.TargetStateError, match="content escapes"):
        target_state.spec_fingerprint(project, "goal", target)


@pytest.mark.parametrize("verdict", [None, "flagged", "rejected"])
def test_non_clean_or_missing_ai_review_is_incomplete(tmp_path, verdict):
    project = _project(tmp_path, targets={"goal": _target()})
    (tmp_path / "lean" / "Concentration").mkdir()
    (tmp_path / "lean" / "Concentration" / "Main.lean").touch()
    if verdict is not None:
        (project / "review_status.json").write_text(
            json.dumps({"reviews": {"goal": {"ai": {"verdict": verdict}}}}),
            encoding="utf-8",
        )

    report = cc.check_completion(project)

    assert not report.complete
    assert any("AI review" in issue for issue in report.issues)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_open_queue_task_is_incomplete_even_for_another_node(tmp_path, status):
    project = _project(tmp_path, targets={"goal": _target()})
    (tmp_path / "lean" / "Concentration").mkdir()
    (tmp_path / "lean" / "Concentration" / "Main.lean").touch()
    _write_clean_review(project, "goal")
    (project / "task_queue.json").write_text(json.dumps([{"id": "planner:other", "status": status}]), encoding="utf-8")

    report = cc.check_completion(project)

    assert not report.complete
    assert any("queue task 'planner:other'" in issue for issue in report.issues)


def test_malformed_queue_fails_closed(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    (project / "task_queue.json").write_text("[BROKEN", encoding="utf-8")

    with pytest.raises(cc.CompletionStateError, match="valid queue JSON"):
        cc.check_completion(project)


def test_unknown_queue_status_fails_closed(tmp_path):
    project = _project(tmp_path, targets={"goal": _target()})
    (project / "task_queue.json").write_text(json.dumps([{"id": "worker:goal", "status": "mystery"}]), encoding="utf-8")

    with pytest.raises(cc.CompletionStateError, match="invalid status"):
        cc.check_completion(project)


@pytest.mark.parametrize("lean_file", ["", "../Outside.lean", "/tmp/Outside.lean"])
def test_target_lean_file_must_be_a_safe_relative_path(tmp_path, lean_file):
    project = _project(tmp_path, targets={"goal": _target(lean_file=lean_file)})

    with pytest.raises(cc.CompletionStateError, match="lean_file"):
        cc.check_completion(project)
