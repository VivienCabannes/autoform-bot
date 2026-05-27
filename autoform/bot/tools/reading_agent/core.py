# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Reading agent core — spawns a lightweight agent to read and summarize files.

No MCP dependencies.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from core.agent import Agent, load_agent_definition
from core.trace import AgentTrace
from core.trace.store import TraceStore
from core.inference.client import create_inference, lookup_model
from tools import resolve_servers
from tools.files.filesystem.server import FilesystemConfig

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent.parent
READER_AGENT_DIR = APP_DIR / "agents" / "reader"


class ReadingAgent:
    """Spawns a short-lived reader agent to read a file and return a summary."""

    def __init__(self, allowed_dirs: tuple[str, ...]) -> None:
        self._allowed_dirs = allowed_dirs
        self._defn = load_agent_definition(str(READER_AGENT_DIR))
        self.trace_store: TraceStore | None = None

    async def read_and_summarize(self, path: str, instructions: str = "") -> str:
        """Read a file via a reader agent and return its summary.

        Args:
            path: Absolute path to the file to read.
            instructions: What to look for or extract. If empty, returns a general summary.
        """
        objective = f"Read the file at `{path}`."
        if instructions:
            objective += f"\n\n{instructions}"
        else:
            objective += "\n\nProvide a structured summary of the file's contents."

        inference = create_inference(lookup_model(self._defn.config.model))
        server_configs = resolve_servers(
            self._defn.tool_servers,
            filesystem=FilesystemConfig(allowed_dirs=self._allowed_dirs),
        )

        agent_id = f"readers/reader-{uuid.uuid4().hex[:8]}"
        agent = Agent(
            self._defn,
            inference=inference,
            server_configs=server_configs,
            id=agent_id,
            trace_store=self.trace_store,
        )

        trace: AgentTrace | None = None
        if self.trace_store is not None:
            trace = AgentTrace(id=agent_id)
            agent.set_trace(trace)

        error: str | None = None
        try:
            async with agent:
                result = await agent.call(objective)
            return result
        except Exception as e:
            error = str(e)
            logger.error("Reading agent %s failed: %s", agent_id, e)
            return f"Reading agent failed: {e}"
        finally:
            if trace is not None and self.trace_store is not None:
                trace.finalize(
                    status="failed" if error else "completed",
                    total_turns=agent.total_turns,
                    messages=agent.inference.get_messages(),
                    error=error,
                )
                self.trace_store.save(trace)
