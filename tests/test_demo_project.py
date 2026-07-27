"""Regression checks for the bundled cold-start demo project."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-project"


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
    assert "skills/plan/references/plan-json-schema.md" in targets
    assert "autoform-extract" not in targets


def test_demo_workspace_scanner_reports_the_intended_gaps():
    environment = os.environ.copy()
    environment["PATH"] = ""
    process = subprocess.run(
        [sys.executable, str(ROOT / "skills" / "workspace" / "inspect.py"), str(DEMO)],
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
    bootstrap = (ROOT / "skills" / "make-project" / "make-project.sh").read_text()
    exporter = (ROOT / "scripts" / "export_blueprint.py").read_text()
    assert "LC_ALL=C LANG=C lake exe cache get" in bootstrap
    assert "LC_ALL=C LANG=C lake build" in bootstrap
    assert "LC_ALL=C LANG=C $(LAKE) update" in exporter
    assert "LC_ALL=C LANG=C $(LAKE) exe cache get" in exporter
