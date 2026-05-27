# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Archive trace store — append-only trace history for the visualizer.

Wraps TraceStore to maintain archive files that preserve the full message
history across compaction events. The archive files use the same JSON format
as normal traces, so the visualizer can read them directly.

Also provides utilities to archive reports and skills before deletion,
and to snapshot usage data before pruning traces.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.trace import TraceStore

logger = logging.getLogger(__name__)


class ArchiveTraceStore(TraceStore):
    """TraceStore that maintains append-only archive traces.

    Messages grow monotonically in the archive — when compaction is detected
    (message count drops), post-compaction messages (including the summary)
    are appended rather than replacing the history.

    Only currently-active traces are cached in memory. Finalized traces are
    evicted to keep memory proportional to the number of running agents.
    """

    def __init__(self, run_dir: Path, archive_dir: Path):
        super().__init__(run_dir)
        self._archive_dir = Path(archive_dir)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

        # In-memory caches — only for active (non-finalized) traces
        self._archive_messages: dict[str, list[dict[str, Any]]] = {}
        self._message_counts: dict[str, int] = {}

    def _archive_path(self, trace_id: str) -> Path:
        """Resolve trace_id to an archive file path, supporting subdirectories."""
        return self._archive_dir / f"{trace_id}.json"

    def save(self, trace: Any) -> None:
        """Save live trace and update the append-only archive."""
        super().save(trace)

        trace_id = getattr(trace, "trace_id", None)
        if trace_id is None:
            return

        messages = getattr(trace, "messages", None)
        if messages is None:
            # Non-conversation traces (e.g. step traces) — copy directly to archive
            archive_path = self._archive_path(trace_id)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = archive_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(trace.to_dict(), f, indent=2)
            tmp_path.rename(archive_path)
            return

        # First encounter for this trace_id — initialize from cache or disk
        if trace_id not in self._archive_messages:
            archive_path = self._archive_path(trace_id)
            if archive_path.exists():
                try:
                    with open(archive_path) as f:
                        archived = json.load(f)
                    self._archive_messages[trace_id] = archived.get("messages", [])
                except (json.JSONDecodeError, OSError):
                    logger.warning("Failed to load archive %s, reinitializing from current trace", archive_path)
                    self._archive_messages[trace_id] = list(messages)
            else:
                self._archive_messages[trace_id] = list(messages)
            self._message_counts[trace_id] = len(messages)

        # Diff and append — runs on every save, including the first after loading from disk
        prev_count = self._message_counts[trace_id]
        curr_count = len(messages)

        if curr_count > prev_count:
            # Growth: append only the new messages
            self._archive_messages[trace_id].extend(messages[prev_count:])
        elif curr_count < prev_count:
            # Compaction: append post-compaction messages (skip system prompt)
            self._archive_messages[trace_id].extend(messages[1:])

        self._message_counts[trace_id] = curr_count

        # Write archive file — same format as to_dict() but with accumulated messages
        archive_dict = trace.to_dict()
        archive_dict["messages"] = self._archive_messages[trace_id]

        archive_path = self._archive_path(trace_id)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = archive_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(archive_dict, f, indent=2)
        tmp_path.rename(archive_path)

        # Evict finalized traces from memory
        final_status = getattr(trace, "final_status", "running")
        if final_status != "running":
            self._archive_messages.pop(trace_id, None)
            self._message_counts.pop(trace_id, None)


def archive_reports(reports_path: Path, archive_dir: Path, round_num: int) -> None:
    """Copy all reports/*.json to archive/reports/ with round suffix before clearing.

    Each report is saved as {task_id}_round{round_num}.json so reports from
    different rounds don't overwrite each other.
    """
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    for f in reports_path.glob("*.json"):
        dest = archive_dir / f"{f.stem}_round{round_num}{f.suffix}"
        shutil.copy2(f, dest)
        logger.debug("Archived report %s -> %s", f.name, dest.name)


def archive_skills(skills_path: Path, archive_dir: Path, task_id: str) -> None:
    """Copy skills/tasks/{task_id}/ to archive/skills/tasks/{task_id}/ before deletion."""
    src = Path(skills_path) / "tasks" / task_id
    if not src.exists():
        return

    dest = Path(archive_dir) / "tasks" / task_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    logger.debug("Archived skills for task %s", task_id)


# ---------------------------------------------------------------------------
# Usage snapshot
# ---------------------------------------------------------------------------


