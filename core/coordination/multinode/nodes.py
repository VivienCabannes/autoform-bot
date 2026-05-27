# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Generic Node base class, WorkerNode, and CoordinatorNode for multi-node coordination.

Node defines the shared lifecycle interface (initialize / run / shutdown).

WorkerNode handles the ZMQ event loop, task ACKs, and node lifecycle.
Subclasses implement _execute() for domain-specific task handling.

CoordinatorNode handles signal handling and node lifecycle.
Subclasses implement run_pipeline() for domain-specific pipeline logic.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from abc import ABC, abstractmethod
from typing import Any

from .zmq_queue import ZmqTaskClient

logger = logging.getLogger(__name__)


class Node(ABC):
    """Base class for all nodes in a multi-node pipeline.

    Both the coordinator (rank 0) and worker nodes (rank 1+) share the same
    lifecycle: initialize, run, shutdown.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Set up resources before the node starts doing work."""
        ...

    @abstractmethod
    async def run(self) -> None:
        """Main entry point — initialize, do work, then shutdown."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources after work is complete."""
        ...


class WorkerNode(Node):
    """Generic worker node: connects to coordinator, receives tasks, sends results.

    Owns the ZMQ event loop. Automatically ACKs task receipt so the coordinator
    can detect delivery failures. Subclasses implement initialize(), shutdown(),
    _execute(), and the capacity property.

    Args:
        rank: This node's rank in the distributed job.
        host: Coordinator's hostname (rank 0).
        port: Coordinator's ZMQ port.
    """

    def __init__(
        self,
        rank: int,
        host: str,
        port: int,
    ) -> None:
        self.rank = rank
        self.host = host
        self.port = port

    @property
    def capacity(self) -> int:
        """Number of agents currently available. Override in subclass."""
        return 0

    async def initialize(self) -> None:
        """Set up resources before serving. Override in subclass."""

    async def shutdown(self) -> None:
        """Release resources after serving. Override in subclass."""

    @abstractmethod
    async def _execute(self, msg: dict, send_queue: asyncio.Queue) -> None:
        """Handle a task message. Must send a result dict to send_queue."""
        ...

    async def run(self) -> None:
        """Install signal handler, initialize, serve tasks, then shut down.

        If the coordinator sends a restart signal (e.g. after an ACK timeout),
        the worker shuts down its pool, reinitializes, and re-registers.
        Restarts use exponential backoff and are capped at MAX_RESTARTS.
        """
        MAX_RESTARTS = 5

        loop = asyncio.get_running_loop()
        cancel = asyncio.current_task().cancel
        loop.add_signal_handler(signal.SIGTERM, cancel)
        loop.add_signal_handler(signal.SIGINT, cancel)

        initialized = False
        restart_count = 0
        try:
            while True:
                logger.info("[rank %d] Initializing...", self.rank)
                await self.initialize()
                initialized = True
                logger.info("[rank %d] Ready", self.rank)
                restart = await self._serve()
                await self.shutdown()
                initialized = False
                if not restart:
                    break
                restart_count += 1
                if restart_count > MAX_RESTARTS:
                    logger.error("[rank %d] Max restarts (%d) exceeded — exiting", self.rank, MAX_RESTARTS)
                    break
                delay = min(2**restart_count, 60)
                logger.info(
                    "[rank %d] Restarting in %ds (attempt %d/%d)...", self.rank, delay, restart_count, MAX_RESTARTS
                )
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("[rank %d] Cancelled", self.rank)
        except Exception:
            logger.exception("[rank %d] Fatal error", self.rank)
        finally:
            if initialized:
                await self.shutdown()
        logger.info("[rank %d] Shutdown complete", self.rank)

    async def _serve(self) -> bool:
        """ZMQ event loop — register, ACK tasks, receive tasks, send results.

        Returns True if the worker should restart, False if it should exit.
        """
        send_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        restart = False

        with ZmqTaskClient(host=self.host, port=self.port, rank=self.rank) as client:
            client.send({"type": "register", "rank": self.rank, "capacity": self.capacity})
            logger.info("[rank %d] Registered (capacity=%d)", self.rank, self.capacity)

            async def sender() -> None:
                while True:
                    msg = await send_queue.get()
                    if msg is None:
                        break
                    await asyncio.to_thread(client.send, msg)

            sender_task = asyncio.create_task(sender())

            in_flight: set[asyncio.Task] = set()

            try:
                while True:
                    msg = await asyncio.to_thread(client.recv, 200)
                    if msg is None:
                        continue
                    if msg["type"] == "shutdown":
                        logger.info("[rank %d] Shutdown received", self.rank)
                        restart = False
                        break
                    if msg["type"] == "restart":
                        logger.info("[rank %d] Restart requested by coordinator", self.rank)
                        restart = True
                        break
                    if msg["type"] == "task":
                        await send_queue.put({"type": "ack", "task_id": msg["task_id"], "rank": self.rank})
                        task = asyncio.create_task(self._execute(msg, send_queue))
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)
            finally:
                for t in in_flight:
                    t.cancel()
                if in_flight:
                    await asyncio.gather(*in_flight, return_exceptions=True)
                await send_queue.put(None)
                await sender_task

        return restart


class CoordinatorNode(Node):
    """Generic coordinator node: signal handling and node lifecycle.

    Subclasses implement initialize(), shutdown(), and run_pipeline().
    """

    async def initialize(self) -> None:
        """Set up resources before running pipeline. Override in subclass."""

    async def shutdown(self) -> None:
        """Release resources after pipeline. Override in subclass."""

    @abstractmethod
    async def run_pipeline(self) -> Any:
        """Run the pipeline. Subclass implements domain-specific logic."""
        ...

    async def run(self) -> Any:
        """Install signal handler, initialize, run pipeline, then shut down."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def _on_sigterm():
            logger.warning("CoordinatorNode received SIGTERM — cancelling")
            task.cancel()

        def _on_sigint():
            logger.warning("CoordinatorNode received SIGINT — cancelling")
            task.cancel()

        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        loop.add_signal_handler(signal.SIGINT, _on_sigint)

        await self.initialize()
        try:
            return await self.run_pipeline()
        except asyncio.CancelledError:
            logger.info("Coordinator cancelled")
        finally:
            await self.shutdown()
