"""Task dispatch core — manages task submission and result tracking.

No MCP dependencies.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.coordination.pool import AgentPool
from core.task import Task


class TaskDispatcher:
    """Manages task submission to an agent pool and tracks results."""

    def __init__(
        self,
        task_id: str,
        pool: AgentPool,
        team_run_fn,
    ) -> None:
        self.task_id = task_id
        self.pool = pool
        self.team_run_fn = team_run_fn
        self.completed_results: list[dict[str, Any]] = []
        self.running_tasks: list[asyncio.Task] = []
        self.done = False

    async def submit_task(self, prompt: str, num_agents: int = 1) -> str:
        agents = self.pool.checkout(num_agents)
        if len(agents) < num_agents:
            self.pool.checkin(agents)
            avail = len(self.pool.available())
            return (
                f"Error: requested {num_agents} agent(s) but only "
                f"{avail} available. Use show_agents() to check availability."
            )

        task = Task(id=self.task_id, description=prompt)

        async def _run():
            try:
                result = await self.team_run_fn(task, agents, get_reviewer=self.pool.get_reviewer)
                self.completed_results.append(
                    {
                        "task_id": task.id,
                        "description": task.description,
                        "success": result.success,
                        "error": result.error,
                        "winner_id": result.winner_id,
                        "team_trace": result.team_trace,
                    }
                )
            finally:
                self.pool.checkin(agents)

        bg_task = asyncio.create_task(_run())
        self.running_tasks.append(bg_task)

        agent_ids = [a.id for a in agents]
        return f"Launched {len(agents)} agent(s): {agent_ids}. Results will be delivered to you when done."

    def show_agents(self) -> str:
        return json.dumps(self.pool.status(), indent=2)

    def show_completed(self) -> str:
        display = [{k: v for k, v in r.items() if k != "team_trace"} for r in self.completed_results]
        return json.dumps(display, indent=2)

    def mark_done(self) -> str:
        self.done = True
        return "Task marked as done."
