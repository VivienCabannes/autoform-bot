"""Host-facing plugin configuration regression tests."""

from __future__ import annotations

import json


def test_codex_mcp_servers_use_resolvable_plugin_relative_paths(repo_root):
    config = json.loads((repo_root / ".mcp.json").read_text())

    for name, server in config["mcpServers"].items():
        assert server["cwd"] == ".", name
        assert "${" not in json.dumps(server), name
        assert server["command"] == "uv", name
        assert server["args"][0] == "run", name
        assert "python" in server["args"], name


def test_codex_workflow_syntax_is_documented(repo_root):
    for path in (repo_root / "README.md", repo_root / "QUICKSTART.md"):
        text = path.read_text()
        for skill in ("setup", "orchestrate", "set-backend"):
            assert f"$autoform:{skill}" in text, path
