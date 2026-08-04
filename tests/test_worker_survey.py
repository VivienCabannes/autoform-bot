"""Survey tests — build_state_of, prove eligibility, and the collect() buckets.

Everything is offline: GitHost gets a canned runner, the claim board is a local
bare repo, and the dispatch project is synthesized under tmp_path.
"""
from __future__ import annotations

import json
import subprocess

from autoform_worker import scoreboard, survey
from autoform_worker.claims import ClaimBoard, author_claim_key
from autoform_worker.config import resolve_config
from autoform_worker.constants import STAGES
from autoform_worker.counters import Counters
from autoform_worker.githost import GitHost, build_state_of

GREEN = [{"conclusion": "SUCCESS", "status": "COMPLETED"}]
RED = [{"conclusion": "FAILURE", "status": "COMPLETED"}]
RUNNING = [{"conclusion": "", "status": "IN_PROGRESS"}]
HEAD = "b" * 40


# -- fixtures-by-hand --------------------------------------------------------

def make_cfg(tmp_path, monkeypatch, worker_id="w1"):
    """A synthetic dispatch project (graph v2 + sidecar + Lean files) and its config."""
    lean = tmp_path / "lean"
    lean.mkdir(exist_ok=True)
    (lean / "Sorried.lean").write_text("theorem s : True := by sorry\n", encoding="utf-8")
    (lean / "Rejected.lean").write_text("theorem r : True := trivial\n", encoding="utf-8")
    (lean / "Clean.lean").write_text("theorem c : True := trivial\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    nodes = {
        "t1": {"tier": 1, "mathlib_status": "exists", "depends_on": []},
        "t1-untrusted": {"tier": 1, "mathlib_status": "missing", "depends_on": []},
        "no-lean": {"tier": 2, "mathlib_status": "missing", "depends_on": ["t1"]},
        "sorried": {"tier": 2, "mathlib_status": "missing", "depends_on": ["t1"],
                    "lean_file": "Sorried.lean"},
        "rejected": {"tier": 2, "mathlib_status": "missing", "depends_on": ["t1"],
                     "lean_file": "Rejected.lean"},
        "clean": {"tier": 2, "mathlib_status": "missing", "depends_on": ["t1"],
                  "lean_file": "Clean.lean"},
        "blocked": {"tier": 2, "mathlib_status": "missing", "depends_on": ["t1-untrusted"]},
        "reused": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
    }
    (proj / "graph.json").write_text(json.dumps(
        {"version": 2, "metadata": {"lean_root": str(lean)}, "nodes": nodes}), encoding="utf-8")
    (proj / "review_status.json").write_text(json.dumps({
        "version": 1, "settings": {"dial": "on-demand"},
        "reviews": {
            "rejected": {"ai": {"verdict": "rejected"}},
            "clean": {"ai": {"verdict": "clean"}},
        },
    }), encoding="utf-8")
    monkeypatch.setenv("AUTOFORM_DISPATCH_PROJECT", str(proj))
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "autoform-config.json"))
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", str(tmp_path / "remotes"))
    monkeypatch.delenv("AUTOFORM_RESPECT_CLAIMS", raising=False)
    monkeypatch.delenv("AUTOFORM_CANONICAL_REPO", raising=False)
    return resolve_config(worker_id=worker_id)


def pr_raw(number, author="me", head_oid=HEAD, mergeable="MERGEABLE", rollup=None,
           body="", draft=False, head_owner=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "author": {"login": author},
        "headRefName": f"autoform/branch-{number}",
        "headRefOid": head_oid,
        "headRepositoryOwner": {"login": head_owner or author},
        "isDraft": draft,
        "mergeable": mergeable,
        "labels": [{"name": "autoform"}],
        "body": body,
        "statusCheckRollup": rollup if rollup is not None else list(GREEN),
        "updatedAt": "2026-08-03T00:00:00Z",
    }


def make_runner(open_prs=(), merged_prs=(), comments=None, issues=(),
                login="me", permission="WRITE", has_issues=True,
                collaborators=("other", "peer")):
    """A tiny canned gh: routes on the argv subcommand, nothing else.

    ``collaborators`` answers the trust-boundary check (`/collaborators/<login>`,
    204 → trusted); PR authors and scoreboard/marker authors outside it are
    untrusted by the hardened survey.
    """
    repo = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
            "parent": None, "hasIssuesEnabled": has_issues, "viewerPermission": permission,
            "visibility": "PRIVATE"}

    def done(payload):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")

    def fake(args, input_text=None):
        if args[:2] == ["api", "user"]:
            return done(login + "\n")
        if args[:2] == ["repo", "view"]:
            return done(json.dumps(repo))
        if args[:2] == ["pr", "list"]:
            state = args[args.index("--state") + 1]
            return done(json.dumps(list(merged_prs if state == "merged" else open_prs)))
        if args[:2] == ["issue", "list"]:
            return done(json.dumps(list(issues)))
        if args[0] == "api" and "/collaborators/" in args[1]:
            who = args[1].rsplit("/", 1)[1]
            ok = who in collaborators or who == login
            return subprocess.CompletedProcess(args=[], returncode=0 if ok else 1,
                                               stdout="", stderr="" if ok else "HTTP 404")
        if args[0] == "api" and "--paginate" in args:
            number = int(args[-1].split("/issues/")[1].split("/")[0])
            return done(json.dumps((comments or {}).get(number, [])))
        raise AssertionError(f"unexpected gh argv: {args}")

    return fake


