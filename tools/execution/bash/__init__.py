# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Bash execution tool — validated shell command execution.

Public API:
- BashExecutor: Validated shell command execution
- validate_command: Check a command against allowlist/blocklist
- bash_server: MCPServerConfig factory for in-process bash execution
"""

from .core import BashExecConfig, BashExecutor, validate_command
from .server import (
    BashConfig,
    BashRestrictedConfig,
    bash_restricted_server,
    bash_server,
    create_bash_restricted_server,
    create_bash_server,
)

__all__ = [
    "BashConfig",
    "BashExecConfig",
    "BashExecutor",
    "BashRestrictedConfig",
    "bash_restricted_server",
    "bash_server",
    "create_bash_restricted_server",
    "create_bash_server",
    "validate_command",
]
