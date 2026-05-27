# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Discovery registries — indexes discovered tools and skills for on-demand lookup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..tool import ToolSpec

if TYPE_CHECKING:
    from .manager import MCPClientManager


@dataclass(frozen=True)
class ToolEntry:
    """Metadata for a single tool function."""

    name: str
    description: str
    parameters: dict[str, Any]
    server_key: str


@dataclass(frozen=True)
class ServerEntry:
    """Metadata for a tool server (collection of tools)."""

    key: str
    description: str
    tool_names: tuple[str, ...]


class ToolRegistry:
    """Indexes discovered tools by server for on-demand lookup.

    Populated after MCPClientManager.discover_tools() runs. Provides
    formatted output for the list_tools/check_tools discovery tools.
    """

    def __init__(self) -> None:
        self._servers: dict[str, ServerEntry] = {}
        self._tools: dict[str, ToolEntry] = {}

    @property
    def servers(self) -> dict[str, ServerEntry]:
        return self._servers

    @property
    def tools(self) -> dict[str, ToolEntry]:
        return self._tools

    def populate(self, manager: MCPClientManager) -> None:
        """Build registry from a manager that has already discovered tools."""
        self._servers.clear()
        self._tools.clear()

        for server_key, tool_names in manager.server_to_tools.items():
            cfg = manager.server_configs.get(server_key)
            server_desc = cfg.description if cfg else ""

            sorted_names = sorted(tool_names)
            self._servers[server_key] = ServerEntry(
                key=server_key,
                description=server_desc,
                tool_names=tuple(sorted_names),
            )

            for name in sorted_names:
                mcp_tool = manager.tools_by_name.get(name)
                if not mcp_tool:
                    continue
                self._tools[name] = ToolEntry(
                    name=name,
                    description=mcp_tool.description or "",
                    parameters=mcp_tool.inputSchema or {"type": "object", "properties": {}},
                    server_key=server_key,
                )

    # ── Formatted output ─────────────────────────────────────────────

    def format_server_list(self) -> str:
        """Compact listing of all servers with tool counts."""
        if not self._servers:
            return "No tool servers registered."

        lines = ["# Available Tool Collections", ""]
        for entry in sorted(self._servers.values(), key=lambda e: e.key):
            count = len(entry.tool_names)
            desc = f" — {entry.description}" if entry.description else ""
            lines.append(f"- **{entry.key}** ({count} tool{'s' if count != 1 else ''}){desc}")
        lines.append("")
        lines.append("Use `check_tools(name)` with a collection name or tool name for details.")
        return "\n".join(lines)

    def format_server_detail(self, key: str) -> str:
        """Full docs for all tools in a server."""
        entry = self._servers.get(key)
        if not entry:
            return f"Unknown collection: '{key}'"

        lines = [f"# {entry.key}"]
        if entry.description:
            lines.append(f"\n{entry.description}")
        lines.append(f"\n## Tools ({len(entry.tool_names)})\n")

        for name in entry.tool_names:
            tool = self._tools.get(name)
            if not tool:
                lines.append(f"### {name}\n")
                continue
            lines.append(f"### {name}")
            if tool.description:
                lines.append(f"\n{tool.description.strip()}")
            params = self._format_parameters(tool.parameters)
            if params:
                lines.append(f"\n**Parameters**: {params}")
            lines.append("")

        return "\n".join(lines)

    def format_tool_detail(self, name: str) -> str:
        """Full docs for a single tool function."""
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: '{name}'"

        lines = [f"# {tool.name}", f"*Collection: {tool.server_key}*"]
        if tool.description:
            lines.append(f"\n{tool.description.strip()}")
        params = self._format_parameters(tool.parameters)
        if params:
            lines.append(f"\n**Parameters**: {params}")

        spec = ToolSpec.get(name)
        if spec:
            lines.append(f"\n**Autonomy**: {spec.autonomy.value}")

        return "\n".join(lines)

    def lookup(self, name: str) -> str:
        """Try server key first, then tool name. Returns formatted docs."""
        if name in self._servers:
            return self.format_server_detail(name)
        if name in self._tools:
            return self.format_tool_detail(name)

        # Suggest close matches
        all_names = list(self._servers.keys()) + list(self._tools.keys())
        suggestions = [n for n in all_names if name and name.lower() in n.lower()]
        if suggestions:
            return f"Unknown: '{name}'. Did you mean: {', '.join(suggestions[:5])}?"
        return f"Unknown: '{name}'. Use list_tools() to see available collections."

    def format_compact_summary(self) -> str:
        """Bulleted list of tool collections for the system prompt."""
        lines = []
        for entry in sorted(self._servers.values(), key=lambda e: e.key):
            count = len(entry.tool_names)
            desc = f" — {entry.description}" if entry.description else ""
            lines.append(f"- **{entry.key}** ({count} tool{'s' if count != 1 else ''}){desc}")
        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _format_parameters(schema: dict[str, Any]) -> str:
        """Format JSON Schema parameters into a compact string."""
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not props:
            return ""

        parts = []
        for pname, pschema in props.items():
            ptype = pschema.get("type", "any")
            suffix = "" if pname in required else ", optional"
            parts.append(f"`{pname}` ({ptype}{suffix})")
        return ", ".join(parts)


