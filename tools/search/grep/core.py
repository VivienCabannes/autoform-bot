"""Grep / Content Search — regex search across file contents.

No MCP dependencies. Attempts to use ripgrep (rg) for speed,
falls back to a pure-Python implementation.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess

DEFAULT_MAX_RESULTS = 200
DEFAULT_SUBPROCESS_TIMEOUT = 30


class GrepSearch:
    """Regex content search scoped to allowed directories."""

    def __init__(self, allowed_dirs: list[str]) -> None:
        self.allowed_dirs = allowed_dirs
        self._rg_path = shutil.which("rg")

    def _validate_path(self, path: str) -> str:
        """Ensure path is within allowed directories."""
        resolved = os.path.realpath(path)
        for d in self.allowed_dirs:
            allowed = os.path.realpath(d)
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return resolved
        raise PermissionError(f"Access denied — {resolved} is outside allowed directories")

    def _rg_search(
        self,
        pattern: str,
        path: str,
        *,
        glob: str = "",
        file_type: str = "",
        context: int = 0,
        max_results: int = DEFAULT_MAX_RESULTS,
        case_insensitive: bool = False,
        multiline: bool = False,
        literal: bool = False,
        invert_match: bool = False,
        word_boundary: bool = False,
        count_only: bool = False,
        json_output: bool = False,
    ) -> str:
        """Search using ripgrep."""
        cmd = [self._rg_path, "--no-heading", "--line-number", "--color=never"]
        if case_insensitive:
            cmd.append("-i")
        if multiline:
            cmd.extend(["-U", "--multiline-dotall"])
        if literal:
            cmd.append("-F")
        if invert_match:
            cmd.append("-v")
        if word_boundary:
            cmd.append("-w")
        if count_only:
            cmd.append("--count")
        if json_output:
            cmd.append("--json")
        if context > 0:
            cmd.extend(["-C", str(context)])
        if glob:
            cmd.extend(["--glob", glob])
        if file_type:
            cmd.extend(["--type", file_type])
        if not count_only:
            cmd.extend(["-m", str(max_results)])
        cmd.append(pattern)
        cmd.append(path)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_SUBPROCESS_TIMEOUT)
        return result.stdout if result.stdout else "(no matches)"

    def _py_search(
        self,
        pattern: str,
        path: str,
        *,
        glob_filter: str = "",
        context: int = 0,
        max_results: int = DEFAULT_MAX_RESULTS,
        case_insensitive: bool = False,
        literal: bool = False,
        invert_match: bool = False,
        word_boundary: bool = False,
        count_only: bool = False,
    ) -> str:
        """Fallback pure-Python search."""
        flags = re.IGNORECASE if case_insensitive else 0
        if literal:
            pattern = re.escape(pattern)
        if word_boundary:
            pattern = rf"\b{pattern}\b"
        regex = re.compile(pattern, flags)
        results: list[str] = []
        counts: dict[str, int] = {}
        root = path

        if os.path.isfile(root):
            items = [(os.path.dirname(root), [], [os.path.basename(root)])]
        else:
            items = os.walk(root)

        for dirpath, _dirs, files in items:
            for fname in files:
                if glob_filter and not fnmatch.fnmatch(fname, glob_filter):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except (OSError, UnicodeDecodeError):
                    continue

                file_count = 0
                for lineno, line in enumerate(lines, 1):
                    match = bool(regex.search(line))
                    if invert_match:
                        match = not match
                    if match:
                        file_count += 1
                        if not count_only:
                            if context > 0:
                                start = max(0, lineno - 1 - context)
                                end = min(len(lines), lineno + context)
                                for ctx_lineno in range(start, end):
                                    prefix = ">" if ctx_lineno == lineno - 1 else " "
                                    results.append(f"{fpath}:{ctx_lineno + 1}:{prefix}{lines[ctx_lineno].rstrip()}")
                                results.append("--")
                            else:
                                results.append(f"{fpath}:{lineno}: {line.rstrip()}")

                            if len(results) >= max_results:
                                break

                if count_only and file_count > 0:
                    counts[fpath] = file_count

                if not count_only and len(results) >= max_results:
                    break
            if not count_only and len(results) >= max_results:
                break

        if count_only:
            if not counts:
                return "(no matches)"
            return "\n".join(f"{f}:{c}" for f, c in sorted(counts.items()))

        return "\n".join(results) if results else "(no matches)"

    def grep(
        self,
        pattern: str,
        path: str = "",
        glob: str = "",
        file_type: str = "",
        context: int = 0,
        max_results: int = DEFAULT_MAX_RESULTS,
        case_insensitive: bool = False,
        multiline: bool = False,
        literal: bool = False,
        invert_match: bool = False,
        word_boundary: bool = False,
        count_only: bool = False,
        json_output: bool = False,
    ) -> str:
        """Search file contents with a regex pattern.

        Uses ripgrep if available, otherwise falls back to pure-Python.

        Args:
            pattern: Regex pattern to search for.
            path: File or directory to search in. Defaults to first allowed dir.
            glob: Glob pattern to filter filenames (e.g. "*.py", "*.lean").
            file_type: File type filter (e.g. "py", "lean"). Ripgrep built-in types.
            context: Number of context lines before and after each match.
            max_results: Maximum number of matching lines to return.
            case_insensitive: Case-insensitive search.
            multiline: Enable multiline matching (ripgrep only).
            literal: Treat pattern as literal string, not regex.
            invert_match: Return lines that do NOT match the pattern.
            word_boundary: Match whole words only.
            count_only: Return match counts per file instead of matched lines.
            json_output: Return results in JSON format (ripgrep only).
        """
        search_path = self._validate_path(path) if path else self._validate_path(self.allowed_dirs[0])

        if self._rg_path:
            return self._rg_search(
                pattern,
                search_path,
                glob=glob,
                file_type=file_type,
                context=context,
                max_results=max_results,
                case_insensitive=case_insensitive,
                multiline=multiline,
                literal=literal,
                invert_match=invert_match,
                word_boundary=word_boundary,
                count_only=count_only,
                json_output=json_output,
            )
        if json_output:
            return "Error: JSON output requires ripgrep (rg) to be installed."
        if multiline:
            return "Error: Multiline matching requires ripgrep (rg) to be installed."
        return self._py_search(
            pattern,
            search_path,
            glob_filter=glob,
            context=context,
            max_results=max_results,
            case_insensitive=case_insensitive,
            literal=literal,
            invert_match=invert_match,
            word_boundary=word_boundary,
            count_only=count_only,
        )
