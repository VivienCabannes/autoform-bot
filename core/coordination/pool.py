# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""AgentPool — agent lifecycle, checkout/checkin, and reviewer pairing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


class AgentPool:
    """Pool of agents with idle/busy tracking and checkout/checkin."""

    def __init__(self, agents: list[Agent], reviewers: dict[str, Agent] | None = None):
        self._agents = agents
        self._busy: set[str] = set()
        self._reviewers: dict[str, Agent] = reviewers or {}

    @property
    def size(self) -> int:
        return len(self._agents)

    def available(self) -> list[Agent]:
        """Return agents that are not currently busy."""
        return [a for a in self._agents if a.id not in self._busy]

    def checkout(self, n: int) -> list[Agent]:
        """Check out up to n available agents. Marks them busy."""
        avail = self.available()
        taken = avail[:n]
        for agent in taken:
            self._busy.add(agent.id)
        return taken

    def checkin(self, agents: list[Agent]) -> None:
        """Return agents to the pool."""
        for agent in agents:
            self._busy.discard(agent.id)

    def get_reviewer(self, agent_id: str) -> Agent | None:
        """Get the reviewer for an agent."""
        return self._reviewers.get(agent_id)

    def status(self) -> dict[str, Any]:
        """Pool status summary."""
        return {
            "total": self.size,
            "available": len(self.available()),
            "busy": len(self._busy),
            "agents": [{"id": a.id, "busy": a.id in self._busy} for a in self._agents],
        }

    async def initialize(self, batch_size: int = 4, max_retries: int = 2) -> None:
        """Start all agents and reviewers in batches with retries.

        Initializing too many agents concurrently can overwhelm shared
        resources (LSP servers, REPL connections). Batching avoids this.

        Args:
            batch_size: Number of agents to initialize concurrently.
            max_retries: Times to retry a failed agent before giving up.
        """
        all_agents = list(self._agents) + list(self._reviewers.values())
        for i in range(0, len(all_agents), batch_size):
            batch = all_agents[i : i + batch_size]
            for agent in batch:
                for attempt in range(1, max_retries + 2):
                    try:
                        await agent.__aenter__()
                        break
                    except Exception:
                        if attempt > max_retries:
                            logger.error("Agent %s failed to initialize after %d attempts", agent.id, attempt)
                            raise
                        logger.warning(
                            "Agent %s init failed (attempt %d/%d), retrying...",
                            agent.id,
                            attempt,
                            max_retries + 1,
                        )
                        await asyncio.sleep(1.0)

    async def shutdown(self) -> None:
        """Close all agents."""
        for reviewer in self._reviewers.values():
            try:
                await reviewer.close()
            except Exception:
                logger.warning("Failed to close reviewer %s", reviewer.id, exc_info=True)
        for agent in self._agents:
            try:
                await agent.close()
            except Exception:
                logger.warning("Failed to close agent %s", agent.id, exc_info=True)
