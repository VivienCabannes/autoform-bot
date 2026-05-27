# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Generic LSP MCP server — diagnostics-focused tools.

Provides language-agnostic LSP operations as MCP tools:
- ``lsp_check_file``: open a file and return diagnostics
- ``lsp_hover``: get hover information at a position
- ``lsp_definition``: go-to-definition at a position

A ``LspSessionManager`` maps workspace paths to ``LspSession`` instances,
so multiple agents on different worktrees share one MCP server but each
get their own LSP session.
"""

from __future__ import annotations

import atexit
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from logging import getLogger

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .session import LspSession, LspSessionConfig
from .structs import Position

logger = getLogger(__name__)


class LspSessionManager:
    """Lazily creates and caches ``LspSession`` instances per workspace."""

    def __init__(self, config_factory: Callable[[str], LspSessionConfig]) -> None:
        self._config_factory = config_factory
        self._sessions: dict[str, LspSession] = {}
        self._lock = threading.Lock()

    def get_session(self, workspace: str) -> LspSession:
        with self._lock:
            if workspace not in self._sessions:
                config = self._config_factory(workspace)
                session = LspSession(config)
                session.start()
                self._sessions[workspace] = session
            return self._sessions[workspace]

    def close_all(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    logger.warning("Error closing LSP session.", exc_info=True)
            self._sessions.clear()


def create_lsp_server(manager: LspSessionManager, *, default_workspace: str = ".") -> FastMCP:
    """Create a FastMCP server with generic LSP tools."""
    server = FastMCP(name="native-lsp")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE_RESTRICTED)
    def lsp_check_file(
        content: str,
        path: str,
        language_id: str = "",
        workspace: str = "",
    ) -> str:
        """Open a file in the language server and return diagnostics.

        Sends the file contents to the LSP server, waits briefly for
        diagnostics, and returns them as a JSON list.

        Args:
            content: File contents to check.
            path: File path (used as the document URI).
            language_id: Language identifier (e.g. "python", "lean").
            workspace: Project root directory (default: configured workspace).
        """
        ws = workspace or default_workspace
        session = manager.get_session(ws)
        uri = path if path.startswith("file://") else f"file://{path}"
        session.open_file(uri, content)
        # Give the server a moment to produce diagnostics
        import time

        time.sleep(0.5)
        diags = session.get_diagnostics(uri)
        return json.dumps([d.model_dump() for d in diags], indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def lsp_hover(
        path: str,
        line: int,
        character: int,
        workspace: str = "",
    ) -> str:
        """Get hover information at a position in a file.

        Args:
            path: File path (document URI).
            line: Zero-based line number.
            character: Zero-based character offset.
            workspace: Project root directory.
        """
        ws = workspace or default_workspace
        session = manager.get_session(ws)
        uri = path if path.startswith("file://") else f"file://{path}"
        result = session.hover(uri, Position(line=line, character=character))
        if result is None:
            return "No hover information available."
        return json.dumps(result.model_dump(), indent=2)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def lsp_definition(
        path: str,
        line: int,
        character: int,
        workspace: str = "",
    ) -> str:
        """Go to definition at a position in a file.

        Args:
            path: File path (document URI).
            line: Zero-based line number.
            character: Zero-based character offset.
            workspace: Project root directory.
        """
        ws = workspace or default_workspace
        session = manager.get_session(ws)
        uri = path if path.startswith("file://") else f"file://{path}"
        result = session.definition(uri, Position(line=line, character=character))
        if isinstance(result, list):
            return json.dumps([r.model_dump() for r in result], indent=2)
        return json.dumps(result.model_dump(), indent=2)

    return server


# ---------------------------------------------------------------------------
# MCPServerConfig factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LspServerConfig:
    """Config for the generic LSP MCP server.

    ``config_factory`` is called with a workspace path and must return
    an ``LspSessionConfig`` for that workspace.
    """

    config_factory: Callable[[str], LspSessionConfig]


def lsp_native_server(config: LspServerConfig, *, default_workspace: str = ".") -> MCPServerConfig:
    """Create an in-process MCPServerConfig for the generic LSP tool."""
    manager = LspSessionManager(config.config_factory)
    atexit.register(manager.close_all)
    mcp = create_lsp_server(manager, default_workspace=default_workspace)
    return MCPServerConfig(
        server_key="native_lsp",
        description="Generic language server protocol client",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp,
    )
