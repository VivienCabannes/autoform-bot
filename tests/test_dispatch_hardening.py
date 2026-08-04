"""Engine-resilience tests for scripts/dispatch_runner.py.

Covers the crash-safety fixes:

  * rubric validation BEFORE claiming — with the internal rubric files absent
    (sibling PR #12 not merged), the engine prints a diagnostic and leaves every
    reviewer task ``queued``; it never bulk-flips them to ``running`` and then
    dies on ``rubrics[axis]``.
  * per-task failure isolation — an unexpected exception while preparing one
    task marks THAT task ``failed`` (error in ``result``) and the rest proceed.
  * startup sweep — reviewer/worker tasks stranded in ``running`` (a previous
    engine died) are reset to ``queued`` with a "requeued after engine restart"
    note; orchestrator-owned kinds are left alone.
  * jury verdict honesty — a partial jury still REJECTS on a failing correctness
    score (never downgraded to flagged by a judge timeout), and an all-abstain
    jury writes NO ai verdict at all: the task fails with "no usable scores".
  * parse_score — an explicit ``{"score": null, "error": …}`` is an abstain
    (score None, error text kept in the reasoning).

All jury runs are simulated by monkeypatching ``run_judge`` / ``load_rubrics``;
no ``claude`` subprocess is ever spawned.
"""
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import dispatch_runner as dr  # noqa: E402
from servers.prover.base import ProofResult  # noqa: E402
from servers.prover.verify import VerifyResult  # noqa: E402


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


def test_nearest_lean_root_uses_dispatch_project_without_lakefile(tmp_path):
    assert dr._nearest_lean_root(tmp_path) == tmp_path.resolve()


def test_nearest_lean_root_finds_lakefile_ancestor(tmp_path):
    (tmp_path / "lakefile.toml").write_text('name = "T"\n')
    plan = tmp_path / "plans" / "main"
    plan.mkdir(parents=True)
    assert dr._nearest_lean_root(plan) == tmp_path.resolve()


def test_build_prompt_prefers_project_target_file():
    rubric = {
        "criteria": {"c": "check it"},
        "prompt_template": "review {lean_file} as {lean_declaration}",
    }
    prompt = dr.build_prompt(
        rubric,
        "target",
        {
            "lean_file": "Project/Target.lean",
            "lean_declarations": ["Project.target"],
            "mathlib_file": "Mathlib/Other.lean",
            "mathlib_declarations": ["Mathlib.other"],
        },
        "",
    )
    assert prompt == "review Project/Target.lean as Project.target"


# ---------------------------------------------------------------------------
# rubric validation BEFORE claim — missing rubrics leave tasks queued
# ---------------------------------------------------------------------------

def test_missing_rubrics_leaves_tasks_queued(tmp_path, monkeypatch, capsys):
    # load_rubrics() returns {} when internal/rubrics/ is absent.
    monkeypatch.setattr(dr, "load_rubrics", lambda: {})
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"},
        {"id": "reviewer:s2", "agent": "reviewer", "node": "s2", "status": "queued"},
    ])
    assert dr.main([str(proj)]) == 0
    # nothing was claimed, nothing crashed — every task is still queued
    assert [t["status"] for t in _queue(proj)] == ["queued", "queued"]
    out = capsys.readouterr().out
    assert "Autoform rubric data not found" in out
    assert "internal/rubrics" in out


def test_rubric_without_prompt_template_also_blocks_claim(tmp_path, monkeypatch):
    # A rubric file that exists but has no prompt_template is just as unusable.
    broken = {ax: {"name": ax, "criteria": {}} for ax in dr.AXES}
    monkeypatch.setattr(dr, "load_rubrics", lambda: broken)
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"}])
    assert dr.main([str(proj)]) == 0
    assert _by_id(proj, "reviewer:s1")["status"] == "queued"


def test_api_backend_requires_explicit_per_process_egress_flag(
    tmp_path, monkeypatch
):
    proj = _proj(tmp_path, [])
    with pytest.raises(SystemExit):
        dr.main(
            [
                str(proj),
                "--dry-run",
                "--backend",
                "openai",
                "--workers",
                "--judge-backend",
                "avocado",
            ]
        )
    assert (
        dr.main(
            [
                str(proj),
                "--dry-run",
                "--backend",
                "openai",
                "--workers",
                "--judge-backend",
                "avocado",
                "--allow-api-egress",
                "openai",
                "--allow-api-egress",
                "avocado",
            ]
        )
        == 0
    )


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


