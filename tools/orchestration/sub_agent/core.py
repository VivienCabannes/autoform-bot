"""Sub-agent core — manager for spawning and tracking background agents.

No MCP dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from core import REPO_ROOT
from core.agent import Agent
from core.inference import InferenceProtocol
from core.agent import load_agent_definition
from core.resources import SubAgentBudget
from core.tool import Autonomy, ToolSpec
from core.trace import AgentTrace
from core.trace.store import TraceStore
from core.inference.client import create_inference, lookup_model
from skills.loader import resolve_agent_skills
from tools import ScratchpadConfig, resolve_servers, resolve_tool_scores

logger = logging.getLogger(__name__)

DEFAULT_PREVIEW_TRUNCATION = 200


@dataclass
class SubAgentRecord:
    """Tracks a spawned sub-agent."""

    id: str
    agent_name: str
    objective: str
    status: str  # "running" | "completed" | "failed"
    result: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)
    agent: Agent | None = field(default=None, repr=False)


class SubAgentManager:
    """Manages sub-agents spawned from declarative agent definitions."""

    def __init__(
        self,
        workspace: str,
        inference: InferenceProtocol,
        scratchpad_dir: str,
        parent_autonomy: Autonomy = Autonomy.BARE,
        allowed_subagents: list[str] | None = None,
        parent_tool_allowlist: list[str] | None = None,
        can_spawn_subagents_with_tools_subset: bool = False,
        can_spawn_subagents_with_same_autonomy_level: bool = False,
        budget: SubAgentBudget | None = None,
    ) -> None:
        self._workspace = workspace
        self._inference = inference
        self._scratchpad_dir = scratchpad_dir
        self._parent_autonomy = parent_autonomy
        self._allowed_subagents = allowed_subagents
        self._parent_tool_allowlist = parent_tool_allowlist
        self._can_spawn_tools_subset = can_spawn_subagents_with_tools_subset
        self._can_spawn_same_autonomy = can_spawn_subagents_with_same_autonomy_level
        self._budget = budget
        self.trace_store: TraceStore | None = None
        self._agents: dict[str, SubAgentRecord] = {}
        self._result_queue: asyncio.Queue[SubAgentRecord] = asyncio.Queue()
        self._completion_event = asyncio.Event()

    def set_parent_autonomy(self, autonomy: Autonomy) -> None:
        self._parent_autonomy = autonomy

    @property
    def inference(self) -> InferenceProtocol:
        """The inference backend used for spawned sub-agents."""
        return self._inference

    @inference.setter
    def inference(self, value: InferenceProtocol) -> None:
        self._inference = value

    def list_available_agents(self) -> str:
        """Scan REPO_ROOT/agents for dirs with prompt.md."""
        agents_dir = REPO_ROOT / "agents"
        available = []
        if not agents_dir.is_dir():
            return "No agent definitions found."
        for d in sorted(agents_dir.iterdir()):
            prompt_file = d / "prompt.md"
            if d.is_dir() and prompt_file.exists():
                if d.name.startswith("fort_bot"):
                    continue
                if self._allowed_subagents is not None and d.name not in self._allowed_subagents:
                    continue
                first_line = prompt_file.read_text().strip().split("\n")[0]
                defn = load_agent_definition(str(d))
                resolve_agent_skills(defn, REPO_ROOT)
                resolve_tool_scores(
                    defn.tool_servers,
                    workspace=self._workspace,
                    base_config=defn.tool_server_config,
                    scratchpad=ScratchpadConfig(dir=self._scratchpad_dir),
                )
                autonomy = ToolSpec.compute_agent_autonomy(defn.tool_allowlist)
                available.append(
                    {
                        "name": d.name,
                        "description": first_line,
                        "autonomy_score": autonomy.score,
                        "autonomy_label": autonomy.value,
                    }
                )
        if not available:
            return "No agent definitions found."
        return json.dumps(available, indent=2)

    async def spawn(self, agent_name: str, objective: str, child_budget: int = 0) -> str:
        """Spawn a sub-agent in the background. Returns the agent ID."""
        agents_dir = REPO_ROOT / "agents" / agent_name
        if not (agents_dir / "prompt.md").exists():
            raise ValueError(f"Agent definition not found: {agent_name}")
        if self._allowed_subagents is not None and agent_name not in self._allowed_subagents:
            raise ValueError(f"Agent '{agent_name}' is not in the allowed agents list.")

        agent_id = str(uuid.uuid4())[:8]

        if self._budget is not None:
            await self._budget.reserve(agent_id, child_budget)

        defn = load_agent_definition(str(agents_dir))
        resolve_agent_skills(defn, REPO_ROOT)

        server_configs = resolve_servers(
            defn.tool_servers,
            workspace=self._workspace,
            base_config=defn.tool_server_config,
            scratchpad=ScratchpadConfig(dir=self._scratchpad_dir),
        )

        child_autonomy = ToolSpec.compute_agent_autonomy(defn.tool_allowlist)
        if child_autonomy.score > self._parent_autonomy.score:
            if self._budget is not None:
                await self._budget.release(agent_id)
            raise ValueError(
                f"Agent '{agent_name}' requires '{child_autonomy.value}' autonomy ({child_autonomy.score}) "
                f"but parent is at '{self._parent_autonomy.value}' ({self._parent_autonomy.score}). "
                f"Use /autonomy to increase the parent level first."
            )

        inference = create_inference(lookup_model(defn.config.model))

        if child_budget > 0:
            from .server import sub_agent_server

            child_manager = SubAgentManager(
                workspace=self._workspace,
                inference=inference,
                scratchpad_dir=self._scratchpad_dir,
                parent_autonomy=child_autonomy,
                allowed_subagents=self._allowed_subagents,
                parent_tool_allowlist=defn.tool_allowlist,
                can_spawn_subagents_with_tools_subset=self._can_spawn_tools_subset,
                can_spawn_subagents_with_same_autonomy_level=self._can_spawn_same_autonomy,
                budget=SubAgentBudget(child_budget),
            )
            child_manager.trace_store = self.trace_store
            server_configs.append(sub_agent_server(child_manager))

        agent = Agent(
            defn,
            inference=inference,
            server_configs=server_configs,
        )

        record = SubAgentRecord(
            id=agent_id,
            agent_name=agent_name,
            objective=objective,
            status="running",
            started_at=time.time(),
            agent=agent,
        )
        self._agents[agent_id] = record

        async def _run() -> None:
            trace: AgentTrace | None = None
            if self.trace_store is not None:
                trace = AgentTrace(id=agent_id)
                agent.set_trace(trace)
                agent._trace_store = self.trace_store
            try:
                async with agent:
                    result = await agent.call(objective)
                record.result = result
                record.status = "completed"
            except Exception as e:
                record.error = str(e)
                record.status = "failed"
                logger.error("Sub-agent %s failed: %s", agent_id, e)
            finally:
                if trace is not None and self.trace_store is not None:
                    trace.finalize(
                        status=record.status,
                        total_turns=agent.total_turns,
                        messages=agent.inference.get_messages(),
                        error=record.error or None,
                    )
                    self.trace_store.save(trace)
                    agent.set_trace(None)
                record.finished_at = time.time()
                if self._budget is not None:
                    await self._budget.release(agent_id)
                await self._result_queue.put(record)
                self._completion_event.set()

        record.task = asyncio.create_task(_run())
        return f"Spawned agent '{agent_name}' (id: {agent_id}). End your turn now — results will be delivered automatically when the agent finishes."

    async def spawn_adhoc(
        self,
        objective: str,
        system_prompt: str,
        tool_allowlist: list[str],
        tool_servers: list[str] | None = None,
        child_budget: int = 0,
    ) -> str:
        """Spawn an ad-hoc sub-agent with a custom prompt and tool subset.

        Validates that the requested tools are within the parent's capabilities
        based on the spawning mode flags set on the parent agent definition.

        Returns a status message with the agent ID.
        """
        if not self._can_spawn_tools_subset and not self._can_spawn_same_autonomy:
            raise ValueError("Ad-hoc sub-agent spawning is not enabled for this agent.")

        if self._can_spawn_tools_subset:
            parent_tools = set(self._parent_tool_allowlist or [])
            requested = set(tool_allowlist)
            extra = requested - parent_tools
            if extra:
                raise ValueError(f"Requested tools not in parent's allowlist: {sorted(extra)}")

        if self._can_spawn_same_autonomy:
            child_autonomy = ToolSpec.compute_agent_autonomy(tool_allowlist)
            if child_autonomy.score > self._parent_autonomy.score:
                raise ValueError(
                    f"Ad-hoc agent requires '{child_autonomy.value}' autonomy ({child_autonomy.score}) "
                    f"but parent is at '{self._parent_autonomy.value}' ({self._parent_autonomy.score})."
                )

        from core.agent import AgentDefinition, AgentConfig

        agent_id = str(uuid.uuid4())[:8]

        if self._budget is not None:
            await self._budget.reserve(agent_id, child_budget)

        defn = AgentDefinition(
            name=f"adhoc-{agent_id}",
            system_prompt=system_prompt,
            config=AgentConfig(),
            tool_servers=tool_servers or [],
            tool_allowlist=tool_allowlist,
        )

        server_configs = resolve_servers(
            defn.tool_servers,
            workspace=self._workspace,
            base_config=defn.tool_server_config,
            scratchpad=ScratchpadConfig(dir=self._scratchpad_dir),
        )

        if child_budget > 0:
            from .server import sub_agent_server

            adhoc_autonomy = ToolSpec.compute_agent_autonomy(tool_allowlist)
            child_manager = SubAgentManager(
                workspace=self._workspace,
                inference=self._inference,
                scratchpad_dir=self._scratchpad_dir,
                parent_autonomy=adhoc_autonomy,
                allowed_subagents=self._allowed_subagents,
                parent_tool_allowlist=tool_allowlist,
                can_spawn_subagents_with_tools_subset=self._can_spawn_tools_subset,
                can_spawn_subagents_with_same_autonomy_level=self._can_spawn_same_autonomy,
                budget=SubAgentBudget(child_budget),
            )
            child_manager.trace_store = self.trace_store
            server_configs.append(sub_agent_server(child_manager))

        agent = Agent(
            defn,
            inference=self._inference,
            server_configs=server_configs,
        )

        record = SubAgentRecord(
            id=agent_id,
            agent_name=defn.name,
            objective=objective,
            status="running",
            started_at=time.time(),
            agent=agent,
        )
        self._agents[agent_id] = record

        async def _run() -> None:
            trace: AgentTrace | None = None
            if self.trace_store is not None:
                trace = AgentTrace(id=agent_id)
                agent.set_trace(trace)
                agent._trace_store = self.trace_store
            try:
                async with agent:
                    result = await agent.call(objective)
                record.result = result
                record.status = "completed"
            except Exception as e:
                record.error = str(e)
                record.status = "failed"
                logger.error("Ad-hoc sub-agent %s failed: %s", agent_id, e)
            finally:
                if trace is not None and self.trace_store is not None:
                    trace.finalize(
                        status=record.status,
                        total_turns=agent.total_turns,
                        messages=agent.inference.get_messages(),
                        error=record.error or None,
                    )
                    self.trace_store.save(trace)
                    agent.set_trace(None)
                record.finished_at = time.time()
                if self._budget is not None:
                    await self._budget.release(agent_id)
                await self._result_queue.put(record)
                self._completion_event.set()

        record.task = asyncio.create_task(_run())
        return f"Spawned ad-hoc agent (id: {agent_id}). End your turn now — results will be delivered automatically when the agent finishes."

    def check_agents(self) -> str:
        """Status of all spawned agents."""
        results = []
        for r in self._agents.values():
            entry: dict = {
                "id": r.id,
                "agent_name": r.agent_name,
                "objective": r.objective,
                "status": r.status,
            }
            elapsed = (r.finished_at or time.time()) - r.started_at
            entry["elapsed_s"] = round(elapsed, 1)
            if r.status == "completed":
                entry["result_preview"] = (
                    r.result[:DEFAULT_PREVIEW_TRUNCATION] + "..."
                    if len(r.result) > DEFAULT_PREVIEW_TRUNCATION
                    else r.result
                )
            if r.status == "failed":
                entry["error"] = r.error
            results.append(entry)
        output: dict = {}
        if self._budget is not None:
            output["budget"] = self._budget.status()
        if not results:
            output["agents"] = "No sub-agents have been spawned."
        else:
            output["agents"] = results
        return json.dumps(output, indent=2)

    def running_count(self) -> int:
        return sum(1 for r in self._agents.values() if r.status == "running")

    def drain_completed(self) -> list[SubAgentRecord]:
        """Non-blocking drain of the result queue."""
        completed = []
        while True:
            try:
                record = self._result_queue.get_nowait()
                completed.append(record)
            except asyncio.QueueEmpty:
                break
        self._completion_event.clear()
        return completed

    async def wait_for_completion(self) -> None:
        """Block until at least one sub-agent finishes."""
        await self._completion_event.wait()
        self._completion_event.clear()

    async def shutdown(self) -> None:
        """Cancel running tasks and close all agent instances."""
        for record in self._agents.values():
            if record.task and not record.task.done():
                record.task.cancel()
        tasks = [r.task for r in self._agents.values() if r.task and not r.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for record in self._agents.values():
            if record.agent:
                try:
                    await record.agent.close()
                except Exception:
                    pass
