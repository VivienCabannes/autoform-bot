"""The agent stage — drain any queued task whose kind maps to a discovered role.

This is what makes ``work --loop`` more than a sorry-filling swarm. Every kind
in the registry (planner, graphreview, contentreview, holistic, mathcheck,
escalation, counterexample, priorart, and anything a project adds) is drained
here by spawning the operator's host CLI with **that role's own Markdown body**
as its instructions. Adding a role is adding a file; no code changes.

Two write paths, matching what the artifact is:

* **Lean proofs** (the ``prove`` unit) go through a PR, get a jury scoreboard,
  and auto-merge — code deserves review.
* **Roadmap curation** (``blueprint/`` plus generated ``graph.json``) is committed and
  CAS-pushed straight to the default branch under a claim. It is frequent,
  conflict-rare, and humans watch it on the dashboards rather than in PRs;
  a lost CAS simply retries next round.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass

from . import agents, gitutil
from .claims import ClaimBoard
from .config import WorkerConfig, scripts_modules
from .constants import MAX_AGENT_ATTEMPTS
from .counters import Counters
from .errors import Die
from .githost import GitHost
from .registry import AgentRole, Registry
from .survey import Candidate, Survey
from .work_units import (
    UnitResult,
    _cooperative_claim,
    _feed_done,
    _feed_start,
    _inside,
    _isolated_worktree,
    _lease_ok,
)

AGENT_TIMEOUT_S = 3600

#: Triage first, then structure, then breadth — the orchestrate skill's order.
KIND_PRIORITY = ("escalation", "planner", "mathcheck", "graphreview",
                 "contentreview", "counterexample", "priorart", "holistic")


def _recovery_outcome(log_path) -> str | None:
    """Read the recovery coordinator's machine-readable final marker."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        match = re.match(r"^\s*RECOVERY:\s*(RETRY|REFUTED|PARK)\b", line)
        if match:
            return match.group(1)
    return None


def _changed_paths(repo, base_ref: str = "HEAD") -> set[str]:
    tracked = gitutil.run_git(["diff", "--name-only", "-z", base_ref], cwd=repo).stdout
    untracked = gitutil.run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo
    ).stdout
    return {path for path in (tracked + untracked).split("\0") if path}


def _agent_paths_allowed(role: AgentRole, cfg: WorkerConfig, paths: set[str]) -> bool:
    """Enforce the role's durable write contract before any commit or push."""
    if not paths:
        return True
    try:
        project_rel = cfg.project.resolve().relative_to(cfg.lean_root.resolve())
    except ValueError:
        return False
    prefix = "" if str(project_rel) == "." else f"{project_rel.as_posix()}/"
    graph_path = f"{prefix}graph.json"
    if (cfg.project / "blueprint" / "roadmap").is_dir():
        content_prefix = f"{prefix}blueprint/"
    else:
        content_prefix = f"{prefix}informal_content/"
    content = all(path.startswith(content_prefix) or path == graph_path for path in paths)
    if role.writes == "content":
        return content
    if role.writes == "graph":
        return content
    return role.writes == "none" and not paths


@dataclass
class QueuedTask:
    task_id: str
    kind: str
    node: str
    node_label: str
    note: str = ""
    recovery: dict | None = None


def queued_agent_tasks(cfg: WorkerConfig, registry: Registry) -> list[QueuedTask]:
    """Open queue tasks this machine can run as host-CLI roles, in priority order."""
    dq = scripts_modules()["dispatch_queue"]
    try:
        tasks = dq.load_queue(cfg.project / "task_queue.json")
    except Exception:
        return []          # a corrupt queue is the orchestrator's to repair
    kinds = set(registry.agent_kinds())
    out = [
        QueuedTask(
            task_id=str(t.get("id")), kind=str(t.get("agent")),
            node=str(t.get("node") or ""), node_label=str(t.get("node_label") or t.get("node") or ""),
            note=str(t.get("note") or ""),
            recovery=t.get("recovery") if isinstance(t.get("recovery"), dict) else None,
        )
        for t in tasks
        if t.get("agent") in kinds and t.get("status") == "queued"
    ]

    def rank(task: QueuedTask) -> tuple:
        try:
            return (KIND_PRIORITY.index(task.kind), task.node)
        except ValueError:
            return (len(KIND_PRIORITY), task.node)

    return sorted(out, key=rank)


