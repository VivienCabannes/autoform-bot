"""Park-resume symmetry — a parked recovery must never be a grave.

The evidence gate that refuses a blind retry (``unchanged_recovery``) has to run
in both directions: the moment a parked node's durable prover inputs move —
prose, Lean, its graph record, or the backend — the park is resumable. Without
that, a parked node is unreachable forever and an unattended fleet silently
loses it.

Everything here is offline: GitHost gets a canned runner, the claim board is
never contacted, and every project lives under ``tmp_path``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import dispatch_runner as dr  # noqa: E402
import recovery_state as rs  # noqa: E402
from autoform_worker import round as round_mod  # noqa: E402
from autoform_worker import survey  # noqa: E402
from autoform_worker.config import resolve_config  # noqa: E402
from autoform_worker.counters import Counters  # noqa: E402
from autoform_worker.errors import Die  # noqa: E402
from autoform_worker.githost import GitHost  # noqa: E402
from tests.worker_markdown import write_markdown_roadmap  # noqa: E402

NODE = "target"
TASK = "escalation:target"
ADAPTER = "claude"          # backend_config.prover_of("max") — the survey's default


# -- fixtures-by-hand --------------------------------------------------------

def make_project(tmp_path):
    """A dispatch project whose every fingerprint input actually exists on disk.

    graph.json + informal_content/<node>.md + a sorry'd Lean file, with the Lean
    root pointed at a sibling dir so a Lean edit is provably distinct from a
    project edit.
    """
    proj = tmp_path / "proj"
    lean = tmp_path / "lean"
    (proj / "informal_content").mkdir(parents=True, exist_ok=True)
    lean.mkdir(exist_ok=True)
    (proj / "informal_content" / f"{NODE}.md").write_text("strategy: induct on n\n", encoding="utf-8")
    (lean / "Target.lean").write_text("theorem target : True := by sorry\n", encoding="utf-8")
    nodes = {NODE: {"tier": 2, "mathlib_status": "missing", "depends_on": [],
                    "lean_file": "Target.lean"}}
    (proj / "graph.json").write_text(json.dumps({
        "version": 2,
        "metadata": {"lean_root": str(lean)},
        "nodes": nodes,
    }), encoding="utf-8")
    write_markdown_roadmap(proj, nodes, lean_root=lean)
    (proj / "review_status.json").write_text(json.dumps({
        "version": 1, "settings": {"dial": "on-demand"}, "reviews": {},
    }), encoding="utf-8")
    return proj, lean


def parked_queue(fingerprint, task_id=TASK, node=NODE, status="parked"):
    return [{"id": task_id, "agent": "escalation", "node": node, "status": status,
             "recovery": {"version": 1, "phase": "parked", "round": 1,
                          "fingerprint": fingerprint, "backend": ADAPTER}}]


def make_cfg(tmp_path, monkeypatch, worker_id="w1"):
    proj, _lean = make_project(tmp_path)
    monkeypatch.setenv("AUTOFORM_DISPATCH_PROJECT", str(proj))
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AUTOFORM_LEAN_ROOT", str(_lean))
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "autoform-config.json"))
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", str(tmp_path / "remotes"))
    monkeypatch.delenv("AUTOFORM_RESPECT_CLAIMS", raising=False)
    monkeypatch.delenv("AUTOFORM_CANONICAL_REPO", raising=False)
    monkeypatch.delenv("AUTOFORM_CLAIM_REPO", raising=False)
    return resolve_config(worker_id=worker_id)


def park_recovery(cfg, fingerprint=None):
    """Park a recovery carrying the fingerprint of the project's CURRENT state."""
    if fingerprint is None:
        fingerprint = rs.proof_fingerprint(
            cfg.compatibility_graph_path, NODE, cfg.lean_root, ADAPTER
        )
    (cfg.project / "task_queue.json").write_text(json.dumps(parked_queue(fingerprint)),
                                                 encoding="utf-8")
    return fingerprint


def make_runner(login="me"):
    """A canned gh: identity, repo view, empty PR/issue lists, fat rate budget."""
    repo = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
            "parent": None, "hasIssuesEnabled": True, "viewerPermission": "WRITE",
            "visibility": "PRIVATE"}

    def done(payload):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")

    def fake(args, input_text=None):
        if args[:2] == ["api", "user"]:
            return done(login + "\n")
        if args[:2] == ["api", "rate_limit"]:
            return done(json.dumps({"resources": {"core": {"remaining": 4999, "reset": 0},
                                                  "graphql": {"remaining": 4999, "reset": 0}}}))
        if args[:2] == ["repo", "view"]:
            return done(json.dumps(repo))
        if args[:2] == ["pr", "list"]:
            return done("[]")
        if args[:2] == ["issue", "list"]:
            return done("[]")
        raise AssertionError(f"unexpected gh argv: {args}")

    return fake


