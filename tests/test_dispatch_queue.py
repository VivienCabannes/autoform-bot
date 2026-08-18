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
import multiprocessing
import sys
import threading
from pathlib import Path

import pytest

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
        dq.main([str(proj), "claim", tid])
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
    dq.main([str(proj), "claim", "pl-1"])
    dq.main([str(proj), "done", "pl-1"])
    dq.main([str(proj), "claim", "esc-1"])
    dq.main([str(proj), "fail", "esc-1", "--reason", "dup"])
    by = {t["id"]: t for t in json.loads((proj / "task_queue.json").read_text())}
    assert by["pl-1"]["status"] == "done"
    assert by["esc-1"]["status"] == "failed"
    assert by["esc-1"]["result"] == "dup"
    assert dq._open_orchestrator_tasks(list(by.values())) == []   # both gone


def test_lifecycle_transitions_are_guarded_and_retries_are_idempotent(tmp_path):
    proj = _proj(tmp_path, [
        {"id": "pl-1", "agent": "planner", "node": "C", "status": "queued"},
    ])
    assert dq.main([str(proj), "done", "pl-1"]) == 1
    assert dq.main([str(proj), "claim", "pl-1"]) == 0
    assert dq.main([str(proj), "claim", "pl-1"]) == 0
    assert dq.main([str(proj), "done", "pl-1"]) == 0
    assert dq.main([str(proj), "done", "pl-1"]) == 0
    assert dq.main([str(proj), "claim", "pl-1"]) == 1


def test_corrupt_queue_is_never_replaced_by_enqueue(tmp_path):
    queue = tmp_path / "task_queue.json"
    original = b'[{\"id\":\"survives\"},BROKEN'
    queue.write_bytes(original)
    assert dq.main([
        str(tmp_path), "enqueue", "--agent", "planner", "--node", "C",
    ]) == 2
    assert queue.read_bytes() == original


def test_duplicate_ids_and_duplicate_active_work_fail_closed(tmp_path):
    queue = tmp_path / "task_queue.json"
    queue.write_text(json.dumps([
        {"id": "same", "agent": "planner", "node": "A", "status": "done"},
        {"id": "same", "agent": "planner", "node": "B", "status": "queued"},
    ]))
    with pytest.raises(dq.QueueStateError, match="duplicate task id"):
        dq.load_queue(queue)

    queue.write_text(json.dumps([
        {"id": "one", "agent": "planner", "node": "A", "status": "queued"},
        {"id": "two", "agent": "planner", "node": "A", "status": "running"},
    ]))
    with pytest.raises(dq.QueueStateError, match="duplicate active task"):
        dq.load_queue(queue)


def test_reenqueue_after_completion_gets_a_new_id(tmp_path):
    proj = _proj(tmp_path, [])
    args = [str(proj), "enqueue", "--agent", "planner", "--node", "C"]
    assert dq.main(args) == 0
    first = dq.load_queue(proj / "task_queue.json")[0]["id"]
    assert dq.main([str(proj), "claim", first]) == 0
    assert dq.main([str(proj), "done", first]) == 0
    assert dq.main(args) == 0
    ids = [task["id"] for task in dq.load_queue(proj / "task_queue.json")]
    assert ids == ["planner:C", "planner:C:2"]


def _enqueue_process(project: str, index: int) -> None:
    rc = dq.main([
        project, "enqueue", "--agent", "planner", "--node", f"node-{index}",
    ])
    if rc:
        raise SystemExit(rc)


def test_cross_process_enqueue_stress_has_no_lost_updates(tmp_path):
    _proj(tmp_path, [])
    ctx = multiprocessing.get_context("fork")
    processes = [
        ctx.Process(target=_enqueue_process, args=(str(tmp_path), index))
        for index in range(24)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    tasks = dq.load_queue(tmp_path / "task_queue.json")
    assert len(tasks) == 24
    assert {task["node"] for task in tasks} == {f"node-{index}" for index in range(24)}


def test_concurrent_duplicate_enqueue_is_idempotent(tmp_path):
    _proj(tmp_path, [])
    results: list[int] = []

    def enqueue() -> None:
        results.append(dq.main([
            str(tmp_path), "enqueue", "--agent", "planner", "--node", "same",
        ]))

    threads = [threading.Thread(target=enqueue) for _ in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert results == [0] * 24
    tasks = dq.load_queue(tmp_path / "task_queue.json")
    assert len(tasks) == 1
    assert tasks[0]["id"] == "planner:same"


def test_queue_sync_preserves_native_host_subagents(tmp_path):
    proj = _proj(tmp_path, [])
    assert dq.main([
        str(proj),
        "orchestrator",
        "--state",
        "working",
        "--phase",
        "planning",
        "--detail",
        "split chapter",
    ]) == 0
    assert dq.main([
        str(proj),
        "agent-start",
        "--role",
        "splitter",
        "--name",
        "splitter:chapter-1",
        "--target",
        "chapter-1",
    ]) == 0
    assert dq.main([
        str(proj),
        "enqueue",
        "--agent",
        "worker",
        "--node",
        "lemma-1",
    ]) == 0
    assert dq.main([str(proj), "claim", "worker:lemma-1"]) == 0

    feed = json.loads((proj / "agents_status.json").read_text())
    by_name = {agent["name"]: agent for agent in feed["agents"]}
    assert set(by_name) == {"splitter:chapter-1", "worker:lemma-1"}
    assert by_name["splitter:chapter-1"]["managed_by"] == "native"
    assert by_name["worker:lemma-1"]["managed_by"] == "queue"

    assert dq.main([str(proj), "done", "worker:lemma-1"]) == 0
    feed = json.loads((proj / "agents_status.json").read_text())
    assert [agent["name"] for agent in feed["agents"]] == ["splitter:chapter-1"]
    assert feed["orchestrator"]["phase"] == "planning"
    assert feed["orchestrator"]["detail"] == "split chapter"
    assert dq.main([
        str(proj), "agent-done", "--name", "splitter:chapter-1",
    ]) == 0
