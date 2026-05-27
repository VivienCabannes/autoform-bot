# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Unit tests for bash server — unlock_commands, approval handler, and restricted mode."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from .server import BashConfig, bash_restricted_server, bash_server, _CMD_ERROR_RE


def _call_bash(config, command: str) -> str:
    """Call the bash tool function directly from the FastMCP server."""
    tool = config.mcp_instance.local_provider._components["tool:bash@"]
    return tool.fn(command=command)


def _call_bash_restricted(config, command: str) -> str:
    """Call the bash_restricted tool function directly from the FastMCP server."""
    tool = config.mcp_instance.local_provider._components["tool:bash_restricted@"]
    return tool.fn(command=command)


class TestUnlockCommands:
    def test_no_unlock_commands_curl_forbidden(self):
        config = bash_server()
        result = _call_bash(config, "curl --help")
        assert "forbidden" in result.lower() or "not in allowlist" in result.lower()

    def test_unlock_commands_curl_allowed(self):
        config = bash_server(BashConfig(default_cwd=".", unlock_commands=["curl"]))
        result = _call_bash(config, "curl --help")
        assert "forbidden" not in result.lower()
        assert "not in allowlist" not in result.lower()

    def test_unlock_multiple_commands(self):
        config = bash_server(BashConfig(default_cwd=".", unlock_commands=["curl", "rm"]))
        result_curl = _call_bash(config, "curl --help")
        assert "forbidden" not in result_curl.lower()
        result_rm = _call_bash(config, "rm --help")
        assert "forbidden" not in result_rm.lower()


class TestApprovalHandler:
    def test_handler_approves_command(self):
        handler = MagicMock(return_value=True)
        config = bash_server(BashConfig(default_cwd=".", command_approval_handler=handler))
        result = _call_bash(config, "curl --help")
        handler.assert_called_once_with("curl", "curl --help")
        assert "forbidden" not in result.lower()
        assert "not in allowlist" not in result.lower()

    def test_handler_denies_command(self):
        handler = MagicMock(return_value=False)
        config = bash_server(BashConfig(default_cwd=".", command_approval_handler=handler))
        result = _call_bash(config, "curl --help")
        handler.assert_called_once_with("curl", "curl --help")
        assert "error" in result.lower()

    def test_handler_does_not_remember_approval(self):
        handler = MagicMock(return_value=True)
        config = bash_server(BashConfig(default_cwd=".", command_approval_handler=handler))
        # First call — handler invoked
        _call_bash(config, "curl --help")
        assert handler.call_count == 1
        # Second call — approval is per-invocation, handler called again
        _call_bash(config, "curl --version")
        assert handler.call_count == 2

    def test_pattern_error_bypasses_handler(self):
        handler = MagicMock(return_value=True)
        config = bash_server(BashConfig(default_cwd=".", command_approval_handler=handler))
        result = _call_bash(config, "echo hello; echo world")
        handler.assert_not_called()
        assert "error" in result.lower()

    def test_chained_commands_multiple_approvals(self):
        handler = MagicMock(return_value=True)
        config = bash_server(BashConfig(default_cwd=".", command_approval_handler=handler))
        result = _call_bash(config, "curl --help && rm --help")
        # Handler should be called for both curl and rm
        assert handler.call_count == 2
        assert "forbidden" not in result.lower()
        assert "not in allowlist" not in result.lower()


class TestCmdErrorRegex:
    def test_matches_forbidden(self):
        m = _CMD_ERROR_RE.search("Command 'curl' is forbidden")
        assert m and m.group(1) == "curl"

    def test_matches_not_in_allowlist(self):
        m = _CMD_ERROR_RE.search("Command 'docker' is not in allowlist")
        assert m and m.group(1) == "docker"

    def test_no_match_pattern_error(self):
        m = _CMD_ERROR_RE.search("Semicolons not allowed (use && for chaining)")
        assert m is None


