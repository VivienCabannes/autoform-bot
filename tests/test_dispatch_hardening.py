"""Engine-resilience tests for scripts/dispatch_runner.py.

Covers the crash-safety fixes:

  * rubric validation BEFORE claiming — with the eval-rubrics files absent
    (sibling PR #12 not merged), the engine prints a diagnostic and leaves every
    reviewer task ``queued``; it never bulk-flips them to ``running`` and then
    dies on ``rubrics[axis]``.
  * per-task failure isolation — an unexpected exception while preparing one
    task marks THAT task ``failed`` (error in ``result``) and the rest proceed.
  * startup sweep — reviewer/worker tasks stranded in ``running`` (a previous
    engine died) are reset to ``queued`` with a "requeued after engine restart"
    note; orchestrator-owned kinds are left alone.

All jury runs are simulated by monkeypatching ``run_judge`` / ``load_rubrics``;
no ``claude`` subprocess is ever spawned.
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import dispatch_runner as dr  # noqa: E402


GRAPH = {
    "metadata": {"title": "t"},
    "nodes": {
        "s1": {"tier": 2, "parent": None, "kind": "lemma", "name": "Stmt 1",
               "mathlib_status": "missing", "depends_on": []},
        "s2": {"tier": 2, "parent": None, "kind": "theorem", "name": "Stmt 2",
               "mathlib_status": "missing", "depends_on": ["s1"]},
    },
}

# A complete rubric set (every axis carries a prompt_template) for simulated runs.
_FAKE_RUBRICS = {ax: {"name": ax, "criteria": {"c": "check it"},
                      "prompt_template": "judge {name} ({kind}) on: {criteria}"}
                 for ax in dr.AXES}


def _proj(tmp_path, queue):
    (tmp_path / "graph.json").write_text(json.dumps(GRAPH))
    (tmp_path / "task_queue.json").write_text(json.dumps(queue))
    return tmp_path


def _queue(tmp_path):
    return json.loads((tmp_path / "task_queue.json").read_text())


def _by_id(tmp_path, tid):
    return next(t for t in _queue(tmp_path) if t["id"] == tid)


def _sidecar(tmp_path):
    p = tmp_path / "review_status.json"
    return json.loads(p.read_text()) if p.exists() else {"reviews": {}}


# ---------------------------------------------------------------------------
# rubric validation BEFORE claim — missing rubrics leave tasks queued
# ---------------------------------------------------------------------------

def test_missing_rubrics_leaves_tasks_queued(tmp_path, monkeypatch, capsys):
    # load_rubrics() returns {} when skills/eval-rubrics/references/ is absent.
    monkeypatch.setattr(dr, "load_rubrics", lambda: {})
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"},
        {"id": "reviewer:s2", "agent": "reviewer", "node": "s2", "status": "queued"},
    ])
    assert dr.main([str(proj)]) == 0
    # nothing was claimed, nothing crashed — every task is still queued
    assert [t["status"] for t in _queue(proj)] == ["queued", "queued"]
    out = capsys.readouterr().out
    assert "eval-rubrics" in out
    assert "PR #12" in out


def test_rubric_without_prompt_template_also_blocks_claim(tmp_path, monkeypatch):
    # A rubric file that exists but has no prompt_template is just as unusable.
    broken = {ax: {"name": ax, "criteria": {}} for ax in dr.AXES}
    monkeypatch.setattr(dr, "load_rubrics", lambda: broken)
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"}])
    assert dr.main([str(proj)]) == 0
    assert _by_id(proj, "reviewer:s1")["status"] == "queued"


# ---------------------------------------------------------------------------
# per-task failure isolation — one bad task never sinks the run
# ---------------------------------------------------------------------------

def test_one_bad_task_fails_alone_others_proceed(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)

    def boom_prompt(rubric, node_id, node, content_text):
        if node_id == "s1":
            raise RuntimeError("synthetic prompt failure")
        return "ok"

    monkeypatch.setattr(dr, "build_prompt", boom_prompt)
    monkeypatch.setattr(dr, "run_judge",
                        lambda axis, prompt, repo, model, timeout:
                        {"score": 5, "reasoning": "fine"})
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"},
        {"id": "reviewer:s2", "agent": "reviewer", "node": "s2", "status": "queued"},
    ])
    assert dr.main([str(proj)]) == 0
    t1, t2 = _by_id(proj, "reviewer:s1"), _by_id(proj, "reviewer:s2")
    assert t1["status"] == "failed"
    assert "synthetic prompt failure" in t1["result"]
    assert t2["status"] == "done"
    assert _sidecar(proj)["reviews"]["s2"]["ai"]["verdict"] == "clean"


# ---------------------------------------------------------------------------
# startup sweep — 'running' engine tasks from a dead engine are re-queued
# ---------------------------------------------------------------------------

def test_sweep_requeues_stranded_running_engine_tasks(tmp_path):
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1",
         "status": "running", "started_at": "2026-01-01T00:00:00Z"},
        {"id": "worker:s2", "agent": "worker", "node": "s2",
         "status": "running", "note": "prior note"},
        {"id": "esc-1", "agent": "escalation", "node": "s1", "status": "running"},
        {"id": "reviewer:s2", "agent": "reviewer", "node": "s2", "status": "done"},
    ])
    n = dr.sweep_stale_running(proj / "task_queue.json", proj / "agents_status.json")
    assert n == 2
    rev = _by_id(proj, "reviewer:s1")
    assert rev["status"] == "queued"
    assert rev["note"] == "requeued after engine restart"
    assert "started_at" not in rev
    wk = _by_id(proj, "worker:s2")
    assert wk["status"] == "queued"
    assert wk["note"] == "prior note · requeued after engine restart"
    # orchestrator-owned + finished tasks are untouched
    assert _by_id(proj, "esc-1")["status"] == "running"
    assert _by_id(proj, "reviewer:s2")["status"] == "done"
    # the feed reflects the swept queue (nothing running -> idle)
    feed = json.loads((proj / "agents_status.json").read_text())
    assert all(a["role"] != "reviewer" for a in feed["agents"])


def test_sweep_noop_without_stranded_tasks(tmp_path):
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"}])
    before = _queue(proj)
    assert dr.sweep_stale_running(proj / "task_queue.json",
                                  proj / "agents_status.json") == 0
    assert _queue(proj) == before


def test_main_runs_the_sweep_then_drains_the_requeued_task(tmp_path, monkeypatch):
    # A stranded 'running' reviewer is recovered at startup AND then drained.
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    monkeypatch.setattr(dr, "run_judge",
                        lambda axis, prompt, repo, model, timeout:
                        {"score": 4, "reasoning": "ok"})
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "running"}])
    assert dr.main([str(proj)]) == 0
    t = _by_id(proj, "reviewer:s1")
    assert t["status"] == "done"
    assert "requeued after engine restart" in t["note"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
