from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.build_muse_plugin import REPO_ROOT, build_muse_plugin


EXPECTED_COMMANDS = {"setup", "orchestrate", "set-backend"}
EXPECTED_MCP_SERVERS = {
    "lean-lsp-mcp",
    "autoform-prover",
}


def _manifest(root: Path) -> dict:
    return json.loads((root / ".muse-plugin" / "plugin.json").read_text())


def test_native_muse_manifest_has_the_portable_autoform_surface():
    manifest = _manifest(REPO_ROOT / "packaging" / "muse")
    assert manifest["schemaVersion"] == 1
    assert manifest["compat"] == {
        "source": "native",
        "manifestDir": ".muse-plugin",
    }

    capabilities = manifest["capabilities"]
    assert set(capabilities) == {
        "skills",
        "commands",
        "hooks",
        "mcpServers",
        "reminders",
    }
    assert capabilities["skills"] == []
    assert {item["id"] for item in capabilities["commands"]} == EXPECTED_COMMANDS
    for command in capabilities["commands"]:
        assert command["path"] == f"skills/{command['id']}/SKILL.md"
        assert command["enabledDefault"] is True
    assert capabilities["reminders"] == []
    assert capabilities["hooks"] == [
        {
            "id": "session-start",
            "event": "SessionStart",
            "command": ["bash", "hooks/session-start"],
            "timeoutMs": 10000,
            "statusMessage": "Loading Autoform context",
        }
    ]
    assert {item["id"] for item in capabilities["mcpServers"]} == EXPECTED_MCP_SERVERS
    for server in capabilities["mcpServers"]:
        assert server["transport"] == "stdio"
        assert server["command"][:2] == ["bash", "servers/run-muse-server.sh"]


def test_muse_builder_emits_one_supported_manifest_family(tmp_path: Path):
    output = build_muse_plugin(tmp_path / "autoform")

    assert (output / ".muse-plugin" / "plugin.json").is_file()
    assert not (output / ".claude-plugin").exists()
    assert not (output / ".codex-plugin").exists()
    assert not [path for path in output.rglob("*") if path.name == ".venv" or path.name.startswith(".venv-")]
    assert not list(output.rglob(".lake"))
    assert not list(output.rglob("__pycache__"))
    assert not [path for path in output.rglob("*") if path.is_symlink()]

    manifest = _manifest(output)
    for skill in manifest["capabilities"]["skills"]:
        assert (output / skill["path"]).is_file()
    for command in manifest["capabilities"]["commands"]:
        assert (output / command["path"]).is_file()
    assert (output / "hooks" / "session-start").is_file()
    assert (output / "servers" / "run-muse-server.sh").is_file()
    assert (output / "pyproject.toml").is_file()
    assert (output / "uv.lock").is_file()


def test_muse_builder_requires_force_to_replace_output(tmp_path: Path):
    output = build_muse_plugin(tmp_path / "autoform")
    marker = output / "marker"
    marker.write_text("old")

    try:
        build_muse_plugin(output)
    except FileExistsError as error:
        assert "--force" in str(error)
    else:
        raise AssertionError("rebuild without --force should fail")

    build_muse_plugin(output, force=True)
    assert not marker.exists()


def test_muse_mcp_launcher_keeps_uv_state_outside_plugin_cache():
    launcher = (REPO_ROOT / "servers" / "run-muse-server.sh").read_text()
    assert "MUSE_PLUGIN_ROOT" in launcher
    assert "MUSE_PLUGIN_DATA_DIR" in launcher
    assert "UV_PROJECT_ENVIRONMENT" in launcher
    assert "PYTHONDONTWRITEBYTECODE=1" in launcher
    assert 'cd "$plugin_root"' in launcher


def test_muse_mcp_launcher_uses_plugin_data_environment(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PWD\" \"$UV_PROJECT_ENVIRONMENT\" "
        "\"$LEAN_PROJECT_DIR\" \"$*\" > \"$CAPTURE\"\n"
    )
    fake_uv.chmod(0o755)
    capture = tmp_path / "capture"
    plugin_data = tmp_path / "plugin-data"
    lean_project = tmp_path / "lean-project"

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CAPTURE": str(capture),
            "MUSE_PLUGIN_ROOT": str(REPO_ROOT),
            "MUSE_PLUGIN_DATA_DIR": str(plugin_data),
            "LEAN_PROJECT_DIR": str(lean_project),
        }
    )
    subprocess.run(
        ["bash", str(REPO_ROOT / "servers" / "run-muse-server.sh"), "prover"],
        check=True,
        env=env,
    )

    cwd, uv_environment, project, args = capture.read_text().splitlines()
    assert cwd == str(REPO_ROOT)
    assert uv_environment == str(plugin_data / "venv-prover")
    assert project == str(lean_project)
    assert args == "run python -m servers.prover.server"


def test_session_start_context_names_muse_as_a_supported_host():
    completed = subprocess.run(
        ["bash", str(REPO_ROOT / "hooks" / "session-start")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Muse" in context
    assert "setup, orchestrate, and set-backend" in context
