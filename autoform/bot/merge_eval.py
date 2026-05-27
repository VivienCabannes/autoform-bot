# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Post-merge evaluation — diff, target matching, and per-statement assessment."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.agent import Agent
from core.agent.loader import load_agent_definition
from core.inference import InferenceProtocol
from core.trace import AgentTrace, TraceStore

from .archive import ArchiveTraceStore
from core.tracker import ItemStatus, ItemTracker
from autoform.eval.types import FormalizationTarget
from tools.files.filesystem.server import FilesystemConfig, filesystem_server

from autoform.eval.generate_report import generate as generate_markdown
from autoform.eval.pipeline import assess_targets
from autoform.bot.tools.task_tracker.core import ConstrainedTracker
from autoform.bot.tools.task_tracker.server import constrained_tracker_server

logger = logging.getLogger(__name__)

_MERGE_MATCHER_DIR = Path(__file__).resolve().parent / "agents" / "merge_matcher"
_TRIAGE_AGENT_DIR = Path(__file__).resolve().parent / "agents" / "merge_eval_triage"


def get_merge_diff(repo_path: Path, pre_hash: str, post_hash: str) -> str:
    """Return the git diff between two commits."""
    result = subprocess.run(
        ["git", "diff", pre_hash, post_hash],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("git diff failed: %s", result.stderr)
        return ""
    return result.stdout


async def identify_affected_targets(
    diff: str,
    targets: list[FormalizationTarget],
    code_path: Path,
    book_path: Path,
    inference_factory: Callable[[], InferenceProtocol],
    trace_id_prefix: str = "merge_eval",
    trace_store: TraceStore | None = None,
) -> list[int]:
    """Ask the merge matcher agent which targets are affected by the diff.

    The agent gets filesystem tools to inspect the code and book directories
    in depth, cross-referencing the diff against the target list.

    Returns a list of 0-based target indices.
    """
    if not diff.strip():
        return []

    target_list = "\n".join(
        f"  [{i}] {t.name} ({t.kind}, {t.location}): {t.description}" for i, t in enumerate(targets)
    )
    prompt = (
        f"Here is a git diff from a recent merge:\n\n```diff\n{diff}\n```\n\n"
        f"Here are the book targets:\n{target_list}\n\n"
        f"Code directory: {code_path}\n"
        f"Book directory: {book_path}\n\n"
        "Inspect the code and book to determine which targets are affected by this diff. "
        "Use the filesystem tools to read the changed files, understand the declarations, "
        "and cross-reference against the targets. "
        "Refer to targets by their index number (the number in square brackets)."
    )

    definition = load_agent_definition(_MERGE_MATCHER_DIR)
    fs_cfg = filesystem_server(FilesystemConfig(allowed_dirs=(str(code_path), str(book_path))))
    agent_id = f"{trace_id_prefix}/merge_matcher"
    trace = AgentTrace(id=agent_id)
    async with Agent(
        definition=definition,
        inference=inference_factory(),
        server_configs=[fs_cfg],
        trace_store=trace_store,
        id=agent_id,
    ) as agent:
        agent.set_trace(trace)
        response = await agent.call(prompt)
        agent.finalize_trace()
        if trace_store:
            trace_store.save(trace)

    if not response:
        logger.warning("Merge matcher produced no response")
        return []

    # Extract JSON from response — look for ```json fence first, then try parsing
    json_str = None
    fence_start = response.rfind("```json")
    if fence_start != -1:
        fence_end = response.find("```", fence_start + 7)
        if fence_end != -1:
            json_str = response[fence_start + 7 : fence_end].strip()

    if not json_str:
        # Fallback: find the last valid JSON object in the response.
        # Search backwards for } then try each { before it until one parses.
        end = response.rfind("}")
        if end != -1:
            for start in range(end, -1, -1):
                if response[start] == "{":
                    try:
                        json.loads(response[start : end + 1])
                        json_str = response[start : end + 1]
                        break
                    except json.JSONDecodeError:
                        continue

    if not json_str:
        logger.warning("Merge matcher response has no JSON: %s", response)
        return []

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("Failed to parse merge matcher response: %s", json_str)
        return []

    indices = parsed.get("affected_targets", [])

    # Validate indices
    valid = [i for i in indices if isinstance(i, int) and 0 <= i < len(targets)]
    if len(valid) != len(indices):
        logger.warning("Merge matcher returned invalid targets: %s (valid: %s)", indices, valid)
    return valid


def _update_tracker(
    tracker: ItemTracker,
    results: list,
    affected_indices: list[int],
) -> None:
    """Update goal statuses in the tracker based on eval results.

    Also appends goal status change events to a JSONL log file next to
    the tracker file (e.g. ``goal_events.jsonl`` beside ``goals.json``).
    This provides a persistent historical record of when each goal
    transitioned between statuses.
    """
    event_log = tracker.path.with_name("goal_events.jsonl")
    events: list[str] = []

    for idx, result in zip(affected_indices, results):
        if result.score.passed:
            status = ItemStatus.COMPLETED
        elif hasattr(result.datum, "match_confidence") and result.datum.match_confidence == "not_found":
            status = ItemStatus.PENDING
        else:
            status = ItemStatus.FAILED

        meta: dict[str, Any] = {
            "score": result.score.value,
            "feedback": result.score.feedback,
            "metrics": result.score.metrics,
        }
        if result.datum is not None:
            if result.datum.lean_declaration:
                meta["lean_declaration"] = result.datum.lean_declaration
            if result.datum.lean_file:
                meta["lean_file"] = result.datum.lean_file

        tracker.update(str(idx), status=status, metadata=meta)

        events.append(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "goal_id": idx,
                    "status": status.value,
                    "score": result.score.value,
                },
            )
        )

    if events:
        with open(event_log, "a") as f:
            f.write("\n".join(events) + "\n")


