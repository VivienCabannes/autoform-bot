"""Host-facing packaging and plugin-surface checks."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile


def test_main_plugin_surface_excludes_deicyde_orchestration(repo_root):
    skills = {path.parent.name for path in (repo_root / "skills").glob("*/SKILL.md")}
    assert skills == {
        "setup",
        "roadmap",
        "human-review",
        "agent-review",
        "develop-plugin",
    }

    review_dir = repo_root / "skills" / "agent-review"
    references = {
        "faithfulness.md",
        "proof-integrity.md",
        "code-quality.md",
        "mathlib-style.md",
        "roadmap-quality.md",
        "thesis-review-case.md",
    }
    assert {path.name for path in (review_dir / "references").glob("*.md")} == references
    skill_text = (review_dir / "SKILL.md").read_text()
    assert all(f"references/{name}" in skill_text for name in references)

    claude = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    assert "mcpServers" not in claude
    assert "hooks" not in claude

    codex_manifest = json.loads((repo_root / ".codex-plugin/plugin.json").read_text())
    assert "mcpServers" not in codex_manifest
    assert len(codex_manifest["interface"]["defaultPrompt"]) == 5
    muse = json.loads((repo_root / ".muse-plugin/plugin.json").read_text())
    assert [command["id"] for command in muse["capabilities"]["commands"]] == [
        "setup",
        "roadmap",
        "human-review",
        "agent-review",
        "develop-plugin",
    ]
    for command in muse["capabilities"]["commands"]:
        assert (repo_root / command["path"]).is_file()
    assert muse["capabilities"]["mcpServers"] == []


def test_lean_beam_registration_is_owned_upstream(repo_root):
    assert not (repo_root / ".mcp.json").exists()
    assert not (repo_root / "scripts" / "launch-lean-beam-mcp.sh").exists()
    docs = (repo_root / "docs" / "lean-beam.md").read_text(encoding="utf-8")
    assert "does not bundle a Beam executable" in docs
    assert "register the canonical\n`lean-beam` server" in docs


def test_wheel_contains_only_the_minimal_runtime(repo_root, tmp_path):
    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheel, = dist.glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert {
            "autoform_cli/__main__.py",
            "autoform_cli/graph.py",
            "autoform_cli/visualize.py",
        } <= names
        assert "autoform_cli/lake.py" not in names
        assert "autoform_cli/templates/github/autoform_audit.py" in names
        assert not any(name.startswith("servers/") for name in names)
        entry_points = archive.read(
            next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        ).decode()
        assert "autoform-lean-runtime" not in entry_points
        metadata = archive.read(
            next(name for name in names if name.endswith(".dist-info/METADATA"))
        ).decode()
        assert "Requires-Dist: fastmcp" not in metadata
        assert "Requires-Dist: psutil" not in metadata
        assert "Requires-Dist: tomli" not in metadata
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
from autoform_cli import graph, visualize
assert Path(graph.__file__).resolve().is_relative_to(site)
assert Path(visualize.__file__).resolve().is_relative_to(site)
""",
            str(site),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
