# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LeanCoordinatorNode — rank 0 in the autoform pipeline.

Responsible for:
- Planning the task DAG via the orchestrator
- Dispatching ready tasks to worker nodes via DistributedExecutor
- Analyzing traces and writing skills after each completed task

Three concurrent processes run once workers are registered:
- Orchestrator loop: wakes on new reports, syncs state, re-plans
- DAGRunner: continuously dispatches ready tasks, pauses while orchestrator is busy
- Trace analyzers: run after each task, write reports, signal the orchestrator
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from autoform.bot.utils.gc_worktrees import gc_worktrees
from typing import Any

from core.agent import load_agent_definition
from core.coordination.dag_runner import DAGRunner
from core.coordination.executor import TaskExecutor
from core.coordination.multinode import CoordinatorNode
from core.inference import InferenceProtocol
from core.trace import TraceStore
from core.tracker import ItemTracker, ItemStatus
from autoform.eval.types import load_task_list
from autoform.eval.utils.tracker import populate_tracker

from .analysis import TraceAnalyzerManager, sync_skill_folders
from .archive import archive_reports
from .config import PipelineConfig
from .orchestration import OrchestratorManager
from core.interaction.registry import get_registry

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent


class LeanCoordinatorNode(CoordinatorNode):
    """Coordinates task planning and dispatch across worker nodes.

    Args:
        config: Typed pipeline configuration.
        executor: DistributedExecutor connected to the worker nodes.
        inference_factory: Factory for LLM inference instances.
        trace_store: Optional trace store for agent traces.
    """

    def __init__(
        self,
        config: PipelineConfig,
        executor: TaskExecutor,
        inference_factory: Callable[[], InferenceProtocol],
        trace_store: TraceStore | None = None,
        test_tasks: int = 0,
        run_id: str | None = None,
    ):
        self.config = config
        self.executor = executor
        self.inference_factory = inference_factory
        self.trace_store = trace_store
        self._test_tasks = test_tasks

        self.code_path = config.run_path / "code"
        self.skills_path = config.run_path / "skills"
        self.book_path = config.run_path / "book"
        self.reports_path = config.run_path / "reports" / "task_reports"
        self.merge_reports_path = config.run_path / "reports" / "merge_reports"
        self.archive_reports_dir = config.run_path / "archive" / "reports" / "task_reports"
        self.archive_skills_dir = config.run_path / "archive" / "skills"
        self._round_count = 0

        if run_id is None:
            run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self._worktrees_dir = config.run_path / "worktrees" / run_id
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        self.trace_analyzer_def = load_agent_definition(APP_DIR / "agents" / "trace_analyzer")
        self.orchestrator_def = load_agent_definition(APP_DIR / "agents" / "orchestrator")

    async def initialize(self) -> None:
        self.skills_path.mkdir(exist_ok=True)
        (self.skills_path / "workflow").mkdir(exist_ok=True)
        (self.skills_path / "tasks").mkdir(exist_ok=True)
        self.reports_path.mkdir(exist_ok=True)

        self.report_queue: asyncio.Queue[str] = asyncio.Queue()

        self.orch_mgr = OrchestratorManager(
            self.orchestrator_def,
            self.inference_factory,
            code_path=self.code_path,
            book_path=self.book_path,
            skills_path=self.skills_path,
            reports_path=self.reports_path,
            trace_store=self.trace_store,
        )
        self.tracker = ItemTracker(self.config.run_path / "dag.json")
        self.tracker.bulk_transition(ItemStatus.IN_PROGRESS, ItemStatus.PENDING)

        self.analyzer_mgr = TraceAnalyzerManager(
            self.trace_analyzer_def,
            self.inference_factory,
            self.trace_store,
            code_path=self.code_path,
            book_path=self.book_path,
            skills_path=self.skills_path,
            reports_path=self.reports_path,
            report_queue=self.report_queue,
            task_tracker=self.tracker,
        )

        # Goal tracker — separate ItemTracker for formalization targets
        self.goal_tracker: ItemTracker | None = None
        self._merge_eval_tasks: list[asyncio.Task] = []
        if self.config.targets_file is not None:
            self.goal_tracker = ItemTracker(self.config.run_path / "goals.json")
            targets = load_task_list(self.config.targets_file)
            populate_tracker(self.goal_tracker, targets)
            self._targets = targets
            logger.info("Goal tracker loaded: %d targets", len(targets))
        else:
            self._targets = []

        if self._test_tasks > 0:
            self._populate_test_tasks()
        self.orchestrator = self.orch_mgr.create(self.tracker, self.goal_tracker)
        self.orch_mgr.resume_trace(self.orchestrator)

    async def shutdown(self) -> None:
        logger.info("Coordinator shutdown starting...")
        try:
            await self.executor.shutdown()
        except Exception:
            logger.exception("Error shutting down executor")
        try:
            # Wait for in-flight merge evals to finish (with timeout)
            if self._merge_eval_tasks:
                logger.info("Waiting for %d merge eval tasks...", len(self._merge_eval_tasks))
                await asyncio.wait(self._merge_eval_tasks, timeout=30)
        except Exception:
            logger.exception("Error waiting for merge eval tasks")
        try:
            self.tracker.bulk_transition(ItemStatus.IN_PROGRESS, ItemStatus.PENDING)
        except Exception:
            logger.exception("Error marking in-progress tasks as pending")
        try:
            await self.analyzer_mgr.finalize_and_close()
        except Exception:
            logger.exception("Error finalizing trace analyzers")
        try:
            OrchestratorManager.finalize_trace(self.orchestrator, self.trace_store)
        except Exception:
            logger.exception("Error finalizing orchestrator trace")
        try:
            registry = get_registry()
            for agent_id in list(registry.active_agents()):
                registry.unregister(agent_id)
        except Exception:
            logger.exception("Error unregistering agents")
        try:
            run_path = self.config.run_path
            for pattern in ("control.url", "registry_rank*.url", "urls.json"):
                for f in run_path.glob(pattern):
                    f.unlink(missing_ok=True)
        except Exception:
            logger.exception("Error removing URL files")
        try:
            gc_worktrees(self.config.run_path, max_age_hours=1)
        except Exception:
            logger.exception("Worktree GC failed")
        logger.info("Coordinator shutdown complete")

    async def _orchestrator_loop(self) -> None:
        """Wait for new reports and re-plan after each batch."""
        await self.report_queue.put("__startup__")
        while True:
            task_id = await self.report_queue.get()
            task_ids = [task_id]
            while not self.report_queue.empty():
                task_ids.append(self.report_queue.get_nowait())
            await self.analyzer_mgr.sync(self.tracker)

            # Separate merge eval notifications from regular task reports
            merge_evals = [t for t in task_ids if t.startswith("__merge_eval__:")]
            regular = [t for t in task_ids if not t.startswith("__")]

            parts: list[str] = []
            if task_ids == ["__startup__"]:
                parts.append(
                    "Review the current DAG state and update the plan. "
                    "Call load_reports() then get_dag_status() first.\n\n"
                    "If all tasks are completed, carefully verify coverage: read the book and compare against "
                    "the git log and codebase. Check if any definitions, theorems, or propositions from the "
                    "book are missing from the formalization. If anything was dropped or deferred, create "
                    "new tasks for the missing parts. Only stop if every result in the book is covered."
                )
            else:
                if regular:
                    msg = (
                        f"New reports landed for tasks: {', '.join(regular)}. "
                        "Call load_reports() then get_dag_status(), then update the DAG accordingly."
                    )
                    if self.goal_tracker is not None:
                        gs = self.goal_tracker.summary()
                        counts = gs["counts"]
                        failed_tasks = len(self.tracker.list(status="failed"))
                        msg += (
                            f"\n\n**Goal scorecard: {counts.get('completed', 0)}/{gs['total']} completed, "
                            f"{counts.get('failed', 0)} failed, {counts.get('pending', 0)} pending. "
                            f"Failed tasks in DAG: {failed_tasks}.** "
                            "Act on every failed task NOW."
                        )
                    parts.append(msg)
                for me in merge_evals:
                    # Format: __merge_eval__:{task_id}:{report_path}[:{fix_task_ids}]
                    me_parts = me.split(":", 3)
                    me_task_id = me_parts[1]
                    me_report = me_parts[2]
                    me_fix_tasks = me_parts[3] if len(me_parts) > 3 else ""
                    msg = (
                        f"Merge eval completed for task {me_task_id}. "
                        f"A report is available at: {me_report}\n"
                        "Check list_goals() to see updated goal statuses. "
                        "Read the report with your filesystem tools for details on affected targets."
                    )
                    if me_fix_tasks:
                        msg += (
                            f"\n\nFix tasks have been auto-created: {me_fix_tasks}. "
                            "Review them with get_item() and dispatch when ready. "
                            "Do NOT create your own fix tasks for the same goals — "
                            "the auto-created tasks already cover them. "
                            "Do NOT ignore them — nothing is out of scope. "
                            "If the task is granular enough, the workers can handle it — dispatch it. "
                            "If they fail, you can update or delete them."
                        )
                    # Append goal scorecard to keep the orchestrator focused
                    if self.goal_tracker is not None:
                        gs = self.goal_tracker.summary()
                        counts = gs["counts"]
                        total = gs["total"]
                        completed = counts.get("completed", 0)
                        failed = counts.get("failed", 0)
                        pending = counts.get("pending", 0)
                        msg += (
                            f"\n\n**Goal scorecard: {completed}/{total} completed, "
                            f"{failed} failed, {pending} pending.** "
                            "Act on every failed goal and every failed task NOW — "
                            "update, split, or delete and replace. Nothing is out of scope."
                        )
                    parts.append(msg)

            prompt = "\n\n".join(parts)
            await self.orchestrator.call(prompt)
            # Archive reports before clearing
            self._round_count += 1
            archive_reports(self.reports_path, self.archive_reports_dir, self._round_count)
            # Clear consumed reports so they aren't re-read next cycle.
            for f in self.reports_path.glob("*.json"):
                f.unlink(missing_ok=True)
            # Reconcile skill folders against updated DAG state
            sync_skill_folders(self.skills_path, self.tracker, self.archive_skills_dir)

    def _populate_test_tasks(self) -> None:
        """Pre-populate the DAG with simple flat tasks for testing dispatch."""
        lib_name = self._read_lib_name()
        for i in range(self._test_tasks):
            task_id = f"test-{i:03d}"
            if self.tracker.get(task_id) is None:
                self.tracker.add(
                    title=f"Test task {i}",
                    description=(
                        f"Create a file `{lib_name}/Test{i:03d}.lean` containing exactly:\n\n"
                        f"```lean\ndef test{i:03d} : Nat := {i}\n```\n\n"
                        f"Then commit the file with git."
                    ),
                    item_id=task_id,
                )
        logger.info("Populated %d test tasks in DAG", self._test_tasks)

    def _read_lib_name(self) -> str:
        """Read the ``[[lean_lib]]`` name from the workspace lakefile."""
        from core.compat import tomllib

        lakefile = self.code_path / "lakefile.toml"
        with open(lakefile, "rb") as f:
            cfg = tomllib.load(f)
        libs = cfg.get("lean_lib", [])
        return libs[0]["name"] if libs else "Formalization"

    def _on_task_complete(self, task_id: str, result: Any) -> None:
        """Callback for DAGRunner — runs trace analysis.

        Merge eval is triggered by the MergeQueue's on_batch_merged callback,
        not per-task.
        """
        # Always run trace analysis
        self.analyzer_mgr.on_task_complete(task_id, result)

    async def run_pipeline(self) -> dict[str, Any]:
        """Run orchestrator loop and DAGRunner concurrently until complete."""
        try:
            async with self.orchestrator:
                runner = DAGRunner(
                    dag=self.tracker,
                    executor=self.executor,
                    trace_store=self.trace_store,
                    on_task_complete=self._on_task_complete,
                    orchestrator=self.orchestrator,
                    auto_dispatch_flavors=frozenset({"task", "meval", "decomposition"}),
                )
                self.orch_mgr.constrained_tracker.dispatch_fn = runner.dispatch_task
                self.orch_mgr.constrained_tracker.dispatch_ready_fn = runner.dispatch_ready
                try:
                    await self.executor.start()
                    await asyncio.gather(
                        self._orchestrator_loop(),
                        runner.run(continuous=True),
                    )
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Pipeline gather failed")
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("LeanCoordinatorNode interrupted — shutting down")
        except Exception:
            logger.exception("run_pipeline failed")

        return self.tracker.summary()
