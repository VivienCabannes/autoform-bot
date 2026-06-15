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


# ---------------------------------------------------------------------------
# Zulip server
# ---------------------------------------------------------------------------


class TestZulipServer:
    """Tests for the Zulip search server."""

    def test_import_server(self):
        """The Zulip server module should import without error."""
        from servers.zulip import server  # noqa: F401

    def test_import_core(self):
        """The Zulip core module should import without error."""
        from servers.zulip import core  # noqa: F401

    def test_create_server(self):
        """create_zulip_server should return a FastMCP instance."""
        from servers.zulip.server import create_zulip_server

        server = create_zulip_server()
        assert server is not None
        assert server.name == "autoform-zulip"

    def test_find_zuliprc_returns_none_when_missing(self, tmp_path):
        """find_zuliprc should return None when no .zuliprc exists."""
        from servers.zulip.core import find_zuliprc

        # Pass a dir with no .zuliprc and clear env
        import os
        env_backup = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        try:
            result = find_zuliprc(str(tmp_path))
            # Result depends on whether ~/.zuliprc exists on the test machine,
            # but should not raise
            assert result is None or result.is_file()
        finally:
            if env_backup is not None:
                os.environ["ZULIPRC"] = env_backup

    def test_find_zuliprc_finds_project_local(self, tmp_path):
        """find_zuliprc should find a .zuliprc in the project directory."""
        from servers.zulip.core import find_zuliprc

        rc = tmp_path / ".zuliprc"
        rc.write_text("[api]\nemail=test@test.com\nkey=fake\nsite=https://example.com\n")

        import os
        env_backup = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        try:
            result = find_zuliprc(str(tmp_path))
            assert result is not None
            assert result == rc
        finally:
            if env_backup is not None:
                os.environ["ZULIPRC"] = env_backup

    def test_find_zuliprc_env_var_takes_priority(self, tmp_path):
        """$ZULIPRC env var should take priority over all other locations."""
        from servers.zulip.core import find_zuliprc

        rc = tmp_path / "custom.zuliprc"
        rc.write_text("[api]\nemail=test@test.com\nkey=fake\nsite=https://example.com\n")

        import os
        old = os.environ.get("ZULIPRC")
        os.environ["ZULIPRC"] = str(rc)
        try:
            result = find_zuliprc()
            assert result == rc
        finally:
            if old is not None:
                os.environ["ZULIPRC"] = old
            else:
                os.environ.pop("ZULIPRC", None)

    def test_parse_zuliprc(self, tmp_path):
        """parse_zuliprc should extract the [api] section."""
        from servers.zulip.core import parse_zuliprc

        rc = tmp_path / ".zuliprc"
        rc.write_text("[api]\nemail=bot@example.com\nkey=secret123\nsite=https://chat.example.com\n")

        result = parse_zuliprc(rc)
        assert result["email"] == "bot@example.com"
        assert result["key"] == "secret123"
        assert result["site"] == "https://chat.example.com"
