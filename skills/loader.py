"""Skill loader — loads skill markdown files and injects them into prompts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.agent import AgentDefinition


def load_skill(skills_dir: str | Path, name: str) -> dict:
    """Load a single skill by relative path.

    Args:
        skills_dir: Root skills directory.
        name: Relative path under skills_dir without .md extension (e.g. "tools/bash").

    Returns:
        Dict with 'name', 'content', 'path' keys.

    Raises:
        FileNotFoundError: If the skill file does not exist.
    """
    skills_dir = Path(skills_dir)
    path = skills_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {path}")
    return {
        "name": path.stem,
        "content": path.read_text(),
        "path": str(path),
    }


def inject_skills_into_prompt(
    base_prompt: str,
    skills: list[dict],
) -> str:
    """Inject loaded skills into a system prompt.

    Args:
        base_prompt: The base system prompt.
        skills: List of skill dicts from load_skills().
        header: Section header for the skills block.

    Returns:
        Augmented prompt with skills appended.
    """
    if not skills:
        return base_prompt

    skill_sections = []
    for skill in skills:
        content = skill["content"].strip()
        skill_sections.append(f"### {skill['name']}\n\n```md\n{content}\n```")

    skills_block = "\n\n## Relevant Skills\n\n" + "\n\n---\n\n".join(skill_sections)
    return base_prompt + skills_block


def resolve_agent_skills(defn: AgentDefinition, project_root: str | Path) -> None:
    """Load and inject skills listed in an AgentDefinition into its system prompt.

    Mutates ``defn.system_prompt`` in place. No-op if ``defn.skills`` is empty.

    Args:
        defn: Agent definition whose skills should be resolved.
        project_root: Project root directory (skills are loaded from ``project_root/skills/``).
    """
    if not defn.skills_prompt:
        return
    skills_dir = Path(project_root) / "skills"
    loaded = [load_skill(skills_dir, s) for s in defn.skills_prompt]
    defn.system_prompt = inject_skills_into_prompt(defn.system_prompt, loaded)
