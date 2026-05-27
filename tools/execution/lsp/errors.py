# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""JSON-RPC and LSP error codes with a typed exception."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrorCodes(IntEnum):
    """Standard JSON-RPC 2.0 and LSP error codes."""

    # JSON-RPC 2.0
    ParseError = -32700
    InvalidRequest = -32600
    MethodNotFound = -32601
    InvalidParams = -32602
    InternalError = -32603
    ServerErrorStart = -32099
    ServerErrorEnd = -32000
    ServerNotInitialized = -32002
    UnknownErrorCode = -32001

    # LSP-specific
    RequestCancelled = -32800
    ContentModified = -32801


class ResponseError(Exception):
    """Error received in a JSON-RPC response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __repr__(self) -> str:
        return f"ResponseError(code={self.code}, message={self.message!r})"
