# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Bash MCP server — FastMCP tool definition and config factory."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import (
    BashExecConfig,
    BashExecutor,
    DEFAULT_ALLOWED_COMMANDS,
    DEFAULT_FORBIDDEN_COMMANDS,
    RESTRICTED_ALLOWED_COMMANDS,
    validate_command,
)


@dataclass(frozen=True)
class BashConfig:
    """Configuration for the bash execution tool."""

    default_cwd: str = "."
    allowed_commands: list[str] | None = None
    unlock_commands: list[str] | None = None
    command_approval_handler: Callable[[str, str], bool] | None = None


@dataclass(frozen=True)
class BashRestrictedConfig:
    """Configuration for the restricted bash execution tool."""

    default_cwd: str = "."


_CMD_ERROR_RE = re.compile(r"Command '(\S+)' is (?:forbidden|not in allowlist)")


def _resolve_approvals(
    command: str,
    executor: BashExecutor,
    approval_handler: Callable[[str, str], bool],
) -> str | None:
    """Pre-validate a command, prompting for approval on each blocked sub-command.

    Returns an error string if any sub-command is denied or structurally invalid,
    or None if all sub-commands are approved.
    """
    MAX_APPROVAL_ROUNDS = 10

    temp_allowed = set(executor.config.allowed_commands)
    temp_forbidden = set(executor.config.forbidden_commands)
    for _ in range(MAX_APPROVAL_ROUNDS):
        is_valid, error = validate_command(command, temp_allowed, temp_forbidden)
        if is_valid:
            return None
        m = _CMD_ERROR_RE.search(error)
        if not m:
            # Pattern-level error (`;`, `$()`, etc.) — never approvable
            return f"Error: {error}"
        blocked_cmd = m.group(1)
        if approval_handler(blocked_cmd, command):
            temp_allowed.add(blocked_cmd)
            temp_forbidden.discard(blocked_cmd)
        else:
            return f"Error: {error}"
    return "Error: too many approval rounds — command has too many blocked sub-commands"


def create_bash_server(
    executor: BashExecutor | None = None,
    command_approval_handler: Callable[[str, str], bool] | None = None,
) -> FastMCP:
    """Create a FastMCP server with bash execution tools."""
    executor = executor or BashExecutor()
    server = FastMCP(name="bash")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE, max_result_chars=30_000)
    def bash(
        command: str,
        cwd: str = "",
        timeout: int | None = None,
    ) -> str:
        """Execute a shell command with safety validation.

        Commands are validated against an allowlist before execution.
        Supports && chaining, pipes, and output redirection.

        Prefer dedicated MCP tools over shell for file modifications
        (edit_file, edit_lines, write_file) and git operations (git_* tools).

        Args:
            command: Shell command to execute.
            cwd: Working directory (defaults to configured default).
            timeout: Command timeout in seconds (overrides default).
        """
        if command_approval_handler is not None:
            error = _resolve_approvals(command, executor, command_approval_handler)
            if error:
                return error
            return executor.exec(command, cwd=cwd, timeout=timeout, validated=True)
        return executor.exec(command, cwd=cwd, timeout=timeout)

    return server


def bash_server(config: BashConfig | None = None) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for bash execution."""
    default_cwd = config.default_cwd if config else "."
    unlock_commands = config.unlock_commands if config else None
    command_approval_handler = config.command_approval_handler if config else None

    if config and config.allowed_commands is not None:
        allowed = set(config.allowed_commands)
    else:
        allowed = set(DEFAULT_ALLOWED_COMMANDS)
    forbidden = set(DEFAULT_FORBIDDEN_COMMANDS) - allowed
    if unlock_commands:
        allowed.update(unlock_commands)
        forbidden -= set(unlock_commands)

    executor = BashExecutor(
        BashExecConfig(
            default_cwd=default_cwd,
            allowed_commands=allowed,
            forbidden_commands=forbidden,
        )
    )
    mcp_instance = create_bash_server(executor, command_approval_handler=command_approval_handler)
    return MCPServerConfig(
        server_key="bash",
        description="Full shell command execution",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )


def create_bash_restricted_server(executor: BashExecutor | None = None) -> FastMCP:
    """Create a FastMCP server with read-only bash execution tools."""
    executor = executor or BashExecutor(
        BashExecConfig(
            allowed_commands=set(RESTRICTED_ALLOWED_COMMANDS),
            restricted=True,
        )
    )
    server = FastMCP(name="bash_restricted")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE_RESTRICTED, max_result_chars=30_000)
    def bash_restricted(
        command: str,
        cwd: str = "",
        timeout: int | None = None,
    ) -> str:
        """Execute a read-only shell command with safety validation.

        Only read-only commands are allowed (cat, grep, git, ls, etc.).
        No interpreters (python, node), no package managers (pip, uv),
        no file creation (touch, mkdir, tee), and no build tools (make, pytest).
        No file-mutating text tools (sed, awk, xargs).

        Args:
            command: Shell command to execute.
            cwd: Working directory (defaults to configured default).
            timeout: Command timeout in seconds (overrides default).
        """
        return executor.exec(command, cwd=cwd, timeout=timeout)

    return server


def bash_restricted_server(config: BashRestrictedConfig | None = None) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for read-only bash execution."""
    executor = BashExecutor(
        BashExecConfig(
            default_cwd=config.default_cwd if config else ".",
            allowed_commands=set(RESTRICTED_ALLOWED_COMMANDS),
            restricted=True,
        )
    )
    mcp_instance = create_bash_restricted_server(executor)
    return MCPServerConfig(
        server_key="bash_restricted",
        description="Read-only shell for investigation without side effects",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
