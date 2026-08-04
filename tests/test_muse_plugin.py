from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

EXPECTED_COMMANDS = {"setup", "orchestrate", "review"}
EXPECTED_MCP_SERVERS = {"autoform-lsp", "autoform-repl"}
REPO_ROOT = Path(__file__).resolve().parents[1]


def _manifest(root: Path) -> dict:
    return json.loads((root / ".muse-plugin" / "plugin.json").read_text())


def test_native_muse_manifest_matches_the_current_plugin_surface() -> None:
    manifest = _manifest(REPO_ROOT)
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
    assert capabilities["hooks"] == []
    assert capabilities["reminders"] == []
    assert {item["id"] for item in capabilities["commands"]} == EXPECTED_COMMANDS
    for command in capabilities["commands"]:
        assert command["path"] == f"skills/{command['id']}/SKILL.md"
        assert command["enabledDefault"] is True
        assert (REPO_ROOT / command["path"]).is_file()

    assert {item["id"] for item in capabilities["mcpServers"]} == EXPECTED_MCP_SERVERS
    for server in capabilities["mcpServers"]:
        assert server["transport"] == "stdio"
        assert server["command"][:2] == ["bash", "servers/run-muse-server.sh"]


@pytest.mark.parametrize(
    ("server", "environment", "arguments"),
    [
        (
            "lsp",
            "venv-lsp",
            "run --project {root} --locked python -m servers.lsp.server",
        ),
        (
            "repl",
            "venv-repl",
            "run --project {root} --locked --extra repl python -m servers.repl.server",
        ),
    ],
)
def test_muse_launcher_uses_plugin_data_environment(
    tmp_path: Path,
    server: str,
    environment: str,
    arguments: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PWD\" \"$UV_PROJECT_ENVIRONMENT\" \"$*\" > \"$CAPTURE\"\n"
    )
    fake_uv.chmod(0o755)
    capture = tmp_path / "capture"
    plugin_data = tmp_path / "plugin-data"

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CAPTURE": str(capture),
            "MUSE_PLUGIN_ROOT": str(REPO_ROOT),
            "MUSE_PLUGIN_DATA_DIR": str(plugin_data),
        }
    )
    subprocess.run(
        ["bash", str(REPO_ROOT / "servers" / "run-muse-server.sh"), server],
        check=True,
        env=env,
    )

    cwd, uv_environment, args = capture.read_text().splitlines()
    assert cwd == str(REPO_ROOT)
    assert uv_environment == str(plugin_data / environment)
    assert args == arguments.format(root=REPO_ROOT)
