"""Work-unit executors — one function per stage, one visible GitHub mark each.

Shared discipline (every unit):

* start from a clean tree, remember the operator's branch, restore it on exit;
* cooperative claim + heartbeat around the work ([COOP]);
* every push is a CAS ``safe_push`` against the OID observed at survey time,
  re-checking the heartbeat first ([HARD] — never push on a lost lease);
* return a :class:`UnitResult`; ``progressed`` is True only when a visible
  GitHub mark landed (new head OID, new comment, new PR).

Prove reuses the existing prover stack end-to-end via
``scripts/dispatch_runner.run_worker`` (spec building → adapter → driver →
independent verification gate → usage ledger). Review reuses the exact jury the
local engine runs (``judge_runtime`` + the rubric prompts). Nothing here
re-implements proving or judging.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import agents, gitutil, scoreboard
from .claims import ClaimBoard, author_claim_key
from .config import WorkerConfig, scripts_modules
from .constants import (
    LABEL_BASE,
    LABEL_ESCALATION,
    PROVE_BRANCH_PREFIX,
    REVIEW_INPROGRESS_TTL_S,
    REVIEW_TMP_BRANCH_PREFIX,
)
from .counters import Counters
from .errors import ClaimTransportError, Die
from .githost import GitHost
from .survey import Candidate, PRInfo, Survey, _load_folded

FIX_AGENT_TIMEOUT_S = 3600
JUDGE_TIMEOUT_S = 600
PROVE_MAX_STEERS = 2
PROVE_TIMEOUT_S = 3600


@dataclass
class UnitResult:
    progressed: bool
    summary: str
    infra_failure: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str, limit: int = 40) -> str:
    import re

    slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return slug[:limit] or "node"


@contextlib.contextmanager
def _worktree_round_trip(cfg: WorkerConfig):
    """Exclusive, restorable occupancy of the operator's checkout.

    * An flock on ``.git/autoform-worker.lock`` serializes units across ALL
      processes sharing this clone — worker ids are identities, the worktree is
      the mutually exclusive resource.
    * An inflight marker (``.git/autoform-worker-inflight.json``) records the
      operator's prior ref; a round killed mid-unit (timeout SIGKILL, crash) is
      recovered by the NEXT round instead of wedging the loop on the
      clean-tree gate forever.
    * Requires a clean tree on entry. On exit, a dirty tree is salvaged before
      restore: every dirty file's current bytes are copied into the worker
      state dir, and human verdicts written into a committed
      ``review_status.json`` by a concurrently running dashboard are merged
      back into the restored sidecar (human slots are immutable by contract —
      a branch switch must never destroy one).
    """
    repo = cfg.lean_root
    git_dir = Path(gitutil.run_git(["rev-parse", "--git-dir"], cwd=repo).stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    marker_path = git_dir / "autoform-worker-inflight.json"
    with open(git_dir / "autoform-worker.lock", "w") as lock_fh:
        import fcntl

        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise Die(f"{repo}: another autoform unit is mutating this checkout right now") from error
        _recover_stale_round(cfg, marker_path)
        if not gitutil.clean_tree(repo):
            raise Die(f"{repo}: working tree not clean — commit or stash before running the worker")
        prior = gitutil.current_branch(repo)
        prior_ref = prior if prior != "HEAD" else gitutil.head_oid(repo)
        marker_path.write_text(json.dumps({"prior_ref": prior_ref, "at": _now_iso()}), encoding="utf-8")
        try:
            yield
        finally:
            _restore_worktree(cfg, prior_ref)
            with contextlib.suppress(OSError):
                marker_path.unlink()


def _restore_worktree(cfg: WorkerConfig, prior_ref: str) -> None:
    repo = cfg.lean_root
    with contextlib.suppress(Die):
        if not gitutil.clean_tree(repo):
            salvaged_sidecar = _salvage_dirty_files(cfg)
            gitutil.run_git(["reset", "--hard", "--quiet"], cwd=repo, check=False)
            gitutil.run_git(["clean", "-fd", "--quiet"], cwd=repo, check=False)
            gitutil.run_git(["checkout", "--quiet", prior_ref], cwd=repo, check=False)
            if salvaged_sidecar is not None:
                _merge_salvaged_human_slots(cfg, salvaged_sidecar)
            return
        gitutil.run_git(["checkout", "--quiet", prior_ref], cwd=repo, check=False)


def _recover_stale_round(cfg: WorkerConfig, marker_path: Path) -> None:
    """A leftover inflight marker under the lock means OUR previous round died
    mid-unit — the dirt is provably the worker's own, so restore and move on."""
    if not marker_path.exists():
        return
    try:
        prior_ref = json.loads(marker_path.read_text(encoding="utf-8")).get("prior_ref")
    except (OSError, json.JSONDecodeError):
        prior_ref = None
    if isinstance(prior_ref, str) and prior_ref:
        _restore_worktree(cfg, prior_ref)
    with contextlib.suppress(OSError):
        marker_path.unlink()


