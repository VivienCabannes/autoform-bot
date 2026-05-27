# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Filesystem MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import FilesystemOps


@dataclass(frozen=True)
class FilesystemConfig:
    """Configuration for the filesystem tool."""

    allowed_dirs: tuple[str, ...]
    write_excluded_dirs: tuple[str, ...] = ()
    extra_read_dirs: tuple[str, ...] = ()


def create_filesystem_server(
    allowed_dirs: Sequence[str],
    write_excluded_dirs: Sequence[str] = (),
    extra_read_dirs: Sequence[str] = (),
) -> FastMCP:
    """Create a FastMCP server with filesystem tools scoped to *allowed_dirs*."""
    ops = FilesystemOps(allowed_dirs, write_excluded_dirs=write_excluded_dirs, extra_read_dirs=extra_read_dirs)
    server = FastMCP(name="filesystem")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ, max_result_chars=float("inf"))
    def read_text_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
        """Read a text file with line numbers.

        By default reads the entire file. Use offset/limit to paginate through larger files.

        Args:
            path: Path to the file to read.
            offset: Start reading from this line (0-indexed). Defaults to beginning.
            limit: Maximum number of lines to return. Defaults to all lines.
        """
        return ops.read_text_file(path, offset=offset, limit=limit)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ, max_result_chars=float("inf"))
    def read_multiple_files(paths: list[str]) -> str:
        """Read multiple files at once. More efficient than multiple read_text_file calls.

        Returns each file's contents separated by a header with the file path.

        Args:
            paths: List of file paths to read.
        """
        return ops.read_multiple_files(paths)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def write_file(path: str, content: str | None = None) -> str:
        """Write content to a file (creates parent directories as needed).

        Both `path` and `content` are required. `content` is the full text to write.

        Prefer edit_file or edit_lines over write_file for modifying existing
        files — they produce diffs and are less error-prone.
        """
        if content is None:
            return "Error: 'content' is required. Provide the full file text as the 'content' argument."
        return ops.write_file(path, content)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def edit_file(path: str, edits: list[dict], dry_run: bool = False) -> str:
        """Apply find-and-replace edits to a file.

        Each element of *edits* must have ``old_text`` and ``new_text`` keys.
        Returns a unified-diff-style preview when *dry_run* is True.
        """
        return ops.edit_file(path, edits, dry_run=dry_run)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def list_directory(path: str) -> str:
        """List entries in a directory (non-recursive)."""
        return ops.list_directory(path)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def directory_tree(path: str, max_depth: int = 3) -> str:
        """Show a recursive tree view of a directory."""
        return ops.directory_tree(path, max_depth=max_depth)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def search_files(path: str, pattern: str) -> str:
        """Recursively search for files matching a glob/fnmatch pattern."""
        return ops.search_files(path, pattern)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def file_grep(path: str, pattern: str, include: str = "") -> str:
        """Search file contents with a regex pattern.

        Walks the directory tree under *path*, optionally filtering filenames
        with *include* (a glob pattern like ``*.py``).
        """
        return ops.file_grep(path, pattern, include=include)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_file_info(path: str) -> str:
        """Return file metadata (size, modified time, permissions, etc.)."""
        return ops.get_file_info(path)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def create_directory(path: str) -> str:
        """Create a directory (and any missing parents)."""
        return ops.create_directory(path)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def move_file(source: str, destination: str) -> str:
        """Move or rename a file/directory."""
        return ops.move_file(source, destination)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def file_delete(path: str) -> str:
        """Delete a file.

        Only files can be deleted (not directories). The file must be within
        an allowed directory.
        """
        return ops.file_delete(path)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def edit_lines(path: str, start_line: int, end_line: int, new_content: str) -> str:
        """Replace a range of lines in a file with new content.

        More reliable than exact string matching when agents can't reproduce
        exact whitespace. Uses line numbers (1-based, inclusive).

        To insert without replacing, set end_line = start_line - 1.
        To delete lines, pass empty string as new_content.
        """
        if not isinstance(start_line, int) or not isinstance(end_line, int):
            return "Error: start_line and end_line must be integers"
        if start_line < 1:
            return "Error: start_line must be >= 1"
        if end_line < start_line - 1:
            return "Error: end_line must be >= start_line - 1"
        return ops.edit_lines(path, start_line, end_line, new_content)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    def list_allowed_directories() -> str:
        """Return the list of directories this server is allowed to access."""
        return "\n".join(list(ops.allowed_dirs) + list(ops.extra_read_dirs))

    return server


def filesystem_server(config: FilesystemConfig) -> MCPServerConfig:
    """Create a filesystem MCP server config."""
    mcp_instance = create_filesystem_server(
        config.allowed_dirs,
        write_excluded_dirs=config.write_excluded_dirs,
        extra_read_dirs=config.extra_read_dirs,
    )
    return MCPServerConfig(
        server_key="filesystem",
        description="File read/write/search operations scoped to allowed directories",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
