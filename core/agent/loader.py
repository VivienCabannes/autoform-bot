# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Loader — reads declarative agent folders (prompt.md + config.yaml).

A declarative agent folder contains:
  - prompt.md: System prompt (markdown)
  - config.yaml: Agent configuration (model, tools, limits)

The loader produces an AgentConfig + system prompt string that can be
passed to the Agent constructor.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .config import AgentConfig
from ..inference import CacheConfig, InferenceConfig
from ..tool import Autonomy


def _pick(source: dict, *keys: str) -> dict[str, Any]:
    """Return subset of *source* containing only the specified keys that are present."""
    return {k: source[k] for k in keys if k in source}


def _pick_fields(source: dict, dc_class: type) -> dict[str, Any]:
    """Return subset of *source* matching field names of dataclass *dc_class*."""
    names = {f.name for f in fields(dc_class)}
    return {k: source[k] for k in source if k in names}


@dataclass
class AgentDefinition:
    """Parsed agent definition from a declarative folder."""

    name: str
    system_prompt: str
    config: AgentConfig
    model: str | None = None
    tool_servers: list[str] = field(default_factory=list)
    tool_allowlist: list[str] = field(default_factory=list)
    tool_server_config: dict[str, dict[str, Any]] | None = None
    autonomy: Autonomy | None = None
    allowed_subagents: list[str] | None = None
    can_spawn_subagents_with_tools_subset: bool = False
    can_spawn_subagents_with_same_autonomy_level: bool = False
    max_turns: int = 200
    tool_timeout_s: float = 300.0
    skills_prompt: list[str] = field(default_factory=list)
    skills_discover: list[str] = field(default_factory=list)


def load_agent_definition(agent_dir: str | Path) -> AgentDefinition:
    """Load an agent definition from a directory.

    Args:
        agent_dir: Path to the agent directory containing prompt.md and config.yaml.

    Returns:
        AgentDefinition with parsed config and prompt.

    Raises:
        FileNotFoundError: If prompt.md is missing.
    """
    agent_dir = Path(agent_dir)
    name = agent_dir.name

    # Read system prompt
    prompt_path = agent_dir / "prompt.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"No prompt.md found in {agent_dir}")
    system_prompt = prompt_path.read_text().strip()

    # Read config (optional)
    config_path = agent_dir / "config.yaml"
    raw_config: dict = {}
    if config_path.exists():
        with open(config_path) as f:
            raw_config = yaml.safe_load(f) or {}

    # Build AgentConfig — only pass keys present in config.yaml;
    # dataclass defaults fill the rest (single source of truth).
    agent_config_kwargs = _pick_fields(raw_config, AgentConfig)

    inference_params = raw_config.get("inference", {})
    if inference_params:
        inference_kwargs = _pick_fields(inference_params, InferenceConfig)
        if "cache" in inference_kwargs and isinstance(inference_kwargs["cache"], dict):
            inference_kwargs["cache"] = CacheConfig(**inference_kwargs["cache"])
        agent_config_kwargs["inference_config"] = InferenceConfig(**inference_kwargs)

    agent_config = AgentConfig(**agent_config_kwargs)

    # Tools
    tools_config = raw_config.get("tools", {})
    tool_servers = tools_config.get("servers", [])
    tool_allowlist = tools_config.get("allowlist", [])
    tool_server_config: dict[str, dict[str, Any]] | None = tools_config.get("server_config") or None

    # Skills — structured as {prompt: [...], discover: [...]}
    skills_config = raw_config.get("skills", {})
    skills_prompt: list[str] = skills_config.get("prompt", [])
    skills_discover: list[str] = skills_config.get("discover", [])

    # Autonomy
    autonomy = Autonomy(raw_config["autonomy"]) if "autonomy" in raw_config else None

    return AgentDefinition(
        name=name,
        system_prompt=system_prompt,
        config=agent_config,
        model=raw_config.get("model"),
        tool_servers=tool_servers,
        tool_allowlist=tool_allowlist,
        tool_server_config=tool_server_config,
        autonomy=autonomy,
        allowed_subagents=raw_config.get("allowed_subagents"),
        skills_prompt=skills_prompt,
        skills_discover=skills_discover,
        **_pick(
            raw_config,
            "can_spawn_subagents_with_tools_subset",
            "can_spawn_subagents_with_same_autonomy_level",
            "max_turns",
            "tool_timeout_s",
        ),
    )
