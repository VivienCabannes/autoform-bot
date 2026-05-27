# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LSP session — manages a language server subprocess and its client.

Combines the 3-layer stack (JsonRpcEndpoint → LspEndpoint → LspClient)
with subprocess lifecycle management. Language-agnostic: subclass and
override ``_get_notify_callbacks`` for language-specific behaviour.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any

from .client import LspClient
from .endpoint import LspEndpoint
from .json_rpc import JsonRpcEndpoint
from .structs import (
    Diagnostic,
    HoverResult,
    Location,
    LocationLink,
    Position,
    PublishDiagnosticsParams,
    TextDocumentIdentifier,
    TextDocumentItem,
)

logger = getLogger(__name__)

_KILL_TIMEOUT = 5


@dataclass(frozen=True)
class LspSessionConfig:
    """Configuration for spawning a language server subprocess."""

    command: list[str]
    workspace: str
    language_id: str
    capabilities: dict[str, Any]
    timeout: int = 60
    preexec_fn: Callable[[], None] | None = None
    env: dict[str, str] = field(default_factory=dict)


class LspSession:
    """Manages an LSP server subprocess and its client.

    Spawns the language server, initializes the LSP protocol, and
    exposes high-level operations (open file, get diagnostics, etc.).
    Subclass ``_get_notify_callbacks`` / ``_get_method_callbacks`` to
    handle language-specific notifications.
    """

    def __init__(self, config: LspSessionConfig) -> None:
        self.config = config
        self._process: subprocess.Popen | None = None
        self._client: LspClient | None = None
        self._endpoint: LspEndpoint | None = None
        self._diagnostics: dict[str, list[Diagnostic]] = {}

    @property
    def client(self) -> LspClient:
        if self._client is None:
            raise RuntimeError("LspSession not started — call start() first")
        return self._client

    # -- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the language server and perform the LSP handshake."""
        cfg = self.config

        self._process = subprocess.Popen(
            cfg.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cfg.workspace,
            preexec_fn=cfg.preexec_fn,
            env={**os.environ, **cfg.env} if cfg.env else None,
        )

        rpc = JsonRpcEndpoint(self._process.stdin, self._process.stdout)  # type: ignore[arg-type]
        self._endpoint = LspEndpoint(
            rpc,
            notify_callbacks=self._get_notify_callbacks(),
            method_callbacks=self._get_method_callbacks(),
            timeout=cfg.timeout,
        )
        self._client = LspClient(self._endpoint)

        self._client.initialize(
            process_id=0,
            root_uri=f"file://{cfg.workspace}",
            capabilities=cfg.capabilities,
        )
        self._client.initialized()

        logger.info("LSP session started (PID %d, workspace %s).", self._process.pid, cfg.workspace)

    def close(self) -> None:
        """Shut down the LSP client and terminate the server subprocess."""
        if self._client is not None:
            try:
                self._client.shutdown()
            except Exception:
                logger.debug("LSP shutdown request failed (server may have exited).", exc_info=True)

        if self._endpoint is not None:
            self._endpoint.stop()
            self._endpoint.join(timeout=3)

        if self._process is not None:
            self._terminate_process()
            self._process = None

        self._client = None
        self._endpoint = None
        logger.info("LSP session closed.")

    # -- High-level API ----------------------------------------------------

    def open_file(self, uri: str, content: str) -> None:
        """Open a document in the language server."""
        self.client.did_open(TextDocumentItem(uri=uri, languageId=self.config.language_id, version=0, text=content))

    def close_file(self, uri: str) -> None:
        """Close a document in the language server."""
        self.client.did_close(TextDocumentIdentifier(uri=uri))

    def get_diagnostics(self, uri: str) -> list[Diagnostic]:
        """Return the latest diagnostics for a URI (from publishDiagnostics notifications)."""
        return self._diagnostics.get(uri, [])

    def hover(self, uri: str, position: Position) -> HoverResult | None:
        return self.client.hover(TextDocumentIdentifier(uri=uri), position)

    def definition(self, uri: str, position: Position) -> Location | list[Location] | list[LocationLink]:
        return self.client.definition(TextDocumentIdentifier(uri=uri), position)

    # -- Subclass hooks ----------------------------------------------------

    def _get_notify_callbacks(self) -> dict[str, Callable]:
        """Override to add language-specific notification handlers.

        The base implementation handles ``textDocument/publishDiagnostics``.
        """
        return {
            "textDocument/publishDiagnostics": self._on_publish_diagnostics,
        }

    def _get_method_callbacks(self) -> dict[str, Callable]:
        """Override to handle server-to-client requests."""
        return {}

    # -- Default handlers --------------------------------------------------

    def _on_publish_diagnostics(self, params: Any) -> None:
        parsed = PublishDiagnosticsParams.model_validate(params)
        self._diagnostics[parsed.uri] = parsed.diagnostics

    # -- Internals ---------------------------------------------------------

    def _terminate_process(self) -> None:
        """Terminate the subprocess, escalating to kill if necessary."""
        proc = self._process
        if proc is None or proc.poll() is not None:
            return

        logger.debug("Terminating LSP process %d.", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=_KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("LSP process %d did not terminate — sending SIGKILL.", proc.pid)
            proc.kill()
            proc.wait(timeout=_KILL_TIMEOUT)
