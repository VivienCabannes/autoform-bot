# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Bash execution — command validation and safe shell execution.

No MCP dependencies.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)


DEFAULT_TIMEOUT = 120
DEFAULT_LAKE_TIMEOUT = 300
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000

# Commands the agent is allowed to run.  These are read-only or
# well-scoped tools that cannot modify the filesystem in dangerous ways
# on their own.  Network-capable tools (git, gh) are allowed because
# git subcommands are further restricted below.
DEFAULT_ALLOWED_COMMANDS: set[str] = {
    # Lean build system
    "lake",
    # Read-only file inspection
    "cat",
    "head",
    "tail",
    "wc",
    "ls",
    # Search & filtering
    "grep",
    "rg",
    "find",
    # Text processing (read-only pipelines)
    "sort",
    "uniq",
    "cut",
    "awk",
    "sed",
    "tr",
    "xargs",
    "diff",
    # Version control & GitHub CLI (subcommands restricted separately)
    "git",
    "gh",
    # Development tooling
    "ruff",
    "make",
    "pip",
    "uv",
    "python",
    "python3",
    "pytest",
    "mypy",
    # File operations (needed for dev workflows)
    "touch",
    "mkdir",
    "tee",
    "patch",
    "which",
    "env",
    # Harmless builtins
    "echo",
    "cd",  # useful in && chains (e.g. `cd dir && make`); no cross-call persistence
    "true",
    "false",
}

# Commands that are explicitly blocked even if an operator adds them to
# the allowed set.  Grouped by risk category.
DEFAULT_FORBIDDEN_COMMANDS: set[str] = {
    # Destructive filesystem operations
    "rm",
    "rmdir",
    "mv",
    "cp",
    "chmod",
    "chown",
    # Privilege escalation
    "sudo",
    "su",
    # Arbitrary network access
    "curl",
    "wget",
    "nc",
    "ssh",
    # Process control
    "kill",
    "pkill",
    # Shell meta-commands (can bypass other restrictions)
    "eval",
    "exec",
    "source",
    # Arbitrary code execution via interpreters
    "node",
    "ruby",
    "perl",
}

RESTRICTED_ALLOWED_COMMANDS: set[str] = {
    # Read-only file inspection
    "cat",
    "head",
    "tail",
    "wc",
    "ls",
    # Search & filtering
    "grep",
    "rg",
    # Text processing (read-only pipelines)
    "uniq",
    "cut",
    "tr",
    "diff",
    # Version control & GitHub CLI (subcommands restricted separately)
    "git",
    "gh",
    # Introspection
    "echo",
    "which",
    # Harmless builtins
    "true",
    "false",
}

GIT_RESTRICTED_SUBCOMMANDS: set[str] = {
    "status",
    "log",
    "show",
    "diff",
    "branch",
    "rev-parse",
    "ls-files",
    "blame",
    "fetch",
    "remote",
}

GH_RESTRICTED_TOPICS: set[str] = {"pr", "issue", "api", "status"}

GH_RESTRICTED_ACTIONS: set[str] = {
    "list",
    "view",
    "diff",
    "checks",
    "status",
    "watch",
}

GIT_FORBIDDEN_SUBCOMMANDS: set[str] = {
    "clone",
    "config",
    "worktree",
    "init",
    "gc",
    "clean",
    "filter-branch",
}

_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r";", "Semicolons not allowed (use && for chaining)"),
    (r"(?<![0-9>&])&(?!&)", "Background execution (&) not allowed"),
    (r"\$\(", "Command substitution $() not allowed"),
    (r"`", "Backtick substitution not allowed"),
    (r"(?<![0-9])<<", "Here-doc not allowed"),
    (r"(?<![0-9&>])<(?![&<])", "Input redirect (<) not allowed"),
    (r"\$\{", "Variable expansion ${} not allowed"),
]


