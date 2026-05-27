# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Orchestrator agent lifecycle — creation, trace resume, and round execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from core.agent import Agent, AgentDefinition
from core.inference import InferenceProtocol
from core.interaction import get_registry
from core.trace import TraceStore, AgentTrace
from tools.files.filesystem import filesystem_server
from tools.files.filesystem.server import FilesystemConfig
from tools.vcs.git import git_server
from tools.vcs.git.server import GitConfig
from autoform.bot.tools.task_tracker import task_tracker_server
from autoform.bot.tools.task_tracker.core import ConstrainedTracker
from autoform.bot.tools.reports import reports_server
from autoform.bot.tools.analysis import lean_analysis_server
from autoform.bot.tools.reading_agent.server import reading_agent_server
from autoform.bot.tools.escalate import escalate_server
from autoform.bot.tools.todo import todo_server
from core.tracker import ItemTracker

logger = logging.getLogger(__name__)


class OrchestratorManager:
    """Manages the persistent orchestrator agent across planning rounds.

    Trace lifecycle is delegated to Agent (set_trace / load_from_trace /
    incremental save via trace_store). This class only handles server
    wiring, prompt templates, and trace resume/finalize at session boundaries.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        inference_factory: Callable[[], InferenceProtocol],
        *,
        code_path: Path,
        book_path: Path,
        skills_path: Path,
        reports_path: Path,
        trace_store: TraceStore | None,
    ):
        self._definition = definition
        self._inference_factory = inference_factory
        self._code_path = code_path
        self._book_path = book_path
        self._skills_path = skills_path
        self._reports_path = reports_path
        self._trace_store = trace_store

    def create(self, tracker: ItemTracker, goal_tracker: ItemTracker | None = None) -> Agent:
        """Construct the orchestrator agent with its tool servers."""
        self.constrained_tracker = ConstrainedTracker(tracker, mutable_flavors=None, default_flavor="task")
        reading_server, reading_ops = reading_agent_server(
            allowed_dirs=(
                str(self._book_path),
                str(self._code_path),
                str(self._skills_path / "lean"),
                str(self._skills_path / "workflow"),
                str(self._code_path.parent / "reports" / "eval_reports"),
                str(self._code_path.parent / "tool-results"),
                str(self._code_path.parent / "reports" / "merge_reports"),
            ),
        )
        reading_ops.trace_store = self._trace_store
        servers = [
            task_tracker_server(self.constrained_tracker),
            reports_server(self._reports_path),
            lean_analysis_server(self._code_path),
            reading_server,
            git_server(GitConfig(repo_root=str(self._code_path))),
            filesystem_server(
                FilesystemConfig(
                    allowed_dirs=(
                        str(self._book_path),
                        str(self._code_path),
                        str(self._skills_path / "lean"),
                        str(self._skills_path / "workflow"),
                        str(self._code_path.parent / "reports" / "eval_reports"),
                        str(self._code_path.parent / "tool-results"),
                        str(self._code_path.parent / "reports" / "merge_reports"),
                    )
                )
            ),
        ]
        esc_cfg, _ = escalate_server(self._code_path.parent, agent_id="orchestrator")
        servers.append(esc_cfg)
        servers.append(todo_server(self._code_path.parent / "orchestrator_todos.json"))
        if goal_tracker is not None:
            from autoform.bot.tools.goal_tracker.server import goal_tracker_server

            servers.append(goal_tracker_server(goal_tracker))
        agent = Agent(
            self._definition,
            self._inference_factory(),
            server_configs=servers,
            id="orchestrator",
            trace_store=self._trace_store,
            message_queue=asyncio.Queue(),
            persist_dir=self._code_path.parent,
        )
        get_registry().register("orchestrator", agent)
        return agent

    def resume_trace(self, agent: Agent) -> None:
        """Resume from saved trace if available, or attach a fresh one."""
        if self._trace_store:
            saved = self._trace_store.load("orchestrator")
            if saved:
                logger.info("Resuming orchestrator from saved trace (%d turns)", saved.get("total_turns", 0))
                agent.load_from_trace(saved)
                return
        agent.set_trace(AgentTrace(id="orchestrator"))

    @staticmethod
    def finalize_trace(agent: Agent, trace_store: TraceStore | None) -> None:
        """Finalize and save the orchestrator trace."""
        if trace_store and agent._trace is not None:
            agent._trace.finalize(
                status="completed",
                total_turns=agent.total_turns,
                messages=agent.messages,
            )
            trace_store.save(agent._trace)