def build_prompt(role: AgentRole, task: QueuedTask, cfg: WorkerConfig, survey: Survey) -> str:
    """The role's own instructions plus this task's concrete context.

    The role body is authored material (``agents/<role>.md``) and is passed
    through verbatim — the registry never paraphrases a role.
    """
    sources = ""
    graph_path = cfg.graph_path
    with contextlib.suppress(Exception):
        import json

        meta = json.loads(graph_path.read_text(encoding="utf-8")).get("metadata", {})
        listed = [s.get("file", "") for s in (meta.get("sources") or []) if s.get("file")]
        if listed:
            sources = "\n".join(f"  - {s}" for s in listed)

    markdown_roadmap = (cfg.project / "blueprint" / "roadmap").is_dir()
    context = [
        "# Your assignment",
        "",
        f"- role: `{role.name}` (queue kind `{role.kind}`)",
        f"- target node: `{task.node}` ({task.node_label})",
        f"- dispatch project: {cfg.project}",
        f"- {'generated engine graph (read-only)' if markdown_roadmap else 'graph'}: {graph_path}",
        f"- Lean project: {cfg.lean_root}",
        (
            f"- authoritative roadmap: {cfg.project / 'blueprint' / 'roadmap'}"
            if markdown_roadmap
            else f"- node prose lives in: {cfg.project / 'informal_content'}/<node>.md"
        ),
    ]
    if sources:
        context += ["- sources:", sources]
    if task.note:
        context += ["", "## Task note (verbatim — often a worker's own words)", "",
                    "```", task.note[:2400], "```"]
    context += ["", "## How to finish", ""]
    if markdown_roadmap:
        context += [
            "- Edit only the Markdown roadmap. Folder structure defines chapters, frontmatter "
            "records checked facts, and links under `Depends on` / `Proof depends on` define edges.",
            "- Never edit `graph.json`; the worker harness regenerates that compatibility projection.",
        ]
    else:
        context += [
            "- Make every graph edit through "
            f"`python {cfg.plugin_root}/scripts/merge_node.py {graph_path} --payload <file>` — "
            "it is the only writer of graph.json.",
            "- Write node prose directly to `informal_content/<node>.md`.",
        ]
    context += [
        "- Do NOT run `git push` and do NOT open PRs; the worker harness commits and "
        "pushes what you leave in the tree, under a lease.",
        "- Do not edit `lean-toolchain`, `lakefile.*`, CI workflows, or anything under "
        "`scripts/`.",
        "- End with a one-line summary of what changed. If you could not do the work "
        "honestly, say `FAILED: <specific reason>` and change nothing.",
    ]
    return f"{role.body().strip()}\n\n---\n\n" + "\n".join(context)