def _categorize_trace(rel_parts: tuple[str, ...]) -> str:
    """Categorize a trace file by its path relative to traces/.

    Must stay in sync with the visualizer's categorization in
    ``autoform/visualizer/app.py`` ``_load_run_data``.
    """
    if rel_parts == ("orchestrator.json",):
        return "orchestrator"
    if rel_parts[0] == "tasks" and len(rel_parts) >= 2:
        fname = rel_parts[-1]
        if fname == "analyzer.json":
            return "analyzers"
        if "worker" in fname:
            return "workers"
        if "reviewer" in fname:
            return "reviewers"
        return "other"
    if rel_parts[0] == "readers":
        return "readers"
    if rel_parts[0] in ("eval", "judge"):
        return "eval"
    if rel_parts[0] == "merge_eval":
        return "merge_eval"
    return "other"


def _compute_usage_snapshot(
    traces_dir: Path,
    trace_files: list[Path],
    timestamp: str,
) -> dict[str, Any]:
    """Aggregate usage from *trace_files* into a snapshot dict.

    The snapshot captures total cost, tokens, per-category cost breakdown,
    and per-model token stats — everything the visualizer needs to reconstruct
    historical usage after the live traces are deleted.
    """
    total_cost = 0.0
    total_tokens = 0
    category_costs: dict[str, float] = {
        "workers": 0.0,
        "reviewers": 0.0,
        "orchestrator": 0.0,
        "analyzers": 0.0,
        "readers": 0.0,
        "eval": 0.0,
        "merge_eval": 0.0,
        "other": 0.0,
    }
    model_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "total_input": 0,
            "total_output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "total_cost": 0.0,
        }
    )

    for path in trace_files:
        if path.name == "steps.json":
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Skipping unreadable trace %s", path)
            continue

        s = data.get("summary")
        if not s:
            continue

        cost = s.get("total_cost_usd", 0.0)
        tokens = s.get("total_tokens", 0)
        total_cost += cost
        total_tokens += tokens

        rel = path.relative_to(traces_dir)
        category = _categorize_trace(rel.parts)
        category_costs[category] += cost

        for call in data.get("llm_calls", []):
            model = call.get("model", "unknown")
            ms = model_stats[model]
            ms["total_input"] += call.get("input_tokens", 0)
            ms["total_output"] += call.get("output_tokens", 0)
            ms["cache_read"] += call.get("cached_input_tokens", 0)
            ms["cache_creation"] += call.get("cache_creation_input_tokens", 0)
            ms["total_cost"] += call.get("cost_usd", 0) or 0

    return {
        "timestamp": timestamp,
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "cost_by_category": category_costs,
        "model_stats": dict(model_stats),
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic write of JSON data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Fresh-run preparation
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"completed", "deleted"})