def test_sweep_refuses_corrupt_queue_instead_of_claiming_recovery(tmp_path):
    queue = tmp_path / "task_queue.json"
    queue.write_text("[BROKEN")
    with pytest.raises(dr.dq.QueueStateError):
        dr.sweep_stale_running(queue, tmp_path / "agents_status.json")
    assert queue.read_text() == "[BROKEN"


def test_dispatcher_lease_blocks_a_second_engine_and_releases(tmp_path, capsys):
    proj = _proj(tmp_path, [])
    with dr.dispatcher_lease(proj):
        assert dr.main([str(proj), "--backend", "codex"]) == 3
        assert "already owns" in capsys.readouterr().err
    # The kernel releases flock on normal exit; a later dispatcher proceeds.
    assert dr.main([str(proj), "--backend", "codex"]) == 0


def test_dispatcher_lease_releases_after_exception(tmp_path):
    proj = _proj(tmp_path, [])
    with pytest.raises(RuntimeError, match="synthetic crash"):
        with dr.dispatcher_lease(proj):
            raise RuntimeError("synthetic crash")
    with dr.dispatcher_lease(proj):
        pass


@pytest.mark.parametrize("backend", ["aristotle", "claude", "codex", "openai", "avocado"])
def test_worker_timeout_reaches_every_adapter(backend):
    adapter = dr._worker_adapter(backend, "/repo", "/repo/graph.json", 17)
    if backend in {"openai", "avocado"}:
        assert adapter._timeout == 17
    else:
        assert adapter._max_wait_seconds == 17


def test_direct_dispatch_worker_records_the_shared_usage_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "_PROVER_OK", True)
    monkeypatch.setattr(
        dr,
        "_build_node_spec",
        lambda *args, **kwargs: "prove the pilot theorem",
    )
    monkeypatch.setattr(
        dr,
        "_worker_adapter",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        dr,
        "_prove",
        lambda *args, **kwargs: ProofResult(
            status="proved",
            backend="codex",
            meta={
                "model": "pilot-model",
                "usage": {
                    "worker": {"input_tokens": 12, "output_tokens": 3},
                    "judge": {},
                    "wall_seconds": 1.5,
                },
            },
        ),
    )
    status, _reason, _detail = dr.run_worker(
        "Pilot",
        {"kind": "theorem"},
        tmp_path,
        str(tmp_path / "graph.json"),
        str(tmp_path),
        0,
        backend="codex",
        worker_timeout=17,
    )
    assert status == "proved"
    entries = [
        json.loads(line)
        for line in (tmp_path / ".autoform" / "usage.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 1
    assert entries[0]["node"] == "Pilot"
    assert entries[0]["backend"] == "codex"
    assert entries[0]["usage"]["worker"]["input_tokens"] == 12


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--timeout", "0"),
        ("--jobs", "0"),
        ("--poll", "0"),
        ("--max-steers", "-1"),
        ("--max-escalations", "-1"),
    ],
)
def test_dispatcher_rejects_nonpositive_runtime_bounds(tmp_path, flag, value):
    proj = _proj(tmp_path, [])
    with pytest.raises(SystemExit) as error:
        dr.main([str(proj), "--backend", "codex", flag, value])
    assert error.value.code == 2


def test_escalation_ids_remain_unique_across_retries():
    queue = []
    assert dr._raise_escalation(queue, "s1", "S1", "first", max_escalations=3)
    queue[0]["status"] = "done"
    assert dr._raise_escalation(queue, "s1", "S1", "second", max_escalations=3)
    assert [task["id"] for task in queue] == [
        "escalation:s1",
        "escalation:s1:2",
    ]


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


