# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""DistributedExecutor — multi-node task dispatch over ZMQ.

Implements the TaskExecutor protocol for distributed execution across
worker nodes. The coordinator (rank 0) holds this executor; worker nodes
hold local agent pools and communicate over ZMQ.

Protocol (plain dicts over JSON):

    # worker → coordinator
    {"type": "register", "rank": 2, "capacity": 5}
    {"type": "ack",      "rank": 2, "task_id": "..."}
    {"type": "result",   "rank": 2, "task_id": "...", "success": True,
                         "winner_id": "...", "error": None, "capacity": 4}

    # coordinator → worker
    {"type": "task",     "task_id": "...", "title": "...", "description": "...",
                         "attempt_number": 1, "n_agents": 3, "metadata": {...}}
    {"type": "shutdown"}
    {"type": "restart"}   # worker should reinitialize and re-register
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import StrEnum

from typing import Any

from core.coordination.executor import TaskExecutor
from core.coordination.concurrent_agents import ConcurrentResult, FailureCause

from .zmq_queue import ZmqTaskServer

logger = logging.getLogger(__name__)


class NodePickStrategy(StrEnum):
    """Strategy for selecting which worker node receives a task."""

    BIGGEST_FIRST = "biggest_first"
    BEST_FIT = "best_fit"


