from __future__ import annotations

import json
from pathlib import Path


def test_lean_beam_pin_is_immutable_and_explicit(repo_root: Path) -> None:
    lock = json.loads((repo_root / "lean-beam.lock.json").read_text(encoding="utf-8"))

    assert lock == {
        "repository": "https://github.com/leanprover/lean-beam.git",
        "commit": "d5dc8fe9d3928899bf55968a93d9e309d9fad1bc",
        "version": "0.2.0-beta",
        "mcp_protocol": "2026-07-28",
        "tested_toolchains": [
            "leanprover/lean4:v4.32.2",
            "leanprover/lean4:v4.33.0",
        ],
        "development_only": True,
    }
    workflow = (repo_root / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )
    assert f"ref: {lock['commit']}" in workflow
    for toolchain in lock["tested_toolchains"]:
        assert toolchain in workflow