# Rubric name → pass threshold (must match rubrics/*.json)
_RUBRIC_THRESHOLDS: dict[str, int] = {
    "faithfulness": 4,
    "proof_integrity": 3,
    "code_quality": 3,
}


def _build_goal_section(
    idx: int,
    result: Any,
    target: FormalizationTarget,
) -> str | None:
    """Build a structured summary for a single failed goal.

    Returns None if the goal should be skipped (passed, not found, or
    no rubric below threshold).
    """
    if result.score.passed:
        return None
    if hasattr(result.datum, "match_confidence") and result.datum.match_confidence == "not_found":
        return None

    metrics = result.score.metrics
    failing = [name for name, threshold in _RUBRIC_THRESHOLDS.items() if name in metrics and metrics[name] < threshold]
    if not failing:
        return None

    lean_decl = (result.datum and result.datum.lean_declaration) or target.lean_declaration or "(unknown)"
    lean_file = (result.datum and result.datum.lean_file) or target.lean_file or "(unknown)"
    score_parts = [f"{name}={metrics.get(name, '?')}/5" for name in _RUBRIC_THRESHOLDS]

    section = [
        f"### Goal {idx}: {target.name} ({target.kind})",
        f"- **Scores:** {', '.join(score_parts)}",
        f"- **Failing rubrics:** {', '.join(failing)}",
        f"- **Declaration:** `{lean_decl}`",
        f"- **File:** `{lean_file}`",
        f"- **Book location:** {target.location}",
    ]

    # Per-rubric feedback
    feedback = result.score.feedback
    for rubric in failing:
        blocks = re.split(r"(?=\[[\w_]+=\d+/\d+\])", feedback)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            tag = re.match(r"\[([\w_]+)=\d+/\d+\]", block)
            if tag and tag.group(1) == rubric:
                section.append(f"- **{rubric} feedback:** {block}")
                break

    # Unjustified axioms
    verdicts = metrics.get("axiom_verdicts", {})
    unjustified = [name for name, v in verdicts.items() if isinstance(v, dict) and not v.get("justified", True)]
    if unjustified:
        section.append(f"- **Unjustified axioms:** {', '.join(f'`{a}`' for a in unjustified)}")

    return "\n".join(section)


def _parse_created_tasks(response: str | None) -> list[str]:
    """Extract created task IDs from a triage agent response."""
    if not response:
        return []
    json_start = response.rfind('{"created_tasks"')
    if json_start == -1:
        return []
    json_end = response.find("}", json_start)
    if json_end == -1:
        return []
    try:
        parsed = json.loads(response[json_start : json_end + 1])
        return parsed.get("created_tasks", [])
    except json.JSONDecodeError:
        logger.warning("Failed to parse created_tasks JSON from triage response")
        return []


