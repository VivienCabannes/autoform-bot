# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""TaskExecutor — abstract base and implementations for task dispatch.

Decouples DAGRunner (scheduling) from how tasks are actually executed.
DAGRunner only calls available_slots(), run(), start(), and shutdown();
the executor owns all resource management and lifecycle.

Implementations:
    LocalExecutor  — wraps AgentPool + ConcurrentAgents for single-node execution.
    ZmqExecutor    — distributed execution over ZMQ (see core.coordination.multinode).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from core.task import Task
from core.trace import TraceStore

from .concurrent_agents import ConcurrentAgents, ConcurrentResult
from .pool import AgentPool


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class TaskExecutor(ABC):
    """Abstract base for task execution strategies.

    DAGRunner calls start() once before dispatching, available_slots() to
    check capacity, run() per task, and shutdown() on exit. Subclasses own
    all resource management (agent checkout, remote dispatch, etc.).
    """

    async def start(self) -> None:
        """Initialize the executor before any tasks are dispatched."""

    @abstractmethod
    def available_slots(self) -> int:
        """Number of additional tasks that can be dispatched right now."""
        ...

    @abstractmethod
    async def run(
        self, task: dict[str, Any], attempt_number: int, *, on_dispatched: Callable[[], None] | None = None
    ) -> ConcurrentResult:
        """Execute a task and return the result.

        Args:
            task: The task to execute.
            attempt_number: Which attempt this is (1-based).
            on_dispatched: Optional callback invoked once the task has actually
                been handed to a worker (agent checked out, message sent, etc.).
        """
        ...

    async def shutdown(self) -> None:
        """Clean up resources after all tasks are done."""


# ---------------------------------------------------------------------------
# LocalExecutor
# ---------------------------------------------------------------------------


class LocalExecutor(TaskExecutor):
    """Runs tasks against a local AgentPool using ConcurrentAgents.

    Args:
        pool: Pool of local agents to check out for task execution.
        concurrent: ConcurrentAgents instance providing build/review/merge logic.
        agents_per_task: How many agents to assign to each task (for racing).
        trace_store: Optional trace store passed through to run_task.
    """

    def __init__(
        self,
        pool: AgentPool,
        concurrent: ConcurrentAgents,
        agents_per_task: int = 1,
        trace_store: TraceStore | None = None,
    ) -> None:
        self._pool = pool
        self._concurrent = concurrent
        self._agents_per_task = agents_per_task
        self._trace_store = trace_store

    def available_slots(self) -> int:
        """Number of tasks dispatchable given current pool availability."""
        available = len(self._pool.available())
        return available // self._agents_per_task

    async def run(
        self, task: dict[str, Any], attempt_number: int, *, on_dispatched: Callable[[], None] | None = None
    ) -> ConcurrentResult:
        """Check out agents, run the task, check agents back in."""
        agents = self._pool.checkout(self._agents_per_task)
        if not agents:
            return ConcurrentResult(success=False, error="No agents available")
        if on_dispatched:
            on_dispatched()
        task_obj = Task(
            id=task["id"],
            title=task.get("title", task["id"]),
            description=task.get("description", ""),
            metadata=task.get("metadata", {}),
        )
        try:
            return await self._concurrent.run_task(
                task_obj,
                agents,
                get_reviewer=self._pool.get_reviewer,
                attempt_number=attempt_number,
                trace_store=self._trace_store,
            )
        finally:
            self._pool.checkin(agents)
