# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean native LSP MCP server — type-check files and query proof state.

Wraps ``LeanNativeLspSession`` as MCP tools:
- ``lean_check_file``: type-check Lean code incrementally (warm-up + didChange)
- ``lean_proof_state``: get proof goals at a position
"""

from __future__ import annotations

import atexit
import json
import threading
from dataclasses import dataclass
from logging import getLogger

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .session import LeanNativeLspSession, _DEFAULT_WARMUP_IMPORTS
from .structs import LeanDiagnostics

logger = getLogger(__name__)


class LeanNativeLspSessionManager:
    """Lazily creates and caches ``LeanNativeLspSession`` instances per workspace."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        warmup_imports: tuple[str, ...] = _DEFAULT_WARMUP_IMPORTS,
        warmup_timeout: int | None = None,
    ) -> None:
        self._timeout = timeout
        self._warmup_imports = warmup_imports
        self._warmup_timeout = warmup_timeout
        self._sessions: dict[str, LeanNativeLspSession] = {}
        self._lock = threading.Lock()

    def get_session(self, workspace: str) -> LeanNativeLspSession:
        with self._lock:
            if workspace not in self._sessions:
                session = LeanNativeLspSession(
                    workspace,
                    timeout=self._timeout,
                    warmup_imports=self._warmup_imports,
                    warmup_timeout=self._warmup_timeout,
                )
                session.start()
                self._sessions[workspace] = session
            return self._sessions[workspace]

    def close_all(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    logger.warning("Error closing Lean LSP session.", exc_info=True)
            self._sessions.clear()


def _format_diagnostics(diagnostics: LeanDiagnostics | None, header_lines: int = 0) -> str:
    """Format Lean diagnostics into a readable string.

    Args:
        diagnostics: Raw diagnostics from the LSP.
        header_lines: Number of import header lines to subtract from
            reported line numbers (so the user sees line numbers
            relative to their code, not the full document).
    """
    if diagnostics is None:
        return "No diagnostics received."

    if not diagnostics.diagnostics:
        return "No errors or warnings."

    lines: list[str] = []
    for d in diagnostics.diagnostics:
        severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(d.severity, f"severity-{d.severity}")
        adjusted_line = d.range.start.line + 1 - header_lines
        loc = f"{adjusted_line}:{d.range.start.character}"
        lines.append(f"[{severity}] {loc}: {d.message}")
    return "\n".join(lines)


def create_lean_native_lsp_server(
    manager: LeanNativeLspSessionManager,
    *,
    default_workspace: str = ".",
) -> FastMCP:
    """Create a FastMCP server with Lean-specific LSP tools."""
    server = FastMCP(name="lean-native-lsp")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE_RESTRICTED)
    def lean_check_file(
        code: str,
        path: str = "",
        workspace: str = "",
    ) -> str:
        """Type-check Lean code and return diagnostics.

        The first call warms up the LSP by elaborating imports (e.g.
        ``import Mathlib``). Subsequent calls use incremental compilation
        — only the changed proof body is re-elaborated, making iteration
        very fast.

        Do NOT include import statements in ``code`` — they are handled
        automatically by the warm-up. Just provide the theorem/proof body.

        For checking a complete file with its own imports, pass the full
        content including imports and set ``path`` to the file URI.

        Args:
            code: Lean proof body (without imports) for incremental checking.
                  If ``path`` is set, this is treated as the full file content.
            path: If set, opens this as a standalone file (non-incremental).
            workspace: Lean project root directory.
        """
        ws = workspace or default_workspace
        session = manager.get_session(ws)

        if path:
            # Non-incremental: open a standalone file with its own imports
            uri = path if path.startswith("file://") else f"file://{path}"
            try:
                diagnostics = session.run_file(uri, code)
                return _format_diagnostics(diagnostics)
            except TimeoutError as exc:
                return f"Timeout: {exc}"
        else:
            # Incremental: warm up once, then didChange for fast iteration
            try:
                diagnostics = session.check_code(code)
                header_lines = len(session._import_header().splitlines())
                return _format_diagnostics(diagnostics, header_lines=header_lines)
            except TimeoutError as exc:
                return f"Timeout: {exc}"

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def lean_proof_state(
        line: int,
        character: int,
        path: str = "",
        workspace: str = "",
    ) -> str:
        """Get the proof state (goals) at a position in a Lean file.

        When used after ``lean_check_file`` (without ``path``), line
        numbers are relative to the code you submitted — the import
        header offset is added automatically.

        Args:
            line: Zero-based line number in your code (not the full document).
            character: Zero-based character offset.
            path: File URI. If empty, uses the incremental scratch file.
            workspace: Lean project root directory.
        """
        from tools.execution.lsp.structs import Position

        ws = workspace or default_workspace
        session = manager.get_session(ws)

        if path:
            uri = path if path.startswith("file://") else f"file://{path}"
            result = session.get_proof_state(uri, Position(line=line, character=character))
        else:
            # Adjust line number to account for the import header
            header_lines = len(session._import_header().splitlines())
            adjusted_line = line + header_lines
            result = session.get_check_proof_state(Position(line=adjusted_line, character=character))

        if result is None:
            return "No proof state available at this position."
        return json.dumps(result.model_dump(), indent=2)

    return server


# ---------------------------------------------------------------------------
# MCPServerConfig factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeanNativeLspConfig:
    """Config for the Lean native LSP MCP server."""

    workspace: str = "."
    timeout: int = 60
    warmup_imports: tuple[str, ...] = _DEFAULT_WARMUP_IMPORTS
    warmup_timeout: int | None = None


def lean_native_lsp_server(config: LeanNativeLspConfig) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for the Lean native LSP."""
    manager = LeanNativeLspSessionManager(
        timeout=config.timeout,
        warmup_imports=config.warmup_imports,
        warmup_timeout=config.warmup_timeout,
    )
    atexit.register(manager.close_all)
    mcp = create_lean_native_lsp_server(manager, default_workspace=config.workspace)
    return MCPServerConfig(
        server_key="lean_native_lsp",
        description="Native Lean language server with diagnostics and completions",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp,
    )
