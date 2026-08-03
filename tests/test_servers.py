"""Smoke tests for stateful MCP configuration and server implementation code."""

from __future__ import annotations

import json


def test_mcp_servers_keep_only_stateful_services(repo_root):
    cfg = json.loads((repo_root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]

    assert set(cfg) == {"lean-lsp-mcp", "autoform-prover"}
    assert cfg["lean-lsp-mcp"]["command"] == "lean-lsp-mcp"
    disabled = cfg["lean-lsp-mcp"]["args"][1].split(",")
    assert set(disabled) == {"lean_leansearch", "lean_loogle", "lean_leanfinder"}
    assert "cwd" not in cfg["lean-lsp-mcp"]
    assert cfg["autoform-prover"]["env"]["UV_PROJECT_ENVIRONMENT"] == ".venv-autoform-prover"
    assert all(entry["startup_timeout_sec"] >= 120 for entry in cfg.values())


# ---------------------------------------------------------------------------
# Aristotle core
# ---------------------------------------------------------------------------


class TestAristotleCore:
    """Tests for the Aristotle implementation behind the unified prover."""

    def test_import_core(self):
        """The Aristotle core module should import without initializing its client."""
        from servers.aristotle import core  # noqa: F401

    def test_manager_constructs_without_aristotlelib(self):
        """Constructing the manager must not initialize the client."""
        from servers.aristotle.core import AristotleManager

        mgr = AristotleManager(download_dir="./out")
        assert mgr.list_sessions() == {"sessions": []}


# ---------------------------------------------------------------------------
# Stateless Zulip API helper
# ---------------------------------------------------------------------------


class TestZulipCore:
    """Tests for the one-shot Zulip API helper."""

    def test_import_core(self):
        """The Zulip core module should import without error."""
        from servers.zulip import core  # noqa: F401