async def _triage_one_goal(
    goal_section: str,
    diff: str,
    code_path: Path,
    repo_path: Path,
    book_path: Path,
    task_tracker: ItemTracker,
    inference_factory: Callable[[], InferenceProtocol],
    agent_id: str,
    trace_store: TraceStore | None = None,
) -> list[str]:
    """Spawn a single triage agent for one failed goal.

    Returns the list of task IDs created by this agent.
    """
    prompt = (
        f"The following goal failed merge evaluation. Investigate it and create "
        f"targeted fix tasks.\n\n"
        f"**Code directory:** {code_path}\n"
        f"**Book directory:** {book_path}\n\n"
        f"## Merge diff\n\n```diff\n{diff}\n```\n\n"
        f"## Failed goal\n\n{goal_section}\n\n"
        f"Read the book at the given location, read the Lean code for the "
        f"declaration, and create micro-tasks to fix each specific problem."
    )

    read_dirs = (str(repo_path), str(book_path))
    fs_cfg = filesystem_server(FilesystemConfig(allowed_dirs=read_dirs, write_excluded_dirs=read_dirs))
    meval_tracker = ConstrainedTracker(task_tracker, mutable_flavors=frozenset({"meval"}))
    tracker_cfg = constrained_tracker_server(meval_tracker)

    definition = load_agent_definition(_TRIAGE_AGENT_DIR)
    trace = AgentTrace(id=agent_id)

    try:
        async with Agent(
            definition=definition,
            inference=inference_factory(),
            server_configs=[fs_cfg, tracker_cfg],
            trace_store=trace_store,
            id=agent_id,
        ) as agent:
            agent.set_trace(trace)
            response = await agent.call(prompt)
            agent.finalize_trace()
            if trace_store:
                trace_store.save(trace)
    except Exception:
        logger.exception("Triage agent %s failed", agent_id)
        return []

    return _parse_created_tasks(response)


async def _run_triage_agent(
    results: list,
    affected_indices: list[int],
    targets: list[FormalizationTarget],
    diff: str,
    code_path: Path,
    repo_path: Path,
    book_path: Path,
    task_tracker: ItemTracker,
    inference_factory: Callable[[], InferenceProtocol],
    trace_store: TraceStore | None = None,
) -> list[str]:
    """Spawn one triage agent per failed goal to create granular fix tasks.

    Each agent investigates a single failure — reads the book, reads the
    code, reads the eval feedback — and creates micro-tasks (one per sorry,
    one per axiom, one per faithfulness issue) in the DAG. Agents run
    concurrently.

    Returns the list of created task IDs.
    """
    # Build per-goal summaries, filtering out passed/not-found/no-failing-rubric
    goal_items: list[tuple[int, str]] = []
    for idx, result in zip(affected_indices, results):
        section = _build_goal_section(idx, result, targets[idx])
        if section is not None:
            goal_items.append((idx, section))

    if not goal_items:
        return []

    # Spawn one agent per failed goal concurrently
    tasks = [
        _triage_one_goal(
            goal_section=section,
            diff=diff,
            code_path=code_path,
            repo_path=repo_path,
            book_path=book_path,
            task_tracker=task_tracker,
            inference_factory=inference_factory,
            agent_id=f"triage/goal_{idx}",
            trace_store=trace_store,
        )
        for idx, section in goal_items
    ]
    per_agent_results = await asyncio.gather(*tasks)
    created = [task_id for agent_ids in per_agent_results for task_id in agent_ids]

    # Fallback: query tracker for any pending meval tasks
    if not created:
        created = [item["id"] for item in task_tracker.list(status="pending") if item.get("flavor") == "meval"]
    logger.info("Triage agents created %d fix tasks across %d goals", len(created), len(goal_items))
    return created