def run_collect(cfg, runner, board=None):
    return survey.collect(cfg, GitHost(runner=runner), board, Counters(cfg.counters_path),
                          "o/r", "main")


def prove_map(s, actionable=True):
    bucket = s.stages if actionable else s.suppressed
    return {c.node: c.reason for c in bucket["prove"]}


# -- build_state_of ----------------------------------------------------------

def test_build_state_of_matrix():
    assert build_state_of({}) == "success"
    assert build_state_of({"statusCheckRollup": []}) == "success"
    assert build_state_of({"statusCheckRollup": list(GREEN)}) == "success"
    assert build_state_of({"statusCheckRollup": [{"conclusion": "FAILURE"}]}) == "failed"
    assert build_state_of({"statusCheckRollup": [{"state": "ERROR"}]}) == "failed"
    assert build_state_of({"statusCheckRollup": list(RUNNING)}) == "pending"
    mixed_fail = [{"conclusion": "SUCCESS"}, {"conclusion": "", "status": "QUEUED"},
                  {"conclusion": "TIMED_OUT"}]
    assert build_state_of({"statusCheckRollup": mixed_fail}) == "failed"
    mixed_wait = [{"conclusion": "SUCCESS"}, {"conclusion": "", "status": "QUEUED"}]
    assert build_state_of({"statusCheckRollup": mixed_wait}) == "pending"


# -- eligible_prove_nodes ----------------------------------------------------