def run_collect(cfg):
    return survey.collect(cfg, GitHost(runner=make_runner()), None,
                          Counters(cfg.counters_path), "o/r", "main")


def prove_reasons(s, actionable=True):
    bucket = s.stages if actionable else s.suppressed
    return {c.node: c.reason for c in bucket["prove"]}


# -- resumable_park: when it must stay silent -------------------------------

def test_resumable_park_returns_none_without_a_parked_fingerprinted_recovery(tmp_path):
    proj, lean = make_project(tmp_path)
    graph_path = proj / "graph.json"
    current = rs.proof_fingerprint(graph_path, NODE, lean, ADAPTER)
    moved = "a-fingerprint-from-another-life"

    def check(tasks):
        return rs.resumable_park(tasks, NODE, graph_path, lean, ADAPTER)

    assert check([]) is None                                   # no recovery at all
    assert check(parked_queue(moved, node="other-node")) is None   # another node's park
    # A recovery still in flight owns the node — only a PARKED one can be revived.
    for status in ("queued", "running", "done", "failed"):
        assert check(parked_queue(moved, status=status)) is None, status
    # Parked but unfingerprinted: no evidence recorded, so no evidence can differ.
    assert check([{"id": TASK, "agent": "escalation", "node": NODE, "status": "parked",
                   "recovery": {}}]) is None
    assert check(parked_queue("")) is None
    # Parked with the CURRENT fingerprint: nothing moved, parking holds.
    assert check(parked_queue(current)) is None
    # Only the LATEST recovery speaks: an old park behind a newer done record is closed.
    assert check(parked_queue(moved) + parked_queue(current, task_id=TASK + ":2", status="done")) is None
    # Parked + inputs moved: resumable, and the caller gets the task itself back.
    tasks = parked_queue(moved)
    assert check(tasks) is tasks[0]


# -- resumable_park: fingerprint sensitivity, end to end --------------------

def test_park_resumes_after_each_material_input_moves(tmp_path):
    proj, lean = make_project(tmp_path)
    graph_path = proj / "graph.json"
    tasks = parked_queue(rs.proof_fingerprint(graph_path, NODE, lean, ADAPTER))

    def resumable(backend=ADAPTER):
        return rs.resumable_park(tasks, NODE, graph_path, lean, backend)

    assert resumable() is None                          # nothing moved

    # Noise is not evidence: files nobody proves from must not un-park a node.
    (proj / "informal_content" / "other-node.md").write_text("someone else's prose\n", encoding="utf-8")
    (proj / "notes.log").write_text("chatter\n", encoding="utf-8")
    (lean / "Unrelated.lean").write_text("theorem u : True := trivial\n", encoding="utf-8")
    assert resumable() is None

    # 1. the node's prose (the strategy a human or researcher rewrites)
    prose = proj / "informal_content" / f"{NODE}.md"
    before = prose.read_text(encoding="utf-8")
    prose.write_text("strategy: strong induction, then Nat.le_induction\n", encoding="utf-8")
    assert resumable() is tasks[0]
    prose.write_text(before, encoding="utf-8")
    assert resumable() is None                          # content, not timestamps

    # 2. the Lean file (a merged sibling or a Mathlib bump rewrites it)
    lean_file = lean / "Target.lean"
    before = lean_file.read_text(encoding="utf-8")
    lean_file.write_text("theorem target : True := by\n  have h : True := trivial\n  sorry\n",
                         encoding="utf-8")
    assert resumable() is tasks[0]
    lean_file.write_text(before, encoding="utf-8")
    assert resumable() is None

    # 3. the node record itself (a re-plan adds a dependency)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][NODE]["depends_on"] = ["new-lemma"]
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert resumable() is tasks[0]
    graph["nodes"][NODE]["depends_on"] = []
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert resumable() is None

    # 4. the backend (a different prover is a different attempt)
    assert resumable("codex") is tasks[0]
    assert resumable("aristotle") is tasks[0]
    assert resumable() is None


