# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Resource pool with active-count tracking.

Provides a simple wrapper around asyncio.Semaphore for controlling
concurrent call volume across multiple agents.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class ResourcePool:
    """Pool of capacity slots with status reporting.

    Wraps asyncio.Semaphore with convenience methods and active-count tracking.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Acquire the semaphore, blocking if at capacity."""
        async with self._semaphore:
            self._active += 1
            try:
                yield
            finally:
                self._active -= 1

    @property
    def active(self) -> int:
        """Number of currently active calls."""
        return self._active

    @property
    def available(self) -> int:
        """Number of available slots."""
        return self._max_concurrent - self._active

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def status(self) -> dict[str, int]:
        """Return semaphore status."""
        return {
            "max_concurrent": self._max_concurrent,
            "active": self._active,
            "available": self.available,
        }


class SubAgentBudget:
    """Hierarchical budget for sub-agent spawning.

    A parent agent receives a fixed capacity of sub-agent slots. Spawning a
    child with ``child_budget`` costs ``child_budget + 1`` from the parent
    (1 for the child itself + ``child_budget`` slots the child may use to
    spawn its own sub-agents). Budget is returned when the child completes,
    making this a concurrency limiter for the agent tree.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._available = capacity
        self._lock = asyncio.Lock()
        self._reservations: dict[str, int] = {}  # agent_id -> cost reserved

    async def reserve(self, agent_id: str, child_budget: int) -> None:
        """Reserve ``child_budget + 1`` slots for *agent_id*.

        Raises ``ValueError`` if insufficient budget remains.
        """
        cost = 1 + child_budget
        async with self._lock:
            if cost > self._available:
                raise ValueError(
                    f"Insufficient sub-agent budget: need {cost} "
                    f"(1 + {child_budget}), only {self._available} of "
                    f"{self._capacity} available."
                )
            self._available -= cost
            self._reservations[agent_id] = cost

    async def release(self, agent_id: str) -> None:
        """Release the reservation held by *agent_id*."""
        async with self._lock:
            cost = self._reservations.pop(agent_id, 0)
            self._available += cost

    def status(self) -> dict[str, int]:
        """Return capacity, available, and reserved counts."""
        return {
            "capacity": self._capacity,
            "available": self._available,
            "reserved": self._capacity - self._available,
        }
