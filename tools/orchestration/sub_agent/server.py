"""Sub-agent MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import SubAgentManager


def create_sub_agent_server(manager: SubAgentManager) -> FastMCP:
    """Create an in-process MCP server exposing sub-agent tools."""
    server = FastMCP(name="sub-agent")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    def list_agents() -> str:
        """List available agent definitions that can be spawned as sub-agents."""
        return manager.list_available_agents()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    async def spawn_agent(agent_name: str, objective: str, child_budget: int = 0) -> str:
        """Spawn a sub-agent to work on a task in the background.

        After spawning, end your turn — do not poll check_agents in a loop.
        Results are delivered automatically when sub-agents finish.
        Always ask the user for permission before spawning.

        Args:
            agent_name: Name of the agent definition to spawn.
            objective: The task for the sub-agent to complete.
            child_budget: Number of sub-agent slots to grant the child.
                Costs child_budget+1 from the parent's budget (1 for the child
                itself + child_budget for its own spawning capacity).
        """
        try:
            return await manager.spawn(agent_name, objective, child_budget=child_budget)
        except ValueError as e:
            return f"Error: {e}"

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    def check_agents() -> str:
        """Check the status of all spawned sub-agents."""
        return manager.check_agents()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    async def spawn_adhoc_agent(
        objective: str, system_prompt: str, tool_allowlist_json: str, child_budget: int = 0
    ) -> str:
        """Spawn an ad-hoc sub-agent with a custom system prompt and tool subset.

        Args:
            objective: The task for the sub-agent to complete.
            system_prompt: Custom system prompt for the sub-agent.
            tool_allowlist_json: JSON array of tool names the sub-agent may use.
            child_budget: Number of sub-agent slots to grant the child.
                Costs child_budget+1 from the parent's budget.
        """
        try:
            tool_allowlist = json.loads(tool_allowlist_json)
            if not isinstance(tool_allowlist, list):
                return "Error: tool_allowlist_json must be a JSON array of strings."
            return await manager.spawn_adhoc(
                objective=objective,
                system_prompt=system_prompt,
                tool_allowlist=tool_allowlist,
                child_budget=child_budget,
            )
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON in tool_allowlist_json: {e}"
        except ValueError as e:
            return f"Error: {e}"

    return server


def sub_agent_server(manager: SubAgentManager) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for the sub-agent tool server."""
    mcp_instance = create_sub_agent_server(manager)
    return MCPServerConfig(
        server_key="sub-agent",
        description="Sub-agent spawning and management",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
