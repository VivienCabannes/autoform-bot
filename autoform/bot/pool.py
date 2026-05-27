# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LeanPool — creates Lean agent + reviewer pairs with worktree support."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.agent import Agent, AgentDefinition
from core.inference import InferenceProtocol
from core.coordination.pool import AgentPool
from core.interaction import get_registry
from core import worktree
from tools import resolve_servers
from tools.execution.lean.repl.server import ReplConfig
from tools.files.filesystem.server import FilesystemConfig
from autoform.bot.tools.reading_agent.server import reading_agent_server
from autoform.bot.tools.escalate import escalate_server

logger = logging.getLogger(__name__)


def create_lean_pool(
    repo_root: Path,
    num_agents: int,
    inference_factory: Callable[[], InferenceProtocol],
    worker_def: AgentDefinition,
    reviewer_def: AgentDefinition,
    *,
    agent_id_prefix: str = "lean",
    allowed_paths: list[Path] | None = None,
    trace_store: Any | None = None,
    repl_config: ReplConfig | None = None,
    run_id: str | None = None,
) -> AgentPool:
    """Create an AgentPool with Lean worker + reviewer pairs.

    Each run creates worktrees under a shared run directory
    (``worktrees/run-{ts}/``) so runs are fully independent.

    Args:
        repo_root: Path to the main git repository.
        num_agents: Number of worker agents to create.
        inference_factory: Factory that creates a fresh InferenceProtocol per agent.
        worker_def: Agent definition for workers.
        reviewer_def: Agent definition for reviewers.
        agent_id_prefix: Prefix for agent IDs and worktree names. Use the node
            rank in distributed runs to avoid filesystem collisions on NFS.
        allowed_paths: Extra paths agents can access.
        trace_store: Optional trace store for incremental saving.
        repl_config: ReplConfig with cwd/repl_command for auto-start.
        run_id: Shared run identifier (e.g. ``run-20260421-100300``).
            Generated if not provided.

    Returns:
        AgentPool with paired workers and reviewers.
    """
    allowed_paths = allowed_paths or []
    agents = []
    reviewers = {}

    if run_id is None:
        from datetime import datetime, timezone

        run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_worktrees_dir = repo_root.parent / "worktrees" / run_id
    run_worktrees_dir.mkdir(parents=True, exist_ok=True)
    logger.info("[%s] Worktrees dir: %s", agent_id_prefix, run_worktrees_dir)

    # Serialize worktree creation across nodes via file lock.
    # Multiple nodes share the same git repo on NFS — concurrent
    # `git worktree add` corrupts .git/worktrees/ internal state.
    lock_path = repo_root / ".worktree_lock"
    with open(lock_path, "w") as lock_file:
        logger.info("[%s] Waiting for worktree lock...", agent_id_prefix)
        fcntl.lockf(lock_file, fcntl.LOCK_EX)
        logger.info("[%s] Acquired worktree lock, creating %d worktrees", agent_id_prefix, num_agents)

        subprocess.run(["git", "-C", str(repo_root), "worktree", "prune"], capture_output=True)

        for i in range(num_agents):
            wt_name = f"{run_id}-{agent_id_prefix}-worker-{i}"
            worktree.create_worktree(repo_root, wt_name, worktrees_dir=run_worktrees_dir)

        logger.info("[%s] Released worktree lock", agent_id_prefix)

    # Build agents (no git operations — safe to run concurrently)
    for i in range(num_agents):
        agent_id = f"{agent_id_prefix}-worker-{i}"
        reviewer_id = f"{agent_id_prefix}-reviewer-{i}"
        wt_name = f"{run_id}-{agent_id_prefix}-worker-{i}"
        worktree_path = run_worktrees_dir / wt_name

        # Symlink .lake/packages from main repo so worktrees share
        # pre-resolved dependencies instead of re-downloading them.
        lake_src = repo_root / ".lake" / "packages"
        lake_dst = worktree_path / ".lake" / "packages"
        if lake_src.exists() and not lake_dst.exists():
            lake_dst.parent.mkdir(parents=True, exist_ok=True)
            lake_dst.symlink_to(lake_src.resolve())

        workspace = str(worktree_path)
        git_wt_internal = str(repo_root / ".git" / "worktrees" / wt_name)
        extra_read = [str(p) for p in allowed_paths] + [git_wt_internal]
        packages_ro = (str(worktree_path / ".lake" / "packages"),)

        # Resolve all servers — LSP auto-starts per worktree (via workspace),
        # REPL auto-starts once (via repl_config with cwd).
        # Workers don't get bash — all functionality is covered by dedicated
        # tools (filesystem, git, LSP, REPL, mathlib). Bash was a sandbox
        # risk: agents could write outside their worktree via shell redirects,
        # and `lake build` would corrupt the shared Mathlib build cache.
        worker_servers = [s for s in worker_def.tool_servers if s != "bash"]
        agent_servers = resolve_servers(
            worker_servers,
            workspace=workspace,
            lean_repl=repl_config,
            filesystem=FilesystemConfig(
                allowed_dirs=(workspace,),
                write_excluded_dirs=packages_ro,
                extra_read_dirs=tuple(extra_read),
            ),
        )
        reading_server, reading_ops = reading_agent_server(allowed_dirs=tuple([workspace] + extra_read))
        reading_ops.trace_store = trace_store
        agent_servers.append(reading_server)
        esc_server, esc_logger = escalate_server(repo_root.parent, agent_id=agent_id)
        agent_servers.append(esc_server)

        agent = Agent(
            worker_def,
            inference_factory(),
            server_configs=agent_servers,
            id=agent_id,
            trace_store=trace_store,
            message_queue=asyncio.Queue(),
            persist_dir=worktree_path,
        )
        agent.worktree_path = workspace
        agent.escalation_logger = esc_logger
        get_registry().register(agent_id, agent)
        agents.append(agent)

        # Reviewer — shares the same worktree (same LSP via rdv join)
        reviewer_servers = resolve_servers(
            reviewer_def.tool_servers,
            workspace=workspace,
            lean_repl=repl_config,
            filesystem=FilesystemConfig(
                allowed_dirs=(workspace,),
                write_excluded_dirs=packages_ro,
                extra_read_dirs=tuple(extra_read),
            ),
        )
        rev_reading_server, rev_reading_ops = reading_agent_server(allowed_dirs=tuple([workspace] + extra_read))
        rev_reading_ops.trace_store = trace_store
        reviewer_servers.append(rev_reading_server)

        reviewer = Agent(
            reviewer_def,
            inference_factory(),
            server_configs=reviewer_servers,
            id=reviewer_id,
            trace_store=trace_store,
            message_queue=asyncio.Queue(),
            persist_dir=worktree_path,
        )
        get_registry().register(reviewer_id, reviewer)
        reviewers[agent_id] = reviewer

    return AgentPool(agents=agents, reviewers=reviewers)