_SALVAGE_MAX_BYTES = 5 * 1024 * 1024


def _salvage_dirty_files(cfg: WorkerConfig) -> bytes | None:
    """Copy every dirty file's current bytes into the state dir before reset.

    Returns the current bytes of a dirty committed ``review_status.json`` (the
    one file whose concurrent writes we can safely re-merge), or None.
    """
    repo = cfg.lean_root
    sidecar_path = cfg.project / "review_status.json"
    salvage_dir = cfg.state_dir / "salvage" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sidecar_bytes: bytes | None = None
    proc = gitutil.run_git(["status", "--porcelain", "--untracked-files=all"], cwd=repo, check=False)
    for line in proc.stdout.splitlines():
        rel = line[3:].strip().strip('"')
        if not rel:
            continue
        src = repo / rel
        try:
            if not src.is_file() or src.stat().st_size > _SALVAGE_MAX_BYTES:
                continue
            data = src.read_bytes()
        except OSError:
            continue
        if src.resolve() == sidecar_path.resolve():
            sidecar_bytes = data
        dst = salvage_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            dst.write_bytes(data)
    return sidecar_bytes


def _merge_salvaged_human_slots(cfg: WorkerConfig, salvaged: bytes) -> None:
    """Re-apply human verdicts from a salvaged sidecar onto the restored one.

    Human slots are immutable and additive — merging them back is always safe;
    everything else in the salvaged copy was derived from the wrong branch."""
    try:
        data = json.loads(salvaged.decode("utf-8"))
        reviews = data.get("reviews") if isinstance(data, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(reviews, dict):
        return
    humans = {node: rec["human"] for node, rec in reviews.items()
              if isinstance(rec, dict) and isinstance(rec.get("human"), dict)}
    if not humans:
        return
    mods = scripts_modules()
    rm, fslock = mods["review_model"], mods["fslock"]
    sidecar_path = cfg.project / "review_status.json"
    with fslock.locked(sidecar_path):
        sidecar = rm.load_sidecar(sidecar_path)
        changed = False
        for node, human in humans.items():
            slot = sidecar.setdefault("reviews", {}).setdefault(node, {})
            if slot.get("human") != human:
                slot["human"] = human
                changed = True
        if changed:
            rm.save_sidecar(sidecar_path, sidecar)


@contextlib.contextmanager
def _cooperative_claim(board: ClaimBoard | None, respect: bool, key: str, notes: list[str]):
    """Acquire+heartbeat a claim when the board is usable; fail open otherwise.

    Yields ``(acquired, heartbeat)`` — ``heartbeat`` may be None. When the board
    says a *live peer* holds the key the caller must skip the candidate; that is
    signaled by ``acquired is False`` with ``heartbeat`` None.
    """
    if board is None or not respect:
        yield True, None
        return
    try:
        if not board.acquire(key):
            yield False, None
            return
    except ClaimTransportError as error:
        notes.append(f"claim board down ({error}); continuing uncoordinated")
        yield True, None
        return
    try:
        with board.heartbeat(key) as hb:
            yield True, hb
    finally:
        with contextlib.suppress(ClaimTransportError):
            board.release(key)


def _lease_ok(heartbeat) -> bool:
    return heartbeat is None or not heartbeat.lost.is_set()


def _enqueue_escalation(cfg: WorkerConfig, node_id: str, note: str) -> None:
    """Mirror the engine's escalation contract into the local queue so the
    orchestrator (and the dashboard) see the wall this worker hit."""
    mods = scripts_modules()
    dq, fslock = mods["dispatch_queue"], mods["fslock"]
    qp = cfg.project / "task_queue.json"
    fp = cfg.project / "agents_status.json"
    with fslock.locked(qp):
        try:
            tasks = dq.load_queue(qp)
        except dq.QueueStateError:
            return  # a corrupt queue is the orchestrator's problem, not ours to overwrite
        if any(t.get("agent") == "escalation" and t.get("node") == node_id
               and t.get("status") in ("queued", "running") for t in tasks):
            return
        tasks.append({
            "id": dq.new_task_id("escalation", node_id, tasks),
            "agent": "escalation", "node": node_id, "node_label": node_id,
            "status": "queued", "at": _now_iso(), "source": "engine",
            "note": note[:2400],
        })
        dq._save(qp, tasks)
        dq.sync_feed(fp, tasks)


def _feed_start(cfg: WorkerConfig, role: str, name: str, target: str) -> None:
    """Surface this round in the live dashboard feed (best-effort)."""
    with contextlib.suppress(Exception):
        mods = scripts_modules()
        dq = mods["dispatch_queue"]
        dq.main([str(cfg.project), "agent-start", "--role", role, "--name", name, "--target", target])


def _feed_done(cfg: WorkerConfig, name: str) -> None:
    with contextlib.suppress(Exception):
        mods = scripts_modules()
        dq = mods["dispatch_queue"]
        dq.main([str(cfg.project), "agent-done", "--name", name])


# ---------------------------------------------------------------------------
# prove
# ---------------------------------------------------------------------------

def do_prove(
    cfg: WorkerConfig,
    host: GitHost,
    board: ClaimBoard | None,
    counters: Counters,
    survey: Survey,
    candidate: Candidate,
    *,
    backend: str,
    judge_backend: str,
    allowed_egress: frozenset[str] = frozenset(),
    prover=None,
) -> UnitResult:
    """Claim a node, prove it through the existing prover stack, open a PR.

    ``prover`` is the injectable seam for tests; the default is
    ``dispatch_runner.run_worker`` (spec → adapter → driver → verification gate
    → usage ledger).
    """
    node_id = candidate.node
    assert node_id
    mods = scripts_modules()
    backend_config, rm = mods["backend_config"], mods["review_model"]
    adapter = backend_config.prover_of(backend)  # fails closed on unknown backends
    if adapter in {"openai", "avocado"} and adapter not in allowed_egress:
        raise Die(
            f"backend {backend!r} sends project data to a configured API endpoint; "
            f"re-run with --allow-api-egress {adapter} after explicit user approval"
        )
    nodes, _meta = rm.load_graph(cfg.graph_path)
    node = nodes.get(node_id)
    if node is None:
        return UnitResult(False, f"prove {node_id}: node vanished from graph")

    if prover is None:
        import dispatch_runner  # noqa: PLC0415  — path set up by scripts_modules()

        prover = dispatch_runner.run_worker

    push_slug = survey.canonical if survey.can_push else host.ensure_fork(survey.canonical)
    push_url = gitutil.slug_url(push_slug)
    branch = f"{PROVE_BRANCH_PREFIX}{_slug(node_id)}-{cfg.worker_id}-{time.strftime('%Y%m%d%H%M')}"
    feed_name = f"worker-cli:{node_id}"

    notes: list[str] = []
    with _cooperative_claim(board, cfg.respect_claims, author_claim_key(node_id), notes) as (acquired, hb):
        if not acquired:
            return UnitResult(False, f"prove {node_id}: claimed by a live peer meanwhile")
        with _worktree_round_trip(cfg):
            gitutil.fetch(cfg.lean_root, gitutil.slug_url(survey.canonical), survey.default_branch)
            gitutil.run_git(["checkout", "--quiet", "-B", branch, "FETCH_HEAD"], cwd=cfg.lean_root)

            counters.bump(f"prove-{node_id}")
            _feed_start(cfg, "worker", feed_name, node_id)
            try:
                status, reason, detail = prover(
                    node_id, node, cfg.project, str(cfg.graph_path), str(cfg.lean_root),
                    PROVE_MAX_STEERS, backend=adapter, judge_backend=judge_backend,
                    worker_timeout=PROVE_TIMEOUT_S,
                )
            finally:
                _feed_done(cfg, feed_name)

            if status != "proved":
                note = f"{reason}\n\n{detail}".strip()[:2400]
                _enqueue_escalation(cfg, node_id, note or "prove failed without detail")
                return UnitResult(False, f"prove {node_id}: FAILED — {reason[:200]}",
                                  infra_failure="prover error" if "prover error" in (reason or "") else None)

            gitutil.run_git(["add", "-A"], cwd=cfg.lean_root)
            if gitutil.clean_tree(cfg.lean_root):
                return UnitResult(False, f"prove {node_id}: claimed proved but landed no changes")
            gitutil.run_git(["commit", "--quiet", "-m",
                             f"prove: {node_id} ({node.get('kind', 'statement')})\n\n{(reason or '')[:400]}"],
                            cwd=cfg.lean_root)

            if not _lease_ok(hb):
                return UnitResult(False, f"prove {node_id}: author lease lost — refusing to push")
            if not gitutil.safe_push(cfg.lean_root, branch, remote=push_url, expect=None):
                return UnitResult(False, f"prove {node_id}: create-only push refused (branch exists?)")

            body = _prove_pr_body(node_id, node, reason, backend, notes)
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, dir=str(cfg.state_dir)) as f:
                f.write(body)
                body_file = Path(f.name)
            try:
                host.ensure_labels(survey.canonical, [LABEL_BASE])
                head = f"{push_slug.split('/', 1)[0]}:{branch}" if push_slug != survey.canonical else branch
                url = host.create_pr(survey.canonical, head, survey.default_branch,
                                     f"prove: {node_id}", body_file, labels=[LABEL_BASE])
            finally:
                with contextlib.suppress(OSError):
                    body_file.unlink()
            counters.clear(f"prove-{node_id}")
            return UnitResult(True, f"prove {node_id}: PR opened {url}")


