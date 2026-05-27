# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Mathlib source search — find declarations, grep source, read files.

No MCP dependencies. Pure search logic over local Mathlib source.
"""

from __future__ import annotations

import subprocess
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)


DEFAULT_MAX_RESULTS = 50
DEFAULT_SUBPROCESS_TIMEOUT = 30
DEFAULT_MAX_NAME_RESULTS = 30


def find_mathlib_path(repo_root: Path) -> Path | None:
    """Find the Mathlib installation path from a Lean project.

    Checks lakefile.toml for a local path entry first,
    then falls back to .lake/packages/mathlib.
    """
    lakefile_toml = repo_root / "lakefile.toml"
    if lakefile_toml.exists():
        try:
            from core.compat import tomllib

            with open(lakefile_toml, "rb") as f:
                lakefile = tomllib.load(f)

            for req in lakefile.get("require", []):
                if req.get("name") == "mathlib" and "path" in req:
                    local_path = (repo_root / req["path"]).resolve()
                    if local_path.exists() and (local_path / "Mathlib").exists():
                        return local_path
        except Exception:
            pass

    mathlib_path = repo_root / ".lake" / "packages" / "mathlib"
    if mathlib_path.exists() and (mathlib_path / "Mathlib").exists():
        return mathlib_path

    return None


def grep_mathlib(
    repo_root: Path,
    pattern: str,
    kind: str = "",
    subdir: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
    context_lines: int = 0,
    literal: bool = False,
) -> str:
    """Search Mathlib source code using ripgrep."""
    mathlib_path = find_mathlib_path(repo_root)
    if not mathlib_path:
        return "Error: Mathlib not found. Check lakefile.toml or .lake/packages/mathlib."

    search_path = mathlib_path / "Mathlib"
    if subdir:
        search_path = search_path / subdir

    if not search_path.exists():
        return f"Error: Path not found: {search_path}"

    cmd = ["rg", "--line-number", "-m", str(max_results)]

    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    if literal:
        cmd.append("-F")
    if kind:
        cmd.extend(["--regexp", f"^{kind}\\s+.*{pattern}"])
    else:
        cmd.append(pattern)

    cmd.extend(["-g", "*.lean", str(search_path)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_SUBPROCESS_TIMEOUT)
        output = result.stdout
        if not output:
            return "No matches found"
        lines = output.strip().split("\n")
        count = len([line for line in lines if line and not line.startswith("--")])
        return f"Found {count} matches:\n\n{output}"
    except subprocess.TimeoutExpired:
        return "Error: Search timed out"
    except FileNotFoundError:
        return "Error: ripgrep (rg) not found. Please install it."


def find_name_in_mathlib(
    repo_root: Path,
    name: str,
    exact: bool = False,
    max_results: int = DEFAULT_MAX_NAME_RESULTS,
) -> str:
    """Find a theorem, lemma, or definition by name in Mathlib."""
    pattern = f"\\b{name}\\b" if exact else name
    return grep_mathlib(repo_root, pattern=pattern, max_results=max_results)


def read_mathlib_file(
    repo_root: Path,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a Mathlib source file with optional line range."""
    mathlib_path = find_mathlib_path(repo_root)
    if not mathlib_path:
        return "Error: Mathlib not found"

    full_path = mathlib_path / file_path
    if not full_path.exists():
        return f"Error: File not found: {file_path}"

    try:
        content = full_path.read_text()
    except Exception as e:
        return f"Error reading file: {e}"

    lines = content.split("\n")
    total_lines = len(lines)

    start = (start_line - 1) if start_line else 0
    end = end_line if end_line else len(lines)
    selected = lines[start:end]

    numbered = [f"{i + start + 1:6}  {line}" for i, line in enumerate(selected)]

    header = f"# {file_path} ({total_lines} lines)"
    if start_line or end_line:
        header += f" [lines {start + 1}-{end}]"

    return header + "\n" + "\n".join(numbered)