# ── Skill registry ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillEntry:
    """Metadata for a single skill."""

    name: str
    description: str
    path: str


class SkillRegistry:
    """Indexes discovered skills for on-demand lookup.

    Scans a skills directory for .md files and provides formatted output
    for the list_skills/check_skills discovery tools.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillEntry] = {}

    @property
    def skills(self) -> dict[str, SkillEntry]:
        return self._skills

    def populate(self, skills_dir: Path, names: list[str]) -> None:
        """Register the specified skills from skills_dir.

        Args:
            skills_dir: Root skills directory.
            names: Skill names (relative paths without .md extension).
        """
        self._skills.clear()
        for name in names:
            md_path = skills_dir / f"{name}.md"
            if not md_path.is_file():
                continue
            description = self._extract_description(md_path)
            self._skills[name] = SkillEntry(
                name=name,
                description=description,
                path=str(md_path),
            )

    # ── Formatted output ─────────────────────────────────────────

    def format_skill_list(self) -> str:
        """Compact listing of all skills."""
        if not self._skills:
            return "No skills registered."

        lines = ["# Available Skills", ""]
        for entry in sorted(self._skills.values(), key=lambda e: e.name):
            desc = f" — {entry.description}" if entry.description else ""
            lines.append(f"- **{entry.name}**{desc}")
        lines.append("")
        lines.append("Use `check_skills(name)` for the full skill content.")
        return "\n".join(lines)

    def format_skill_detail(self, name: str) -> str:
        """Full content of a skill file."""
        entry = self._skills.get(name)
        if not entry:
            return f"Unknown skill: '{name}'"

        content = Path(entry.path).read_text(encoding="utf-8")
        return f"# Skill: {entry.name}\n\n{content}"

    def lookup(self, name: str) -> str:
        """Look up a skill by name. Returns formatted content."""
        if name in self._skills:
            return self.format_skill_detail(name)

        all_names = list(self._skills.keys())
        suggestions = [n for n in all_names if name and name.lower() in n.lower()]
        if suggestions:
            return f"Unknown skill: '{name}'. Did you mean: {', '.join(suggestions[:5])}?"
        return f"Unknown skill: '{name}'. Use list_skills() to see available skills."

    def format_compact_summary(self) -> str:
        """Bulleted list of skills for the system prompt."""
        lines = []
        for entry in sorted(self._skills.values(), key=lambda e: e.name):
            desc = f" — {entry.description}" if entry.description else ""
            lines.append(f"- **{entry.name}**{desc}")
        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_description(path: Path) -> str:
        """Extract description from the first non-heading, non-empty line."""
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    return stripped
        except OSError:
            return ""
        return ""
