# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean 4 language server session.

Extends ``LspSession`` with Lean-specific behaviour:
- Spawns ``lake env lean --server`` with memory limits
- Loads Lean client capabilities from ``capabilities.json``
- Tracks file progress via ``$/lean/fileProgress``
- Captures Lean-specific diagnostics
- **Warm-up**: preloads imports once (e.g. ``import Mathlib``), then uses
  ``didChange`` for incremental re-checking so only the proof body is
  re-elaborated on each iteration.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from logging import getLogger
from pathlib import Path
from typing import Any

from tools.execution.lsp.session import LspSession, LspSessionConfig
from tools.execution.lsp.structs import (
    Position,
    TextDocumentIdentifier,
    TextDocumentItem,
    VersionedTextDocumentIdentifier,
)

from .structs import FileProgress, LeanDiagnostic, LeanDiagnostics, PlainGoal

logger = getLogger(__name__)

_CAPABILITIES_PATH = Path(__file__).with_name("capabilities.json")
_DEFAULT_MEMORY_LIMIT_MB = 10 * 1024  # 10 GB
_DEFAULT_WARMUP_IMPORTS = ("Mathlib",)


def _set_memory_limit(max_mem_mb: int = _DEFAULT_MEMORY_LIMIT_MB) -> None:
    """Set the memory limit for the subprocess (used as preexec_fn).

    Uses ``resource.RLIMIT_AS`` on Linux. On macOS (Darwin) this limit
    is not supported, so the function is a no-op.
    """
    if sys.platform == "darwin":
        return
    import resource

    max_bytes = max_mem_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))