def test_unchanged_recovery_still_gates_completed_recoveries(tmp_path):
    """Regression guard: granting resumes must not have loosened the refusal."""
    proj, lean = make_project(tmp_path)
    graph_path = proj / "graph.json"
    current = rs.proof_fingerprint(graph_path, NODE, lean, ADAPTER)
    done = parked_queue(current, status="done")

    assert rs.unchanged_recovery(done, NODE, graph_path, lean, ADAPTER)
    assert rs.resumable_park(done, NODE, graph_path, lean, ADAPTER) is None   # done ≠ parked
    assert not rs.unchanged_recovery(done, NODE, graph_path, lean, "codex")   # backend moved
    assert not rs.unchanged_recovery(parked_queue(current, status="queued"), NODE,
                                     graph_path, lean, ADAPTER)
    assert not rs.unchanged_recovery(parked_queue(""), NODE, graph_path, lean, ADAPTER)
    (proj / "informal_content" / f"{NODE}.md").write_text("a new route\n", encoding="utf-8")
    assert not rs.unchanged_recovery(done, NODE, graph_path, lean, ADAPTER)


# -- survey.collect ---------------------------------------------------------

def test_collect_keeps_a_park_that_no_new_evidence_supports(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    park_recovery(cfg)
    s = run_collect(cfg)
    assert "parked" in prove_reasons(s, actionable=False)[NODE]
    assert NODE not in prove_reasons(s)
    assert s.resumable_parks == [] and s.to_json()["resumable_parks"] == []


def test_collect_reports_a_resumable_park_once_the_prose_moves(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    park_recovery(cfg)
    article = cfg.project / "blueprint" / "roadmap" / f"{NODE}.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nstrategy: try Nat.rec\n",
                       encoding="utf-8")
    cfg = resolve_config(project=cfg.project, worker_id=cfg.worker_id, lean_root=cfg.lean_root)
    s = run_collect(cfg)
    # Reported as resumable, and deliberately NOT prove-actionable: the round
    # resumes the recovery and re-surveys, so an actionable prove here would
    # make --dry-run promise work a real round does not do on this pass.
    assert s.resumable_parks == [(NODE, TASK)]
    assert s.to_json()["resumable_parks"] == [{"node": NODE, "task": TASK}]
    assert NODE not in prove_reasons(s)
    assert "resumable" in prove_reasons(s, actionable=False)[NODE]


def test_collect_park_survives_irrelevant_churn(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    park_recovery(cfg)
    (cfg.project / "informal_content" / "someone-else.md").write_text("not ours\n", encoding="utf-8")
    (cfg.lean_root / "Other.lean").write_text("theorem o : True := trivial\n", encoding="utf-8")
    s = run_collect(cfg)
    assert "parked" in prove_reasons(s, actionable=False)[NODE]
    assert s.resumable_parks == []


# -- round.run_round --------------------------------------------------------

def _opted_out(cfg, monkeypatch):
    """Re-resolve *cfg* with the operator's identity opt-out set."""
    monkeypatch.setenv("AUTOFORM_DURABLE_IDENTITY", "0")
    return resolve_config(project=cfg.project, worker_id=cfg.worker_id, lean_root=cfg.lean_root)


def _origin(cfg):
    subprocess.run(["git", "init", "--quiet", str(cfg.lean_root)], check=True)
    subprocess.run(["git", "-C", str(cfg.lean_root), "remote", "add", "origin",
                    "https://github.com/o/r.git"], check=True)


def test_run_round_does_not_unpark_when_identity_is_opted_out(tmp_path, monkeypatch):
    """The identity gate must preserve queued recovery state byte-for-byte."""
    cfg = make_cfg(tmp_path, monkeypatch)
    park_recovery(cfg)
    article = cfg.project / "blueprint" / "roadmap" / f"{NODE}.md"
    article.write_text(article.read_text(encoding="utf-8") + "\nstrategy: try Nat.rec\n",
                       encoding="utf-8")
    cfg = _opted_out(cfg, monkeypatch)
    _origin(cfg)

    # Only the merge stage runs, so nothing here can spawn a prover; the resume
    # happens before the stage cascade either way.
    opts = round_mod.RoundOpts(only=("merge",))
    deps = round_mod.RoundDeps(host=GitHost(runner=make_runner()),
                               board_factory=lambda cfg, canonical: None)
    queue_path = cfg.project / "task_queue.json"
    before = queue_path.read_bytes()
    with pytest.raises(Die, match="durable article identity"):
        round_mod.run_round(cfg, opts, deps)
    assert queue_path.read_bytes() == before


def test_run_round_leaves_unmoved_park_alone_when_identity_is_opted_out(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    park_recovery(cfg)
    cfg = _opted_out(cfg, monkeypatch)
    _origin(cfg)

    opts = round_mod.RoundOpts(only=("merge",))
    deps = round_mod.RoundDeps(host=GitHost(runner=make_runner()),
                               board_factory=lambda cfg, canonical: None)
    queue_path = cfg.project / "task_queue.json"
    before = queue_path.read_bytes()
    with pytest.raises(Die, match="durable article identity"):
        round_mod.run_round(cfg, opts, deps)
    assert queue_path.read_bytes() == before


def test_a_task_naming_a_vanished_article_is_parked_not_silently_dropped(tmp_path, monkeypatch):
    """Path-derived IDs move when a graph role splits or renames an article.

    The task can never run again under the old ID, so the round must say so
    rather than leave the node quietly unworked.
    """
    cfg = make_cfg(tmp_path, monkeypatch)
    queue_path = cfg.project / "task_queue.json"
    queue_path.write_text(json.dumps([
        {"id": "escalation:gone", "agent": "escalation", "node": "chapter/renamed-away",
         "status": "queued"},
        {"id": "escalation:here", "agent": "escalation", "node": NODE, "status": "queued"},
    ]), encoding="utf-8")

    assert round_mod._park_orphaned_tasks(cfg) == 1

    queue = {task["id"]: task for task in json.loads(queue_path.read_text(encoding="utf-8"))}
    assert queue["escalation:gone"]["status"] == "parked"
    assert "no longer exists" in json.dumps(queue["escalation:gone"])
    assert queue["escalation:here"]["status"] == "queued"


# -- dispatch_runner: the engine-side gate ----------------------------------

def _engine_project(tmp_path, fingerprint):
    proj = tmp_path / "engine"
    proj.mkdir()
    (proj / "graph.json").write_text(json.dumps({
        "metadata": {"title": "t"},
        "nodes": {"s1": {"tier": 2, "parent": None, "kind": "lemma", "name": "Stmt 1",
                         "mathlib_status": "missing", "depends_on": []}},
    }), encoding="utf-8")
    (proj / "task_queue.json").write_text(json.dumps(
        parked_queue(fingerprint, task_id="escalation:s1", node="s1")
        + [{"id": "worker:s1", "agent": "worker", "node": "s1", "status": "queued"}],
    ), encoding="utf-8")
    return proj


def _task(proj, tid):
    queue = json.loads((proj / "task_queue.json").read_text(encoding="utf-8"))
    return next(t for t in queue if t["id"] == tid)


def test_engine_skips_the_worker_while_the_park_holds(tmp_path, monkeypatch):
    proj = _engine_project(tmp_path, "unset")
    fingerprint = dr.recovery_state.proof_fingerprint(proj / "graph.json", "s1", proj, "codex")
    queue = json.loads((proj / "task_queue.json").read_text(encoding="utf-8"))
    queue[0]["recovery"]["fingerprint"] = fingerprint
    (proj / "task_queue.json").write_text(json.dumps(queue), encoding="utf-8")
    monkeypatch.setattr(dr, "run_worker", lambda *a, **k: pytest.fail("blind retry ran"))

    assert dr.main([str(proj), "--repo", str(proj), "--workers", "--backend", "codex"]) == 0
    assert _task(proj, "worker:s1")["status"] == "queued"      # still owned by the park
    assert _task(proj, "escalation:s1")["status"] == "parked"


def test_engine_admits_the_worker_once_the_parked_inputs_move(tmp_path, monkeypatch):
    proj = _engine_project(tmp_path, "unset")
    fingerprint = dr.recovery_state.proof_fingerprint(proj / "graph.json", "s1", proj, "codex")
    queue = json.loads((proj / "task_queue.json").read_text(encoding="utf-8"))
    queue[0]["recovery"]["fingerprint"] = fingerprint
    (proj / "task_queue.json").write_text(json.dumps(queue), encoding="utf-8")
    (proj / "informal_content").mkdir()
    (proj / "informal_content" / "s1.md").write_text("a researched route\n", encoding="utf-8")

    calls = []

    def fake_worker(node, *a, **k):
        calls.append(node)
        return "proved", "ok", ""

    monkeypatch.setattr(dr, "run_worker", fake_worker)
    assert dr.main([str(proj), "--repo", str(proj), "--workers", "--backend", "codex"]) == 0
    assert calls == ["s1"]
    assert _task(proj, "worker:s1")["status"] == "done"
