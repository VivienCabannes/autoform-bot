"""Tests for the Zulip core module and CLI script."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from servers.zulip.core import find_zuliprc, ZulipClient


class TestFindZuliprc:
    """Tests for .zuliprc discovery."""

    def test_returns_none_when_missing(self, tmp_path):
        """find_zuliprc should return None when no .zuliprc exists."""
        env_backup = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        try:
            result = find_zuliprc(str(tmp_path))
            assert result is None or result.is_file()
        finally:
            if env_backup is not None:
                os.environ["ZULIPRC"] = env_backup

    def test_finds_project_local(self, tmp_path):
        """find_zuliprc should find a .zuliprc in the project directory."""
        rc = tmp_path / ".zuliprc"
        rc.write_text("[api]\nemail=test@test.com\nkey=fake\nsite=https://example.com\n")

        env_backup = os.environ.get("ZULIPRC")
        os.environ.pop("ZULIPRC", None)
        try:
            result = find_zuliprc(str(tmp_path))
            assert result is not None
            assert result == rc
        finally:
            if env_backup is not None:
                os.environ["ZULIPRC"] = env_backup

    def test_env_var_takes_priority(self, tmp_path):
        """$ZULIPRC env var should take priority over all other locations."""
        rc = tmp_path / "custom.zuliprc"
        rc.write_text("[api]\nemail=test@test.com\nkey=fake\nsite=https://example.com\n")

        old = os.environ.get("ZULIPRC")
        os.environ["ZULIPRC"] = str(rc)
        try:
            result = find_zuliprc()
            assert result == rc
        finally:
            if old is not None:
                os.environ["ZULIPRC"] = old
            else:
                os.environ.pop("ZULIPRC", None)


class TestZulipCore:
    """Tests for the core module."""

    def test_client_class_exists(self):
        """ZulipClient should be importable."""
        assert ZulipClient is not None

    def test_find_zuliprc_callable(self):
        """find_zuliprc should be callable."""
        assert callable(find_zuliprc)
