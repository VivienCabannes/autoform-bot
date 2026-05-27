# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for FilesystemOps path validation."""

from __future__ import annotations

import pytest

from .core import FilesystemOps


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a workspace with a nested file."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("hello")
    return tmp_path


@pytest.fixture
def ops(tmp_workspace):
    return FilesystemOps(allowed_dirs=[str(tmp_workspace)])


class TestValidate:
    def test_relative_path_within_allowed_dir(self, ops, tmp_workspace, monkeypatch):
        monkeypatch.chdir(tmp_workspace)
        result = ops._validate("src/main.py")
        assert result == tmp_workspace / "src" / "main.py"

    def test_absolute_path_within_allowed_dir(self, ops, tmp_workspace):
        result = ops._validate(str(tmp_workspace / "src" / "main.py"))
        assert result == tmp_workspace / "src" / "main.py"

    def test_absolute_path_outside_allowed_dirs_raises(self, ops):
        with pytest.raises(PermissionError, match="outside allowed directories"):
            ops._validate("/home/other/user/file.py")

    def test_error_message_includes_allowed_dirs(self, ops, tmp_workspace):
        with pytest.raises(PermissionError, match=str(tmp_workspace)):
            ops._validate("/etc/passwd")

    def test_wrong_filesystem_layout_rejected(self, ops):
        """Path from a different machine's filesystem should not be silently re-rooted."""
        with pytest.raises(PermissionError):
            ops._validate("/home/antml/repos/nimstral/apps/foo.py")

    def test_allowed_dir_itself_is_valid(self, ops, tmp_workspace):
        result = ops._validate(str(tmp_workspace))
        assert result == tmp_workspace


class TestWriteExcludedDirs:
    def test_read_allowed_in_write_excluded_dir(self, tmp_workspace):
        ro_dir = tmp_workspace / "src"
        ops = FilesystemOps(allowed_dirs=[str(tmp_workspace)], write_excluded_dirs=[str(ro_dir)])
        result = ops._validate(str(ro_dir / "main.py"))
        assert result == ro_dir / "main.py"

    def test_write_denied_in_write_excluded_dir(self, tmp_workspace):
        ro_dir = tmp_workspace / "src"
        ops = FilesystemOps(allowed_dirs=[str(tmp_workspace)], write_excluded_dirs=[str(ro_dir)])
        with pytest.raises(PermissionError, match="write-excluded"):
            ops._validate(str(ro_dir / "main.py"), write=True)

    def test_write_allowed_outside_write_excluded_dir(self, tmp_workspace):
        ro_dir = tmp_workspace / "src"
        ops = FilesystemOps(allowed_dirs=[str(tmp_workspace)], write_excluded_dirs=[str(ro_dir)])
        new_file = tmp_workspace / "other.py"
        result = ops._validate(str(new_file), write=True)
        assert result == new_file


class TestExtraReadDirs:
    def test_read_allowed_in_extra_read_dir(self, tmp_path):
        workspace = tmp_path / "work"
        workspace.mkdir()
        extra = tmp_path / "reference"
        extra.mkdir()
        (extra / "notes.txt").write_text("ref")

        ops = FilesystemOps(allowed_dirs=[str(workspace)], extra_read_dirs=[str(extra)])
        result = ops._validate(str(extra / "notes.txt"))
        assert result == extra / "notes.txt"

    def test_write_denied_in_extra_read_dir(self, tmp_path):
        workspace = tmp_path / "work"
        workspace.mkdir()
        extra = tmp_path / "reference"
        extra.mkdir()

        ops = FilesystemOps(allowed_dirs=[str(workspace)], extra_read_dirs=[str(extra)])
        with pytest.raises(PermissionError, match="read-only"):
            ops._validate(str(extra / "notes.txt"), write=True)

    def test_extra_read_does_not_grant_write(self, tmp_path):
        """extra_read_dirs directories are never writable."""
        workspace = tmp_path / "work"
        workspace.mkdir()
        extra = tmp_path / "reference"
        extra.mkdir()

        ops = FilesystemOps(allowed_dirs=[str(workspace)], extra_read_dirs=[str(extra)])
        with pytest.raises(PermissionError, match="read-only"):
            ops._validate(str(extra / "file.txt"), write=True)

    def test_path_outside_both_raises(self, tmp_path):
        workspace = tmp_path / "work"
        workspace.mkdir()
        extra = tmp_path / "reference"
        extra.mkdir()

        ops = FilesystemOps(allowed_dirs=[str(workspace)], extra_read_dirs=[str(extra)])
        with pytest.raises(PermissionError, match="outside allowed directories"):
            ops._validate("/somewhere/else/file.txt")