def _prove_pr_body(node_id: str, node: dict, reason: str, backend: str, notes: list[str]) -> str:
    lines = [
        f"Proves graph node `{node_id}` ({node.get('kind', 'statement')}).",
        "",
        node.get("description", "").strip(),
        "",
        f"Verification: the shared gate confirmed a clean build with no `sorry`/`admit` "
        f"and an audited axiom set before this PR was opened. {reason[:300]}".strip(),
    ]
    if notes:
        lines += ["", *(f"> note: {n}" for n in notes)]
    lines += [
        "",
        scoreboard.format_target(node_id),
        "",
        f"🤖 Prepared with `{backend}` via autoform worker",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def do_review(
    cfg: WorkerConfig,
    host: GitHost,
    counters: Counters,
    survey: Survey,
    candidate: Candidate,
    *,
    judge_backend: str,
    judge_timeout: int = JUDGE_TIMEOUT_S,
    judge=None,
) -> UnitResult:
    """Check out the PR head, run the 3-axis jury, post the scoreboard comment."""
    pr = candidate.pr
    assert pr is not None and pr.node
    mods = scripts_modules()
    rm, judge_runtime = mods["review_model"], mods["judge_runtime"]
    judge = judge or judge_runtime.run_judge

    import dispatch_runner  # noqa: PLC0415

    nodes, _meta = rm.load_graph(cfg.graph_path)
    node = nodes.get(pr.node)
    if node is None:
        counters.bump(f"review-err-{pr.number}")
        return UnitResult(False, f"review #{pr.number}: target node {pr.node!r} not in local graph — sync first")

    tmp_branch = f"{REVIEW_TMP_BRANCH_PREFIX}{pr.number}"
    marker_comment_id: int | None = None
    with _worktree_round_trip(cfg):
        gitutil.fetch(cfg.lean_root, gitutil.slug_url(survey.canonical), f"pull/{pr.number}/head")
        gitutil.run_git(["checkout", "--quiet", "-B", tmp_branch, "FETCH_HEAD"], cwd=cfg.lean_root)
        head = gitutil.head_oid(cfg.lean_root)

        marker_comment_id = _post_returning_id(
            host, survey.canonical, pr.number, scoreboard.format_inprogress(head, cfg.worker_id)
        )
        try:
            content_text = ""
            content_rel = node.get("content")
            if content_rel:
                # graph fields are data, not paths we trust — never follow one
                # outside the project (a poisoned graph must not read ~/.ssh
                # into a judge prompt or a public comment).
                content_path = (cfg.project / content_rel)
                if _inside(cfg.project, content_path) and content_path.exists():
                    content_text = content_path.read_text(encoding="utf-8")[:6000]

            rubrics = rm.load_rubrics()
            scores: dict = {}
            notes: dict = {}
            for axis in rm.AXES:
                prompt = dispatch_runner.build_prompt(rubrics[axis], pr.node, node, content_text)
                result = judge(axis, prompt, str(cfg.lean_root), None, judge_timeout, backend=judge_backend)
                scores[axis] = result.get("score")
                notes[axis] = result.get("reasoning", "")
            if all(score is None for score in scores.values()):
                counters.bump(f"review-err-{pr.number}")
                return UnitResult(False, f"review #{pr.number}: every judge abstained/errored",
                                  infra_failure="all judges abstained")

            verdict = rm.jury_verdict(scores)
            host.post_comment(survey.canonical, pr.number,
                              scoreboard.format_scoreboard(pr.node, head, scores, verdict,
                                                           cfg.worker_id, notes))
            counters.clear(f"review-err-{pr.number}")
            return UnitResult(True, f"review #{pr.number} ({pr.node}): verdict {verdict}")
        finally:
            if marker_comment_id is not None:
                host.delete_comment(survey.canonical, marker_comment_id)
            _drop_tmp_branch(cfg.lean_root, tmp_branch)


def _post_returning_id(host: GitHost, slug: str, number: int, body: str) -> int | None:
    data = host.gh_json(["api", "-X", "POST", f"/repos/{slug}/issues/{number}/comments",
                         "-f", f"body={body}"], check=False)
    return data.get("id") if isinstance(data, dict) else None


def _drop_tmp_branch(repo: Path, branch: str) -> None:
    """Delete a temporary work branch. HEAD may still be on it — detach first
    (a plain ``branch -D`` of the current branch is a silent no-op)."""
    gitutil.run_git(["checkout", "--quiet", "--detach"], cwd=repo, check=False)
    gitutil.run_git(["branch", "-D", branch], cwd=repo, check=False)


# ---------------------------------------------------------------------------
# merge — the auto-merge gate
# ---------------------------------------------------------------------------

def do_merge(
    cfg: WorkerConfig,
    host: GitHost,
    counters: Counters,
    survey: Survey,
    candidate: Candidate,
) -> UnitResult:
    """Merge a PR whose machine checks all pass.

    Humans are in the loop through the dashboards, not a merge button: the
    survey already established green CI, a trusted `clean` scoreboard at this
    exact head, an allowlisted path set, no hold label, and no human
    flagged/rejected verdict. This unit re-confirms the head at merge time —
    `--match-head-commit` is the merge-time CAS, so a push landing between
    survey and merge cancels the merge instead of merging unreviewed code.
    """
    pr = candidate.pr
    assert pr is not None
    counters.bump(f"merge-{pr.number}")
    fresh = next((p for p in host.pr_list(survey.canonical) if int(p.get("number", 0)) == pr.number), None)
    if fresh is None:
        return UnitResult(False, f"merge #{pr.number}: PR vanished (already merged or closed)")
    fresh_head = str(fresh.get("headRefOid", ""))
    if fresh_head != pr.head_oid:
        return UnitResult(False, f"merge #{pr.number}: head moved since review — re-review first")

    if not host.merge_pr(survey.canonical, pr.number, pr.head_oid):
        return UnitResult(False, f"merge #{pr.number}: GitHub refused the merge "
                                 "(branch protection, conflict, or head moved)")
    counters.clear(f"merge-{pr.number}")
    return UnitResult(True, f"merge #{pr.number} ({pr.node}): auto-merged on a clean jury verdict")


# ---------------------------------------------------------------------------
# fix-like (fix / fix-ci / rebase)
# ---------------------------------------------------------------------------

_FIX_PROMPTS = {"fix": "fix.md", "fix-ci": "fix-ci.md", "rebase": "rebase.md"}
_FIX_COUNTER = {
    "fix": lambda pr: f"fix-{pr.number}-{pr.head_oid[:12]}",
    "fix-ci": lambda pr: f"ci-{pr.number}-{pr.head_oid[:12]}",
    "rebase": lambda pr: f"rebase-pr-{pr.number}",
}


def do_fixlike(
    kind: str,
    cfg: WorkerConfig,
    host: GitHost,
    board: ClaimBoard | None,
    counters: Counters,
    survey: Survey,
    candidate: Candidate,
    *,
    backend: str,
    agent_timeout: int = FIX_AGENT_TIMEOUT_S,
    runner=None,
) -> UnitResult:
    """Run a host agent (claude/codex) over an owned PR branch, then CAS-push.

    The agent edits and commits; it never pushes. Progress is judged by the
    branch head actually moving — an agent run that changed nothing is honest
    no-progress and burns the attempt.
    """
    pr = candidate.pr
    assert pr is not None
    provider = agents.fixlike_provider(backend)
    runner = runner or agents.run_host_agent

    head_owner = pr.head_owner or survey.me
    repo_name = survey.canonical.split("/", 1)[1]
    push_url = gitutil.slug_url(f"{head_owner}/{repo_name}")

    counter_key = _FIX_COUNTER[kind](pr)
    tmp_branch = f"autoform-fix/{pr.number}"
    notes: list[str] = []
    with _cooperative_claim(board, cfg.respect_claims, f"branch/{pr.number}", notes) as (acquired, hb):
        if not acquired:
            return UnitResult(False, f"{kind} #{pr.number}: claimed by a live peer meanwhile")
        with _worktree_round_trip(cfg):
            # ONE ref on ONE connection is both the work base and the CAS
            # expect. Mixing `refs/pull/N/head` (canonical, mirror-lagged) with
            # `refs/heads/<branch>` (head repo) opens a lost-update window: the
            # CAS could certify a base we never actually built on.
            gitutil.fetch(cfg.lean_root, push_url, f"refs/heads/{pr.head_ref}")
            observed = gitutil.run_git(["rev-parse", "FETCH_HEAD"], cwd=cfg.lean_root).stdout.strip()
            gitutil.run_git(["checkout", "--quiet", "-B", tmp_branch, "FETCH_HEAD"], cwd=cfg.lean_root)
            try:
                start_oid = gitutil.head_oid(cfg.lean_root)

                counters.bump(counter_key)
                if kind == "fix-ci":
                    counters.bump(f"ci-pr-{pr.number}")
                prompt = agents.fill_prompt(
                    agents.prompts_dir() / _FIX_PROMPTS[kind],
                    pr=str(pr.number),
                    canonical=survey.canonical,
                    default_branch=survey.default_branch,
                    node=pr.node or "",
                    worker_id=cfg.worker_id,
                )
                feed_name = f"worker-cli:{kind}:#{pr.number}"
                _feed_start(cfg, kind, feed_name, pr.node or f"#{pr.number}")
                try:
                    rc, log_path = runner(provider, cfg.lean_root, prompt, cfg.log_dir, agent_timeout)
                finally:
                    _feed_done(cfg, feed_name)

                if rc != 0:
                    infra = agents.classify_infra_failure(log_path)
                    if infra and counters.refund(counter_key):
                        return UnitResult(False, f"{kind} #{pr.number}: {infra} (attempt refunded)",
                                          infra_failure=infra)
                    return UnitResult(False, f"{kind} #{pr.number}: agent failed rc={rc} (log: {log_path})")

                if not gitutil.clean_tree(cfg.lean_root):
                    gitutil.run_git(["add", "-A"], cwd=cfg.lean_root)
                    gitutil.run_git(["commit", "--quiet", "-m", f"autoform {kind}: PR #{pr.number}"],
                                    cwd=cfg.lean_root, check=False)
                if gitutil.head_oid(cfg.lean_root) == start_oid:
                    return UnitResult(False, f"{kind} #{pr.number}: agent finished but changed nothing")

                if not _lease_ok(hb):
                    return UnitResult(False, f"{kind} #{pr.number}: branch lease lost — refusing to push")
                if not gitutil.safe_push(cfg.lean_root, pr.head_ref, remote=push_url, expect=observed):
                    return UnitResult(False, f"{kind} #{pr.number}: CAS push lost (branch moved meanwhile)")
                return UnitResult(True, f"{kind} #{pr.number}: pushed a new head")
            finally:
                _drop_tmp_branch(cfg.lean_root, tmp_branch)


# ---------------------------------------------------------------------------
# progress — fold merged scoreboards, sync escalation issues, push
# ---------------------------------------------------------------------------

def do_progress(
    cfg: WorkerConfig,
    host: GitHost,
    board: ClaimBoard | None,
    counters: Counters,
    survey: Survey,
    *,
    push: bool = True,
) -> UnitResult:
    """Deterministically converge shared state after merges.

    1. Fold merged PRs' scoreboard verdicts into ``review_status.json``.
    2. Commit + CAS-push when the sidecar lives inside the repo (the static
       dashboard workflow republishes from the pushed commit).
    3. Mirror open engine escalations to GitHub issues (when Issues are on).

    Any machine produces the same fold from the same merged PRs — conflicts are
    structurally rare, and the CAS push refuses the race when they happen.
    """
    mods = scripts_modules()
    notes: list[str] = []
    with _cooperative_claim(board, cfg.respect_claims, "progress", notes) as (acquired, hb):
        if not acquired:
            return UnitResult(False, "progress: claimed by a live peer meanwhile")

        folded_already = _load_folded(cfg.folded_path)
        targets = [pr for pr in (PRInfo.from_gh(raw) for raw in
                                 host.pr_list(survey.canonical, state="merged", limit=50))
                   if pr.node and pr.number not in folded_already]

        # Collect scoreboard metas first (network). Only TRUSTED comment
        # authors count — on a public repo anyone can comment, and a forged
        # meta must never become a sidecar verdict. A merged PR without a
        # scoreboard is dismissed only after a grace window: its review may
        # still be in flight.
        trusted = _trusted_login_predicate(cfg, host, survey)
        metas: dict[int, dict] = {}
        scoreboardless: list[int] = []
        for pr in targets:
            meta = scoreboard.parse_meta(host.pr_comments(survey.canonical, pr.number),
                                         trusted=trusted, require_head=pr.head_oid or None)
            if meta and meta.get("node") == pr.node:
                metas[pr.number] = meta
            elif _past_review_grace(pr.updated_at):
                scoreboardless.append(pr.number)

        sidecar_path = cfg.project / "review_status.json"
        in_repo = _inside(cfg.lean_root, sidecar_path)
        pushed = False
        fold_oid: str | None = None
        folded_now: list[int] = []
        if metas and push and in_repo and survey.can_push:
            # Fold on the FRESH remote base so we never clobber updates that
            # only exist remotely, then CAS-push the fold.
            url = gitutil.slug_url(survey.canonical)
            with _worktree_round_trip(cfg):
                gitutil.fetch(cfg.lean_root, url, survey.default_branch)
                base_oid = gitutil.run_git(["rev-parse", "FETCH_HEAD"], cwd=cfg.lean_root).stdout.strip()
                gitutil.run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=cfg.lean_root)
                changed = _fold_metas(mods, sidecar_path, metas)
                if changed:
                    gitutil.run_git(["add", str(sidecar_path)], cwd=cfg.lean_root)
                    pr_list_str = " ".join(f"#{n}" for n in sorted(metas))
                    gitutil.run_git(["commit", "--quiet", "-m",
                                     f"autoform progress: fold scoreboards from {pr_list_str}"],
                                    cwd=cfg.lean_root)
                    if _lease_ok(hb):
                        pushed = gitutil.safe_push(cfg.lean_root, survey.default_branch,
                                                  remote=url, expect=base_oid)
                        if pushed:
                            fold_oid = gitutil.head_oid(cfg.lean_root)
                else:
                    pushed = True  # fold already present remotely — converged
            if pushed:
                folded_now = sorted(metas)
                if fold_oid:
                    # Land the fold in the operator's checkout too — otherwise
                    # local state stays stale and the next survey re-proves
                    # already-merged nodes. Fast-forward only; anything else is
                    # the operator's business.
                    if (gitutil.current_branch(cfg.lean_root) == survey.default_branch
                            and gitutil.clean_tree(cfg.lean_root)):
                        gitutil.run_git(["merge", "--ff-only", "--quiet", fold_oid],
                                        cwd=cfg.lean_root, check=False)
            # a lost CAS leaves metas unrecorded — the next round retries
        elif metas:
            _fold_metas(mods, sidecar_path, metas)
            folded_now = sorted(metas)
            if in_repo:
                notes.append("sidecar folded locally only (no push access) — tree left dirty for you to commit")

        if folded_now or scoreboardless:
            _record_folded(cfg.folded_path, folded_already | set(folded_now) | set(scoreboardless))

        issues_changed = 0
        if survey.issues_enabled:
            issues_changed = _sync_escalation_issues(cfg, host, survey.canonical)

        progressed = pushed or bool(folded_now) or issues_changed > 0
        parts = []
        if folded_now:
            parts.append(f"folded {len(folded_now)} PR(s)" + (" + pushed" if pushed else " (local only)"))
        if issues_changed:
            parts.append(f"synced {issues_changed} escalation issue(s)")
        parts.extend(notes)
        return UnitResult(progressed, "progress: " + ("; ".join(parts) or "nothing to fold"))


