"""Glob / Pattern Search — path-validated file pattern matching.

No MCP dependencies. Returns matching file paths sorted by
modification time (most recent first).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MAX_RESULTS = 500


class GlobSearch:
    """File pattern search scoped to allowed directories."""

    def __init__(self, allowed_dirs: list[str]) -> None:
        self.allowed_dirs = allowed_dirs

    def _validate_path(self, path: str) -> str:
        """Ensure path is within allowed directories."""
        resolved = os.path.realpath(path)
        for d in self.allowed_dirs:
            allowed = os.path.realpath(d)
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved
        raise PermissionError(f"Access denied — {resolved} is outside allowed directories")

    def search(
        self,
        pattern: str,
        path: str = "",
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> str:
        """Find files matching a glob pattern.

        Supports patterns like "**/*.py", "src/**/*.ts", "*.json".
        Results are sorted by modification time (most recent first).

        Args:
            pattern: Glob pattern to match files against.
            path: Directory to search in. Defaults to first allowed dir.
            max_results: Maximum number of results to return.
        """
        search_path = Path(self._validate_path(path)) if path else Path(self._validate_path(self.allowed_dirs[0]))

        if not search_path.is_dir():
            return f"Error: {search_path} is not a directory"

        matches: list[tuple[float, str]] = []
        for match in search_path.glob(pattern):
            if match.is_file():
                try:
                    mtime = match.stat().st_mtime
                except OSError:
                    mtime = 0
                matches.append((mtime, str(match)))

        # Sort by modification time, most recent first
        matches.sort(reverse=True)

        if not matches:
            return "(no matches)"

        paths = [p for _, p in matches[:max_results]]
        result = "\n".join(paths)
        if len(matches) > max_results:
            result += f"\n\n({len(matches) - max_results} more results not shown)"
        return result