def do_agent_task(
    cfg: WorkerConfig,
    host: GitHost,
    board: ClaimBoard | None,
    counters: Counters,
    survey: Survey,
    candidate: Candidate,
    *,
    registry: Registry,
    backend: str,
    agent_timeout: int = AGENT_TIMEOUT_S,
    runner=None,
) -> UnitResult:
    """Run one queued role task, then durably land whatever it curated."""
    task: QueuedTask = candidate.task           # set by the survey
    role = registry.get(task.kind)
    if role is None:
        return UnitResult(False, f"{task.kind}: no role file for this queue kind")
    provider = agents.fixlike_provider(backend)
    runner = runner or agents.run_host_agent
    dq = scripts_modules()["dispatch_queue"]
    counter_key = f"agent-{task.kind}-{task.node}"

    if not _inside(cfg.lean_root, cfg.project):
        return UnitResult(False, f"{task.kind} {task.node}: dispatch project is outside the Git repository")
    if role.writes != "none" and not survey.can_push:
        return UnitResult(False, f"{task.kind} {task.node}: no canonical push access; task remains queued")

    claim_key = f"task/{task.kind}/{task.task_id.replace(':', '-')}"
    notes: list[str] = []
    with _cooperative_claim(board, cfg.respect_claims, claim_key, notes) as (acquired, hb):
        if not acquired:
            return UnitResult(False, f"{task.kind} {task.node}: claimed by a live peer")

        url = gitutil.slug_url(survey.canonical)
        gitutil.fetch(cfg.lean_root, url, survey.default_branch)
        base_oid = gitutil.run_git(["rev-parse", "FETCH_HEAD"], cwd=cfg.lean_root).stdout.strip()
        with _isolated_worktree(cfg, "FETCH_HEAD") as work_cfg:

            counters.bump(counter_key)
            claimed = dq.main([str(cfg.project), "claim", task.task_id, "--detail",
                               f"{role.name} via autoform worker {cfg.worker_id}"])
            if claimed != 0:
                return UnitResult(False, f"{task.kind} {task.node}: queue task was claimed meanwhile")
            feed_name = f"worker-cli:{task.kind}:{task.node}"
            _feed_start(cfg, task.kind, feed_name, task.node)
            try:
                prompt = build_prompt(role, task, work_cfg, survey)
                rc, log_path = runner(provider, work_cfg.project, prompt, cfg.log_dir, agent_timeout)
            finally:
                _feed_done(cfg, feed_name)

            if rc != 0:
                infra = agents.classify_infra_failure(log_path)
                if infra and counters.refund(counter_key):
                    dq.main([str(cfg.project), "fail", task.task_id, "--reason",
                             f"{infra} (attempt refunded)", "--report-file", str(log_path)])
                    return UnitResult(False, f"{task.kind} {task.node}: {infra}", infra_failure=infra)
                dq.main([str(cfg.project), "fail", task.task_id, "--reason",
                         f"agent rc={rc} (log: {log_path})", "--report-file", str(log_path)])
                return UnitResult(False, f"{task.kind} {task.node}: agent failed rc={rc}")

            if role.writes != "none" and (work_cfg.project / "blueprint" / "roadmap").is_dir():
                from autoform_cli.engine_graph import write_engine_graph

                write_engine_graph(
                    work_cfg.project / "blueprint",
                    work_cfg.graph_path,
                    project_root=work_cfg.project,
                    lean_root=work_cfg.lean_root,
                )

            pushed = False
            changed = _changed_paths(work_cfg.lean_root, base_oid)
            recovery_outcome = _recovery_outcome(log_path) if task.kind == "escalation" else None
            recovery_fingerprint = ""
            if task.kind == "escalation" and task.recovery:
                recovery_fingerprint = scripts_modules()["recovery_state"].proof_fingerprint(
                    work_cfg.graph_path,
                    task.node,
                    work_cfg.lean_root,
                    str(task.recovery.get("backend") or ""),
                )
            invalid_recovery = task.kind == "escalation" and (
                recovery_outcome is None
                or (recovery_outcome in {"RETRY", "REFUTED"} and not changed)
            )
            if not _agent_paths_allowed(role, work_cfg, changed):
                paths = ", ".join(sorted(changed)[:8]) or "(none)"
                dq.main([str(cfg.project), "fail", task.task_id, "--reason",
                         f"role write contract rejected: {paths}"])
                return UnitResult(False, f"{task.kind} {task.node}: rejected out-of-contract paths: {paths}")
            if invalid_recovery:
                reason = ("recovery produced no outcome marker" if recovery_outcome is None
                          else "recovery requested action without durable evidence")
                args = [str(cfg.project), "park", task.task_id, "--reason", reason]
                if log_path.is_file():
                    args += ["--report-file", str(log_path)]
                if recovery_fingerprint:
                    args += ["--fingerprint", recovery_fingerprint]
                dq.main(args)
                return UnitResult(True, f"{task.kind} {task.node}: parked; {reason}")
            if changed:
                if not gitutil.clean_tree(work_cfg.lean_root):
                    gitutil.run_git(["add", "-A"], cwd=work_cfg.lean_root)
                    gitutil.run_git(["commit", "--quiet", "-m",
                                     f"{role.name}: {task.node}\n\nRoadmap curation by autoform worker "
                                     f"{cfg.worker_id} ({role.kind})."], cwd=work_cfg.lean_root)
                if not _lease_ok(hb):
                    dq.main([str(cfg.project), "fail", task.task_id, "--reason", "lease lost before push"])
                    return UnitResult(False, f"{task.kind} {task.node}: lease lost — refusing to push")
                pushed = gitutil.safe_push(work_cfg.lean_root, survey.default_branch,
                                           remote=url, expect=base_oid)
                if not pushed:
                    dq.main([str(cfg.project), "fail", task.task_id, "--reason",
                             "CAS lost — base moved; will retry"])
                    return UnitResult(False, f"{task.kind} {task.node}: CAS lost, retry next round")

            if task.kind == "escalation" and recovery_outcome in {"PARK", "REFUTED"}:
                parked_reason = ("statement refuted; correct it before retry"
                                 if recovery_outcome == "REFUTED"
                                 else "recovery wave exhausted; evidence ledger preserved")
                args = [str(cfg.project), "park", task.task_id, "--reason", parked_reason]
                if log_path.is_file():
                    args += ["--report-file", str(log_path)]
                if recovery_fingerprint:
                    args += ["--fingerprint", recovery_fingerprint]
                dq.main(args)
            else:
                done_args = [str(cfg.project), "done", task.task_id, "--result",
                             f"{role.name} completed" + (" (pushed)" if pushed else "")]
                if log_path.is_file():
                    done_args += ["--report-file", str(log_path)]
                dq.main(done_args)
            counters.clear(counter_key)
            detail = "; ".join([f"{task.kind} {task.node}: {role.name} done"
                                + (" + pushed" if pushed else " (no durable change)"), *notes])
            return UnitResult(True, detail)


