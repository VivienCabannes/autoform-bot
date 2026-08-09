"""The survey — one pass that builds the whole work picture.

Reads three sources and produces stage buckets of candidates:

* GitHub PR state (``gh pr list --json`` on the canonical repo),
* the local graph + review sidecar (prove eligibility, via ``review_model``),
* the claim board (avoid nodes/branches other workers hold).

Detection only — no unit execution here, so `status`/`--dry-run` are free of
side effects. Candidate randomization is seeded by worker id so parallel
workers naturally de-contend without coordinating.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil, scoreboard
from .claims import ClaimBoard, author_claim_key
from .config import WorkerConfig, scripts_modules
from .constants import (
    HOLD_LABELS,
    MAX_CI_ATTEMPTS,
    MAX_CI_PR_ATTEMPTS,
    MAX_FIX_ATTEMPTS,
    MAX_MERGE_ATTEMPTS,
    MAX_REBASE_ATTEMPTS,
    MAX_REVIEW_ERRORS,
    STAGES,
    merge_paths_allowed,
)
from .counters import Counters
from .errors import ClaimTransportError
from .githost import GitHost, build_state_of
from .runtime_graph import eligible_prove_nodes as runtime_eligible_prove_nodes


@dataclass
class PRInfo:
    number: int
    title: str
    author: str
    head_ref: str
    head_oid: str
    head_owner: str
    is_draft: bool
    mergeable: str
    build: str          # success | failed | pending
    labels: list[str]
    node: str | None    # from the body's target marker
    updated_at: str = ""
    files: tuple = ()   # changed paths (the merge gate's path allowlist input)

    @classmethod
    def from_gh(cls, raw: dict) -> "PRInfo":
        return cls(
            number=int(raw.get("number", 0)),
            title=str(raw.get("title", "")),
            author=str((raw.get("author") or {}).get("login", "")),
            head_ref=str(raw.get("headRefName", "")),
            head_oid=str(raw.get("headRefOid", "")),
            head_owner=str((raw.get("headRepositoryOwner") or {}).get("login", "")),
            is_draft=bool(raw.get("isDraft")),
            mergeable=str(raw.get("mergeable", "")),
            build=build_state_of(raw),
            labels=[label.get("name", "") for label in (raw.get("labels") or [])],
            node=scoreboard.parse_target(raw.get("body")),
            updated_at=str(raw.get("updatedAt", "")),
            files=tuple(f.get("path", "") for f in (raw.get("files") or [])),
        )


@dataclass
class Candidate:
    kind: str
    reason: str
    pr: PRInfo | None = None
    node: str | None = None
    task: object = None      # QueuedTask for agent-stage candidates


@dataclass
class Survey:
    canonical: str
    default_branch: str
    me: str
    stages: dict = field(default_factory=dict)      # stage -> [Candidate]
    suppressed: dict = field(default_factory=dict)  # stage -> [Candidate] (budget/claimed/waiting)
    notes: list = field(default_factory=list)
    prs: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    can_push: bool = False
    issues_enabled: bool = False
    targets: dict = field(default_factory=dict)   # target id -> distance metrics
    resumable_parks: list = field(default_factory=list)   # (node_id, task_id) — parked but inputs moved
    allow_unchecked_merge: bool = False
    extra_identities: tuple = ()

    def actionable(self, stage: str) -> list:
        return self.stages.get(stage, [])

    def to_json(self) -> dict:
        return {
            "canonical": self.canonical,
            "default_branch": self.default_branch,
            "me": self.me,
            "can_push": self.can_push,
            "issues_enabled": self.issues_enabled,
            "stages": {
                stage: [
                    {"reason": c.reason, "pr": c.pr.number if c.pr else None, "node": c.node}
                    for c in cands
                ]
                for stage, cands in self.stages.items()
            },
            "suppressed": {
                stage: [
                    {"reason": c.reason, "pr": c.pr.number if c.pr else None, "node": c.node}
                    for c in cands
                ]
                for stage, cands in self.suppressed.items()
            },
            "claims": [
                {k: v for k, v in lease.items() if not k.startswith("_")} | {
                    "key": lease.get("_key"), "expired": lease.get("_expired"),
                }
                for lease in self.claims
            ],
            "notes": self.notes,
            "targets": self.targets,
            "resumable_parks": [{"node": n, "task": t} for n, t in self.resumable_parks],
        }


def _tended(pr: PRInfo, me: str, extra_identities: list[str]) -> bool:
    """A PR this worker may rewrite: authored by us (or a declared alias)."""
    identities = {me, *extra_identities}
    return pr.author in identities or pr.head_owner in identities


def eligible_prove_nodes(cfg: WorkerConfig) -> list[tuple[str, dict, str]]:
    """Refine runtime-eligible leaves with private review and Lean evidence."""
    rm = scripts_modules()["review_model"]
    sidecar = rm.load_sidecar(cfg.project / "review_status.json")
    eligible: list[tuple[str, dict, str]] = []
    for node_id, node, runtime_reason in runtime_eligible_prove_nodes(cfg.runtime):
        verdict = rm.verdict_of(node_id, sidecar)
        lean_file = node.get("lean_file")
        lean_path = (cfg.lean_root / str(lean_file)) if lean_file else None
        if lean_path is not None:
            try:
                lean_path.resolve().relative_to(cfg.lean_root.resolve())
            except ValueError:
                lean_path = None
        has_lean = lean_path is not None and lean_path.is_file()
        sorried = has_lean and rm.lean_has_incomplete_proof(
            lean_path.read_text(encoding="utf-8", errors="replace")
        )
        if verdict == "rejected":
            reason = "verdict rejected — needs repair"
        elif not has_lean:
            reason = "no Lean landed yet"
        elif sorried:
            reason = "Lean present but sorry'd"
        else:
            continue
        eligible.append((node_id, node, reason))
    return sorted(eligible, key=lambda item: item[0])


def collect(
    cfg: WorkerConfig,
    host: GitHost,
    board: ClaimBoard | None,
    counters: Counters,
    canonical: str,
    default_branch: str,
    extra_identities: list[str] | None = None,
    allow_foreign_review: bool = False,
    allow_unchecked_merge: bool = False,
    proof_backend: str = "max",
) -> Survey:
    me = host.me()
    survey = Survey(canonical=canonical, default_branch=default_branch, me=me,
                    allow_unchecked_merge=allow_unchecked_merge,
                    extra_identities=tuple(extra_identities or ()))
    survey.can_push = host.can_push(canonical)
    survey.issues_enabled = host.has_issues(canonical)
    extra = extra_identities or []

    def trusted(login: str) -> bool:
        """The trust boundary for review targets, scoreboards, and markers."""
        if not login:
            return False
        if login == me or login in extra:
            return True
        try:
            return host.is_collaborator(canonical, login)
        except Exception:
            return False

    prs = [PRInfo.from_gh(raw) for raw in host.pr_list(canonical)]
    survey.prs = prs

    # The local sidecar is the human's channel into the merge gate: a verdict
    # recorded in the review dashboard outranks the jury and holds the PR.
    rm = scripts_modules()["review_model"]
    sidecar = rm.load_sidecar(cfg.project / "review_status.json")

    claims: list[dict] = []
    claim_board_available = True
    if board is not None and cfg.respect_claims:
        try:
            claims = board.list()
        except ClaimTransportError as error:
            claim_board_available = False
            survey.notes.append(f"claim board unreachable — coordinated mutation disabled: {error}")
    survey.claims = claims
    live_foreign = {
        lease.get("resource"): lease
        for lease in claims
        if not lease.get("_expired") and lease.get("owner") != cfg.worker_id
    }

    for stage in STAGES:
        survey.stages.setdefault(stage, [])
        survey.suppressed.setdefault(stage, [])

    def push_cand(stage: str, cand: Candidate, ok: bool) -> None:
        if ok and cfg.respect_claims and not claim_board_available and stage != "review":
            cand.reason = "claim board unreachable - coordinated mutation disabled"
            ok = False
        (survey.stages if ok else survey.suppressed)[stage].append(cand)

    # --- PR-tending stages --------------------------------------------------
    for pr in prs:
        if pr.is_draft:
            continue
        tended = _tended(pr, me, extra)
        head12 = pr.head_oid[:12]

        if tended and pr.mergeable == "CONFLICTING":
            ok = counters.get(f"rebase-pr-{pr.number}") < MAX_REBASE_ATTEMPTS
            claimed = f"branch/{pr.number}" in live_foreign
            push_cand("rebase", Candidate("rebase", "conflicts with base" if ok and not claimed
                                          else "budget spent" if not ok else "claimed by peer", pr=pr),
                      ok and not claimed)
            continue

        if tended and pr.build == "failed":
            ok = (counters.get(f"ci-{pr.number}-{head12}") < MAX_CI_ATTEMPTS
                  and counters.get(f"ci-pr-{pr.number}") < MAX_CI_PR_ATTEMPTS)
            claimed = f"branch/{pr.number}" in live_foreign
            push_cand("fix-ci", Candidate("fix-ci", "checks failing" if ok and not claimed
                                          else "budget spent" if not ok else "claimed by peer", pr=pr),
                      ok and not claimed)
            continue

        if pr.build == "failed":
            # Foreign PR with red checks: not ours to fix, and never reviewable
            # until green (the jury judges code, not CI archaeology).
            if pr.node:
                push_cand("review", Candidate("review", "checks failing — author must green them first",
                                              pr=pr, node=pr.node), False)
            continue

        if pr.build == "pending":
            push_cand("review", Candidate("review", "checks still running", pr=pr), False)
            continue

        # Build green from here on.
        meta = None
        comments: list[dict] = []
        if pr.node:  # only autoform PRs (target-marked) enter review/fix flows
            if not allow_foreign_review and not trusted(pr.author):
                # Reviewing means checking out and BUILDING the head — running
                # a stranger's code on this machine. Off by default; opt in
                # with --review-foreign after reading the PR yourself.
                push_cand("review", Candidate(
                    "review", "author is not a collaborator — reviewing runs their code "
                              "(--review-foreign to opt in)", pr=pr, node=pr.node), False)
                continue
            comments = host.pr_comments(canonical, pr.number)
            meta = scoreboard.parse_meta(comments, trusted=trusted, require_head=pr.head_oid or None)

            reviewed_at_head = meta is not None
            if not reviewed_at_head:
                busy = scoreboard.active_inprogress(comments, pr.head_oid, trusted=trusted)
                ok = counters.get(f"review-err-{pr.number}") < MAX_REVIEW_ERRORS
                push_cand("review", Candidate(
                    "review",
                    "head not yet scoreboarded" if ok and not busy
                    else "peer review in flight" if busy else "review error budget spent",
                    pr=pr, node=pr.node,
                ), ok and not busy)
                continue

            if tended and meta and meta.get("verdict") in {"flagged", "rejected"}:
                ok = counters.get(f"fix-{pr.number}-{head12}") < MAX_FIX_ATTEMPTS
                claimed = f"branch/{pr.number}" in live_foreign
                push_cand("fix", Candidate("fix", f"verdict {meta.get('verdict')} at head"
                                           if ok and not claimed else "budget spent" if not ok
                                           else "claimed by peer", pr=pr, node=pr.node),
                          ok and not claimed)
                continue

            if meta and meta.get("verdict") == "clean":
                # The auto-merge gate. Humans steer through the dashboards: a
                # human rejected/flagged verdict in the sidecar blocks the gate,
                # as does any hold label; toolchain/CI/tooling paths always
                # wait for a human.
                cand = Candidate("merge", "clean verdict at head + green CI", pr=pr, node=pr.node)
                block = merge_block_reason(
                    pr, sidecar, can_push=survey.can_push,
                    allow_unchecked_merge=allow_unchecked_merge,
                )
                if block:
                    cand.reason = block
                    push_cand("merge", cand, False)
                elif counters.get(f"merge-{pr.number}") >= MAX_MERGE_ATTEMPTS:
                    cand.reason = "merge attempt budget spent"
                    push_cand("merge", cand, False)
                else:
                    push_cand("merge", cand, True)

    # --- progress -----------------------------------------------------------
    folded = _load_folded(cfg.folded_path)
    merged = [PRInfo.from_gh(raw) for raw in host.pr_list(canonical, state="merged", limit=50)]
    unfolded = [pr for pr in merged if pr.node and pr.number not in folded]
    if unfolded:
        cand = Candidate("progress", f"{len(unfolded)} merged scoreboard(s) to fold")
        if survey.can_push:
            claimed = "progress" in live_foreign
            push_cand("progress", cand if not claimed else Candidate("progress", "claimed by peer"),
                      not claimed)
        else:
            cand.reason += " — no push access, skipping"
            push_cand("progress", cand, False)

    # --- agent roles (planner / mathcheck / counterexample / … from the registry) ---
    from .agent_work import agent_candidates  # noqa: PLC0415 — avoids an import cycle
    from .registry import Registry  # noqa: PLC0415

    registry = Registry(cfg.plugin_root, cfg.project)
    ready, held = agent_candidates(cfg, registry, counters, live_foreign,
                                   can_push=survey.can_push)
    survey.stages["agents"].extend(ready)
    survey.suppressed["agents"].extend(held)

    # --- prove --------------------------------------------------------------
    # Spend pacing: an unattended fleet has nobody watching the meter, and
    # recovery retries are deliberately uncapped. The governor bounds resources
    # only — it never ends work, and the fleet self-resumes as the rolling
    # window clears.
    spend = scripts_modules()["spend_governor"].check(cfg.lean_root, proof_backend)
    if not spend["allowed"]:
        survey.notes.append(f"prove paced: {spend['reason']}")
    open_pr_nodes = {pr.node for pr in prs if pr.node}
    intentions = _intention_avoid_list(host, canonical) if survey.issues_enabled else set()
    eligible = eligible_prove_nodes(cfg)
    try:
        queue_tasks = scripts_modules()["dispatch_queue"].load_queue(
            cfg.project / "task_queue.json"
        )
    except Exception:
        queue_tasks = []
    recovery_state = scripts_modules()["recovery_state"]
    adapter = scripts_modules()["backend_config"].prover_of(proof_backend)
    # Decontend first (worker-seeded shuffle), then order by mission priority:
    # with declared targets, in-cone nodes come first, tallest untrusted chain
    # above them first (the critical path). The stable sort keeps ties in
    # shuffled order so parallel workers still spread. No targets → the
    # shuffle alone stands, as before.
    rng = random.Random(cfg.worker_id)
    rng.shuffle(eligible)
    try:
        graph_nodes, graph_meta = rm.load_graph(cfg.compatibility_graph_path)
        priority = rm.prove_priority(graph_nodes, sidecar, graph_meta)
        survey.targets = {t: rm.target_metrics(t, graph_nodes, sidecar)
                          for t in rm.graph_targets(graph_meta) if t in graph_nodes}
    except Exception:
        priority = {}
    if priority:
        eligible.sort(key=lambda item: priority.get(item[0], (2, 0))[:2])
    for node_id, _node, reason in eligible:
        if node_id in open_pr_nodes:
            push_cand("prove", Candidate("prove", "open PR already targets it", node=node_id), False)
            continue
        if author_claim_key(node_id) in live_foreign:
            push_cand("prove", Candidate("prove", "claimed by peer", node=node_id), False)
            continue
        if node_id in intentions:
            push_cand("prove", Candidate("prove", "human intention registered", node=node_id), False)
            continue
        recovery = recovery_state.latest_recovery(queue_tasks, node_id)
        # A parked recovery whose inputs have since moved is resumable: the
        # evidence gate works in both directions, so upstream progress (a merged
        # sibling, a Mathlib bump, a re-plan) revives the node without anyone
        # asking. Checked BEFORE the parked suppression below, which would
        # otherwise make parking permanent and silently cost the fleet a node.
        resumable = recovery_state.resumable_park(
            queue_tasks, node_id, cfg.compatibility_graph_path, cfg.lean_root, adapter)
        if resumable is not None:
            # Report it, but do NOT make it prove-actionable: the round resumes
            # the recovery and re-surveys, so claiming a prove here would make
            # --dry-run promise work a real round does not do on this pass.
            survey.resumable_parks.append((node_id, str(resumable.get("id") or "")))
            push_cand("prove", Candidate(
                "prove", "parked recovery resumable — resuming on this round", node=node_id
            ), False)
            continue
        if recovery and recovery.get("status") in {"queued", "running", "parked"}:
            push_cand("prove", Candidate(
                "prove", f"proof recovery {recovery.get('status')}", node=node_id
            ), False)
            continue
        if recovery_state.unchanged_recovery(
                queue_tasks, node_id, cfg.compatibility_graph_path, cfg.lean_root, adapter):
            push_cand("prove", Candidate(
                "prove", "proof recovery produced no new prover input", node=node_id
            ), False)
            continue
        if not spend["allowed"]:
            push_cand("prove", Candidate("prove", f"paced — {spend['reason']}", node=node_id), False)
            continue
        push_cand("prove", Candidate("prove", reason, node=node_id), True)

    # Local staleness check: prove eligibility above was read from the LOCAL
    # checkout; if the local default branch is behind the remote, the operator
    # should sync before trusting the prove bucket.
    try:
        remote_default = gitutil.remote_ref_oid(gitutil.slug_url(canonical),
                                                f"refs/heads/{default_branch}")
        local = gitutil.run_git(["rev-parse", "--quiet", "--verify",
                                 f"refs/heads/{default_branch}"], cwd=cfg.lean_root, check=False)
        local_default = local.stdout.strip() if local.returncode == 0 else None
        if remote_default and local_default and remote_default != local_default:
            survey.notes.append(
                f"local {default_branch} ({local_default[:8]}) differs from remote "
                f"({remote_default[:8]}) — run `git pull` / `autoform-worker sync` before trusting prove eligibility"
            )
    except Exception:
        pass  # a staleness hint must never break the survey

    return survey


def _human_verdict(sidecar: dict, node_id: str | None) -> str | None:
    """A human's own verdict on this node when it blocks the merge gate.

    Humans steer through the dashboards, so a ``flagged``/``rejected`` verdict
    recorded there is a hold — the jury cannot overrule it (the human slot is
    immutable to machines by contract).
    """
    if not node_id:
        return None
    record = (sidecar.get("reviews") or {}).get(node_id) or {}
    human = record.get("human") or {}
    verdict = human.get("verdict")
    return verdict if verdict in {"flagged", "rejected"} else None


def merge_block_reason(
    pr: PRInfo,
    sidecar: dict,
    *,
    can_push: bool,
    allow_unchecked_merge: bool,
) -> str | None:
    """Return the reason this PR cannot pass the deterministic merge gate."""
    if pr.build != "success" and not allow_unchecked_merge:
        return "no successful CI check ran on this head — install a verify workflow or pass --merge-without-ci"
    if not can_push:
        return "no merge permission on canonical"
    holds = sorted(HOLD_LABELS.intersection(pr.labels))
    if holds:
        return f"hold label: {', '.join(holds)}"
    human_block = _human_verdict(sidecar, pr.node)
    if human_block:
        return f"human verdict {human_block} blocks the gate"
    if not merge_paths_allowed(pr.files):
        return "touches non-roadmap paths — needs a human"
    return None


def _load_folded(path: Path) -> set[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(n) for n in data.get("prs", [])} if isinstance(data, dict) else set()
    except (OSError, json.JSONDecodeError, ValueError):
        return set()


def _intention_avoid_list(host: GitHost, canonical: str) -> set[str]:
    """Node ids humans have claimed via assigned ``autoform:intention`` issues.

    Fail-open: an API error yields an empty set rather than blocking the round.
    """
    from .constants import LABEL_INTENTION

    avoid: set[str] = set()
    try:
        for issue in host.issue_list(canonical, LABEL_INTENTION):
            if not issue.get("assignees"):
                continue  # unassigned intention — informational, not a claim
            title = str(issue.get("title", ""))
            if ":" in title:
                node = title.split(":", 1)[1].strip()
                if node:
                    avoid.add(node)
    except Exception:
        return set()
    return avoid
