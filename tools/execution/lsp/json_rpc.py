# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Thread-safe JSON-RPC 2.0 transport over stdin/stdout pipes.

Implements the LSP base protocol framing:
``Content-Length: N\\r\\n\\r\\n{json_body}``

Uses ``selectors`` for non-blocking reads (cross-platform, unlike
``select.poll()`` which is unavailable on some macOS builds).
"""

from __future__ import annotations

import json
import os
import selectors
import threading
from typing import Any, BinaryIO

from .errors import ErrorCodes, ResponseError

_LEN_HEADER = "Content-Length: "
_TYPE_HEADER = "Content-Type: "


class JsonRpcEndpoint:
    """Thread-safe JSON-RPC 2.0 send/receive over byte streams."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self._read_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._read_buf = b""
        self.stop_event: threading.Event | None = None

    def send_request(self, message: dict[str, Any]) -> None:
        """Serialize *message* as JSON-RPC and write to stdin."""
        json_bytes = json.dumps(message, default=_default_encoder).encode()
        header = f"Content-Length: {len(json_bytes)}\r\n\r\n".encode()
        with self._write_lock:
            self.stdin.write(header + json_bytes)
            self.stdin.flush()

    def recv_response(self) -> dict[str, Any] | None:
        """Read one JSON-RPC message from stdout (blocking)."""
        with self._read_lock:
            message_size = self._read_headers()
            if message_size is None:
                return None
            body = self._read_bytes(message_size)
            return json.loads(body.decode())

    # -- internals ---------------------------------------------------------

    def _read_headers(self) -> int | None:
        """Parse LSP headers and return the Content-Length value."""
        message_size: int | None = None
        while True:
            line = self._nonblocking_readline()
            if not line:
                return None
            decoded = line.decode()
            if not decoded.endswith("\r\n"):
                raise ResponseError(ErrorCodes.ParseError, "Bad header: missing \\r\\n")
            decoded = decoded[:-2]
            if decoded == "":
                break
            elif decoded.startswith(_LEN_HEADER):
                size_str = decoded[len(_LEN_HEADER) :]
                if not size_str.isdigit():
                    raise ResponseError(ErrorCodes.ParseError, "Bad header: Content-Length is not an integer")
                message_size = int(size_str)
            elif decoded.startswith(_TYPE_HEADER):
                pass  # ignored for now
            else:
                raise ResponseError(ErrorCodes.ParseError, f"Bad header: unknown header {decoded!r}")
        if message_size is None:
            raise ResponseError(ErrorCodes.ParseError, "Bad header: missing Content-Length")
        return message_size

    def _nonblocking_readline(self) -> bytes:
        """Read one line from stdout, checking stop_event periodically.

        Uses an internal buffer to read in chunks rather than one byte
        at a time, reducing the number of system calls.
        """
        sel = selectors.DefaultSelector()
        try:
            sel.register(self.stdout, selectors.EVENT_READ)
            while self.stop_event is None or not self.stop_event.is_set():
                newline_idx = self._read_buf.find(b"\n")
                if newline_idx != -1:
                    line = self._read_buf[: newline_idx + 1]
                    self._read_buf = self._read_buf[newline_idx + 1 :]
                    return line
                events = sel.select(timeout=0.1)
                if events:
                    chunk = os.read(self.stdout.fileno(), 4096)
                    if not chunk:
                        # EOF — return whatever is left in the buffer.
                        remaining = self._read_buf
                        self._read_buf = b""
                        return remaining
                    self._read_buf += chunk
        finally:
            sel.close()
        raise RuntimeError("stopped")

    def _read_bytes(self, count: int) -> bytes:
        """Read exactly *count* bytes, draining the internal buffer first."""
        while len(self._read_buf) < count:
            chunk = os.read(self.stdout.fileno(), count - len(self._read_buf))
            if not chunk:
                raise ResponseError(ErrorCodes.ParseError, "Unexpected end of stream")
            self._read_buf += chunk
        result = self._read_buf[:count]
        self._read_buf = self._read_buf[count:]
        return result


def _default_encoder(obj: Any) -> Any:
    """Fallback JSON encoder for objects with ``__dict__``."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