def _fold_metas(mods, sidecar_path: Path, metas: dict[int, dict]) -> bool:
    """Apply scoreboard metas to the sidecar's ``ai`` slots. Returns whether
    anything actually changed (idempotent re-folds are no-ops).

    The whole load-mutate-save cycle runs under the shared ``fslock`` — the
    dashboard writes human verdicts to this same file under that lock, and an
    unlocked read-modify-write here could erase one.
    """
    rm, fslock = mods["review_model"], mods["fslock"]
    with fslock.locked(sidecar_path):
        sidecar = rm.load_sidecar(sidecar_path)
        changed = False
        for number, meta in sorted(metas.items()):
            node = meta.get("node")
            ai = {axis: score for axis, score in (meta.get("scores") or {}).items()
                  if isinstance(score, int)}
            ai["verdict"] = meta.get("verdict")
            ai["source"] = f"scoreboard:pr-{number}"
            slot = sidecar.setdefault("reviews", {}).setdefault(node, {})
            prior = {k: v for k, v in (slot.get("ai") or {}).items() if k != "at"}
            if prior != ai:
                slot["ai"] = {**ai, "at": _now_iso()}  # human slot untouched — immutable by contract
                changed = True
        if changed:
            rm.save_sidecar(sidecar_path, sidecar)
        return changed


