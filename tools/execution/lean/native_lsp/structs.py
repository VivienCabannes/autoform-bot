# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean-specific LSP data structures.

Extends the generic LSP types with Lean 4 server notifications
(file progress, diagnostics) and requests (proof goals).
"""

from __future__ import annotations

from pydantic import BaseModel

from tools.execution.lsp.structs import Range


class TextDocumentItemSmall(BaseModel):
    """Minimal document identifier used in file progress notifications."""

    uri: str
    version: int


class FileProcessing(BaseModel):
    range: Range
    kind: int


class FileProgress(BaseModel):
    """``$/lean/fileProgress`` notification payload."""

    textDocument: TextDocumentItemSmall
    processing: list[FileProcessing]


class LeanDiagnostic(BaseModel):
    """A single Lean diagnostic (error, warning, info)."""

    source: str
    severity: int
    range: Range
    message: str
    fullRange: Range | None = None


class LeanDiagnostics(BaseModel):
    """``textDocument/publishDiagnostics`` payload from Lean."""

    version: int
    uri: str
    diagnostics: list[LeanDiagnostic]


class PlainGoal(BaseModel):
    """``$/lean/plainGoal`` response — proof state at a position."""

    rendered: str
    goals: list[str]
