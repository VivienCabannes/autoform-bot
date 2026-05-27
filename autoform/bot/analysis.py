# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Trace analysis and skill folder management.

TraceAnalyzerManager owns persistent per-task analyzer agents that accumulate
conversation history across attempts. sync() reconciles analyzer agents and
skills/tasks/ directory with current DAG state.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.tracker import ItemStatus, ItemTracker

from core.agent import Agent, AgentDefinition
from core.inference import InferenceProtocol
from core.interaction import get_registry
from core.trace import TraceStore, AgentTrace
from tools.files.filesystem import filesystem_server
from tools.files.filesystem.server import FilesystemConfig
from tools.observability.trace_inspector import trace_inspector_server

from .archive import archive_skills
from .tools.escalate import escalation_reader_server
from .tools.task_tracker.core import ConstrainedTracker
from .tools.task_tracker.server import constrained_tracker_server

logger = logging.getLogger(__name__)


class TraceAnalyzerManager:
    """Manages persistent per-task trace analyzer agents.

    Trace lifecycle is delegated to Agent (set_trace / incremental save
    via trace_store). This class manages multi-instance creation and the
    analyzer prompt template.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        inference_factory: Callable[[], InferenceProtocol],
        trace_store: TraceStore | None,
        *,
        code_path: Path,
        book_path: Path,
        skills_path: Path,
        reports_path: Path,
        report_queue: asyncio.Queue | None = None,
        task_tracker: ItemTracker | None = None,
    ):
        self._definition = definition
        self._inference_factory = inference_factory
        self._trace_store = trace_store
        self._code_path = code_path
        self._book_path = book_path
        self._skills_path = skills_path
        self._reports_path = reports_path
        self._report_queue = report_queue
        self._task_tracker = task_tracker

        self._analyzers: dict[str, Agent] = {}
        self._pending: list[asyncio.Task] = []

    async def get_or_create(self, task_id: str) -> Agent:
        """Return the persistent analyzer for task_id, creating if needed.

        Each task gets its own Agent that retains conversation history across
        all attempts so it can reason about how the task evolves over time.
        """
        if task_id not in self._analyzers:
            analyzer_id = f"trace_analyzer-{task_id}"
            server_configs = [
                trace_inspector_server(self._trace_store.run_dir, task_id),
                escalation_reader_server(
                    run_path=self._trace_store.run_dir.parent,
                    traces_dir=self._trace_store.run_dir,
                    task_id=task_id,
                ),
                filesystem_server(
                    FilesystemConfig(
                        allowed_dirs=(
                            str(self._book_path),
                            str(self._code_path),
                            str(self._skills_path / "tasks"),
                            str(self._reports_path),
                            str(self._trace_store.run_dir / "tool-results"),
                        ),
                        write_excluded_dirs=(
                            str(self._book_path),
                            str(self._code_path),
                        ),
                    )
                ),
            ]
            if self._task_tracker is not None:
                decomp_tracker = ConstrainedTracker(
                    self._task_tracker,
                    mutable_flavors=None,
                    default_flavor="decomposition",
                )
                server_configs.append(constrained_tracker_server(decomp_tracker))
            agent = Agent(
                self._definition,
                self._inference_factory(),
                server_configs=server_configs,
                trace_store=self._trace_store,
                message_queue=asyncio.Queue(),
                persist_dir=self._trace_store.run_dir,
            )
            get_registry().register(analyzer_id, agent)
            await agent.__aenter__()
            trace = AgentTrace(id="trace_analyzer")
            trace.trace_id = f"tasks/{task_id}/analyzer"
            agent.set_trace(trace)
            self._analyzers[task_id] = agent
        return self._analyzers[task_id]

    async def run(self, task_id: str) -> None:
        """Call the persistent trace analyzer for one failed attempt."""
        if self._trace_store is None:
            return

        self._reports_path.mkdir(exist_ok=True)
        (self._skills_path / "tasks" / task_id).mkdir(parents=True, exist_ok=True)

        logger.info("Running trace analyzer for task %s (failed)...", task_id)

        agent = await self.get_or_create(task_id)
        await agent.call(
            f"The latest attempt for task '{task_id}' just finished. Status: failed. "
            f"Inspect the most recent trace, update reports/{task_id}.json with your findings. "
            f"Write task-specific skills (proof patterns, correct API names, what to try next) "
            f"to skills/tasks/{task_id}/guide.md."
        )

    def _write_report(self, task_id: str, status: str, reason: str) -> None:
        """Write a minimal report without running the analyzer."""
        import json
        import os

        self._reports_path.mkdir(exist_ok=True)
        report = {"task_id": task_id, "status": status, "reason": reason}
        path = self._reports_path / f"{task_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(report, indent=2))
        os.replace(tmp, path)

    def on_task_complete(self, task_id: str, result: Any) -> None:
        """Callback for DAGRunner — schedules analysis for a completed task."""
        from core.coordination.concurrent_agents import ConcurrentResult, FailureCause

        if isinstance(result, ConcurrentResult) and result.failure_cause == FailureCause.INFRASTRUCTURE:
            logger.info("Skipping trace analysis for task %s (infrastructure failure: %s)", task_id, result.error)
            self._write_report(task_id, "error", f"Infrastructure failure: {result.error}")
            if self._report_queue is not None:
                self._report_queue.put_nowait(task_id)
            return

        failed = not (isinstance(result, ConcurrentResult) and result.success)

        if not failed:
            self._write_report(task_id, "completed", "Task succeeded")
            if self._report_queue is not None:
                self._report_queue.put_nowait(task_id)
            return

        async def _analyze() -> None:
            try:
                await self.run(task_id)
            except Exception:
                logger.exception("Trace analysis failed for task %s", task_id)
                self._write_report(task_id, "error", "Trace analyzer crashed")
            if self._report_queue is not None:
                await self._report_queue.put(task_id)

        task = asyncio.create_task(_analyze())
        self._pending.append(task)

    async def sync(self, tracker: ItemTracker) -> None:
        """Reconcile analyzer agents and skills/tasks/ folders with current DAG state.

        For completed or deleted tasks: close the analyzer agent and delete the skill folder.
        For pending / in_progress / failed tasks: ensure the skill folder exists.
        """
        for task in tracker.list():
            task_id = task["id"]
            folder = self._skills_path / "tasks" / task_id
            if task["status"] in (ItemStatus.COMPLETED, ItemStatus.DELETED):
                await self.close_task(task_id)
                if folder.exists():
                    shutil.rmtree(folder)
            elif task["status"] in (ItemStatus.PENDING, ItemStatus.IN_PROGRESS, ItemStatus.FAILED):
                folder.mkdir(parents=True, exist_ok=True)

    async def close_task(self, task_id: str) -> None:
        """Finalize and close the analyzer for a task that has been removed or completed."""
        agent = self._analyzers.pop(task_id, None)
        if agent is None:
            return
        if self._trace_store and agent._trace is not None:
            agent._trace.finalize(
                status="completed",
                total_turns=agent.total_turns,
                messages=agent.messages,
            )
            self._trace_store.save(agent._trace)
        try:
            await agent.close()
        except Exception:
            pass
        logger.info("Closed trace analyzer for task %s", task_id)

    async def drain_pending(self) -> None:
        """Wait for all in-flight analyzer tasks to complete."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()

    async def finalize_and_close(self) -> None:
        """Finalize all analyzer traces and close agents."""
        await self.drain_pending()
        for agent in self._analyzers.values():
            if self._trace_store and agent._trace is not None:
                agent._trace.finalize(
                    status="completed",
                    total_turns=agent.total_turns,
                    messages=agent.messages,
                )
                self._trace_store.save(agent._trace)
            try:
                await agent.close()
            except Exception:
                pass


def sync_skill_folders(skills_path: Path, dag_store: Any, archive_skills_dir: Path | None = None) -> None:
    """Reconcile skills/tasks/ subfolders against current DAG state.

    pending / in_progress / failed → ensure skills/tasks/{task_id}/ exists
    completed / removed            → delete skills/tasks/{task_id}/ if present
    """
    for task in dag_store.list():
        folder = skills_path / "tasks" / task["id"]
        if task["status"] in (ItemStatus.PENDING, ItemStatus.IN_PROGRESS, ItemStatus.FAILED):
            folder.mkdir(parents=True, exist_ok=True)
        elif task["status"] in (ItemStatus.COMPLETED, ItemStatus.DELETED):
            if folder.exists():
                if archive_skills_dir:
                    archive_skills(skills_path, archive_skills_dir, task["id"])
                shutil.rmtree(folder)
