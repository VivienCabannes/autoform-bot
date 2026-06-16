"""Mathlib source search — pure search logic over local Mathlib source.

No MCP dependencies. Uses ripgrep when a real `rg` binary is available,
otherwise falls back to a pure-Python search so the plugin works on any
machine without external dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

DEFAULT_MAX_RESULTS = 50
DEFAULT_SUBPROCESS_TIMEOUT = 30
DEFAULT_MAX_NAME_RESULTS = 30


def _find_rg() -> str | None:
    """Locate a real ripgrep binary, or None if unavailable.

    Note: shutil.which only finds real executables, not shell functions/aliases
    (some environments shim `rg` as a shell function, which subprocess can't use).
    """
    return shutil.which("rg")


def find_mathlib_path(repo_root: Path) -> Path | None:
    """Find the Mathlib installation path from a Lean project.

    Resolution order:
      1. An explicit override env var (LEAN_PLANNER_MATHLIB or MATHLIB_PATH)
         pointing straight at a checkout (a dir containing a Mathlib/ subdir).
      2. repo_root itself being a Mathlib checkout (has a Mathlib/ subdir).
      3. lakefile.toml with a local `require mathlib` path entry.
      4. repo_root/.lake/packages/mathlib (the lake-resolved dependency).
    """
    # 1. Explicit override — most robust, independent of any Lean project layout.
    for env_var in ("LEAN_PLANNER_MATHLIB", "MATHLIB_PATH"):
        override = os.environ.get(env_var)
        if override:
            p = Path(override).expanduser().resolve()
            if (p / "Mathlib").exists():
                return p

    # 2. repo_root is itself a Mathlib checkout.
    if (repo_root / "Mathlib").exists():
        return repo_root

    lakefile_toml = repo_root / "lakefile.toml"
    if lakefile_toml.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        try:
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
    """Search Mathlib source code.

    Uses ripgrep if a real `rg` binary is available, otherwise falls back to a
    pure-Python search.
    """
    mathlib_path = find_mathlib_path(repo_root)
    if not mathlib_path:
        return "Error: Mathlib not found. Check lakefile.toml or .lake/packages/mathlib."

    search_path = mathlib_path / "Mathlib"
    if subdir:
        search_path = search_path / subdir

    if not search_path.exists():
        return f"Error: Path not found: {search_path}"

    rg = _find_rg()
    if rg:
        return _grep_with_rg(rg, search_path, pattern, kind, max_results, context_lines, literal)
    return _grep_with_python(search_path, pattern, kind, max_results, context_lines, literal)


def _grep_with_rg(
    rg: str,
    search_path: Path,
    pattern: str,
    kind: str,
    max_results: int,
    context_lines: int,
    literal: bool,
) -> str:
    """Search using the ripgrep binary (fast path)."""
    cmd = [rg, "--line-number", "-m", str(max_results)]

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


def _grep_with_python(
    search_path: Path,
    pattern: str,
    kind: str,
    max_results: int,
    context_lines: int,
    literal: bool,
) -> str:
    """Search using pure Python (fallback when ripgrep is unavailable).

    Output format mirrors ripgrep: `path:lineno:line`, paths relative to the
    Mathlib root. `max_results` limits the total number of matching lines.
    """
    if literal:
        pattern = re.escape(pattern)
    regex_str = f"^{kind}\\s+.*{pattern}" if kind else pattern
    try:
        regex = re.compile(regex_str)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    # Anchor relative paths at the Mathlib package root (parent of the Mathlib/ dir)
    # so output looks like Mathlib/Topology/Basic.lean.
    rel_root = search_path
    while rel_root.name != "Mathlib" and rel_root.parent != rel_root:
        rel_root = rel_root.parent
    rel_root = rel_root.parent if rel_root.name == "Mathlib" else search_path

    blocks: list[str] = []
    count = 0
    for lean_file in sorted(search_path.rglob("*.lean")):
        if count >= max_results:
            break
        try:
            file_lines = lean_file.read_text(encoding="utf-8", errors="replace").split("\n")
        except Exception:
            continue
        try:
            rel = lean_file.relative_to(rel_root)
        except ValueError:
            rel = lean_file
        for i, line in enumerate(file_lines):
            if count >= max_results:
                break
            if regex.search(line):
                if context_lines > 0:
                    lo = max(0, i - context_lines)
                    hi = min(len(file_lines), i + context_lines + 1)
                    ctx = "\n".join(
                        f"{rel}:{j + 1}:{file_lines[j]}" for j in range(lo, hi)
                    )
                    blocks.append(ctx)
                else:
                    blocks.append(f"{rel}:{i + 1}:{line}")
                count += 1

    if count == 0:
        return "No matches found"

    separator = "\n--\n" if context_lines > 0 else "\n"
    output = separator.join(blocks)
    return f"Found {count} matches:\n\n{output}"


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