class TestBashRestricted:
    """Tests for bash_restricted — read-only mode enforcement."""

    @pytest.fixture
    def config(self):
        return bash_restricted_server()

    # --- Output redirects blocked ---

    @pytest.mark.parametrize(
        "command",
        [
            'echo "test" >> file.txt',
            "cat file > other.txt",
            "head -n 5 file > out.txt",
        ],
    )
    def test_redirects_blocked(self, config, command):
        result = _call_bash_restricted(config, command)
        lower = result.lower()
        assert "redirect" in lower or "restricted" in lower

    # --- Git write subcommands blocked ---

    @pytest.mark.parametrize(
        "command",
        [
            "git checkout main",
            "git add .",
            'git commit -m "x"',
            "git push",
            "git pull",
            "git reset --hard HEAD",
            "git rebase main",
            "git cherry-pick abc123",
            "git restore file.txt",
        ],
    )
    def test_git_write_blocked(self, config, command):
        result = _call_bash_restricted(config, command)
        assert "error" in result.lower()
        assert "not allowed" in result.lower()

    # --- Git read subcommands allowed ---

    @pytest.mark.parametrize(
        "subcommand",
        ["log", "diff", "status", "branch", "show", "blame", "rev-parse", "ls-files"],
    )
    def test_git_read_allowed(self, config, subcommand):
        result = _call_bash_restricted(config, f"git {subcommand}")
        assert not result.startswith("Error:")

    # --- Basic read commands allowed ---

    @pytest.mark.parametrize(
        "command",
        ["cat --help", "ls", "echo hello"],
    )
    def test_basic_read_allowed(self, config, command):
        result = _call_bash_restricted(config, command)
        assert not result.startswith("Error:")

    # --- Pipes work ---

    def test_pipes_work(self, config):
        result = _call_bash_restricted(config, "echo hello | grep hello")
        assert not result.startswith("Error:")

    # --- >& file redirect blocked ---

    @pytest.mark.parametrize(
        "command",
        [
            'echo "payload" >& TODO.md',
            "echo test >&file.txt",
        ],
    )
    def test_redirect_ampersand_file_blocked(self, config, command):
        result = _call_bash_restricted(config, command)
        lower = result.lower()
        assert "redirect" in lower or "restricted" in lower

    # --- 2>&1 allowed (stderr redirect to stdout) ---

    def test_stderr_redirect_allowed(self, config):
        result = _call_bash_restricted(config, "git status 2>&1")
        assert not result.startswith("Error:")

    # --- Write commands blocked ---

    @pytest.mark.parametrize(
        "command",
        ["sed -i 's/a/b/' file", "python -c 'print(1)'", "touch newfile", "mkdir newdir", "tee output.txt"],
    )
    def test_write_commands_blocked(self, config, command):
        result = _call_bash_restricted(config, command)
        assert "error" in result.lower()

    # --- env sandbox escape blocked ---

    def test_env_blocked(self, config):
        result = _call_bash_restricted(config, "env python3 -c 'print(1)'")
        assert "not in allowlist" in result.lower()

    def test_env_arbitrary_command_blocked(self, config):
        result = _call_bash_restricted(config, "env sh -c 'echo pwned'")
        assert "not in allowlist" in result.lower()

    # --- gh read actions allowed ---

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr view 123",
            "gh pr list",
            "gh issue view 456",
            "gh issue list",
            "gh api /repos/foo/bar/pulls",
            "gh status",
        ],
    )
    def test_gh_read_allowed(self, config, command):
        # Validates command acceptance; actual execution may fail without auth
        result = _call_bash_restricted(config, command)
        assert "not allowed" not in result.lower()
        assert "not in allowlist" not in result.lower()

    # --- gh write actions blocked ---

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create",
            "gh pr merge 123",
            "gh issue create",
            "gh api -X POST /repos/foo/bar/issues",
            "gh api --method DELETE /repos/foo/bar",
        ],
    )
    def test_gh_write_blocked(self, config, command):
        result = _call_bash_restricted(config, command)
        assert "error" in result.lower()

    # --- gh disallowed topics blocked ---

    @pytest.mark.parametrize(
        "command",
        [
            "gh release download v1.0",
            "gh repo clone foo/bar",
            "gh repo delete foo/bar",
            "gh extension install foo",
        ],
    )
    def test_gh_disallowed_topic(self, config, command):
        result = _call_bash_restricted(config, command)
        assert "error" in result.lower()
        assert "not allowed" in result.lower()

    # --- git --output flag blocked ---

    @pytest.mark.parametrize(
        "command",
        [
            "git diff --output=leaked.txt",
            "git log --output leaked.txt",
            "git show HEAD --output=out.txt",
        ],
    )
    def test_git_output_flag_blocked(self, config, command):
        result = _call_bash_restricted(config, command)
        assert "error" in result.lower()
        assert "--output" in result.lower()

    # --- bash -r enforcement (catches redirects regex might miss) ---

    def test_bash_restricted_shell_blocks_redirect(self, config):
        """bash -r blocks redirects at the shell level, even if regex doesn't catch them."""
        result = _call_bash_restricted(config, "echo test > /dev/null")
        assert "error" in result.lower() or "restricted" in result.lower()

    # --- find and sort removed from restricted allowlist ---

    @pytest.mark.parametrize("command", ["find .", "sort file.txt"])
    def test_find_and_sort_not_allowed(self, config, command):
        result = _call_bash_restricted(config, command)
        assert "not in allowlist" in result.lower()


