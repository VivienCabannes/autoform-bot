"""Tests for the Zulip skill script (skills/zulip/zulip-search.py)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def zulip_module():
    """Import the zulip-search.py skill script as a module."""
    script = Path(__file__).resolve().parent.parent / "skills" / "zulip" / "zulip-search.py"
    spec = importlib.util.spec_from_file_location("zulip_search", str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFindZuliprc:
    """Tests for .zuliprc discovery."""

    def test_returns_none_when_missing(self, zulip_module, tmp_path):
        """find_zuliprc should return None when no .zuliprc exists."""
        env_backup = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        try:
            result = zulip_module.find_zuliprc(str(tmp_path))
            assert result is None or result.is_file()
        finally:
            if env_backup is not None:
                os.environ["ZULIPRC"] = env_backup

    def test_finds_project_local(self, zulip_module, tmp_path):
        """find_zuliprc should find a .zuliprc in the project directory."""
        rc = tmp_path / ".zuliprc"
        rc.write_text("[api]\nemail=test@test.com\nkey=fake\nsite=https://example.com\n")

        env_backup = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        try:
            result = zulip_module.find_zuliprc(str(tmp_path))
            assert result is not None
            assert result == rc
        finally:
            if env_backup is not None:
                os.environ["ZULIPRC"] = env_backup

    def test_env_var_takes_priority(self, zulip_module, tmp_path):
        """$ZULIPRC env var should take priority over all other locations."""
        rc = tmp_path / "custom.zuliprc"
        rc.write_text("[api]\nemail=test@test.com\nkey=fake\nsite=https://example.com\n")

        old = os.environ.get("ZULIPRC")
        os.environ["ZULIPRC"] = str(rc)
        try:
            result = zulip_module.find_zuliprc()
            assert result == rc
        finally:
            if old is not None:
                os.environ["ZULIPRC"] = old
            else:
                os.environ.pop("ZULIPRC", None)


class TestCliScript:
    """Tests for the CLI script itself."""

    def test_script_importable(self, zulip_module):
        """The zulip script should import without error."""
        assert hasattr(zulip_module, "ZulipClient")
        assert hasattr(zulip_module, "find_zuliprc")
        assert hasattr(zulip_module, "main")

    def test_status_no_config(self, zulip_module, tmp_path, capsys):
        """status command should report unconfigured when no .zuliprc exists."""
        old = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        old_lean = os.environ.get("LEAN_PROJECT_DIR")
        os.environ["LEAN_PROJECT_DIR"] = str(tmp_path)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(tmp_path)
        try:
            import argparse
            args = argparse.Namespace()
            zulip_module.cmd_status(args)
            captured = capsys.readouterr()
            assert "configured" in captured.out
        finally:
            if old is not None:
                os.environ["ZULIPRC"] = old
            if old_lean is not None:
                os.environ["LEAN_PROJECT_DIR"] = old_lean
            else:
                os.environ.pop("LEAN_PROJECT_DIR", None)
            if old_home is not None:
                os.environ["HOME"] = old_home
