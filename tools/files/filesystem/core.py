# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Filesystem operations — path-validated file and directory operations.

No MCP dependencies. All operations are restricted to allowed directories.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import stat
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MAX_DEPTH = 3


# ANSI color codes for diff visualization (background-only, Claude Code-inspired)
_ANSI_RED = "\033[48;5;52m"  # dark red background, keep original text color
_ANSI_GREEN = "\033[48;5;22m"  # dark green background, keep original text color
_ANSI_CYAN = "\033[2;36m"  # dim cyan
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


def _colored_diff(original: str, modified: str, filepath: str) -> str:
    """Generate an ANSI-colored unified diff between *original* and *modified*.

    Removed lines are shown in red, added lines in green, and hunk
    headers (@@) in cyan.  Returns an empty string when there are no
    differences.
    """
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=filepath,
            tofile=filepath,
        )
    )
    if not diff_lines:
        return ""

    colored: list[str] = []
    for line in diff_lines:
        stripped = line.rstrip("\n")
        if line.startswith("---") or line.startswith("+++"):
            colored.append(f"{_ANSI_DIM}{stripped}{_ANSI_RESET}")
        elif line.startswith("@@"):
            colored.append(f"{_ANSI_CYAN}{stripped}{_ANSI_RESET}")
        elif line.startswith("-"):
            colored.append(f"{_ANSI_RED}{stripped}{_ANSI_RESET}")
        elif line.startswith("+"):
            colored.append(f"{_ANSI_GREEN}{stripped}{_ANSI_RESET}")
        else:
            colored.append(stripped)

    return "\n" + "\n".join(colored)