async def run_merge_eval(
    task_id: str,
    code_path: Path,
    repo_path: Path,
    pre_hash: str,
    post_hash: str,
    targets: list[FormalizationTarget],
    mathlib_path: Path,
    book_path: Path,
    merge_reports_path: Path,
    inference_factory: Callable[[], InferenceProtocol],
    tracker: ItemTracker | None = None,
    trace_store: TraceStore | None = None,
    worktrees_dir: Path | None = None,
    task_tracker: ItemTracker | None = None,
) -> tuple[Path | None, list[str]]:
    """Run post-merge evaluation on affected targets.

    Produces a merge report at ``merge_reports/{post_hash}/report.md``
    using the same format as the full eval report. Updates the goal
    tracker with pass/fail/pending status per target. When a task_tracker
    is provided, auto-creates fix tasks for failed goals.

    Args:
        worktrees_dir: Directory for eval worktrees. When provided, creates
            ``worktrees_dir/eval_{post_hash[:12]}``. Defaults to
            ``repo_path/../merge_eval_worktrees/`` for backward compatibility.
        task_tracker: The task DAG tracker. When provided alongside the goal
            tracker, auto-creates fix tasks for failed goals.

    Returns a tuple of (report_path, auto_created_task_ids). report_path
    is None if nothing to eval; auto_created_task_ids is empty if no fix
    tasks were created.
    """
    # Trace prefix: merge_batches/{post_hash_short} — colocated with queue steps
    trace_prefix = f"merge_batches/{post_hash[:8]}"

    # 1. Compute diff
    diff = get_merge_diff(repo_path, pre_hash, post_hash)
    if not diff:
        return None, []

    # 2. Identify affected targets
    affected_indices = await identify_affected_targets(
        diff,
        targets,
        code_path,
        book_path,
        inference_factory,
        trace_id_prefix=trace_prefix,
        trace_store=trace_store,
    )
    if not affected_indices:
        return None, []

    affected_targets = [targets[i] for i in affected_indices]
    logger.info(
        "Merge eval: %d affected targets: %s",
        len(affected_targets),
        [t.name for t in affected_targets],
    )

    # 3. Assess affected targets in an isolated worktree at post_hash.
    # This avoids race conditions with concurrent merges on main.
    if worktrees_dir is not None:
        wt_name = f"eval_{post_hash[:12]}"
        wt_path = worktrees_dir / wt_name
    else:
        wt_path = repo_path.parent / "merge_eval_worktrees" / post_hash[:12]
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    merge_trace_store = None
    if trace_store:
        sub_run_dir = trace_store.run_dir / trace_prefix
        if isinstance(trace_store, ArchiveTraceStore):
            merge_trace_store = ArchiveTraceStore(sub_run_dir, trace_store._archive_dir / trace_prefix)
        else:
            merge_trace_store = TraceStore(sub_run_dir)

    try:
        # Create worktree at the exact merge commit (under lock to prevent
        # concurrent git worktree add from corrupting .git/worktrees/).
        lock_path = repo_path / ".worktree_lock"
        with open(lock_path, "w") as lock_file:
            fcntl.lockf(lock_file, fcntl.LOCK_EX)
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), post_hash],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
        logger.info("Merge eval worktree created at %s", wt_path)

        # Symlink .lake/packages so lake build finds pre-resolved deps
        # instead of writing into the shared mathlib directories.
        lake_src = repo_path / ".lake" / "packages"
        lake_dst = wt_path / ".lake" / "packages"
        if lake_src.exists() and not lake_dst.exists():
            lake_dst.parent.mkdir(parents=True, exist_ok=True)
            lake_dst.symlink_to(lake_src.resolve())

        wt_code_dir = wt_path / code_path.name  # e.g. wt_path / "Differential_Analysis"

        report, results = await assess_targets(
            targets=affected_targets,
            code_dir=wt_code_dir,
            repo_dir=wt_path,
            book_dir=book_path,
            mathlib_path=mathlib_path,
            inference_factory=inference_factory,
            indices=affected_indices,
            trace_store=merge_trace_store,
        )
    finally:
        # Clean up worktree (under lock — remove also mutates .git/worktrees/)
        lock_path = repo_path / ".worktree_lock"
        with open(lock_path, "w") as lock_file:
            fcntl.lockf(lock_file, fcntl.LOCK_EX)
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
        logger.info("Merge eval worktree removed")

    # 4. Update goal tracker
    if tracker is not None:
        _update_tracker(tracker, results, affected_indices)

    # 4b. Auto-create fix tasks for failed goals via triage agent
    fix_task_ids: list[str] = []
    if task_tracker is not None:
        fix_task_ids = await _run_triage_agent(
            results=results,
            affected_indices=affected_indices,
            targets=targets,
            diff=diff,
            code_path=code_path,
            repo_path=repo_path,
            book_path=book_path,
            task_tracker=task_tracker,
            inference_factory=inference_factory,
            trace_store=merge_trace_store,
        )

    # 5. Write report
    report["merge"] = {
        "task_id": task_id,
        "pre_hash": pre_hash,
        "post_hash": post_hash,
    }

    # 5. Write report under merge_reports/{post_hash_short}/
    report_dir = merge_reports_path / post_hash[:8]
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "report.json"
    tmp = json_path.with_name(json_path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2))
    os.replace(tmp, json_path)

    generate_markdown(str(json_path))

    logger.info("Merge eval report: %s", json_path.with_suffix(".md"))
    return json_path.with_suffix(".md"), fix_task_ids