def prepare_fresh_run(run_path: Path) -> None:
    """Prune terminal tasks from the DAG, snapshot usage, and clean traces.

    1. Snapshot dag.json → archive/dag_{timestamp}.json
    2. Remove completed/deleted items; clean dangling dependency edges
    3. Delete tool-results/ (ephemeral, not archived)
    4. Collect all traces to be deleted (orchestrator, pruned tasks, eval,
       merge_eval, readers) and save a usage snapshot
    5. Delete the collected traces and reports for pruned task IDs
    """
    dag_path = run_path / "dag.json"
    if not dag_path.exists():
        logger.info("No dag.json found — nothing to prune")
        return

    with open(dag_path) as f:
        dag_data = json.load(f)

    items: list[dict[str, Any]] = dag_data.get("items", [])

    # --- 1. Snapshot full DAG to archive ---
    archive_dir = run_path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = archive_dir / f"dag_{ts}.json"
    shutil.copy2(dag_path, snapshot_path)
    logger.info("Snapshotted dag.json → %s", snapshot_path.name)

    # --- 2. Prune terminal items ---
    pruned_ids: set[str] = {str(item["id"]) for item in items if item.get("status") in _TERMINAL_STATUSES}
    if not pruned_ids:
        logger.info("No terminal tasks to prune")
    else:
        kept = [item for item in items if str(item["id"]) not in pruned_ids]
        # Remove dangling dependency edges pointing to pruned items
        for item in kept:
            item["depends_on"] = [d for d in item.get("depends_on", []) if str(d) not in pruned_ids]
            item["dependents"] = [d for d in item.get("dependents", []) if str(d) not in pruned_ids]
        dag_data["items"] = kept
        # Preserve max_id so new task IDs don't collide
        tmp = dag_path.with_name("dag.json.tmp")
        with open(tmp, "w") as f:
            json.dump(dag_data, f, indent=2)
        os.replace(tmp, dag_path)
        logger.info("Pruned %d terminal tasks from dag.json (%d remaining)", len(pruned_ids), len(kept))

    # --- 3. Delete tool-results (ephemeral, not archived) ---
    tool_results_dir = run_path / "tool-results"
    if tool_results_dir.exists():
        shutil.rmtree(tool_results_dir)
        logger.info("Deleted tool-results/")

    # --- 3b. Delete stale stats.json cache (will be recomputed by visualizer) ---
    stats_cache = run_path / "stats.json"
    if stats_cache.exists():
        stats_cache.unlink()
        logger.info("Deleted stale stats.json cache")

    # --- 4. Snapshot usage from all traces about to be deleted ---
    traces_dir = run_path / "traces"
    files_to_snapshot: list[Path] = []

    orch_trace = traces_dir / "orchestrator.json"
    if orch_trace.exists():
        files_to_snapshot.append(orch_trace)

    for task_id in pruned_ids:
        task_dir = traces_dir / "tasks" / task_id
        if task_dir.exists():
            files_to_snapshot.extend(task_dir.rglob("*.json"))

    dirs_to_delete: list[Path] = []
    for subdir in ("eval", "merge_eval", "merge_batches", "readers"):
        d = traces_dir / subdir
        if d.exists():
            files_to_snapshot.extend(d.rglob("*.json"))
            dirs_to_delete.append(d)

    if files_to_snapshot:
        usage = _compute_usage_snapshot(traces_dir, files_to_snapshot, ts)
        _write_json(archive_dir / f"usage_snapshot_{ts}.json", usage)
        logger.info(
            "Saved usage snapshot (%d traces, $%.2f) → usage_snapshot_%s.json",
            len(files_to_snapshot),
            usage["total_cost_usd"],
            ts,
        )

    # --- 5. Delete traces and reports ---
    if orch_trace.exists():
        shutil.copy2(orch_trace, archive_dir / f"orchestrator_{ts}.json")
        orch_trace.unlink()
        logger.info("Archived and deleted live orchestrator trace")

    for task_id in pruned_ids:
        trace_dir = traces_dir / "tasks" / task_id
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
            logger.debug("Deleted traces for pruned task %s", task_id)
        report_file = run_path / "reports" / "task_reports" / f"{task_id}.json"
        if report_file.exists():
            report_file.unlink()
            logger.debug("Deleted report for pruned task %s", task_id)

    # Move eval traces to archive (not incrementally archived); delete the rest
    # (merge_batches, merge_eval, readers are already in archive via ArchiveTraceStore).
    archive_traces_dir = archive_dir / "traces"
    archive_traces_dir.mkdir(parents=True, exist_ok=True)
    for d in dirs_to_delete:
        if d.name == "eval":
            dest = archive_traces_dir / f"eval_{ts}"
            shutil.move(str(d), str(dest))
            logger.info("Archived traces/%s/ → %s", d.name, dest.name)
        else:
            shutil.rmtree(d)
            logger.info("Deleted traces/%s/", d.name)

    # --- 6. Archive merge_reports and eval_reports ---
    reports_dir = run_path / "reports"
    archive_reports_dir = archive_dir / "reports"

    # Move all merge_reports to archive
    merge_reports = reports_dir / "merge_reports"
    if merge_reports.exists() and any(merge_reports.iterdir()):
        dest = archive_reports_dir / f"merge_reports_{ts}"
        shutil.move(str(merge_reports), str(dest))
        merge_reports.mkdir(parents=True, exist_ok=True)
        logger.info("Archived merge_reports → %s", dest.name)

    # Move eval_reports to archive, keeping the latest symlink target
    eval_reports = reports_dir / "eval_reports"
    if eval_reports.exists():
        latest_link = eval_reports / "latest"
        latest_target = latest_link.resolve().name if latest_link.is_symlink() else None

        dirs_to_move = [
            d for d in eval_reports.iterdir() if d.is_dir() and not d.is_symlink() and d.name != latest_target
        ]
        if dirs_to_move:
            dest = archive_reports_dir / f"eval_reports_{ts}"
            dest.mkdir(parents=True, exist_ok=True)
            for d in dirs_to_move:
                shutil.move(str(d), str(dest / d.name))
            logger.info("Archived %d eval report(s) → %s", len(dirs_to_move), dest.name)
