# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Threaded LSP message dispatcher with request-response correlation.

Sits between the JSON-RPC transport and the high-level LSP client.
Runs a background thread that reads incoming messages and dispatches
them to callbacks (notifications) or unblocks waiting callers (responses).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from logging import getLogger
from typing import Any

from .errors import ResponseError
from .json_rpc import JsonRpcEndpoint

logger = getLogger(__name__)


class LspEndpoint(threading.Thread):
    """Background-threaded LSP message router."""

    def __init__(
        self,
        json_rpc_endpoint: JsonRpcEndpoint,
        *,
        method_callbacks: dict[str, Callable[[Any], Any]] | None = None,
        notify_callbacks: dict[str, Callable[[Any], None]] | None = None,
        timeout: int = 60,
    ) -> None:
        super().__init__(daemon=True)
        self.json_rpc_endpoint = json_rpc_endpoint
        self.method_callbacks = method_callbacks or {}
        self.notify_callbacks = notify_callbacks or {}
        self._timeout = timeout

        self._next_id: int = 0
        self._id_lock = threading.Lock()
        self._event_dict: dict[int, threading.Condition] = {}
        self._response_dict: dict[int, tuple[Any, dict | None]] = {}

        self.stop_event = threading.Event()
        self.json_rpc_endpoint.stop_event = self.stop_event

    # -- Thread loop -------------------------------------------------------

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                msg = self.json_rpc_endpoint.recv_response()
                if msg is None:
                    logger.debug("LSP server closed the connection.")
                    break
                self._dispatch(msg)
            except ResponseError as exc:
                logger.warning("JSON-RPC error in read loop: %s", exc)
            except RuntimeError:
                # stop_event was set inside json_rpc
                break

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        rpc_id = msg.get("id")

        if method is not None:
            params = msg.get("params")
            if rpc_id is not None:
                # Server-to-client request (needs a response)
                cb = self.method_callbacks.get(method)
                if cb is not None:
                    result = cb(params)
                    self._send_response(rpc_id, result=result)
                else:
                    # Acknowledge unknown server requests with an empty result
                    # rather than failing (common for capability registrations).
                    logger.debug("Unhandled server request: %s (id=%s)", method, rpc_id)
                    self._send_response(rpc_id, result=None)
            else:
                # Server-to-client notification
                cb = self.notify_callbacks.get(method)
                if cb is not None:
                    cb(params)
                else:
                    logger.debug("Unhandled notification: %s", method)
        else:
            # Response to a client request
            result = msg.get("result")
            error = msg.get("error")
            if rpc_id is not None:
                self._handle_result(rpc_id, result, error)

    # -- Request-response correlation --------------------------------------

    def call_method(self, method_name: str, **kwargs: Any) -> Any:
        """Send a request and block until the response arrives."""
        with self._id_lock:
            current_id = self._next_id
            self._next_id += 1

        cond = threading.Condition()
        self._event_dict[current_id] = cond

        with cond:
            self._send_message(method_name, kwargs, rpc_id=current_id)
            if self.stop_event.is_set():
                return None
            if not cond.wait(timeout=self._timeout):
                raise TimeoutError(f"LSP request {method_name!r} (id={current_id}) timed out after {self._timeout}s")

        self._event_dict.pop(current_id, None)
        result, error = self._response_dict.pop(current_id)
        if error:
            raise ResponseError(error.get("code", -1), error.get("message", "Unknown error"), error.get("data"))
        return result

    def send_notification(self, method_name: str, **kwargs: Any) -> None:
        """Send a notification (no response expected)."""
        self._send_message(method_name, kwargs)

    def stop(self) -> None:
        """Signal the read loop and JSON-RPC layer to stop."""
        self.stop_event.set()

    # -- Internals ---------------------------------------------------------

    def _handle_result(self, rpc_id: int, result: Any, error: dict | None) -> None:
        self._response_dict[rpc_id] = (result, error)
        cond = self._event_dict.get(rpc_id)
        if cond is not None:
            with cond:
                cond.notify()

    def _send_message(self, method: str, params: dict, rpc_id: int | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if rpc_id is not None:
            msg["id"] = rpc_id
        self.json_rpc_endpoint.send_request(msg)

    def _send_response(self, rpc_id: int, result: Any = None, error: Exception | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
        if error is not None:
            msg["error"] = {"code": getattr(error, "code", -1), "message": str(error)}
        else:
            # JSON-RPC requires either "result" or "error" in every response.
            msg["result"] = result
        self.json_rpc_endpoint.send_request(msg)
