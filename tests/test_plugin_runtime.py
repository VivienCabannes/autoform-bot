"""Host-facing plugin configuration regression tests."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile


def test_documented_review_server_launch_imports_shared_helpers(repo_root, tmp_path):
    process = subprocess.run(
        [
            sys.executable,
            str(repo_root / "visualization" / "serve_review.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 0, process.stderr
    assert "--graph" in process.stdout


def test_wheel_contains_importable_runtime_dashboard_and_assets(repo_root, tmp_path):
    dist = tmp_path / "dist"
    process = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr

    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {
            "autoform/prover/runtime.py",
            "scripts/__init__.py",
            "scripts/backend_config.py",
            "scripts/dispatch_queue.py",
            "scripts/formalization.py",
            "scripts/fslock.py",
            "visualization/serve_review.py",
            "visualization/assets/review.css",
            "visualization/assets/review.js",
            "visualization/templates/dep_graph.html",
        }
        assert required <= names
        assert not any(name.startswith("scripts/.lake/") for name in names)
        archive.extractall(site)

    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import sys
from pathlib import Path

site = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(site))

from autoform.prover import runtime
from scripts import dispatch_queue, formalization
from visualization import serve_review

for module in (runtime, dispatch_queue, formalization, serve_review):
    assert Path(module.__file__).resolve().is_relative_to(site), module.__file__
assert callable(runtime.run_prove_node)
assets = Path(serve_review.__file__).resolve().parent / "assets"
assert (assets / "review.css").is_file()
assert (assets / "review.js").is_file()
""",
            str(site),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_codex_mcp_servers_use_resolvable_plugin_relative_paths(repo_root):
    config = json.loads((repo_root / ".mcp.json").read_text())

    for name, server in config["mcpServers"].items():
        assert server["cwd"] == ".", name
        assert "${" not in json.dumps(server), name
        assert server["command"] == "uv", name
        assert server["args"][0] == "run", name
        assert server["args"][1:3] == ["--project", "."], name
        assert "python" in server["args"], name


def test_claude_mcp_servers_do_not_default_lean_root_to_plugin(repo_root):
    config = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())

    for name, server in config["mcpServers"].items():
        assert server["cwd"] == "${CLAUDE_PLUGIN_ROOT}", name
        assert server["args"][0:3] == [
            "run",
            "--project",
            "${CLAUDE_PLUGIN_ROOT}",
        ], name
        assert "LEAN_PROJECT_DIR" not in json.dumps(server), name
        assert "project_dir" in server["description"], name


def test_codex_workflow_syntax_is_documented(repo_root):
    for path in (repo_root / "README.md", repo_root / "QUICKSTART.md"):
        text = path.read_text()
        for skill in ("setup", "orchestrate", "set-backend"):
            assert f"$autoform:{skill}" in text, path
