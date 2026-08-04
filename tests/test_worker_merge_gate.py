"""Auto-merge gate tests — path allowlist, the survey's merge bucket, and do_merge.

Humans steer through the dashboards, not merge buttons, so a PR auto-merges when
every machine check passes: green CI, a TRUSTED `clean` scoreboard at the exact
head, an allowlisted path set, no hold label, no human flagged/rejected verdict,
push permission, and attempts left. Each of those is exercised here.

Everything is offline: GitHost gets a canned runner and the whole world (project,
sidecar, counters, git base url) lives under tmp_path.
"""
from __future__ import annotations

import json
import subprocess

from autoform_worker import scoreboard, survey, work_units
from autoform_worker.config import resolve_config
from autoform_worker.constants import HOLD_LABELS, MAX_MERGE_ATTEMPTS, merge_paths_allowed
from autoform_worker.counters import Counters
from autoform_worker.githost import GitHost
from autoform_worker.survey import Candidate, PRInfo, Survey

GREEN = [{"conclusion": "SUCCESS", "status": "COMPLETED"}]
HEAD = "b" * 40
MOVED_HEAD = "c" * 40
NODE = "clean"
PR_NUMBER = 42
CONTENT_FILES = ("Proj/Basic.lean", "informal_content/clean.md")


# -- world -------------------------------------------------------------------

def make_cfg(tmp_path, monkeypatch, human_verdict=None, worker_id="w1"):
    """A synthetic dispatch project whose only node is the merge target."""
    lean = tmp_path / "lean"
    lean.mkdir(exist_ok=True)
    (lean / "Clean.lean").write_text("theorem c : True := trivial\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    nodes = {NODE: {"tier": 2, "mathlib_status": "missing", "depends_on": [],
                    "lean_file": "Clean.lean"}}
    (proj / "graph.json").write_text(json.dumps(
        {"version": 2, "metadata": {"lean_root": str(lean)}, "nodes": nodes}), encoding="utf-8")
    review = {NODE: {"ai": {"verdict": "clean"}}}
    if human_verdict is not None:
        review[NODE]["human"] = {"verdict": human_verdict, "by": "jack", "at": "2026-08-01T00:00:00Z"}
    (proj / "review_status.json").write_text(json.dumps(
        {"version": 1, "settings": {"dial": "on-demand"}, "reviews": review}), encoding="utf-8")
    monkeypatch.setenv("AUTOFORM_DISPATCH_PROJECT", str(proj))
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "autoform-config.json"))
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", str(tmp_path / "remotes"))
    monkeypatch.delenv("AUTOFORM_RESPECT_CLAIMS", raising=False)
    monkeypatch.delenv("AUTOFORM_CANONICAL_REPO", raising=False)
    return resolve_config(worker_id=worker_id)


def pr_raw(number=PR_NUMBER, author="me", head_oid=HEAD, labels=("autoform",),
           files=CONTENT_FILES, node=NODE):
    return {
        "number": number,
        "title": f"prove: {node}",
        "author": {"login": author},
        "headRefName": f"autoform/branch-{number}",
        "headRefOid": head_oid,
        "headRepositoryOwner": {"login": author},
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "labels": [{"name": name} for name in labels],
        "body": scoreboard.format_target(node),
        "statusCheckRollup": list(GREEN),
        "updatedAt": "2026-08-03T00:00:00Z",
        "files": [{"path": path} for path in files],
    }


def done(payload=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")


def make_runner(open_prs=(), comments=None, login="me", permission="WRITE",
                collaborators=("peer",)):
    """A tiny canned gh, routed on the argv subcommand. No network, no merges."""
    repo = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
            "parent": None, "hasIssuesEnabled": False, "viewerPermission": permission,
            "visibility": "PRIVATE"}

    def fake(args, input_text=None):
        if args[:2] == ["api", "user"]:
            return done(login + "\n")
        if args[:2] == ["repo", "view"]:
            return done(json.dumps(repo))
        if args[:2] == ["pr", "list"]:
            state = args[args.index("--state") + 1]
            return done(json.dumps([] if state == "merged" else list(open_prs)))
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


def sb_comment(head=HEAD, by="me", verdict="clean", node=NODE):
    body = scoreboard.format_scoreboard(node, head, {"faithfulness": 5}, verdict, by)
    return {"id": 1, "user": {"login": by}, "body": body}