class FilesystemOps:
    """File and directory operations scoped to allowed directories."""

    def __init__(
        self,
        allowed_dirs: Sequence[str],
        write_excluded_dirs: Sequence[str] = (),
        extra_read_dirs: Sequence[str] = (),
    ) -> None:
        self.allowed_dirs = allowed_dirs
        self.write_excluded_dirs = write_excluded_dirs
        self.extra_read_dirs = extra_read_dirs

    def _validate(self, raw_path: str, *, write: bool = False) -> Path:
        """Resolve a path and ensure it falls within an allowed or readable directory.

        Checks *allowed_dirs* first (read+write), then *extra_read_dirs* (read-only).
        When *write* is True, rejects paths inside *write_excluded_dirs* (a subset of
        allowed_dirs) and paths inside *extra_read_dirs*.
        """
        resolved = Path(raw_path).resolve()
        # Also normalize the path without following symlinks, so that paths
        # through symlinks inside allowed dirs are still permitted.
        normalized = Path(os.path.normpath(Path(raw_path).absolute()))

        for d in self.allowed_dirs:
            allowed = Path(d).resolve()
            if resolved.is_relative_to(allowed) or normalized.is_relative_to(allowed):
                if write:
                    for ew in self.write_excluded_dirs:
                        ew_resolved = Path(ew).resolve()
                        ew_normalized = Path(os.path.normpath(Path(ew).absolute()))
                        if resolved.is_relative_to(ew_resolved) or normalized.is_relative_to(ew_normalized):
                            raise PermissionError(
                                f"Write denied: {raw_path!r} is inside write-excluded directory {ew!r}."
                            )
                return resolved

        for d in self.extra_read_dirs:
            extra = Path(d).resolve()
            if resolved.is_relative_to(extra) or normalized.is_relative_to(extra):
                if write:
                    raise PermissionError(f"Write denied: {raw_path!r} is inside read-only directory {d!r}.")
                return resolved

        raise PermissionError(
            f"Access denied: {raw_path!r} is outside allowed directories "
            f"{self.allowed_dirs}. Call list_allowed_directories to discover "
            f"valid roots."
        )

    def read_text_file(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        p = self._validate(path)
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total = len(lines)

        start = max(0, offset) if offset is not None else 0
        end = start + limit if limit is not None else total
        selected = lines[start:end]

        # Add line numbers (1-indexed)
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:6}\t{line.rstrip()}")
        result = "\n".join(numbered)

        remaining = total - end
        if remaining > 0:
            result += f"\n\n[truncated: {remaining} more lines. Use offset/limit to read more.]"

        return result

    def read_multiple_files(self, paths: list[str]) -> str:
        results: list[str] = []
        for path in paths:
            try:
                p = self._validate(path)
                content = p.read_text(encoding="utf-8", errors="replace")
                results.append(f"--- {p} ---\n{content}")
            except PermissionError as e:
                results.append(f"--- {path} ---\nError: {e}")
            except FileNotFoundError:
                results.append(f"--- {path} ---\nError: File not found")
            except Exception as e:
                results.append(f"--- {path} ---\nError: {e}")
        return "\n\n".join(results)

    def write_file(self, path: str, content: str) -> str:
        p = self._validate(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {p}"

    def edit_file(self, path: str, edits: list[dict], dry_run: bool = False) -> str:
        p = self._validate(path, write=not dry_run)
        original = p.read_text(encoding="utf-8", errors="replace")
        modified = original
        for edit in edits:
            old_text = edit["old_text"]
            new_text = edit["new_text"]
            if old_text not in modified:
                return f"Error: old_text not found in {p}:\n{old_text!r}"
            modified = modified.replace(old_text, new_text, 1)

        if dry_run:
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                modified.splitlines(keepends=True),
                fromfile=str(p),
                tofile=str(p),
            )
            return "".join(diff) or "(no changes)"

        p.write_text(modified, encoding="utf-8")
        diff_viz = _colored_diff(original, modified, str(p))
        return f"Applied {len(edits)} edit(s) to {p}{diff_viz}"

    def list_directory(self, path: str) -> str:
        p = self._validate(path)
        if not p.is_dir():
            return f"Error: {p} is not a directory"
        entries: list[str] = []
        for child in sorted(p.iterdir()):
            prefix = "[DIR] " if child.is_dir() else "[FILE] "
            entries.append(prefix + child.name)
        return "\n".join(entries) if entries else "(empty directory)"

    def directory_tree(self, path: str, max_depth: int = DEFAULT_MAX_DEPTH) -> str:
        root = self._validate(path)
        if not root.is_dir():
            return f"Error: {root} is not a directory"

        lines: list[str] = [str(root)]

        def _walk(dir_path: Path, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                children = sorted(dir_path.iterdir())
            except PermissionError:
                return
            for i, child in enumerate(children):
                is_last = i == len(children) - 1
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{child.name}{'/' if child.is_dir() else ''}")
                if child.is_dir():
                    extension = "    " if is_last else "│   "
                    _walk(child, prefix + extension, depth + 1)

        _walk(root, "", 1)
        return "\n".join(lines)

    def search_files(self, path: str, pattern: str) -> str:
        root = self._validate(path)
        if not root.is_dir():
            return f"Error: {root} is not a directory"
        matches: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                if fnmatch.fnmatch(fname, pattern):
                    matches.append(os.path.join(dirpath, fname))
        return "\n".join(matches) if matches else "(no matches)"

    def file_grep(self, path: str, pattern: str, include: str = "") -> str:
        root = self._validate(path)
        regex = re.compile(pattern)
        results: list[str] = []

        if root.is_file():
            items = [(str(root.parent), [], [root.name])]
        else:
            items = os.walk(root)

        for dirpath, _dirs, files in items:
            for fname in files:
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append(f"{fpath}:{lineno}: {line.rstrip()}")
                except (OSError, UnicodeDecodeError):
                    continue

        return "\n".join(results) if results else "(no matches)"

    def get_file_info(self, path: str) -> str:
        p = self._validate(path)
        try:
            st = p.stat()
        except FileNotFoundError:
            return f"Error: {p} does not exist"
        info = {
            "path": str(p),
            "size": st.st_size,
            "is_file": p.is_file(),
            "is_dir": p.is_dir(),
            "created": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
            "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "accessed": datetime.fromtimestamp(st.st_atime, tz=timezone.utc).isoformat(),
            "permissions": stat.filemode(st.st_mode),
        }
        return "\n".join(f"{k}: {v}" for k, v in info.items())

    def create_directory(self, path: str) -> str:
        p = self._validate(path, write=True)
        p.mkdir(parents=True, exist_ok=True)
        return f"Created directory {p}"

    def move_file(self, source: str, destination: str) -> str:
        src = self._validate(source, write=True)
        dst = self._validate(destination, write=True)
        src.rename(dst)
        return f"Moved {src} -> {dst}"

    def file_delete(self, path: str) -> str:
        p = self._validate(path, write=True)
        if not p.exists():
            return f"Error: File not found: {p}"
        if p.is_dir():
            return f"Error: Cannot delete directory: {p}"
        try:
            p.unlink()
            return f"Deleted: {p}"
        except Exception as e:
            return f"Error deleting file: {e}"

    def edit_lines(self, path: str, start_line: int, end_line: int, new_content: str) -> str:
        p = self._validate(path, write=True)
        if not p.exists():
            return f"Error: File not found: {p}"
        if p.is_dir():
            return f"Error: Cannot edit directory: {p}"
        if start_line < 1:
            return "Error: start_line must be >= 1"
        if end_line < start_line - 1:
            return "Error: end_line must be >= start_line - 1"

        try:
            original_text = p.read_text(encoding="utf-8", errors="replace")
            lines = original_text.splitlines(keepends=True)
            total_lines = len(lines)

            start_idx = start_line - 1
            end_idx = end_line

            if start_idx > total_lines:
                return f"Error: start_line {start_line} is beyond end of file ({total_lines} lines)"

            end_idx = min(end_idx, total_lines)
            replaced_count = end_idx - start_idx

            if new_content == "":
                new_lines = []
            else:
                new_lines = [line + "\n" for line in new_content.split("\n")]

            lines[start_idx:end_idx] = new_lines
            modified_text = "".join(lines)
            p.write_text(modified_text, encoding="utf-8")

            new_total = len(lines)
            action = "Inserted" if replaced_count == 0 else "Replaced"
            range_desc = (
                f"at line {start_line}"
                if replaced_count == 0
                else f"lines {start_line}-{start_line + replaced_count - 1}"
            )

            diff_viz = _colored_diff(original_text, modified_text, str(p))
            return (
                f"{action} {range_desc} in {p}: "
                f"{replaced_count} lines removed, {len(new_lines)} lines added. "
                f"File: {total_lines} -> {new_total} lines.{diff_viz}"
            )
        except Exception as e:
            return f"Error editing file: {e}"