class DistributedExecutor(TaskExecutor):
    """Dispatches tasks to remote worker nodes over ZMQ.

    Each worker node registers its available agent capacity on startup. The
    executor tracks per-node free counts, selects the best node for each task,
    and resolves per-task futures when results arrive from the background listener.

    Dead worker detection: workers must ACK task receipt within ack_timeout
    seconds. If no ACK arrives, the worker is declared dead and all its
    pending tasks are failed immediately.

    Task-level overrides: a Task may carry "min_agents" and "max_agents" in
    its metadata to override the global defaults for that specific task.

    Args:
        server: Bound ZmqTaskServer ready to accept worker connections.
        num_workers: Number of worker nodes expected to register.
        min_agents_per_task: Default minimum agents required per task.
        max_agents_per_task: Default maximum agents to assign per task.
        pick_strategy: How to select nodes — biggest_first gives each task
            maximum agents (more racing), best_fit spreads tasks across nodes.
        registration_timeout: Seconds to wait for all workers to register.
        ack_timeout: Seconds to wait for a task ACK before declaring the worker dead.
    """

    def __init__(
        self,
        server: ZmqTaskServer,
        num_workers: int,
        min_agents_per_task: int = 1,
        max_agents_per_task: int = 1,
        pick_strategy: NodePickStrategy = NodePickStrategy.BIGGEST_FIRST,
        registration_timeout: float = 3600.0,
        ack_timeout: float = 300.0,
    ) -> None:
        self._server = server
        self._num_workers = num_workers
        self._min_agents = min_agents_per_task
        self._max_agents = max_agents_per_task
        self._pick_strategy = pick_strategy
        self._registration_timeout = registration_timeout
        self._ack_timeout = ack_timeout
        self._node_capacity: dict[int, int] = {}
        self._pending: dict[str, asyncio.Future[ConcurrentResult]] = {}
        self._task_rank: dict[str, int] = {}
        self._unacked: dict[str, float] = {}  # task_id -> dispatch timestamp
        self._listener_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Wait for at least one worker to register, then start the background listener.

        Late-registering workers are handled by ``_listen()``.
        Must be called before any tasks are dispatched.
        """
        logger.info("Waiting for workers to register (timeout=%ds)...", int(self._registration_timeout))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._registration_timeout

        while not self._node_capacity:
            if loop.time() > deadline:
                raise TimeoutError(f"No workers registered within {self._registration_timeout}s")
            result = await asyncio.to_thread(self._server.recv, 500)
            if result is None:
                continue
            rank, msg = result
            if msg["type"] == "register":
                self._node_capacity[rank] = msg["capacity"]
                logger.info("Worker rank %d registered (capacity=%d)", rank, msg["capacity"])

        total = sum(self._node_capacity.values())
        registered = len(self._node_capacity)
        logger.info("%d/%d workers ready — total capacity %d agents", registered, self._num_workers, total)
        self._listener_task = asyncio.create_task(self._listen(), name="zmq-listener")

    def available_slots(self) -> int:
        """Tasks dispatchable right now, summed across all nodes."""
        return sum(cap // self._min_agents for cap in self._node_capacity.values())

    def _pick_node(self, min_agents: int, max_agents: int) -> tuple[int, int] | None:
        """Select the best node for a task.

        Returns:
            (rank, n_agents) tuple, or None if no suitable node exists.
        """
        candidates = [(rank, cap) for rank, cap in self._node_capacity.items() if cap >= min_agents]
        if not candidates:
            return None
        match self._pick_strategy:
            case NodePickStrategy.BIGGEST_FIRST:
                rank, cap = max(candidates, key=lambda x: x[1])
            case NodePickStrategy.BEST_FIT:
                rank, cap = min(candidates, key=lambda x: x[1])
        return rank, min(max_agents, cap)

    async def run(
        self, task: dict[str, Any], attempt_number: int, *, on_dispatched: Callable[[], None] | None = None
    ) -> ConcurrentResult:
        """Dispatch a task to the best available node and await its result.

        Waits until a node with sufficient capacity is available before dispatching.
        """
        metadata = task.get("metadata", {})
        min_agents = metadata.get("min_agents", self._min_agents)
        max_agents = metadata.get("max_agents", self._max_agents)

        while True:
            if not self._node_capacity:
                raise RuntimeError("All workers are dead — no nodes available to dispatch tasks")
            node = self._pick_node(min_agents, max_agents)
            if node is not None:
                break
            await asyncio.sleep(0.5)

        rank, n_agents = node
        task_id = task["id"]
        fut: asyncio.Future[ConcurrentResult] = asyncio.get_running_loop().create_future()
        self._pending[task_id] = fut
        self._task_rank[task_id] = rank

        self._server.send(
            rank,
            {
                "type": "task",
                "task_id": task_id,
                "title": task.get("title", task_id),
                "description": task.get("description", ""),
                "attempt_number": attempt_number,
                "n_agents": n_agents,
                "metadata": metadata,
            },
        )
        self._node_capacity[rank] -= n_agents
        self._unacked[task_id] = time.monotonic()
        if on_dispatched:
            on_dispatched()
        logger.info("Dispatched task %s to rank %d (%d agents)", task_id, rank, n_agents)

        return await fut

    async def _listen(self) -> None:
        """Background task — poll ZMQ, check ACK deadlines, resolve futures as results arrive.

        Drains all queued messages before checking deadlines so that a process
        stall (swap pressure, NFS hang, CPU saturation, etc.) doesn't cause
        false declarations from stale timestamps.
        """
        while True:
            try:
                # Block up to 200ms for the first message.
                first = await asyncio.to_thread(self._server.recv, 200)
                now = time.monotonic()

                # Drain any additional queued messages (non-blocking).
                batch: list[tuple[int, dict]] = []
                if first is not None:
                    batch.append(first)
                while True:
                    extra = self._server.recv(0)
                    if extra is None:
                        break
                    batch.append(extra)

                # Process ACKs first — must happen before deadline checks
                # to avoid false positives at the timeout boundary.
                for rank, msg in batch:
                    if msg["type"] == "register":
                        self._node_capacity[rank] = msg["capacity"]
                        logger.info("Worker rank %d registered (capacity=%d)", rank, msg["capacity"])
                    elif msg["type"] == "ack":
                        task_id = msg.get("task_id")
                        if task_id:
                            self._unacked.pop(task_id, None)

                self._check_unacked_tasks(now)

                # Process results.
                for rank, msg in batch:
                    if msg["type"] != "result":
                        continue

                    task_id = msg["task_id"]
                    self._unacked.pop(task_id, None)
                    if "capacity" in msg:
                        self._node_capacity[rank] = msg["capacity"]

                    self._task_rank.pop(task_id, None)
                    fut = self._pending.pop(task_id, None)
                    if fut is not None and not fut.done():
                        cause_raw = msg.get("failure_cause")
                        fut.set_result(
                            ConcurrentResult(
                                success=msg.get("success", False),
                                winner_id=msg.get("winner_id"),
                                error=msg.get("error"),
                                failure_cause=FailureCause(cause_raw) if cause_raw else None,
                                pre_merge_hash=msg.get("pre_merge_hash"),
                                post_merge_hash=msg.get("post_merge_hash"),
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error in _listen — continuing")

    def _check_unacked_tasks(self, now: float) -> None:
        """Declare workers dead if they haven't ACKed a task within the timeout."""
        for task_id in list(self._unacked):
            if task_id not in self._unacked:
                continue  # already removed by _handle_dead_worker for a sibling task
            if now - self._unacked[task_id] > self._ack_timeout:
                rank = self._task_rank.get(task_id)
                if rank is not None:
                    self._handle_dead_worker(rank)

    def _handle_dead_worker(self, rank: int) -> None:
        """Declare a worker dead and fail all its pending tasks.

        Sends a restart signal so the worker can reinitialize and re-register
        rather than exiting permanently.
        """
        logger.error(
            "Worker rank %d declared dead (no ACK within %.0fs) — requesting restart",
            rank,
            self._ack_timeout,
        )
        try:
            self._server.send(rank, {"type": "restart"})
        except Exception:
            pass
        for task_id, task_rank in list(self._task_rank.items()):
            if task_rank == rank:
                self._unacked.pop(task_id, None)
                fut = self._pending.pop(task_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(
                        ConcurrentResult(
                            success=False,
                            error=f"Worker rank {rank} declared dead (no ACK within {self._ack_timeout:.0f}s)",
                            failure_cause=FailureCause.INFRASTRUCTURE,
                        )
                    )
                del self._task_rank[task_id]
        self._node_capacity.pop(rank, None)

    async def shutdown(self) -> None:
        """Send shutdown to all workers, stop the listener, and close the server."""
        for rank in self._node_capacity:
            try:
                self._server.send(rank, {"type": "shutdown"})
            except Exception:
                pass
        if self._listener_task:
            self._listener_task.cancel()
        self._server.close()