class TestAllowedGitSubcommands:
    """Tests for the allowed_git_subcommands parameter on validate_command."""

    ALLOWED = {"status", "log", "diff"}

    def _validate(self, command: str) -> tuple[bool, str]:
        from .core import validate_command, DEFAULT_ALLOWED_COMMANDS, DEFAULT_FORBIDDEN_COMMANDS

        return validate_command(
            command,
            DEFAULT_ALLOWED_COMMANDS,
            DEFAULT_FORBIDDEN_COMMANDS,
            allowed_git_subcommands=self.ALLOWED,
        )

    @pytest.mark.parametrize("sub", ["status", "log", "diff"])
    def test_allowed_subcommand_passes(self, sub):
        ok, _ = self._validate(f"git {sub}")
        assert ok

    @pytest.mark.parametrize("sub", ["checkout", "add", "commit", "push", "pull", "fetch", "remote", "rebase"])
    def test_unlisted_subcommand_blocked(self, sub):
        ok, err = self._validate(f"git {sub}")
        assert not ok
        assert "not allowed" in err

    def test_forbidden_subcommand_still_blocked(self):
        """GIT_FORBIDDEN_SUBCOMMANDS takes priority even if in allowed set."""
        from .core import validate_command, DEFAULT_ALLOWED_COMMANDS, DEFAULT_FORBIDDEN_COMMANDS

        ok, err = validate_command(
            "git clone https://example.com/repo",
            DEFAULT_ALLOWED_COMMANDS,
            DEFAULT_FORBIDDEN_COMMANDS,
            allowed_git_subcommands={"clone", "status"},
        )
        assert not ok
        assert "forbidden" in err

    @pytest.mark.parametrize(
        "command",
        [
            "git diff --output=leaked.txt",
            "git log --output leaked.txt",
        ],
    )
    def test_output_flag_blocked(self, command):
        ok, err = self._validate(command)
        assert not ok
        assert "--output" in err

    def test_bare_git_allowed(self):
        """git with no subcommand passes (just prints help)."""
        ok, _ = self._validate("git")
        assert ok

    def test_does_not_affect_restricted_fallback(self):
        """When allowed_git_subcommands is None, restricted mode still works."""
        from .core import validate_command, RESTRICTED_ALLOWED_COMMANDS

        ok, err = validate_command(
            "git push",
            RESTRICTED_ALLOWED_COMMANDS,
            set(),
            restricted=True,
            allowed_git_subcommands=None,
        )
        assert not ok
        assert "not allowed" in err

        # But a restricted-allowed subcommand passes
        ok, _ = validate_command(
            "git fetch",
            RESTRICTED_ALLOWED_COMMANDS,
            set(),
            restricted=True,
            allowed_git_subcommands=None,
        )
        assert ok
