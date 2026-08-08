"""Regression checks for the cold-start Lean workspace fixture."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "tests" / "fixtures" / "demo-project"


def test_demo_lake_manifest_is_explicit_and_buildable():
    manifest = tomllib.loads((DEMO / "lakefile.toml").read_text())
    assert manifest["name"] == "MyBook"
    assert manifest["defaultTargets"] == ["MyBook"]
    assert manifest["require"] == [
        {
            "name": "mathlib",
            "git": "https://github.com/leanprover-community/mathlib4.git",
            "rev": "v4.9.0",
        }
    ]
    assert manifest["lean_lib"] == [{"name": "MyBook"}]
    assert (DEMO / "lean-toolchain").read_text().strip() == "leanprover/lean4:v4.9.0"


def test_demo_has_a_root_module_and_no_stale_schema_reference():
    root_module = (DEMO / "MyBook.lean").read_text()
    assert "import MyBook.Convex" in root_module
    targets = (DEMO / "targets.yaml").read_text()
    assert "internal/references/plan-json-schema.md" in targets
    assert "autoform-extract" not in targets


def test_demo_workspace_scanner_reports_the_intended_gaps():
    environment = os.environ.copy()
    environment["PATH"] = ""
    process = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "workspace_inspector.py"), str(DEMO)],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    report = json.loads(process.stdout)
    assert report["lean_file_count"] == 2
    assert report["declaration_count"] == 3
    assert report["sorry_count"] == 2
    assert report["axiom_count"] == 1


def test_project_bootstrap_commands_use_a_portable_locale():
    bootstrap = (ROOT / "scripts" / "make_project.sh").read_text()
    exporter = (ROOT / "scripts" / "export_blueprint.py").read_text()
    assert "LC_ALL=C LANG=C lake exe cache get" in bootstrap
    assert "LC_ALL=C LANG=C lake build" in bootstrap
    assert "LC_ALL=C LANG=C $(LAKE) update" in exporter
    assert "LC_ALL=C LANG=C $(LAKE) exe cache get" in exporter


def test_project_creation_is_internal_to_setup():
    setup = (ROOT / "skills" / "setup" / "SKILL.md").read_text()
    assert not (ROOT / "skills" / "make-project" / "SKILL.md").exists()
    assert "scripts/make_project.sh" in setup
    assert (ROOT / "scripts" / "make_project.sh").is_file()


def test_public_skill_surface_is_exact_and_internal_workflows_are_preserved():
    public = {
        path.parent.name
        for path in (ROOT / "skills").glob("*/SKILL.md")
    }
    assert public == {
        "setup",
        "roadmap",
        "orchestrate",
        "evaluate",
        "agent-review",
        "human-review",
        "develop-plugin",
    }

    setup = (ROOT / "skills" / "setup" / "SKILL.md").read_text()
    roadmap = (ROOT / "skills" / "roadmap" / "SKILL.md").read_text()
    orchestrate = (ROOT / "skills" / "orchestrate" / "SKILL.md").read_text()
    evaluate = (ROOT / "skills" / "evaluate" / "SKILL.md").read_text()
    assert "internal/runbooks/planning.md" in roadmap
    assert "scripts/workspace_inspector.py" in setup
    assert "internal/runbooks/proving.md" in orchestrate
    assert "internal/runbooks/review.md" in orchestrate
    assert "scripts/backend_config.py" in orchestrate
    assert "scripts/evaluate.py" in evaluate

    for path in (ROOT / "internal" / "runbooks").glob("*.md"):
        assert not path.read_text().lstrip().startswith("---")


def test_environment_installer_resolves_the_new_top_level_script_location():
    installer = (ROOT / "scripts" / "install_autoform.sh").read_text()
    assert 'cd "$(dirname "$0")/.." && pwd' in installer
    assert '--project "$AUTOFORM_RESOLVED_ROOT"' in installer
    assert "uv tool install lean-lsp-mcp" in installer


def test_project_creation_helper_runs_from_an_arbitrary_directory(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"

    commands = {
        "git": """#!/bin/sh
if [ "$1" = "clone" ]; then
  /bin/mkdir -p "$3/scripts"
  : > "$3/scripts/customize_template.py"
fi
printf 'git %s\n' "$*" >> "$AUTOFORM_TEST_LOG"
""",
        "python3": """#!/bin/sh
printf 'python3 %s\n' "$*" >> "$AUTOFORM_TEST_LOG"
""",
        "lake": """#!/bin/sh
printf 'lake %s\n' "$*" >> "$AUTOFORM_TEST_LOG"
""",
    }
    for name, body in commands.items():
        executable = fake_bin / name
        executable.write_text(body)
        executable.chmod(0o755)

    working_dir = tmp_path / "working"
    working_dir.mkdir()
    target = tmp_path / "CreatedProject"
    environment = os.environ.copy()
    environment["AUTOFORM_TEST_LOG"] = str(call_log)
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"

    process = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "make_project.sh"), "CreatedProject", str(target)],
        cwd=working_dir,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert process.returncode == 0, process.stderr
    calls = call_log.read_text()
    assert f"python3 {ROOT / 'scripts' / 'formalization.py'} init ." in calls
    assert "python3 scripts/customize_template.py CreatedProject" in calls
    assert "lake exe cache get" in calls
    assert "lake build" in calls