def agent_candidates(cfg: WorkerConfig, registry: Registry, counters: Counters,
                     live_foreign: dict, *, can_push: bool = True) -> tuple[list[Candidate], list[Candidate]]:
    """(actionable, suppressed) candidates for every queued role task."""
    ready: list[Candidate] = []
    held: list[Candidate] = []
    for task in queued_agent_tasks(cfg, registry):
        cand = Candidate(task.kind, f"queued {task.kind} task", node=task.node)
        cand.task = task
        key = f"task/{task.kind}/{task.task_id.replace(':', '-')}"
        if key in live_foreign:
            cand.reason = "claimed by peer"
            held.append(cand)
        elif counters.get(f"agent-{task.kind}-{task.node}") >= MAX_AGENT_ATTEMPTS:
            cand.reason = "attempt budget spent"
            held.append(cand)
        elif role := registry.get(task.kind):
            if role.writes != "none" and not can_push:
                cand.reason = "no canonical push access"
                held.append(cand)
            else:
                ready.append(cand)
        else:
            cand.reason = "role disappeared"
            held.append(cand)
    return ready, held


def ensure_role(registry: Registry, kind: str) -> AgentRole:
    role = registry.get(kind)
    if role is None:
        raise Die(f"no agent role registered for kind {kind!r}; "
                  f"add agents/{kind}.md (or .autoform/agents/{kind}.md)")
    return role


def role_summary(registry: Registry) -> list[str]:
    """Human-readable registry listing for `autoform agents`."""
    lines = []
    for kind in sorted(registry.roles):
        role = registry.roles[kind]
        lines.append(f"  {role.icon} {kind:15} {role.name:24} "
                     f"[{role.drained_by}/{role.applies}] ({role.source})")
    return lines