def test_watch_reloads_graph_before_reviewing_new_node(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    prompts = []
    monkeypatch.setattr(
        dr,
        "run_judge",
        lambda axis, prompt, repo, model, timeout: (
            prompts.append(prompt) or {"score": 5, "reasoning": "fine"}
        ),
    )
    proj = _proj(tmp_path, [])
    polls = 0

    def advance_watch(_seconds):
        nonlocal polls
        polls += 1
        if polls == 1:
            graph = json.loads((proj / "graph.json").read_text())
            graph["nodes"]["new"] = {
                "tier": 2,
                "parent": None,
                "kind": "corollary",
                "name": "New statement",
                "mathlib_status": "missing",
                "depends_on": [],
            }
            (proj / "graph.json").write_text(json.dumps(graph))
            (proj / "task_queue.json").write_text(json.dumps([
                {"id": "reviewer:new", "agent": "reviewer", "node": "new",
                 "status": "queued"},
            ]))
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(dr.time, "sleep", advance_watch)
    assert dr.main([str(proj), "--watch", "--poll", "1"]) == 0
    assert _by_id(proj, "reviewer:new")["status"] == "done"
    assert len(prompts) == len(dr.AXES)
    assert all("(corollary)" in prompt for prompt in prompts)


def test_watch_reloads_graph_for_each_new_worker_task(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    seen_nodes = []

    def worker(node_id, node, *args, **kwargs):
        seen_nodes.append((node_id, node))
        return "proved", "complete", ""

    monkeypatch.setattr(dr, "run_worker", worker)
    proj = _proj(tmp_path, [])
    polls = 0

    def advance_watch(_seconds):
        nonlocal polls
        polls += 1
        if polls == 1:
            graph = json.loads((proj / "graph.json").read_text())
            graph["nodes"]["new"] = {
                "tier": 2,
                "parent": None,
                "kind": "lemma",
                "name": "Fresh worker target",
                "description": "Use the newly merged prerequisite",
                "mathlib_status": "missing",
                "depends_on": ["s2"],
            }
            (proj / "graph.json").write_text(json.dumps(graph))
            (proj / "task_queue.json").write_text(json.dumps([
                {"id": "worker:new", "agent": "worker", "node": "new",
                 "status": "queued"},
            ]))
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(dr.time, "sleep", advance_watch)
    assert dr.main([
        str(proj), "--watch", "--poll", "1", "--workers", "--backend", "codex",
    ]) == 0
    assert _by_id(proj, "worker:new")["status"] == "done"
    assert seen_nodes == [("new", json.loads((proj / "graph.json").read_text())["nodes"]["new"])]


def test_verified_explicit_target_is_marked_and_requeued_for_review(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    monkeypatch.setattr(
        dr,
        "run_worker",
        lambda *args, **kwargs: ("proved", "kernel gate clean", "proof report"),
    )
    proj = _proj(tmp_path, [
        {"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    graph["nodes"]["s1"].update({
        "spec_status": "ready",
        "proof_status": "pending",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    (proj / "graph.json").write_text(json.dumps(graph))
    target = proj / "Project" / "S1.lean"
    target.parent.mkdir()
    target.write_text("theorem s1 : True := by trivial\n")
    (proj / "review_status.json").write_text(json.dumps({
        "version": 1,
        "settings": {"dial": "on-demand"},
        "reviews": {"s1": {
            "ai": {"verdict": "rejected"},
            "human": {"verdict": "accepted"},
        }},
    }))

    assert dr.main([
        str(proj), "--workers", "--backend", "codex", "--judge-backend", "codex",
    ]) == 0

    saved = json.loads((proj / "graph.json").read_text())
    assert saved["nodes"]["s1"]["proof_status"] == "proved"
    assert saved["nodes"]["s1"]["proof_verified_at"]
    assert saved["nodes"]["s1"]["proof_fingerprint"]
    review = _sidecar(proj)["reviews"]["s1"]
    assert "ai" not in review
    assert review["human"] == {"verdict": "accepted"}
    queue = _queue(proj)
    assert _by_id(proj, "worker:s1")["status"] == "done"
    queued = [t for t in queue if t["agent"] == "reviewer" and t["node"] == "s1"]
    assert len(queued) == 1 and queued[0]["status"] == "queued"
    assert queued[0]["source"] == "engine:verified-proof"


def test_existing_clean_target_is_stamped_without_prover_run(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    monkeypatch.setattr(
        dr,
        "_verify_existing_proof",
        lambda *args, **kwargs: VerifyResult(True, "", {"kernel": "clean"}),
    )
    monkeypatch.setattr(
        dr,
        "run_worker",
        lambda *args, **kwargs: pytest.fail("clean existing target must skip prover"),
    )
    proj = _proj(tmp_path, [
        {"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    graph["nodes"]["s1"].update({
        "spec_status": "ready",
        "proof_status": "pending",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    (proj / "graph.json").write_text(json.dumps(graph))
    target = proj / "Project" / "S1.lean"
    target.parent.mkdir()
    target.write_text("theorem s1 : True := by trivial\n")

    assert dr.main([str(proj), "--workers", "--backend", "codex"]) == 0

    saved = json.loads((proj / "graph.json").read_text())
    assert saved["nodes"]["s1"]["proof_status"] == "proved"
    assert saved["nodes"]["s1"]["proof_fingerprint"]
    assert _by_id(proj, "worker:s1")["result"].startswith("proved: existing target verified")


def test_repair_target_with_prior_proof_is_sent_to_prover(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    monkeypatch.setattr(
        dr,
        "_verify_existing_proof",
        lambda *args, **kwargs: pytest.fail("repair must not use existing-target shortcut"),
    )
    called = False

    def worker(*args, **kwargs):
        nonlocal called
        called = True
        return "proved", "repaired", "proof report"

    monkeypatch.setattr(dr, "run_worker", worker)
    proj = _proj(tmp_path, [
        {"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    graph["nodes"]["s1"].update({
        "spec_status": "ready",
        "proof_status": "pending",
        "proof_fingerprint": "previous-rejected-proof",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    (proj / "graph.json").write_text(json.dumps(graph))
    target = proj / "Project" / "S1.lean"
    target.parent.mkdir()
    target.write_text("theorem s1 : True := by trivial\n")

    assert dr.main([str(proj), "--workers", "--backend", "codex"]) == 0

    assert called
    saved = json.loads((proj / "graph.json").read_text())
    assert saved["nodes"]["s1"]["proof_status"] == "proved"
    assert saved["nodes"]["s1"]["proof_fingerprint"] != "previous-rejected-proof"


def test_draft_explicit_target_is_not_sent_to_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    called = False

    def worker(*args, **kwargs):
        nonlocal called
        called = True
        return "proved", "", ""

    monkeypatch.setattr(dr, "run_worker", worker)
    proj = _proj(tmp_path, [
        {"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    graph["nodes"]["s1"].update({
        "spec_status": "draft",
        "proof_status": "pending",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    (proj / "graph.json").write_text(json.dumps(graph))

    assert dr.main([str(proj), "--workers", "--backend", "codex"]) == 0

    assert not called
    task = _by_id(proj, "worker:s1")
    assert task["status"] == "failed"
    assert "not proof-ready" in task["result"]


def test_target_changed_during_worker_is_not_marked_proved(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    proj = _proj(tmp_path, [
        {"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    graph["nodes"]["s1"].update({
        "spec_status": "ready",
        "proof_status": "pending",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    (proj / "graph.json").write_text(json.dumps(graph))
    target = proj / "Project" / "S1.lean"
    target.parent.mkdir()
    target.write_text("theorem s1 : True := by trivial\n")

    def worker(*args, **kwargs):
        changed = json.loads((proj / "graph.json").read_text())
        changed["nodes"]["s1"]["description"] = "a changed statement"
        (proj / "graph.json").write_text(json.dumps(changed))
        return "proved", "kernel gate clean", "proof report"

    monkeypatch.setattr(dr, "run_worker", worker)

    assert dr.main([str(proj), "--workers", "--backend", "codex"]) == 0

    saved = json.loads((proj / "graph.json").read_text())
    assert saved["nodes"]["s1"]["proof_status"] == "pending"
    task = _by_id(proj, "worker:s1")
    assert task["status"] == "failed"
    assert "specification changed" in task["result"]


def test_proved_target_review_records_current_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    monkeypatch.setattr(
        dr,
        "run_judge",
        lambda *args, **kwargs: {"score": 5, "reasoning": "clean"},
    )
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    node = graph["nodes"]["s1"]
    node.update({
        "spec_status": "ready",
        "proof_status": "proved",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    target = proj / "Project" / "S1.lean"
    target.parent.mkdir()
    target.write_text("theorem s1 : True := by trivial\n")
    fingerprint = dr.target_state.artifact_fingerprint(proj, proj, "s1", node)
    node["proof_fingerprint"] = fingerprint
    (proj / "graph.json").write_text(json.dumps(graph))

    assert dr.main([str(proj), "--judge-backend", "codex"]) == 0

    ai = _sidecar(proj)["reviews"]["s1"]["ai"]
    assert ai["verdict"] == "clean"
    assert ai["fingerprint"] == fingerprint


def test_escalation_cap_marks_explicit_target_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)
    monkeypatch.setattr(
        dr,
        "run_worker",
        lambda *args, **kwargs: pytest.fail("capped target must not run"),
    )
    proj = _proj(tmp_path, [
        {"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"},
        {"id": "escalation:s1", "agent": "escalation", "node": "s1", "status": "done"},
    ])
    graph = json.loads((proj / "graph.json").read_text())
    graph["nodes"]["s1"].update({
        "spec_status": "ready",
        "proof_status": "pending",
        "lean_file": "Project/S1.lean",
        "lean_declarations": ["s1"],
        "roadmap_id": "scalar.s1",
    })
    (proj / "graph.json").write_text(json.dumps(graph))

    assert dr.main([
        str(proj), "--workers", "--backend", "codex", "--max-escalations", "1",
    ]) == 0

    saved = json.loads((proj / "graph.json").read_text())
    assert saved["nodes"]["s1"]["proof_status"] == "blocked"
    assert saved["nodes"]["s1"]["proof_blocked_at"]
    assert _by_id(proj, "worker:s1")["status"] == "failed"


# ---------------------------------------------------------------------------
# jury verdict honesty — no downgrade on a partial jury; no verdict on abstain
# ---------------------------------------------------------------------------

def _run_with_scores(tmp_path, monkeypatch, scores):
    """Drive one reviewer task through main() with canned per-axis judge results."""
    monkeypatch.setattr(dr, "load_rubrics", lambda: _FAKE_RUBRICS)

    def judge(axis, prompt, repo, model, timeout):
        s = scores.get(axis)
        if s is None:
            return {"score": None, "reasoning": f"{axis}: judge timed out after {timeout}s",
                    "error": "timeout"}
        return {"score": s, "reasoning": "canned"}

    monkeypatch.setattr(dr, "run_judge", judge)
    proj = _proj(tmp_path, [
        {"id": "reviewer:s1", "agent": "reviewer", "node": "s1", "status": "queued"}])
    assert dr.main([str(proj)]) == 0
    return proj


def test_partial_jury_still_rejects_never_downgrades(tmp_path, monkeypatch):
    # faithfulness=1 (rejectable) + proof_integrity=5, third judge timed out:
    # the verdict must be REJECTED — not silently downgraded to flagged.
    proj = _run_with_scores(tmp_path, monkeypatch,
                            {"faithfulness": 1, "proof_integrity": 5,
                             "code_quality": None})
    ai = _sidecar(proj)["reviews"]["s1"]["ai"]
    assert ai["verdict"] == "rejected"
    assert ai["code_quality"] is None            # the missing score stays visible
    assert _by_id(proj, "reviewer:s1")["status"] == "done"


def test_all_judges_failed_writes_no_ai_verdict_and_fails_task(tmp_path, monkeypatch):
    # Every judge timed out/abstained: no usable score exists, so no ai verdict is
    # written at all and the task fails (re-queueable) rather than pretending.
    proj = _run_with_scores(tmp_path, monkeypatch,
                            {ax: None for ax in dr.AXES})
    assert "s1" not in _sidecar(proj)["reviews"]
    t = _by_id(proj, "reviewer:s1")
    assert t["status"] == "failed"
    assert "no usable scores" in t["result"]


# ---------------------------------------------------------------------------
# parse_score — explicit {"score": null, "error": …} is an abstain
# ---------------------------------------------------------------------------

def test_parse_score_null_is_abstain_and_keeps_error_text():
    out = dr.parse_score(json.dumps(
        {"score": None, "error": "missing source refs — cannot judge faithfulness"}),
        "faithfulness")
    assert out["score"] is None
    assert out["error"] == "abstain"
    assert "missing source refs" in out["reasoning"]


def test_parse_score_null_with_reasoning_keeps_both():
    out = dr.parse_score(json.dumps(
        {"score": None, "reasoning": "no source to compare against",
         "error": "no-source"}), "faithfulness")
    assert out["score"] is None
    assert out["error"] == "abstain"
    assert "no source to compare against" in out["reasoning"]
    assert "no-source" in out["reasoning"]


def test_parse_score_integer_still_parses():
    out = dr.parse_score(json.dumps({"score": 4, "reasoning": "solid"}), "code_quality")
    assert out == {"score": 4, "reasoning": "solid"}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