def validate_command(
    command: str,
    allowed_commands: set[str],
    forbidden_commands: set[str],
    *,
    restricted: bool = False,
    allowed_git_subcommands: set[str] | None = None,
) -> tuple[bool, str]:
    """Validate a shell command for safety.

    When restricted=True, only read-only git subcommands are permitted. Output
    redirects are not regex-checked — bash -r blocks them at the shell level.

    Returns (is_valid, error_message).
    """
    if "\n" in command or "\r" in command:
        return False, "Newline characters are not allowed in commands"

    patterns = _DANGEROUS_PATTERNS
    for pattern, msg in patterns:
        if re.search(pattern, command):
            return False, msg

    subcommands = re.split(r"\s*(?:&&|\|\|?)\s*", command)

    for subcmd in subcommands:
        subcmd = subcmd.strip()
        if not subcmd:
            continue

        subcmd_clean = re.sub(r"\s*\d*>+\s*\S+", "", subcmd)
        subcmd_clean = re.sub(r"\s*2>&1", "", subcmd_clean)
        subcmd_clean = subcmd_clean.strip()

        if not subcmd_clean:
            continue

        try:
            tokens = shlex.split(subcmd_clean)
        except ValueError as e:
            return False, f"Invalid command syntax: {e}"

        if not tokens:
            continue

        base_cmd = tokens[0]
        if base_cmd in forbidden_commands:
            return False, f"Command '{base_cmd}' is forbidden"

        if base_cmd not in allowed_commands:
            return False, f"Command '{base_cmd}' is not in allowlist"

        if base_cmd == "git" and len(tokens) > 1:
            subcommand = tokens[1]
            if subcommand in GIT_FORBIDDEN_SUBCOMMANDS:
                return False, f"Git subcommand '{subcommand}' is forbidden"
            if allowed_git_subcommands is not None:
                if subcommand not in allowed_git_subcommands:
                    return False, f"Git subcommand '{subcommand}' is not allowed"
            elif restricted and subcommand not in GIT_RESTRICTED_SUBCOMMANDS:
                return False, f"Git subcommand '{subcommand}' is not allowed"
            if restricted or allowed_git_subcommands is not None:
                for token in tokens[2:]:
                    if token == "--output" or token.startswith("--output="):
                        return False, "Git --output flag is not allowed"

        if base_cmd == "gh" and restricted:
            if len(tokens) < 2:
                return False, "gh requires a topic (e.g. gh pr view)"
            topic = tokens[1]
            if topic not in GH_RESTRICTED_TOPICS:
                return False, f"gh topic '{topic}' is not allowed in read-only mode"
            if topic == "api":
                for token in tokens[2:]:
                    if token in ("-X", "--method"):
                        return False, "gh api with -X/--method is not allowed in read-only mode"
            elif len(tokens) < 3:
                return False, f"gh {topic} requires an action (e.g. gh {topic} view)"
            elif tokens[2] not in GH_RESTRICTED_ACTIONS:
                return False, f"gh {topic} action '{tokens[2]}' is not allowed in read-only mode"

    return True, ""


@dataclass(frozen=True)
class BashExecConfig:
    """Configuration for a BashExecutor instance."""

    default_cwd: str = "."
    timeout: int = DEFAULT_TIMEOUT
    lake_timeout: int = DEFAULT_LAKE_TIMEOUT
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    allowed_commands: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_COMMANDS))
    forbidden_commands: set[str] = field(default_factory=lambda: set(DEFAULT_FORBIDDEN_COMMANDS))
    allowed_git_subcommands: set[str] | None = None
    restricted: bool = False


class BashExecutor:
    """Validated shell command execution."""

    def __init__(self, config: BashExecConfig | None = None) -> None:
        self.config = config or BashExecConfig()

    def exec(self, command: str, cwd: str = "", timeout: int | None = None, *, validated: bool = False) -> str:
        """Execute a validated shell command and return formatted output.

        Args:
            command: Shell command to execute.
            cwd: Working directory override (empty string uses default_cwd).
            timeout: Timeout override in seconds.
            validated: Internal-only. When True, skips command validation.
                Must only be set after the command has already been validated
                by ``_resolve_approvals`` in the server layer.
        """
        if not command:
            return "Error: command is required"

        if not validated:
            is_valid, error = validate_command(
                command,
                self.config.allowed_commands,
                self.config.forbidden_commands,
                restricted=self.config.restricted,
                allowed_git_subcommands=self.config.allowed_git_subcommands,
            )
            if not is_valid:
                return f"Error: {error}"

        work_dir = Path(cwd) if cwd else Path(self.config.default_cwd)
        cmd_timeout = timeout or self.config.timeout

        if command.strip().startswith("lake"):
            cmd_timeout = self.config.lake_timeout

        try:
            if self.config.restricted:
                run_args: list[str] | str = ["bash", "-r", "-c", command]
                run_shell = False
            else:
                run_args = command
                run_shell = True

            result = subprocess.run(
                run_args,
                shell=run_shell,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
            )

            stdout = result.stdout
            stderr = result.stderr
            max_chars = self.config.max_output_bytes
            if len(stdout) > max_chars:
                stdout = stdout[:max_chars] + f"\n\n... [truncated, {len(stdout)} chars total]"
            if len(stderr) > max_chars:
                stderr = stderr[:max_chars] + f"\n\n... [truncated, {len(stderr)} chars total]"

            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"

            return output if output.strip() else "(no output)"

        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {cmd_timeout}s"
        except Exception as e:
            return f"Error: {e}"