class LeanNativeLspSession(LspSession):
    """Lean 4 language server session with warm-up and incremental checking.

    On first use, opens a warm-up document with the configured imports
    (default: ``import Mathlib``) and waits for Lean to elaborate them.
    Subsequent ``check_code`` calls use ``didChange`` on the same document,
    so Lean only re-elaborates the changed body — not the imports.
    """

    def __init__(
        self,
        workspace: str,
        *,
        timeout: int = 60,
        warmup_imports: tuple[str, ...] = _DEFAULT_WARMUP_IMPORTS,
        warmup_timeout: int | None = None,
    ) -> None:
        capabilities = json.loads(_CAPABILITIES_PATH.read_text())

        config = LspSessionConfig(
            command=["lake", "env", "lean", "--server", "--threads=2"],
            workspace=workspace,
            language_id="lean",
            capabilities=capabilities,
            timeout=timeout,
            preexec_fn=_set_memory_limit,
        )
        super().__init__(config)
        self.processed = threading.Event()
        self.last_diagnostics: LeanDiagnostics | None = None

        self._warmup_imports = warmup_imports
        self._warmup_timeout = warmup_timeout or timeout
        self._warmed_up = False
        self._warmup_uri: str = f"file://{workspace}/__fort_lsp_scratch__.lean"
        self._version: int = 0

    # -- Subclass hooks ----------------------------------------------------

    def _get_notify_callbacks(self) -> dict[str, Callable]:
        return {
            "$/lean/fileProgress": self._file_progress_callback,
            "textDocument/publishDiagnostics": self._publish_diagnostics_callback,
        }

    def _get_method_callbacks(self) -> dict[str, Callable]:
        # Lean sends these as requests (with id) that need a response.
        def _noop(_: Any) -> None:
            pass

        return {
            "client/registerCapability": _noop,
            "workspace/semanticTokens/refresh": _noop,
            "workspace/inlayHint/refresh": _noop,
        }

    # -- Warm-up -----------------------------------------------------------

    def warm_up(self) -> None:
        """Preload imports by opening a document and waiting for elaboration.

        After this returns, the server has fully elaborated the import
        header. Subsequent ``check_code`` calls use ``didChange`` to
        replace the body while keeping the cached import environment.
        """
        if self._warmed_up:
            return

        header = self._import_header()
        logger.info("Warming up Lean LSP with imports: %s", ", ".join(self._warmup_imports))

        self.processed.clear()
        self._version = 0
        self.client.did_open(
            TextDocumentItem(uri=self._warmup_uri, languageId="lean", version=self._version, text=header)
        )

        if not self.processed.wait(timeout=self._warmup_timeout):
            raise TimeoutError(
                f"Lean warm-up timed out after {self._warmup_timeout}s. "
                "The project may not be built — try running `lake build` first."
            )

        self._warmed_up = True
        logger.info("Lean LSP warm-up complete.")

    # -- Incremental checking ----------------------------------------------

    def check_code(self, code: str) -> LeanDiagnostics | None:
        """Type-check Lean code incrementally.

        On first call, warms up the LSP with imports. Then uses
        ``didChange`` to replace the full document content (import header
        + code), so Lean only re-elaborates the new body.

        Args:
            code: Lean proof body (without import statements).

        Returns:
            Diagnostics from the Lean server, or None on timeout.

        Raises:
            TimeoutError: If processing does not complete within the timeout.
        """
        self.warm_up()

        full_content = self._import_header() + code
        self._version += 1
        self.processed.clear()

        self.client.did_change(
            VersionedTextDocumentIdentifier(uri=self._warmup_uri, version=self._version),
            [{"text": full_content}],
        )

        if self.processed.wait(timeout=self.config.timeout):
            return self.last_diagnostics
        raise TimeoutError(f"Lean processing timed out after {self.config.timeout}s")

    # -- Non-incremental API (for arbitrary files) -------------------------

    def run_file(self, uri: str, content: str) -> LeanDiagnostics | None:
        """Open a standalone file and wait for diagnostics.

        Unlike ``check_code``, this opens a new document (not the warm-up
        scratch file) — useful for checking files that have their own imports.

        Args:
            uri: Document URI.
            content: Full file contents including imports.

        Returns:
            Diagnostics, or None on timeout.

        Raises:
            TimeoutError: If processing does not complete within the timeout.
        """
        self.processed.clear()
        self.client.did_open(TextDocumentItem(uri=uri, languageId="lean", version=0, text=content))
        if self.processed.wait(timeout=self.config.timeout):
            return self.last_diagnostics
        raise TimeoutError(f"Lean processing timed out after {self.config.timeout}s for {uri}")

    # -- Proof state -------------------------------------------------------

    def get_proof_state(self, uri: str, position: Position) -> PlainGoal | None:
        """Get the current proof goal at a position via ``$/lean/plainGoal``."""
        result = self.client.endpoint.call_method(
            "$/lean/plainGoal",
            textDocument=TextDocumentIdentifier(uri=uri),
            position=position,
        )
        if result is None:
            return None
        try:
            return PlainGoal.model_validate(result)
        except Exception:
            return None

    def get_check_proof_state(self, position: Position) -> PlainGoal | None:
        """Get proof state in the warm-up scratch file (used after ``check_code``)."""
        return self.get_proof_state(self._warmup_uri, position)

    def first_error(self, diagnostics: LeanDiagnostics) -> LeanDiagnostic | None:
        """Return the first error-severity diagnostic, or None."""
        for d in diagnostics.diagnostics:
            if d.severity <= 2:
                return d
        return None

    # -- Notification handlers ---------------------------------------------

    def _file_progress_callback(self, params: Any) -> None:
        progress = FileProgress.model_validate(params)
        if not progress.processing:
            self.processed.set()

    def _publish_diagnostics_callback(self, params: Any) -> None:
        self.last_diagnostics = LeanDiagnostics.model_validate(params)

    # -- Helpers -----------------------------------------------------------

    def _import_header(self) -> str:
        if not self._warmup_imports:
            return ""
        return "\n".join(f"import {root}" for root in self._warmup_imports) + "\n\n"