def _past_review_grace(updated_at: str, grace_s: int = REVIEW_INPROGRESS_TTL_S) -> bool:
    """Whether a merged PR is old enough to dismiss as never-getting-a-scoreboard.

    An unparseable timestamp counts as past grace (never wedge folding on bad
    data); a fresh merge stays undismissed so an in-flight review can land."""
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    return (datetime.now(timezone.utc) - updated).total_seconds() > grace_s


def _trusted_login_predicate(cfg: WorkerConfig, host: GitHost, survey: Survey):
    """login -> bool: the trust boundary for scoreboards and review markers.

    Trusted = the operator, declared extra identities, or a repo collaborator
    (checked via the API, cached per host instance). Fail closed on empty."""
    base = {survey.me, *getattr(survey, "extra_identities", ())}

    def trusted(login: str) -> bool:
        if not login:
            return False
        if login in base:
            return True
        try:
            return host.is_collaborator(survey.canonical, login)
        except Die:
            return False

    return trusted


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _record_folded(path: Path, numbers: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"prs": sorted(numbers)}, indent=1), encoding="utf-8")


def _sync_escalation_issues(cfg: WorkerConfig, host: GitHost, canonical: str) -> int:
    """Escalations ↔ issues: open ones get an issue, resolved ones get closed."""
    mods = scripts_modules()
    dq = mods["dispatch_queue"]
    try:
        tasks = dq.load_queue(cfg.project / "task_queue.json")
    except Exception:
        return 0
    open_nodes = {t.get("node"): t for t in tasks
                  if t.get("agent") == "escalation" and t.get("status") in ("queued", "running")}
    existing = host.issue_list(canonical, LABEL_ESCALATION)
    by_node = {}
    for issue in existing:
        title = str(issue.get("title", ""))
        if title.startswith("escalation:"):
            by_node[title.split(":", 1)[1].strip()] = issue

    changed = 0
    host.ensure_labels(canonical, [LABEL_ESCALATION])
    for node, task in open_nodes.items():
        if node and node not in by_node:
            body = (f"The deterministic engine hit a wall on `{node}` and raised an escalation.\n\n"
                    f"```\n{str(task.get('note', ''))[:1500]}\n```\n\n"
                    f"Resolve via `/autoform:orchestrate` on any machine, then this issue closes on the "
                    f"next progress round.\n\n"
                    f'<!--autoform-escalation:v1 {json.dumps({"node": node})}-->')
            if host.create_issue(canonical, f"escalation: {node}", body, [LABEL_ESCALATION]):
                changed += 1
    for node, issue in by_node.items():
        if node not in open_nodes:
            host.close_issue(canonical, int(issue.get("number", 0)),
                             "Escalation resolved locally — closed by autoform progress.")
            changed += 1
    return changed
