"""Smoke tests for all MCP servers.

Each test class verifies that the server module imports cleanly and that
the ``create_*_server()`` factory produces a valid FastMCP instance.
The workspace server, being fully implemented, gets additional tests
for ``inspect_workspace``.

Stub servers (repl, mathlib, lsp, trace, aristotle) have zero-argument
factories that return servers whose tools return "not implemented" strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Workspace server — fully implemented
# ---------------------------------------------------------------------------


class TestWorkspaceServer:
    """Tests for the workspace inspection server."""

    def test_import(self):
        """The workspace server module should import without error."""
        from servers.workspace import server  # noqa: F401

    def test_import_core(self):
        """The workspace core module should import without error."""
        from servers.workspace import core  # noqa: F401

    def test_create_server(self):
        """create_workspace_server should return a FastMCP instance."""
        from servers.workspace.server import create_workspace_server

        server = create_workspace_server()
        assert server is not None
        assert server.name == "autoform-workspace"

    def test_inspect_workspace_returns_dict(self, repo_root: Path):
        """inspect_workspace should return a dict with expected keys."""
        from servers.workspace.core import inspect_workspace

        result = inspect_workspace(str(repo_root))
        assert isinstance(result, dict)

        expected_keys = {
            "workspace",
            "project_root",
            "lakefile",
            "lean_toolchain",
            "targets_file",
            "book_file",
            "lean_file_count",
            "declaration_count",
            "sorry_count",
            "axiom_count",
            "tools_available",
            "next_steps",
        }
        assert expected_keys.issubset(result.keys()), f"Missing keys: {expected_keys - result.keys()}"

    def test_inspect_workspace_tools_available(self, repo_root: Path):
        """tools_available should be a dict with boolean values."""
        from servers.workspace.core import inspect_workspace

        result = inspect_workspace(str(repo_root))
        tools = result["tools_available"]
        assert isinstance(tools, dict)
        for key in ("lake", "lean", "rg"):
            assert key in tools
            assert isinstance(tools[key], bool)

    def test_inspect_workspace_next_steps(self, repo_root: Path):
        """next_steps should be a non-empty list of strings."""
        from servers.workspace.core import inspect_workspace

        result = inspect_workspace(str(repo_root))
        steps = result["next_steps"]
        assert isinstance(steps, list)
        assert len(steps) > 0
        assert all(isinstance(s, str) for s in steps)


# ---------------------------------------------------------------------------
# REPL server — stub
# ---------------------------------------------------------------------------


class TestReplServer:
    """Tests for the REPL server module."""

    def test_import_server(self):
        """The REPL server module should import without error."""
        from servers.repl import server  # noqa: F401

    def test_import_core(self):
        """The REPL core module should import without error."""
        from servers.repl import core  # noqa: F401

    def test_import_pool(self):
        """The REPL pool module should import without error."""
        from servers.repl import pool  # noqa: F401

    def test_create_server(self):
        """create_repl_server should return a FastMCP instance."""
        from servers.repl.server import create_repl_server

        server = create_repl_server()
        assert server is not None
        assert server.name == "autoform-repl"


# ---------------------------------------------------------------------------
# Mathlib server — stub
# ---------------------------------------------------------------------------


class TestMathlibServer:
    """Tests for the Mathlib search server."""

    def test_import_server(self):
        """The Mathlib server module should import without error."""
        from servers.mathlib import server  # noqa: F401

    def test_import_core(self):
        """The Mathlib core module should import without error."""
        from servers.mathlib import core  # noqa: F401

    def test_create_server(self):
        """create_mathlib_server should return a FastMCP instance."""
        from servers.mathlib.server import create_mathlib_server

        server = create_mathlib_server()
        assert server is not None
        assert server.name == "autoform-mathlib"


# ---------------------------------------------------------------------------
# LSP server — stub
# ---------------------------------------------------------------------------


class TestLspServer:
    """Tests for the LSP diagnostics server."""

    def test_import_server(self):
        """The LSP server module should import without error."""
        from servers.lsp import server  # noqa: F401

    def test_create_server(self):
        """create_lsp_server should return a FastMCP instance."""
        from servers.lsp.server import create_lsp_server

        server = create_lsp_server()
        assert server is not None
        assert server.name == "autoform-lsp"


# ---------------------------------------------------------------------------
# Trace server — stub
# ---------------------------------------------------------------------------


class TestTraceServer:
    """Tests for the execution trace server."""

    def test_import_server(self):
        """The trace server module should import without error."""
        from servers.trace import server  # noqa: F401

    def test_import_core(self):
        """The trace core module should import without error."""
        from servers.trace import core  # noqa: F401

    def test_create_server(self):
        """create_trace_server should return a FastMCP instance."""
        from servers.trace.server import create_trace_server

        server = create_trace_server()
        assert server is not None
        assert server.name == "autoform-trace"

    def test_trace_event_dataclass(self):
        """TraceEvent dataclass should be importable and constructable."""
        from servers.trace.core import TraceEvent

        event = TraceEvent(timestamp=1.0, event_type="step", agent="w1", data={"action": "search"})
        assert event.event_type == "step"
        assert event.agent == "w1"

    def test_trace_store_class_exists(self):
        """TraceStore class should be importable."""
        from servers.trace.core import TraceStore

        assert TraceStore is not None


# ---------------------------------------------------------------------------
# Aristotle server — stub
# ---------------------------------------------------------------------------


class TestAristotleServer:
    """Tests for the Aristotle (Harmonic) delegation server."""

    def test_import_server(self):
        """The Aristotle server module should import without error."""
        from servers.aristotle import server  # noqa: F401

    def test_create_server(self):
        """create_aristotle_server should return a FastMCP instance."""
        from servers.aristotle.server import create_aristotle_server

        server = create_aristotle_server()
        assert server is not None
        assert server.name == "autoform-aristotle"