def collect_merge(cfg, *, labels=("autoform",), files=CONTENT_FILES, sb_head=HEAD,
                  sb_by="me", permission="WRITE", pr_author="me"):
    pr = pr_raw(author=pr_author, labels=labels, files=files)
    runner = make_runner(open_prs=[pr], comments={PR_NUMBER: [sb_comment(head=sb_head, by=sb_by)]},
                         permission=permission)
    return survey.collect(cfg, GitHost(runner=runner), None, Counters(cfg.counters_path),
                          "o/r", "main")


def buckets(s, stage="merge"):
    """(actionable reasons, suppressed reasons) for a stage."""
    return ([c.reason for c in s.actionable(stage)],
            [c.reason for c in s.suppressed[stage]])


# -- merge_paths_allowed -----------------------------------------------------

def test_merge_paths_allowed_accepts_roadmap_content():
    assert merge_paths_allowed(["Proj/Basic.lean"])
    assert merge_paths_allowed(["Top.lean"])
    assert merge_paths_allowed(["Proj/Sub/Deep_Name-2.lean"])
    assert merge_paths_allowed(["informal_content/node-a.md"])
    assert merge_paths_allowed(["kernel/node-a/evidence.json"])
    assert merge_paths_allowed(["review_status.json"])
    assert merge_paths_allowed(["Proj/A.lean", "informal_content/a.md", "kernel/a.json",
                                "review_status.json"])


def test_merge_paths_allowed_denies_toolchain_ci_and_tooling():
    for path in ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json",
                 ".github/workflows/x.yml", "scripts/x.py", "hooks/pre-commit",
                 ".claude/settings.json", ".claude", ".autoform/config.json"):
        assert not merge_paths_allowed([path]), path


def test_merge_paths_allowed_denies_unknown_and_empty():
    assert not merge_paths_allowed(["Makefile"])
    assert not merge_paths_allowed(["README.md"])
    assert not merge_paths_allowed(["docs/guide.md"])
    assert not merge_paths_allowed([])
    assert not merge_paths_allowed(())


def test_merge_paths_allowed_is_all_or_nothing():
    assert not merge_paths_allowed(["Proj/Basic.lean", "lean-toolchain"])
    assert not merge_paths_allowed(["informal_content/a.md", ".github/workflows/ci.yml"])
    # deny wins over allow even when the denied path would otherwise match the allowlist
    assert not merge_paths_allowed([".claude/agents/Evil.lean"])


# -- collect(): the merge bucket ---------------------------------------------

