# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""DAGRunner — scheduling loop over an ItemTracker.

Continuously finds ready items and dispatches them via a TaskExecutor.
The ItemTracker is the single source of truth for item state; DAGRunner
only drives the dispatch loop and delegates all state mutations back to it.

Dispatch and result handling are fully decoupled: each dispatched task
handles its own result via a callback when its future resolves, so the
dispatch loop is never blocked waiting for results.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from core.tracker import ItemTracker
from core.trace import TraceStore

from .concurrent_agents import ConcurrentResult, FailureCause
from .executor import TaskExecutor

logger = logging.getLogger(__name__)


@runtime_checkable
class BusyCheckable(Protocol):
    """Object that can report whether it is currently busy."""

    def is_busy(self) -> bool: ...


class DAGRunner:
    """Dispatches ready items in parallel, collects results.

    The runner continuously:
    1. Asks the tracker which items are ready (dependencies met)
    2. Checks executor capacity
    3. Dispatches items via the executor
    4. Results are handled asynchronously as tasks complete

    Args:
        dag: ItemTracker — single source of truth for item state.
        executor: TaskExecutor that handles how tasks are actually run.
        trace_store: Optional trace store for status snapshots.
        on_task_start: Optional callback(task_id) when a task starts.
        on_task_complete: Optional callback(task_id, result) after each task.
        orchestrator: Optional object implementing BusyCheckable — runner
            pauses while it reports busy.
    """

    def __init__(
        self,
        dag: ItemTracker,
        executor: TaskExecutor,
        *,
        trace_store: TraceStore | None = None,
        on_task_start: Callable[[str], Any] | None = None,
        on_task_complete: Callable[[str, ConcurrentResult], Any] | None = None,
        orchestrator: BusyCheckable | None = None,
        auto_dispatch_flavors: frozenset[str] | None = None,
    ):
        self.dag = dag
        self.executor = executor
        self.trace_store = trace_store
        self.on_task_start = on_task_start
        self.on_task_complete = on_task_complete
        self._orchestrator = orchestrator
        self._auto_dispatch_flavors = auto_dispatch_flavors
        self._running: set[str] = set()
        self._paused = asyncio.Event()
        self._paused.set()
        self._result_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def pause(self) -> None:
        self._paused.clear()
        logger.info("DAGRunner paused")

    def resume(self) -> None:
        self._paused.set()
        logger.info("DAGRunner resumed")

    def _orchestrator_busy(self) -> bool:
        if self._orchestrator is None:
            return False
        return self._orchestrator.is_busy()

    def _write_status(self) -> None:
        if not self.trace_store:
            return
        status: dict[str, Any] = {
            "dag": self.dag.summary(),
            "executor_slots": self.executor.available_slots(),
        }
        status_path = self.trace_store.run_dir / "status.json"
        with open(status_path, "w") as f:
            json.dump(status, f, indent=2)

    def dispatch_ready(self) -> int:
        """Dispatch all ready tasks that aren't already running.

        Only auto-dispatches tasks whose flavor is in ``auto_dispatch_flavors``
        (when set). Tasks of other flavors must be explicitly dispatched via
        ``dispatch_task``.

        Returns the number of tasks dispatched.
        """
        ready = [t for t in self.dag.ready() if t["id"] not in self._running]
        if self._auto_dispatch_flavors is not None:
            ready = [t for t in ready if t.get("flavor", "task") in self._auto_dispatch_flavors]
        dispatched = 0
        for task in ready:
            if self.executor.available_slots() <= 0:
                break
            self._dispatch_one(task)
            dispatched += 1
        return dispatched

    def dispatch_task(self, task_id: str) -> str:
        """Dispatch a single task by ID if it is ready and there is capacity.

        Returns a status message describing the outcome.
        """
        if task_id in self._running:
            return f"Task {task_id} is already running."
        ready = self.dag.ready()
        task = next((t for t in ready if t["id"] == task_id), None)
        if task is None:
            return f"Task {task_id} is not ready (either not pending or has unmet dependencies)."
        if self.executor.available_slots() <= 0:
            return "No executor slots available — all workers are busy."
        self._dispatch_one(task)
        return f"Task {task_id} dispatched."

    def _dispatch_one(self, task: dict[str, Any]) -> None:
        """Dispatch a single task — shared by dispatch_ready and dispatch_task.

        Safe to call from any thread: uses the captured event loop to
        schedule the async handler when called outside the loop thread.
        """
        attempt_number = self.dag.get_attempts(task["id"]) + 1
        self._running.add(task["id"])
        coro = self._execute_and_handle(task, attempt_number)
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.create_task, coro)
        else:
            asyncio.create_task(coro)
        logger.info("Dispatched task %s (%s) attempt %d", task["id"], task["title"], attempt_number)
        if self.on_task_start:
            self.on_task_start(task["id"])

    async def _execute_and_handle(self, task: dict[str, Any], attempt_number: int) -> None:
        """Execute a task and handle its result. Runs as an independent async task."""
        try:
            result = await self.executor.run(
                task,
                attempt_number,
                on_dispatched=lambda: self.dag.mark_in_progress(task["id"]),
            )
        except Exception as e:
            logger.error("Task %s raised: %s", task["id"], e)
            result = ConcurrentResult(success=False, error=str(e), failure_cause=FailureCause.INFRASTRUCTURE)

        self._running.discard(task["id"])
        outcome = self.dag.record_result(task["id"], result.success)
        logger.info("Task %s: %s (winner=%s)", task["id"], outcome, result.winner_id or "none")
        if self.on_task_complete:
            try:
                self.on_task_complete(task["id"], result)
            except Exception:
                logger.exception("on_task_complete callback failed for task %s", task["id"])
        self._result_event.set()
        self._write_status()

    async def run(self, continuous: bool = False) -> dict[str, Any]:
        """Run until all tasks complete or are blocked.

        Args:
            continuous: If True, keep polling for new tasks added by the orchestrator.
        """
        logger.info("DAGRunner starting (continuous=%s)", continuous)
        self._loop = asyncio.get_running_loop()
        self._write_status()

        while True:
            await self._paused.wait()

            if self._orchestrator_busy():
                await asyncio.sleep(1)
                continue

            if not self.dag.has_pending() and not self._running:
                if not continuous:
                    break
                await asyncio.sleep(2)
                continue

            dispatched = self.dispatch_ready()
            self._write_status()

            if not dispatched and not self._running:
                if not self.dag.ready() and self.dag.has_pending():
                    logger.warning("DAGRunner stalled — pending tasks have unmet dependencies")
                    self._write_status()
                    if not continuous:
                        break
                await asyncio.sleep(2)
                continue

            # Wait for any result or a short timeout, then loop back to dispatch more
            self._result_event.clear()
            try:
                await asyncio.wait_for(self._result_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

        summary = self.dag.summary()
        completed = summary["counts"].get("completed", 0)
        total = summary["total"]
        rate = summary["completion_rate"]
        logger.info(
            "DAGRunner complete: %d/%d succeeded (%.0f%%)",
            completed,
            total,
            rate * 100,
        )
        self._write_status()
        return summary
