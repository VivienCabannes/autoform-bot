# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""EvalProcess — on-demand formalization assessment.

Standalone process that runs the autoform_eval pipeline against a
pipeline run's codebase. Can be launched independently or triggered
from the visualizer / control plane.

Usage:
    python -m autoform.bot.eval_process --run-path /path/to/run
    python -m autoform.bot.eval_process --name my-run [--config config.yaml]

After each eval, notifies the orchestrator (if running) via the
interaction registry HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from autoform.eval.__main__ import _run as run_eval
from autoform.eval.generate_report import generate as generate_markdown

from .config import PipelineConfig
from .urls import get_urls

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent


def _get_head_commit(repo_path: Path) -> str:
    """Return the current HEAD commit hash."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


class EvalProcess:
    """Runs the autoform_eval pipeline on demand against the live codebase.

    Args:
        config: Pipeline configuration (provides all paths).
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.code_path = config.run_path / "code"
        self.code_dir = self.code_path / config.lib_name
        self.book_path = config.run_path / "book"
        self.eval_reports_path = config.run_path / "reports" / "eval_reports"
        self._checkpoint_count = 0

    def _notify_orchestrator(self, md_path: Path) -> None:
        """Send a message to the orchestrator via the registry HTTP API."""
        registry_urls = get_urls(self.config.run_path, "registry")
        registry_url = registry_urls.get(0)
        if not registry_url:
            logger.info("No registry URL found — pipeline not running, skipping notification")
            return

        message = (
            f"An eval checkpoint report is available at: {md_path}\n\n"
            f"This report evaluates the current codebase against the book's targets. "
            f"Read it with your filesystem tools. It has three sections:\n"
            f"- **Issues**: Targets that have a matching declaration but failed evaluation "
            f"(low faithfulness or unjustified axioms). Fix these first.\n"
            f"- **Not Covered**: Targets with no matching declaration yet. "
            f"Create tasks for these when ready.\n"
            f"- **Passed**: Targets that are in good shape. No action needed."
        )
        try:
            resp = requests.post(
                f"{registry_url}/agent/orchestrator/message",
                json={"message": message},
                timeout=5,
            )
            if resp.ok:
                logger.info("Notified orchestrator about eval report")
            else:
                logger.warning("Failed to notify orchestrator: %s", resp.text)
        except requests.ConnectionError:
            logger.info("Could not reach registry — pipeline likely not running")
        except Exception:
            logger.exception("Failed to notify orchestrator")

    async def run_eval(self, concurrency: int = 100_000) -> Path:
        """Run the full eval pipeline and write report.md.

        Returns:
            Path to the generated report.md.
        """
        if self.config.targets_file is None:
            raise ValueError("No targets_file configured — cannot run eval")

        self._checkpoint_count += 1
        commit = _get_head_commit(self.code_path)
        logger.info(
            "Eval checkpoint %d starting (commit: %s)",
            self._checkpoint_count,
            commit,
        )

        # Create report dir early so traces go inside it
        report_dir = self.eval_reports_path / commit
        report_dir.mkdir(parents=True, exist_ok=True)

        # Mark eval as in-progress for the visualizer
        marker = report_dir / ".evaluating"
        marker.touch()

        # Snapshot the code before running eval
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self.config.run_path / "archive" / "code_backup" / ts
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.code_dir, backup_dir)
        logger.info("Code snapshot saved to %s", backup_dir)

        try:
            json_path = report_dir / "report.json"
            report, trace_store = await run_eval(
                repo_dir=str(self.code_path),
                code_dir=str(self.code_dir),
                task_file=str(self.config.targets_file),
                book_dir=str(self.book_path),
                model=self.config.model,
                concurrency=concurrency,
                trace_dir=str(self.config.run_path / "traces" / "eval" / commit),
                report_path=str(json_path),
            )

            report["checkpoint"] = {
                "number": self._checkpoint_count,
                "commit": commit,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

            json_path = report_dir / "report.json"
            json_path.write_text(json.dumps(report, indent=2))

            # Generate report.md from the JSON
            generate_markdown(str(json_path), targets_path=str(self.config.targets_file))

            md_path = report_dir / "report.md"

            # Symlink latest
            latest = self.eval_reports_path / "latest"
            latest.unlink(missing_ok=True)
            latest.symlink_to(report_dir.name)

            summary = report.get("statements", {}).get("summary", {})
            logger.info(
                "Eval checkpoint %d complete: %d/%d passed (%.1f%%) — %s",
                self._checkpoint_count,
                summary.get("passed", 0),
                summary.get("total", 0),
                summary.get("pass_rate", 0) * 100,
                md_path,
            )

            # Notify the orchestrator
            self._notify_orchestrator(md_path)

            # Copy reports into the code backup directory
            shutil.copy2(json_path, backup_dir / "report.json")
            if md_path.exists():
                shutil.copy2(md_path, backup_dir / "report.md")

            # # Update goal tracker if it exists
            # self._update_goals(report)

            return md_path
        finally:
            marker.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Transfer report → goals
# ---------------------------------------------------------------------------


def transfer_report_to_goals(run_path: Path) -> dict:
    """Update goals.json from the latest eval report.

    Reads the latest report.json, maps each detail entry to the
    corresponding goal by ``idx``, and updates status + metadata.
    Appends status-change events to ``goal_events.jsonl``.

    Returns a summary dict: ``{updated, completed, failed, pending}``.
    """
    from core.tracker import ItemStatus, ItemTracker

    goals_path = run_path / "goals.json"
    if not goals_path.exists():
        raise FileNotFoundError(f"goals.json not found at {goals_path}")

    tracker = ItemTracker(goals_path)

    # Resolve latest report
    latest_link = run_path / "reports" / "eval_reports" / "latest"
    if latest_link.is_symlink():
        report_dir = latest_link.resolve()
    elif latest_link.is_dir():
        report_dir = latest_link
    else:
        raise FileNotFoundError("No latest eval report found")

    report_json = report_dir / "report.json"
    if not report_json.exists():
        raise FileNotFoundError(f"report.json not found at {report_json}")

    with open(report_json) as f:
        report = json.load(f)

    details = report.get("statements", {}).get("details", [])
    if not details:
        return {"updated": 0, "completed": 0, "failed": 0, "pending": 0}

    events: list[str] = []
    counts = {"updated": 0, "completed": 0, "failed": 0, "pending": 0}

    for d in details:
        idx = d.get("idx")
        if idx is None:
            continue
        goal_id = str(idx)

        # Skip if goal doesn't exist in tracker
        if tracker.get(goal_id) is None:
            continue

        # --- Determine status ---
        is_not_covered = (
            d.get("match_confidence") == "not_found"
            or not d.get("lean_declaration")
            or d.get("lean_declaration") == "-"
        )
        if d.get("passed"):
            status = ItemStatus.COMPLETED
        elif is_not_covered:
            status = ItemStatus.PENDING
        else:
            status = ItemStatus.FAILED

        axiom_verdicts = d.get("axiom_verdicts", {})

        # --- Aggregate score (weighted average matching jury grader) ---
        # Rubric weights: code_quality=0.2, faithfulness=0.4, proof_integrity=0.4
        # Each rubric score is normalized to 0-1 (divided by max_score=5)
        scores = d.get("scores", {})
        _RUBRIC_WEIGHTS = {"code_quality": 0.2, "faithfulness": 0.4, "proof_integrity": 0.4}
        weighted_sum = 0.0
        total_weight = 0.0
        for name, weight in _RUBRIC_WEIGHTS.items():
            raw = scores.get(name)
            if isinstance(raw, (int, float)):
                weighted_sum += (raw / 5.0) * weight
                total_weight += weight
        score_val = round(weighted_sum / total_weight, 4) if total_weight > 0 else 0.0

        # --- Build metadata ---
        # Reconstruct the same metrics shape as ground-truth grading:
        # compilation, faithfulness, proof_integrity, code_quality from scores,
        # axiom_verdicts from LLM judge.
        _METRIC_KEYS = {"compilation", "code_quality", "faithfulness", "proof_integrity"}
        metrics: dict[str, Any] = {k: v for k, v in scores.items() if k in _METRIC_KEYS}
        metrics["axiom_verdicts"] = axiom_verdicts
        meta: dict[str, Any] = {
            "score": score_val,
            "feedback": d.get("feedback", ""),
            "metrics": metrics,
        }
        lean_declaration = d.get("lean_declaration")
        if lean_declaration and lean_declaration != "-":
            meta["lean_declaration"] = lean_declaration
        lean_file = d.get("lean_file")
        if lean_file and lean_file != "-":
            meta["lean_file"] = lean_file

        # Clear legacy keys that older transfers may have written.
        existing_meta = tracker.get(goal_id)["metadata"]
        for stale_key in ("match_confidence", "failure_reason"):
            existing_meta.pop(stale_key, None)

        tracker.update(goal_id, status=status, metadata=meta)
        counts["updated"] += 1
        counts[status.value] += 1

        events.append(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "goal_id": idx,
                    "status": status.value,
                    "score": score_val,
                }
            )
        )

    # Append events to goal_events.jsonl
    if events:
        event_log = goals_path.with_name("goal_events.jsonl")
        with open(event_log, "a") as f:
            f.write("\n".join(events) + "\n")

    logger.info(
        "Transferred report to goals: %d updated (%d completed, %d failed, %d pending)",
        counts["updated"],
        counts["completed"],
        counts["failed"],
        counts["pending"],
    )
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_config(config_path: str | None = None, run_path: Path | None = None) -> dict:
    if config_path is not None:
        config_path = Path(config_path)
    elif run_path is not None and (run_path / "config.yaml").exists():
        config_path = run_path / "config.yaml"
    else:
        config_path = APP_DIR / "config.yaml"
    if config_path.exists():
        import yaml

        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def run(
    run_path: str | None = None,
    name: str | None = None,
    config: str | None = None,
    concurrency: int = 1000,
) -> None:
    """Run a one-shot eval against an existing pipeline run.

    Args:
        run_path: Direct path to the run directory.
        name: Run name (resolved via workspace.path in config).
        config: Path to config.yaml. Defaults to the app's config.yaml.
        concurrency: Max concurrent per-statement assessments.
    """
    cfg = _load_config(config)

    if run_path:
        resolved = Path(run_path).expanduser().resolve()
    elif name:
        workspace_root = Path(cfg.get("workspace", {}).get("path", ".")).expanduser().resolve()
        resolved = workspace_root / name
    else:
        print("Error: specify --run-path or --name", file=sys.stderr)
        sys.exit(1)

    # Re-load from run dir if a snapshotted config exists there.
    if config is None:
        cfg = _load_config(run_path=resolved)

    if not (resolved / "code").exists():
        print(f"Error: {resolved / 'code'} does not exist", file=sys.stderr)
        sys.exit(1)

    pipeline_config = PipelineConfig.from_yaml(cfg, run_path=resolved, app_dir=APP_DIR)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    eval_proc = EvalProcess(pipeline_config)
    md_path = asyncio.run(eval_proc.run_eval(concurrency=concurrency))
    print(f"\nReport: {md_path}")


if __name__ == "__main__":
    import fire

    fire.Fire(run)
