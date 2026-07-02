"""Tests for the ``dispatch_queue`` CLI — the orchestrator/engine queue bridge.

Focus: the orchestrator-owned lifecycle surface, so planner/review/mathcheck tasks
(not just escalations) are visible and resolvable:
  * ``_open_orchestrator_tasks`` / ``_open_escalations`` — who owns what; open vs resolved.
  * ``mine``         — the full orchestrator worklist (all 6 kinds, escalations first).
  * ``escalations``  — the escalation subset, with full notes.
  * ``status``       — banners open orchestrator-owned work; gone once resolved.
  * ``done``/``fail`` — both clear a task from the worklist; the engine never does.
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts"))

import dispatch_queue as dq   # noqa: E402


def _proj(tmp_path, tasks):
    (tmp_path / "task_queue.json").write_text(json.dumps(tasks))
    return tmp_path


# one of every relevant shape: both engine kinds, four orchestrator kinds, one resolved
_MIXED = [
    {"id": "rev-1", "agent": "reviewer", "node": "a", "status": "queued"},
    {"id": "wk-1", "agent": "worker", "node": "b", "status": "running"},
    {"id": "pl-1", "agent": "planner", "node": "C", "status": "queued"},
    {"id": "gr-1", "agent": "graphreview", "node": "D", "status": "queued"},
    {"id": "mc-1", "agent": "mathcheck", "node": "e", "status": "running"},
    {"id": "esc-1", "agent": "escalation", "node": "b", "status": "queued",
     "note": "FAILED: needs lemma X"},
    {"id": "rev-2", "agent": "reviewer", "node": "f", "status": "done"},
]


def test_open_orchestrator_tasks_are_the_six_kinds_open_only():
    open_orch = dq._open_orchestrator_tasks(_MIXED)
    assert sorted(t["agent"] for t in open_orch) == \
        ["escalation", "graphreview", "mathcheck", "planner"]
    # engine kinds excluded even when queued/running; resolved tasks excluded
    assert all(t["agent"] not in dq._ENGINE_KINDS for t in open_orch)


def test_open_escalations_is_a_subset_of_the_worklist():
    esc = dq._open_escalations(_MIXED)
    assert [t["id"] for t in esc] == ["esc-1"]
    assert {t["id"] for t in esc} <= {t["id"] for t in dq._open_orchestrator_tasks(_MIXED)}


def test_mine_lists_every_open_orch_task_with_notes(tmp_path, capsys):
    rc = dq.main([str(_proj(tmp_path, _MIXED)), "mine"])
    out = capsys.readouterr().out
    assert rc == 0
    for node in ("C", "D", "e", "b"):                 # planner/graphreview/mathcheck/escalation
        assert node in out
    assert "FAILED: needs lemma X" in out             # escalation note rendered inline
    # engine-owned reviewer/worker tasks are NOT the orchestrator's worklist
    assert "rev-1" not in out and "wk-1" not in out


def test_status_banner_appears_then_clears_on_resolve(tmp_path, capsys):
    proj = _proj(tmp_path, _MIXED)
    dq.main([str(proj), "status"])
    assert "AWAIT THE ORCHESTRATOR" in capsys.readouterr().out
    for tid in ("pl-1", "gr-1", "mc-1", "esc-1"):
        dq.main([str(proj), "done", tid])
    capsys.readouterr()                               # discard the done-line output
    dq.main([str(proj), "status"])
    assert "AWAIT THE ORCHESTRATOR" not in capsys.readouterr().out
    dq.main([str(proj), "mine"])
    assert "nothing awaiting" in capsys.readouterr().out


def test_done_and_fail_both_clear_from_the_worklist(tmp_path):
    tasks = [
        {"id": "pl-1", "agent": "planner", "node": "C", "status": "queued"},
        {"id": "esc-1", "agent": "escalation", "node": "b", "status": "queued", "note": "x"},
    ]
    proj = _proj(tmp_path, tasks)
    dq.main([str(proj), "done", "pl-1"])
    dq.main([str(proj), "fail", "esc-1", "--reason", "dup"])
    by = {t["id"]: t for t in json.loads((proj / "task_queue.json").read_text())}
    assert by["pl-1"]["status"] == "done"
    assert by["esc-1"]["status"] == "failed"
    assert by["esc-1"]["result"] == "dup"
    assert dq._open_orchestrator_tasks(list(by.values())) == []   # both gone
