# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for core.mcp.tool_runtime — call_id sanitization and budgeting helpers."""

from pathlib import Path
from unittest.mock import MagicMock

from core.mcp.tool_runtime import MCPToolRuntime, PERSISTENCE_MARKER


def _make_runtime(tmp_path: Path) -> MCPToolRuntime:
    """Create a minimal MCPToolRuntime with persist_dir for testing."""
    manager = MagicMock()
    return MCPToolRuntime(manager=manager, persist_dir=tmp_path)


class TestCallIdSanitization:
    """_persist_result must sanitize call_id to prevent path traversal."""

    def test_safe_call_id_preserved(self, tmp_path: Path):
        runtime = _make_runtime(tmp_path)
        result = runtime._persist_result("call_abc-123", "hello world")
        assert PERSISTENCE_MARKER in result
        written = (tmp_path / "tool-results" / "call_abc-123.txt").read_text()
        assert written == "hello world"

    def test_path_traversal_sanitized(self, tmp_path: Path):
        runtime = _make_runtime(tmp_path)
        result = runtime._persist_result("../../etc/passwd", "malicious content")
        assert PERSISTENCE_MARKER in result
        # Must NOT write outside tool-results/
        assert not (tmp_path / ".." / ".." / "etc" / "passwd.txt").exists()
        # Sanitized filename should exist
        written_files = list((tmp_path / "tool-results").iterdir())
        assert len(written_files) == 1
        assert written_files[0].name == "______etc_passwd.txt"

    def test_dots_and_slashes_replaced(self, tmp_path: Path):
        runtime = _make_runtime(tmp_path)
        runtime._persist_result("foo/bar.baz", "content")
        written_files = list((tmp_path / "tool-results").iterdir())
        assert len(written_files) == 1
        assert written_files[0].name == "foo_bar_baz.txt"

    def test_special_chars_replaced(self, tmp_path: Path):
        runtime = _make_runtime(tmp_path)
        runtime._persist_result("call$(rm -rf /)", "content")
        written_files = list((tmp_path / "tool-results").iterdir())
        assert len(written_files) == 1
        # Only alphanumeric, underscore, and hyphen should survive
        name = written_files[0].stem
        assert all(c.isalnum() or c in "_-" for c in name)

    def test_empty_call_id(self, tmp_path: Path):
        runtime = _make_runtime(tmp_path)
        result = runtime._persist_result("", "content")
        assert PERSISTENCE_MARKER in result
        written_files = list((tmp_path / "tool-results").iterdir())
        assert len(written_files) == 1
