"""Discovery MCP server — on-demand documentation for tools and skills."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.mcp.registry import SkillRegistry, ToolRegistry
from core.tool import Autonomy, ToolSpec


@dataclass
class DiscoveryConfig:
    """Configuration for the discovery tool server.

    The default registries are empty and will not auto-populate. The caller
    must pass shared instances and call their populate() methods after
    MCP tool discovery (for tools) or at init time (for skills).
    """

    registry: ToolRegistry = field(default_factory=ToolRegistry)
    skill_registry: SkillRegistry = field(default_factory=SkillRegistry)


def create_discovery_server(
    registry: ToolRegistry,
    skill_registry: SkillRegistry,
) -> FastMCP:
    """Create a FastMCP server with tool and skill discovery tools."""
    server = FastMCP(name="discovery")

    # ── Tool discovery ────────────────────────────────────────────

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_tools() -> str:
        """List available tool collections with descriptions and tool counts.

        Returns a summary of all registered tool servers. Use check_tools(name)
        with a collection name or specific tool name for detailed documentation.
        """
        return registry.format_server_list()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def check_tools(name: str) -> str:
        """Get detailed documentation for a tool collection or specific tool.

        Accepts either a collection name (e.g. 'filesystem', 'git') to see all
        tools in that collection, or a specific tool name (e.g. 'read_text_file')
        for that tool's full documentation including parameters and autonomy level.

        Args:
            name: Collection name or tool function name.
        """
        return registry.lookup(name)

    # ── Skill discovery ───────────────────────────────────────────

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_skills() -> str:
        """List available skills with descriptions.

        Returns a summary of all registered skills. Use check_skills(name)
        with a skill name for the full skill content.
        """
        return skill_registry.format_skill_list()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def check_skills(name: str) -> str:
        """Get the full content of a skill.

        Accepts a skill name (e.g. 'git/clean-branch-after-merge',
        'git/clean-branch-after-merge') and returns its full markdown content.

        Args:
            name: Skill name (relative path without .md extension).
        """
        return skill_registry.lookup(name)

    return server


def discovery_server(config: DiscoveryConfig) -> MCPServerConfig:
    """Create a discovery MCP server config."""
    mcp_instance = create_discovery_server(config.registry, config.skill_registry)
    return MCPServerConfig(
        server_key="discovery",
        description="On-demand tool and skill documentation and discovery",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
