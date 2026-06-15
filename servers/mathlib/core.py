"""Mathlib source search — pure search logic over local Mathlib source.

Stub module. See examples/servers/mathlib/core.py for a full implementation
with ripgrep-based search, TOML lakefile parsing, and line-numbered output.
"""

from __future__ import annotations

from pathlib import Path


def find_mathlib_path(repo_root: Path) -> Path | None:
    """Find the Mathlib installation path from a Lean project.

    Checks lakefile.toml for a local path entry first,
    then falls back to .lake/packages/mathlib.

    Args:
        repo_root: Root of the Lean project.

    Returns:
        Path to Mathlib root, or None if not found.
    """
    raise NotImplementedError("See examples/servers/mathlib/core.py for reference implementation.")


def grep_mathlib(
    repo_root: Path,
    pattern: str,
    kind: str = "",
    subdir: str = "",
    max_results: int = 50,
    context_lines: int = 0,
    literal: bool = False,
) -> str:
    """Search Mathlib source code using ripgrep.

    Args:
        repo_root: Root of the Lean project.
        pattern: Search pattern (regex by default).
        kind: Filter by declaration kind (theorem, lemma, def, etc.).
        subdir: Subdirectory to search (e.g. Algebra, Analysis, Topology).
        max_results: Maximum results to return.
        context_lines: Lines of context around matches.
        literal: If true, treat pattern as literal string.

    Returns:
        Formatted match results string.
    """
    raise NotImplementedError("See examples/servers/mathlib/core.py for reference implementation.")


def find_name_in_mathlib(
    repo_root: Path,
    name: str,
    exact: bool = False,
    max_results: int = 30,
) -> str:
    """Find a theorem, lemma, or definition by name in Mathlib.

    Args:
        repo_root: Root of the Lean project.
        name: Name to search for.
        exact: Match exact name only.
        max_results: Maximum results to return.

    Returns:
        Formatted search results string.
    """
    raise NotImplementedError("See examples/servers/mathlib/core.py for reference implementation.")


def read_mathlib_file(
    repo_root: Path,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a Mathlib source file with optional line range.

    Args:
        repo_root: Root of the Lean project.
        file_path: Path relative to Mathlib root.
        start_line: Starting line (1-indexed, optional).
        end_line: Ending line (inclusive, optional).

    Returns:
        Numbered file content string with header.
    """
    raise NotImplementedError("See examples/servers/mathlib/core.py for reference implementation.")
