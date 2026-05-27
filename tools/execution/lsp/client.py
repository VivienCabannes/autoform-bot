# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Typed high-level LSP client API.

Wraps ``LspEndpoint`` with methods for standard LSP operations.
Language-specific extensions (e.g. ``$/lean/plainGoal``) belong
in the language-specific layer, not here.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .endpoint import LspEndpoint
from .structs import (
    CompletionContext,
    CompletionItem,
    CompletionList,
    DocumentSymbol,
    HoverResult,
    Location,
    LocationLink,
    SignatureHelp,
    SymbolInformation,
    TextDocumentIdentifier,
    TextDocumentItem,
    VersionedTextDocumentIdentifier,
    Position,
)


class LspClient:
    """Typed API for standard LSP operations."""

    def __init__(self, endpoint: LspEndpoint) -> None:
        self.endpoint = endpoint

    # -- Lifecycle ---------------------------------------------------------

    def initialize(
        self,
        *,
        process_id: int | None = None,
        root_uri: str | None = None,
        root_path: str | None = None,
        initialization_options: Any = None,
        capabilities: dict | None = None,
        trace: str | None = None,
        workspace_folders: list | None = None,
    ) -> dict:
        """Send the ``initialize`` request (must be first)."""
        if capabilities is None:
            raise ValueError("capabilities is required")
        self.endpoint.start()
        return self.endpoint.call_method(
            "initialize",
            processId=process_id,
            rootPath=root_path,
            rootUri=root_uri,
            initializationOptions=initialization_options,
            capabilities=capabilities,
            trace=trace,
            workspaceFolders=workspace_folders,
        )

    def initialized(self) -> None:
        """Send the ``initialized`` notification."""
        self.endpoint.send_notification("initialized")

    def shutdown(self) -> Any:
        """Send the ``shutdown`` request."""
        self.endpoint.stop()
        self.endpoint.join(timeout=5)
        return self.endpoint.call_method("shutdown")

    def exit(self) -> None:
        """Send the ``exit`` notification."""
        self.endpoint.send_notification("exit")

    # -- Document synchronization ------------------------------------------

    def did_open(self, text_document: TextDocumentItem) -> None:
        """Notify the server that a document was opened."""
        self.endpoint.send_notification("textDocument/didOpen", textDocument=text_document)

    def did_change(self, text_document: VersionedTextDocumentIdentifier, content_changes: Any) -> None:
        """Notify the server that a document changed.

        Args:
            text_document: Versioned document identifier (uri + version).
            content_changes: List of content changes. For full-document sync,
                pass ``[{"text": "<full new content>"}]``.
        """
        self.endpoint.send_notification(
            "textDocument/didChange",
            textDocument=text_document,
            contentChanges=content_changes,
        )

    def did_close(self, text_document: TextDocumentIdentifier) -> None:
        """Notify the server that a document was closed."""
        self.endpoint.send_notification("textDocument/didClose", textDocument=text_document)

    # -- Language features -------------------------------------------------

    def hover(self, text_document: TextDocumentIdentifier, position: Position) -> HoverResult | None:
        """Request hover information at a position."""
        result = self.endpoint.call_method(
            "textDocument/hover",
            textDocument=text_document,
            position=position,
        )
        if result is None:
            return None
        return HoverResult.model_validate(result)

    def definition(
        self, text_document: TextDocumentIdentifier, position: Position
    ) -> Location | list[Location] | list[LocationLink]:
        """Request go-to-definition at a position."""
        result = self.endpoint.call_method(
            "textDocument/definition",
            textDocument=text_document,
            position=position,
        )
        if isinstance(result, dict) and "uri" in result:
            return Location.model_validate(result)
        try:
            return [Location.model_validate(r) for r in result]
        except ValidationError:
            return [LocationLink.model_validate(r) for r in result]

    def declaration(
        self, text_document: TextDocumentIdentifier, position: Position
    ) -> Location | list[Location] | list[LocationLink]:
        """Request go-to-declaration at a position."""
        result = self.endpoint.call_method(
            "textDocument/declaration",
            textDocument=text_document,
            position=position,
        )
        if isinstance(result, dict) and "uri" in result:
            return Location.model_validate(result)
        try:
            return [Location.model_validate(r) for r in result]
        except ValidationError:
            return [LocationLink.model_validate(r) for r in result]

    def document_symbol(self, text_document: TextDocumentIdentifier) -> list[DocumentSymbol] | list[SymbolInformation]:
        """Request document symbols."""
        result = self.endpoint.call_method(
            "textDocument/documentSymbol",
            textDocument=text_document.model_dump(),
        )
        try:
            return [DocumentSymbol.model_validate(s) for s in result]
        except ValidationError:
            return [SymbolInformation.model_validate(s) for s in result]

    def completion(
        self,
        text_document: TextDocumentIdentifier,
        position: Position,
        context: CompletionContext,
    ) -> list[CompletionItem] | CompletionList:
        """Request completions at a position."""
        result = self.endpoint.call_method(
            "textDocument/completion",
            textDocument=text_document,
            position=position,
            context=context,
        )
        if isinstance(result, dict) and "isIncomplete" in result:
            return CompletionList.model_validate(result)
        return [CompletionItem.model_validate(r) for r in result]

    def signature_help(self, text_document: TextDocumentIdentifier, position: Position) -> SignatureHelp:
        """Request signature help at a position."""
        result = self.endpoint.call_method(
            "textDocument/signatureHelp",
            textDocument=text_document,
            position=position,
        )
        return SignatureHelp.model_validate(result)
