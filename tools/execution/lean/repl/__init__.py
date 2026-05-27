# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean REPL tool — pooled REPL instances with MCP server.

Public API:
- LeanRepl, LeanReplConfig: Single REPL session
- LeanReplPool, LeanReplPoolConfig: Thread pool of sessions
- ReplProcessExited, ReplProcessRestarted: REPL lifecycle exceptions
- LeanReplServer, LeanReplServerArgs: PerRdvServer subclass
- start_repl_server: Start the pool as a background HTTP server
- repl_server_config: MCPServerConfig pointing to a running server
"""

from .core import LeanRepl, LeanReplConfig, format_message
from .exceptions import ReplProcessExited, ReplProcessRestarted
from .pool import LeanReplPool, LeanReplPoolConfig
from .server import (
    LeanReplServer,
    LeanReplServerArgs,
    ReplConfig,
    create_repl_mcp,
    repl_server_config,
    start_repl_server,
)

__all__ = [
    "LeanRepl",
    "LeanReplConfig",
    "LeanReplPool",
    "LeanReplPoolConfig",
    "LeanReplServer",
    "LeanReplServerArgs",
    "ReplConfig",
    "ReplProcessExited",
    "ReplProcessRestarted",
    "create_repl_mcp",
    "format_message",
    "repl_server_config",
    "start_repl_server",
]
