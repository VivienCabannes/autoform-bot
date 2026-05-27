# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Agent registry — maps running agent IDs to Agent instances for interactive messaging."""

from __future__ import annotations

from typing import Any

from core.agent import Agent


class AgentRegistry:
    """Maps agent IDs to running Agent instances.

    Used to route interactive messages from external sources (e.g. a web UI)
    to specific running agents via Agent.send_message().
    """

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent_id: str, agent: Agent) -> None:
        self._agents[agent_id] = agent

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def send(self, agent_id: str, message: str) -> bool:
        """Send an interactive message to an agent.

        The agent's background consumer handles it — calling directly
        if idle, or injecting mid-loop if busy.

        Returns True if the agent was found, False otherwise.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            return False
        return agent.send_message(message)

    def get_messages(self, agent_id: str) -> list[dict[str, Any]] | None:
        """Return the agent's conversation history, or None if not found."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return agent.messages

    def get_pending_messages(self, agent_id: str) -> list[str] | None:
        """Return messages waiting in the agent's queue, or None if not found."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return list(agent._pending_messages)

    def active_agents(self) -> list[str]:
        return list(self._agents.keys())


_registry = AgentRegistry()


def get_registry() -> AgentRegistry:
    """Return the module-level AgentRegistry singleton."""
    return _registry
