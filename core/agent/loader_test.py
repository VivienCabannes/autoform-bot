# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for core.loader."""

import tempfile
from pathlib import Path

from core.agent import AgentConfig, AgentDefinition, Autonomy, load_agent_definition


def test_load_agent_definition():
    """Load agent definition from a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "test_agent"
        agent_dir.mkdir()

        # Create prompt.md
        (agent_dir / "prompt.md").write_text("You are a test agent.")

        # Create config.yaml
        (agent_dir / "config.yaml").write_text("""
model: test-model
max_turns: 50
tool_timeout_s: 120
tools:
  servers: [filesystem, git]
  allowlist:
    - read_text_file
    - git_status
""")

        defn = load_agent_definition(agent_dir)
        assert defn.name == "test_agent"
        assert defn.system_prompt == "You are a test agent."
        assert defn.config.model == "test-model"
        assert defn.max_turns == 50
        assert defn.tool_timeout_s == 120
        assert defn.tool_servers == ["filesystem", "git"]
        assert defn.tool_allowlist == ["read_text_file", "git_status"]


def test_load_agent_definition_minimal():
    """Load agent definition with only prompt.md (no config)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "minimal"
        agent_dir.mkdir()
        (agent_dir / "prompt.md").write_text("Minimal agent.")

        defn = load_agent_definition(agent_dir)
        assert defn.name == "minimal"
        assert defn.system_prompt == "Minimal agent."
        assert defn.config.model == AgentConfig().model
        assert defn.tool_allowlist == []
        assert defn.skills_prompt == []
        assert defn.skills_discover == []


def test_load_agent_definition_with_skills():
    """Skills config splits into prompt and discover lists."""
    from skills.loader import resolve_agent_skills

    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "skilled"
        agent_dir.mkdir()
        (agent_dir / "prompt.md").write_text("Base prompt.")

        # Create a skills directory with test skills
        skills_dir = Path(tmpdir) / "skills" / "tools"
        skills_dir.mkdir(parents=True)
        (skills_dir / "bash.md").write_text("Use bash for shell commands.")

        (agent_dir / "config.yaml").write_text("""
skills:
  prompt: [tools/bash]
  discover: [tools/other]
""")

        defn = load_agent_definition(agent_dir)
        assert defn.skills_prompt == ["tools/bash"]
        assert defn.skills_discover == ["tools/other"]
        # Skills are NOT injected by the loader
        assert defn.system_prompt == "Base prompt."

        # Caller resolves prompt skills
        resolve_agent_skills(defn, Path(tmpdir))
        assert "Base prompt." in defn.system_prompt
        assert "Use bash for shell commands." in defn.system_prompt
        assert "## Relevant Skills" in defn.system_prompt


def test_load_agent_definition_skills_default_empty():
    """AgentDefinition skills fields default to empty lists."""
    defn = AgentDefinition(
        name="test",
        system_prompt="prompt",
        config=None,  # type: ignore[arg-type]
    )
    assert defn.skills_prompt == []
    assert defn.skills_discover == []


def test_load_agent_definition_autonomy_from_config():
    """Autonomy field is parsed from config.yaml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "auto_agent"
        agent_dir.mkdir()
        (agent_dir / "prompt.md").write_text("Agent with autonomy.")
        (agent_dir / "config.yaml").write_text("autonomy: read\n")

        defn = load_agent_definition(agent_dir)
        assert defn.autonomy == Autonomy.READ


def test_load_agent_definition_autonomy_defaults_none():
    """Autonomy defaults to None when not in config."""
    defn = AgentDefinition(
        name="test",
        system_prompt="prompt",
        config=None,  # type: ignore[arg-type]
    )
    assert defn.autonomy is None


def test_load_agent_definition_spawning_booleans_default_false():
    """Spawning mode booleans default to False."""
    defn = AgentDefinition(
        name="test",
        system_prompt="prompt",
        config=None,  # type: ignore[arg-type]
    )
    assert defn.can_spawn_subagents_with_tools_subset is False
    assert defn.can_spawn_subagents_with_same_autonomy_level is False


def test_load_agent_definition_allowed_subagents():
    """allowed_subagents is loaded from config.yaml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "sub_agent"
        agent_dir.mkdir()
        (agent_dir / "prompt.md").write_text("Agent with sub-agents.")
        (agent_dir / "config.yaml").write_text("allowed_subagents: [helper, worker]\n")

        defn = load_agent_definition(agent_dir)
        assert defn.allowed_subagents == ["helper", "worker"]


def test_load_agent_definition_spawning_booleans_from_config():
    """Spawning mode booleans are loaded from config.yaml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        agent_dir = Path(tmpdir) / "spawn_agent"
        agent_dir.mkdir()
        (agent_dir / "prompt.md").write_text("Agent with spawning.")
        (agent_dir / "config.yaml").write_text(
            "can_spawn_subagents_with_tools_subset: true\ncan_spawn_subagents_with_same_autonomy_level: true\n"
        )

        defn = load_agent_definition(agent_dir)
        assert defn.can_spawn_subagents_with_tools_subset is True
        assert defn.can_spawn_subagents_with_same_autonomy_level is True
