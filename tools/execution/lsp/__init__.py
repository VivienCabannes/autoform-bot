# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Generic LSP client library and MCP tool server.

Provides a language-agnostic LSP client stack (JSON-RPC transport,
endpoint dispatcher, typed client API) and MCP tools for
diagnostics, hover, and go-to-definition.
"""

from .client import LspClient
from .endpoint import LspEndpoint
from .errors import ErrorCodes, ResponseError
from .json_rpc import JsonRpcEndpoint
from .server import LspServerConfig, LspSessionManager, lsp_native_server
from .session import LspSession, LspSessionConfig

__all__ = [
    "ErrorCodes",
    "JsonRpcEndpoint",
    "LspClient",
    "LspEndpoint",
    "LspServerConfig",
    "LspSession",
    "LspSessionConfig",
    "LspSessionManager",
    "ResponseError",
    "lsp_native_server",
]
