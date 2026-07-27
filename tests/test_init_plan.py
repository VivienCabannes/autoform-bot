from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.init_plan import initialize


def test_initialize_creates_graph_with_lean_root(tmp_path: Path):
    plan = tmp_path / "plan"
    lean = tmp_path / "lean"
    plan.mkdir()
    lean.mkdir()
    action, snapshot = initialize(plan, lean)
    graph = json.loads((plan / "graph.json").read_text(encoding="utf-8"))
    assert action == "created"
    assert snapshot is None
    assert graph["metadata"]["lean_root"] == str(lean.resolve())
    assert graph["nodes"] == {}


def test_initialize_preserves_existing_graph(tmp_path: Path):
    plan = tmp_path / "plan"
    lean = tmp_path / "lean"
    plan.mkdir()
    lean.mkdir()
    graph_path = plan / "graph.json"
    graph_path.write_text(
        json.dumps({"version": 2, "metadata": {}, "nodes": {"n": {"tier": 1}}}),
        encoding="utf-8",
    )
    action, _ = initialize(plan, lean)
    assert action == "updated"
    assert "n" in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
    action, _ = initialize(plan, lean)
    assert action == "unchanged"


def test_initialize_rejects_different_existing_lean_root(tmp_path: Path):
    plan = tmp_path / "plan"
    first = tmp_path / "first"
    second = tmp_path / "second"
    plan.mkdir()
    first.mkdir()
    second.mkdir()
    initialize(plan, first)
    with pytest.raises(ValueError, match="points elsewhere"):
        initialize(plan, second)


def test_reset_snapshots_and_clears_durable_plan_state(tmp_path: Path):
    plan = tmp_path / "plan"
    lean = tmp_path / "lean"
    plan.mkdir()
    lean.mkdir()
    (plan / "informal_content").mkdir()
    (plan / "informal_content" / "n.md").write_text("proof prose", encoding="utf-8")
    (plan / "graph.json").write_text(
        json.dumps({"version": 2, "metadata": {}, "nodes": {"n": {}}}),
        encoding="utf-8",
    )
    (plan / "task_queue.json").write_text("{}", encoding="utf-8")

    action, snapshot = initialize(plan, lean, reset=True)
    assert action == "reset"
    assert snapshot is not None
    assert (snapshot / "graph.json").exists()
    assert (snapshot / "informal_content" / "n.md").read_text() == "proof prose"
    assert (snapshot / "task_queue.json").exists()
    assert not (plan / "task_queue.json").exists()
    assert not (plan / "informal_content").exists()
    assert json.loads((plan / "graph.json").read_text())["nodes"] == {}


def test_reset_refuses_symlinked_plan_state(tmp_path: Path):
    plan = tmp_path / "plan"
    lean = tmp_path / "lean"
    outside = tmp_path / "outside"
    plan.mkdir()
    lean.mkdir()
    outside.mkdir()
    (plan / "informal_content").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        initialize(plan, lean, reset=True)