def test_collect_clean_scoreboard_at_head_is_a_merge_candidate(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    s = collect_merge(cfg)
    cands = s.actionable("merge")
    assert [(c.pr.number, c.node, c.kind) for c in cands] == [(PR_NUMBER, NODE, "merge")]
    assert cands[0].reason == "clean verdict at head + green CI"
    assert s.suppressed["merge"] == []
    assert s.actionable("review") == [] and s.actionable("fix") == []


def test_collect_merge_suppressed_by_each_hold_label(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    assert HOLD_LABELS  # the gate is only meaningful if there is something to hold with
    for label in sorted(HOLD_LABELS):
        s = collect_merge(cfg, labels=("autoform", label))
        assert buckets(s) == ([], [f"hold label: {label}"]), label


def test_collect_merge_suppressed_by_human_rejected_verdict(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, human_verdict="rejected")
    assert buckets(collect_merge(cfg)) == ([], ["human verdict rejected blocks the gate"])


def test_collect_merge_suppressed_by_human_flagged_verdict(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, human_verdict="flagged")
    assert buckets(collect_merge(cfg)) == ([], ["human verdict flagged blocks the gate"])


def test_collect_human_clean_verdict_does_not_block_the_gate(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, human_verdict="clean")
    assert buckets(collect_merge(cfg)) == (["clean verdict at head + green CI"], [])


def test_collect_merge_suppressed_by_non_allowlisted_file(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    s = collect_merge(cfg, files=("Proj/Basic.lean", "lean-toolchain"))
    assert buckets(s) == ([], ["touches non-roadmap paths — needs a human"])


def test_collect_merge_suppressed_without_push_permission(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    s = collect_merge(cfg, permission="READ")
    assert not s.can_push
    assert buckets(s) == ([], ["no merge permission on canonical"])


def test_collect_merge_suppressed_when_attempt_budget_spent(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    counters = Counters(cfg.counters_path)
    for _ in range(MAX_MERGE_ATTEMPTS - 1):
        counters.bump(f"merge-{PR_NUMBER}")
    assert buckets(collect_merge(cfg)) == (["clean verdict at head + green CI"], [])

    counters.bump(f"merge-{PR_NUMBER}")
    assert counters.get(f"merge-{PR_NUMBER}") == MAX_MERGE_ATTEMPTS
    assert buckets(collect_merge(cfg)) == ([], ["merge attempt budget spent"])


def test_collect_clean_scoreboard_for_another_head_is_review_not_merge(tmp_path, monkeypatch):
    """A clean verdict on a stale commit must never merge the current head."""
    cfg = make_cfg(tmp_path, monkeypatch)
    s = collect_merge(cfg, sb_head=MOVED_HEAD)
    assert buckets(s) == ([], [])
    assert buckets(s, "review") == (["head not yet scoreboarded"], [])


def test_collect_untrusted_scoreboard_author_does_not_enable_merge(tmp_path, monkeypatch):
    """Comments are attacker-writable: a stranger's `clean` is not a verdict."""
    cfg = make_cfg(tmp_path, monkeypatch)
    s = collect_merge(cfg, sb_by="drive-by")
    assert buckets(s) == ([], [])
    assert buckets(s, "review") == (["head not yet scoreboarded"], [])


# -- do_merge ----------------------------------------------------------------

class MergeRunner:
    """Canned gh for do_merge: records argv, serves one `pr list`, answers `pr merge`."""

    def __init__(self, open_prs=(), merge_rc=0):
        self.calls: list[list[str]] = []
        self.open_prs = list(open_prs)
        self.merge_rc = merge_rc

    def __call__(self, args, input_text=None):
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return done(json.dumps(self.open_prs))
        if args[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(
                args=[], returncode=self.merge_rc, stdout="",
                stderr="" if self.merge_rc == 0 else "GraphQL: Head branch was modified",
            )
        raise AssertionError(f"unexpected gh argv: {args}")

    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


def merge_candidate(head_oid=HEAD):
    pr = PRInfo(number=PR_NUMBER, title=f"prove: {NODE}", author="me",
                head_ref=f"autoform/branch-{PR_NUMBER}", head_oid=head_oid, head_owner="me",
                is_draft=False, mergeable="MERGEABLE", build="success", labels=["autoform"],
                node=NODE, files=CONTENT_FILES)
    return Candidate("merge", "clean verdict at head + green CI", pr=pr, node=NODE)


def a_survey():
    return Survey(canonical="o/r", default_branch="main", me="me", can_push=True)


def test_do_merge_happy_path_matches_the_reviewed_head(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    counters = Counters(cfg.counters_path)
    runner = MergeRunner(open_prs=[pr_raw()])

    result = work_units.do_merge(cfg, GitHost(runner=runner), counters, a_survey(),
                                 merge_candidate())
    assert result.progressed
    assert f"merge #{PR_NUMBER}" in result.summary and "auto-merged" in result.summary
    assert runner.merges() == [["pr", "merge", str(PR_NUMBER), "--repo", "o/r", "--squash",
                                "--match-head-commit", HEAD]]
    assert counters.get(f"merge-{PR_NUMBER}") == 0  # cleared on success


def test_do_merge_refuses_when_head_moved_since_review(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    counters = Counters(cfg.counters_path)
    runner = MergeRunner(open_prs=[pr_raw(head_oid=MOVED_HEAD)])

    result = work_units.do_merge(cfg, GitHost(runner=runner), counters, a_survey(),
                                 merge_candidate())
    assert not result.progressed and "head moved since review" in result.summary
    assert runner.merges() == []  # never even attempted
    assert counters.get(f"merge-{PR_NUMBER}") == 1  # attempt burned, not cleared


def test_do_merge_no_progress_when_pr_vanished(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    counters = Counters(cfg.counters_path)
    runner = MergeRunner(open_prs=[])

    result = work_units.do_merge(cfg, GitHost(runner=runner), counters, a_survey(),
                                 merge_candidate())
    assert not result.progressed and "vanished" in result.summary
    assert runner.merges() == []
    assert counters.get(f"merge-{PR_NUMBER}") == 1


def test_do_merge_keeps_the_counter_when_github_refuses(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    counters = Counters(cfg.counters_path)
    runner = MergeRunner(open_prs=[pr_raw()], merge_rc=1)

    result = work_units.do_merge(cfg, GitHost(runner=runner), counters, a_survey(),
                                 merge_candidate())
    assert not result.progressed and "GitHub refused the merge" in result.summary
    assert len(runner.merges()) == 1
    assert counters.get(f"merge-{PR_NUMBER}") == 1  # NOT cleared — refusals must burn budget
