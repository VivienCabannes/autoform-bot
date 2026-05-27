# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Interaction handler — frame, deliver, respond, react."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.agent import Agent


@dataclass
class Interaction:
    """Bidirectional external interaction lifecycle.

    Three phases:
    1. Frame — transform a raw external message into an agent-ready prompt
    2. Deliver — call the agent and get its natural-language response
    3. Respond + React — send the response back to the source, then
       optionally trigger side effects (e.g. rebuild a DAG)

    The agent does not need communication tools — framing and response
    delivery are handled by the interaction layer, keeping the agent
    channel-agnostic.
    """

    frame: Callable[[str], str]
    respond: Callable[[str], Awaitable[None]]
    react: Callable[[], Awaitable[None]] | None = None

    async def handle(self, agent: Agent, message: str) -> str:
        """Run the full interaction lifecycle. Returns the agent's response."""
        prompt = self.frame(message)
        response = await agent.call(prompt)
        await self.respond(response)
        if self.react:
            await self.react()
        return response
