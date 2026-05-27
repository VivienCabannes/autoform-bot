#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""E2E test for ZmqExecutor multi-node task dispatch.

Launch with srun so each rank runs this same script:

    srun --nodes=2 --ntasks-per-node=1 python tests/test_zmq_executor.py

Rank 0 runs an asyncio coordinator using ZmqExecutor (the real class).
Ranks 1+ simulate a worker: register capacity, receive tasks, send results.

Exercises:
  - Worker registration and capacity tracking
  - available_slots() before and after dispatch
  - run() returning ConcurrentResult via the future+listener pattern
  - min/max agents per task respected in node selection
  - Multiple concurrent tasks dispatched to the same node
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time

from core.coordination.multinode import (
    DistributedExecutor,
    ZmqTaskClient,
    ZmqTaskServer,
    get_master_addr,
    get_master_port,
    get_rank,
    get_world_size,
)
from core.coordination.concurrent_agents import ConcurrentResult

AGENTS_PER_WORKER = 4

TASKS = [
    {"id": "task-A", "title": "Solo task A", "metadata": {"min_agents": 1, "max_agents": 2, "_sim_duration": 1.0}},
    {"id": "task-B", "title": "Solo task B", "metadata": {"min_agents": 1, "max_agents": 2, "_sim_duration": 0.8}},
    {"id": "task-C", "title": "Heavy task C", "metadata": {"min_agents": 3, "max_agents": 4, "_sim_duration": 1.5}},
    {"id": "task-D", "title": "Solo task D", "metadata": {"min_agents": 1, "max_agents": 2, "_sim_duration": 0.5}},
]


# ---------------------------------------------------------------------------
# Simulated worker (rank 1+)
# ---------------------------------------------------------------------------


def run_worker(rank: int, host: str, port: int) -> None:
    """Simulate a worker node: register, execute tasks (sleep), return results."""
    send_queue: queue.Queue[dict] = queue.Queue()
    running: dict[str, threading.Thread] = {}
    cancelled: set[str] = set()
    free_agents = AGENTS_PER_WORKER
    lock = threading.Lock()

    def _send_result(task_id: str, n_agents: int) -> None:
        with lock:
            nonlocal free_agents
            free_agents += n_agents
            cap = free_agents
        send_queue.put(
            {
                "type": "result",
                "rank": rank,
                "task_id": task_id,
                "success": True,
                "winner_id": f"agent-rank{rank}",
                "error": None,
                "capacity": cap,
            }
        )

    def _run_task(task_id: str, n_agents: int, duration: float) -> None:
        time.sleep(duration)
        with lock:
            was_cancelled = task_id in cancelled
            running.pop(task_id, None)
        if was_cancelled:
            print(f"  [rank {rank}] {task_id!r} aborted", flush=True)
            return
        print(f"  [rank {rank}] {task_id!r} done ({n_agents} agents)", flush=True)
        _send_result(task_id, n_agents)

    with ZmqTaskClient(host=host, port=port, rank=rank) as client:
        client.send({"type": "register", "rank": rank, "capacity": AGENTS_PER_WORKER})
        print(f"  [rank {rank}] registered (capacity={AGENTS_PER_WORKER})", flush=True)

        while True:
            while not send_queue.empty():
                try:
                    client.send(send_queue.get_nowait())
                except queue.Empty:
                    break

            msg = client.recv(timeout_ms=100)
            if msg is None:
                continue

            if msg["type"] == "shutdown":
                print(f"  [rank {rank}] shutdown", flush=True)
                break

            elif msg["type"] == "task":
                task_id = msg["task_id"]
                n_agents = msg.get("n_agents", 1)
                duration = msg.get("metadata", {}).get("_sim_duration", 1.0)
                with lock:
                    free_agents -= n_agents
                print(f"  [rank {rank}] received {task_id!r} (n_agents={n_agents})", flush=True)
                t = threading.Thread(target=_run_task, args=(task_id, n_agents, duration), daemon=True)
                running[task_id] = t
                t.start()

            elif msg["type"] == "cancel":
                with lock:
                    cancelled.add(msg["task_id"])


# ---------------------------------------------------------------------------
# Coordinator (rank 0) — uses ZmqExecutor directly
# ---------------------------------------------------------------------------


async def run_coordinator(port: int, num_workers: int) -> None:
    with ZmqTaskServer(port=port) as server:
        executor = DistributedExecutor(
            server=server,
            num_workers=num_workers,
            min_agents_per_task=1,
            max_agents_per_task=AGENTS_PER_WORKER,
            registration_timeout=60.0,
        )

        await executor.start()
        print(f"\n[rank 0] executor ready — slots={executor.available_slots()}\n", flush=True)

        # Simulate DAGRunner: dispatch tasks as capacity becomes available,
        # run multiple concurrently, collect results.
        pending = list(TASKS)
        running: dict[str, asyncio.Task] = {}
        results: dict[str, ConcurrentResult] = {}

        while pending or running:
            # Dispatch as many tasks as capacity allows
            for task in list(pending):
                min_agents = task["metadata"].get("min_agents", 1)
                max_agents = task["metadata"].get("max_agents", AGENTS_PER_WORKER)
                if executor._pick_node(min_agents, max_agents) is None:
                    continue
                pending.remove(task)
                running[task["id"]] = asyncio.create_task(executor.run(task, 1))
                await asyncio.sleep(0)  # yield so run() executes and decrements capacity

            if not running:
                await asyncio.sleep(0.05)
                continue

            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                task_id = next(k for k, v in running.items() if v is fut)
                del running[task_id]
                results[task_id] = fut.result()

        print("\n" + "=" * 60, flush=True)
        passed = 0
        for task_id, result in results.items():
            status = "✓" if result.success else "✗"
            print(f"  {status} {task_id}: winner={result.winner_id} error={result.error}", flush=True)
            if result.success:
                passed += 1

        print(f"\n{passed}/{len(TASKS)} tasks succeeded", flush=True)
        assert passed == len(TASKS), f"Expected all tasks to succeed, got {passed}/{len(TASKS)}"
        print("All assertions passed.", flush=True)
        print("=" * 60, flush=True)

        await executor.shutdown()


# ---------------------------------------------------------------------------
# Entry point — same script on every rank
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    rank = get_rank()
    world_size = get_world_size()
    master_addr = get_master_addr()
    master_port = get_master_port()
    num_workers = world_size - 1

    if rank == 0:
        print("=" * 60, flush=True)
        print(f"ZmqExecutor test  (world_size={world_size})", flush=True)
        print(f"  master: {master_addr}:{master_port}", flush=True)
        print(f"  workers: {num_workers}  agents/worker: {AGENTS_PER_WORKER}", flush=True)
        print(f"  tasks: {len(TASKS)}", flush=True)
        print("=" * 60, flush=True)
        asyncio.run(run_coordinator(master_port, num_workers))
    else:
        run_worker(rank, host=master_addr, port=master_port)
