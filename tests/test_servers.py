"""Smoke tests for all MCP servers.

Each test class verifies that the server module imports cleanly and that
the ``create_*_server()`` factory produces a valid FastMCP instance.

Stub servers (repl, lsp, aristotle) have zero-argument factories that
return servers whose tools return "not implemented" strings.
The zulip server wraps ``skills/zulip/zulip-search.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


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