def test_eligible_prove_nodes(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    out = survey.eligible_prove_nodes(cfg)
    reasons = {nid: reason for nid, _node, reason in out}
    assert reasons == {
        "no-lean": "no Lean landed yet",
        "sorried": "Lean present but sorry'd",
        "rejected": "verdict rejected — needs repair",
    }
    # clean verdict + clean Lean, untrusted prerequisite, and in-Mathlib are all out.
    assert "clean" not in reasons and "blocked" not in reasons and "reused" not in reasons
    assert [nid for nid, _n, _r in out] == sorted(reasons)


# -- collect: PR tending -----------------------------------------------------

def test_collect_own_conflicting_pr_lands_in_rebase(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    s = run_collect(cfg, make_runner(open_prs=[pr_raw(5, mergeable="CONFLICTING")]))
    assert set(s.stages) == set(STAGES) and s.me == "me" and s.can_push
    cands = s.actionable("rebase")
    assert [c.pr.number for c in cands] == [5]
    assert cands[0].reason == "conflicts with base"
    assert s.actionable("fix-ci") == [] and s.actionable("review") == []


def test_collect_own_failing_pr_lands_in_fix_ci(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    s = run_collect(cfg, make_runner(open_prs=[pr_raw(6, rollup=list(RED))]))
    cands = s.actionable("fix-ci")
    assert [c.pr.number for c in cands] == [6]
    assert cands[0].reason == "checks failing"
    assert s.actionable("rebase") == []


def test_collect_flagged_scoreboard_at_head_is_fix(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    body = scoreboard.format_scoreboard("clean", HEAD, {"faithfulness": 2}, "flagged", "peer")
    runner = make_runner(open_prs=[pr_raw(7, body=scoreboard.format_target("clean"))],
                         comments={7: [{"id": 1, "user": {"login": "peer"}, "body": body}]})
    s = run_collect(cfg, runner)
    cands = s.actionable("fix")
    assert [(c.pr.number, c.node) for c in cands] == [(7, "clean")]
    assert cands[0].reason == "verdict flagged at head"
    assert s.actionable("review") == []  # already scoreboarded at this head


def test_collect_green_marker_pr_without_scoreboard_is_review(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    pr = pr_raw(8, author="other", head_owner="other", body=scoreboard.format_target("ghost"))
    s = run_collect(cfg, make_runner(open_prs=[pr], comments={8: []}))
    cands = s.actionable("review")
    assert [c.pr.number for c in cands] == [8]
    assert cands[0].reason == "head not yet scoreboarded"


def test_collect_review_suppressed_by_active_inprogress_marker(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    pr = pr_raw(8, author="other", head_owner="other", body=scoreboard.format_target("ghost"))
    marker = {"id": 2, "user": {"login": "peer"}, "body": scoreboard.format_inprogress(HEAD, "peer")}
    s = run_collect(cfg, make_runner(open_prs=[pr], comments={8: [marker]}))
    assert s.actionable("review") == []
    assert [c.reason for c in s.suppressed["review"]] == ["peer review in flight"]


def test_collect_pending_checks_suppress_review(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    pr = pr_raw(9, author="other", head_owner="other",
                body=scoreboard.format_target("ghost"), rollup=list(RUNNING))
    s = run_collect(cfg, make_runner(open_prs=[pr]))
    assert s.actionable("review") == []
    assert [c.reason for c in s.suppressed["review"]] == ["checks still running"]


def test_collect_red_foreign_marker_pr_is_not_reviewed(tmp_path, monkeypatch):
    """Design contract: review requires checks green. A peer's failing PR must not
    become a review candidate (we'd burn jury tokens on a head CI already rejects)."""
    cfg = make_cfg(tmp_path, monkeypatch)
    pr = pr_raw(12, author="other", head_owner="other",
                body=scoreboard.format_target("ghost"), rollup=list(RED))
    s = run_collect(cfg, make_runner(open_prs=[pr], comments={12: []}))
    assert s.actionable("review") == []


# -- collect: prove avoid-lists ---------------------------------------------

def test_collect_prove_suppressed_by_open_target_pr(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    pr = pr_raw(10, author="other", head_owner="other", body=scoreboard.format_target("sorried"))
    s = run_collect(cfg, make_runner(open_prs=[pr], comments={10: []}))
    assert set(prove_map(s)) == {"no-lean", "rejected"}
    assert prove_map(s, actionable=False) == {"sorried": "open PR already targets it"}


def test_collect_prove_suppressed_by_peer_claim(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    bare = tmp_path / "remotes" / "o" / "r"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "--quiet", str(bare)], check=True)
    peer = ClaimBoard(str(bare), "peer", tmp_path / "peer-scratch")
    key = author_claim_key("no-lean")
    assert peer.acquire(key)
    board = ClaimBoard(str(bare), cfg.worker_id, cfg.claims_scratch)
    s = run_collect(cfg, make_runner(), board=board)
    assert [lease["_key"] for lease in s.claims] == [key]
    assert prove_map(s, actionable=False) == {"no-lean": "claimed by peer"}
    assert set(prove_map(s)) == {"sorried", "rejected"}


def test_collect_prove_suppressed_by_assigned_intention(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    issues = [
        {"number": 1, "title": "intention: rejected", "assignees": [{"login": "human"}],
         "state": "OPEN", "body": ""},
        {"number": 2, "title": "intention: sorried", "assignees": [], "state": "OPEN", "body": ""},
    ]
    s = run_collect(cfg, make_runner(issues=issues))
    assert s.issues_enabled
    assert prove_map(s, actionable=False) == {"rejected": "human intention registered"}
    assert set(prove_map(s)) == {"no-lean", "sorried"}  # unassigned intention is informational


def test_collect_budget_counters_suppress(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    cfg.counters_path.write_text(json.dumps({"prove-no-lean": 3, "review-err-11": 3}),
                                 encoding="utf-8")
    pr = pr_raw(11, author="other", head_owner="other", body=scoreboard.format_target("ghost"))
    s = run_collect(cfg, make_runner(open_prs=[pr], comments={11: []}))
    assert s.actionable("review") == []
    assert [c.reason for c in s.suppressed["review"]] == ["review error budget spent"]
    assert prove_map(s, actionable=False) == {"no-lean": "attempt budget spent"}
    assert set(prove_map(s)) == {"sorried", "rejected"}


# -- collect: progress -------------------------------------------------------

def test_collect_unfolded_merges_actionable_iff_can_push(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    merged = [pr_raw(20, body=scoreboard.format_target("clean"))]

    s = run_collect(cfg, make_runner(merged_prs=merged))
    assert [c.reason for c in s.actionable("progress")] == ["1 merged scoreboard(s) to fold"]

    s = run_collect(cfg, make_runner(merged_prs=merged, permission="READ"))
    assert not s.can_push and s.actionable("progress") == []
    assert [c.reason for c in s.suppressed["progress"]] \
        == ["1 merged scoreboard(s) to fold — no push access, skipping"]

    cfg.folded_path.write_text(json.dumps({"prs": [20]}), encoding="utf-8")
    s = run_collect(cfg, make_runner(merged_prs=merged))
    assert s.actionable("progress") == [] and s.suppressed["progress"] == []
