# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Standard LSP data structures as Pydantic models.

Language-agnostic types only. Language-specific extensions (e.g. Lean's
PlainGoal, FileProgress) belong in the language-specific layer.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import AnyUrl, BaseModel


# ---------------------------------------------------------------------------
# Language identifiers
# ---------------------------------------------------------------------------


class LanguageIdentifier(StrEnum):
    BAT = "bat"
    BIBTEX = "bibtex"
    CLOJURE = "clojure"
    COFFEESCRIPT = "coffeescript"
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    CSS = "css"
    DIFF = "diff"
    DOCKERFILE = "dockerfile"
    FSHARP = "fsharp"
    GIT_COMMIT = "git-commit"
    GIT_REBASE = "git-rebase"
    GO = "go"
    GROOVY = "groovy"
    HANDLEBARS = "handlebars"
    HTML = "html"
    INI = "ini"
    JAVA = "java"
    JAVASCRIPT = "javascript"
    JSON = "json"
    LATEX = "latex"
    LEAN = "lean"
    LESS = "less"
    LUA = "lua"
    MAKEFILE = "makefile"
    MARKDOWN = "markdown"
    OBJECTIVE_C = "objective-c"
    OBJECTIVE_CPP = "objective-cpp"
    PERL = "perl"
    PHP = "php"
    POWERSHELL = "powershell"
    PUG = "jade"
    PYTHON = "python"
    R = "r"
    RAZOR = "razor"
    RUBY = "ruby"
    RUST = "rust"
    SASS = "sass"
    SCSS = "scss"
    SHADERLAB = "shaderlab"
    SHELL_SCRIPT = "shellscript"
    SQL = "sql"
    SWIFT = "swift"
    TYPESCRIPT = "typescript"
    TEX = "tex"
    VB = "vb"
    XML = "xml"
    XSL = "xsl"
    YAML = "yaml"


# ---------------------------------------------------------------------------
# Core document types
# ---------------------------------------------------------------------------


class Position(BaseModel):
    line: int
    character: int


class Range(BaseModel):
    start: Position
    end: Position


class Location(BaseModel):
    uri: str
    range: Range


class LocationLink(BaseModel):
    originSelectionRange: Range | None = None
    targetUri: AnyUrl
    targetRange: Range
    targetSelectionRange: Range


class TextDocumentItem(BaseModel):
    uri: str
    languageId: str
    version: int
    text: str


class TextDocumentIdentifier(BaseModel):
    uri: str


class VersionedTextDocumentIdentifier(BaseModel):
    uri: str
    version: int


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


class SymbolKind(IntEnum):
    File = 1
    Module = 2
    Namespace = 3
    Package = 4
    Class = 5
    Method = 6
    Property = 7
    Field = 8
    Constructor = 9
    Enum = 10
    Interface = 11
    Function = 12
    Variable = 13
    Constant = 14
    String = 15
    Number = 16
    Boolean = 17
    Array = 18
    Object = 19
    Key = 20
    Null = 21
    EnumMember = 22
    Struct = 23
    Event = 24
    Operator = 25
    TypeParameter = 26


class SymbolTag(IntEnum):
    Deprecated = 1


class DocumentSymbol(BaseModel):
    name: str
    detail: str | None = None
    kind: SymbolKind
    tags: list[SymbolTag] | None = None
    deprecated: bool | None = None
    range: Range
    selectionRange: Range
    children: list[DocumentSymbol] | None = None


class SymbolInformation(BaseModel):
    name: str
    kind: SymbolKind
    deprecated: bool | None = None
    location: Location
    containerName: str | None = None


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


class CompletionTriggerKind(IntEnum):
    Invoked = 1
    TriggerCharacter = 2
    TriggerForIncompleteCompletions = 3


class CompletionContext(BaseModel):
    triggerKind: CompletionTriggerKind
    triggerCharacter: str | None = None


class CompletionItemKind(IntEnum):
    Text = 1
    Method = 2
    Function = 3
    Constructor = 4
    Field = 5
    Variable = 6
    Class = 7
    Interface = 8
    Module = 9
    Property = 10
    Unit = 11
    Value = 12
    Enum = 13
    Keyword = 14
    Snippet = 15
    Color = 16
    File = 17
    Reference = 18
    Folder = 19
    EnumMember = 20
    Constant = 21
    Struct = 22
    Event = 23
    Operator = 24
    TypeParameter = 25


class CompletionItem(BaseModel):
    label: str
    kind: CompletionItemKind | None = None
    detail: str | None = None
    documentation: str | dict | None = None
    insertText: str | None = None
    insertTextFormat: int | None = None


class CompletionList(BaseModel):
    isIncomplete: bool
    items: list[CompletionItem]


# ---------------------------------------------------------------------------
# Signature help
# ---------------------------------------------------------------------------


class SignatureInformation(BaseModel):
    label: str
    documentation: str | dict | None = None
    parameters: list[dict] | None = None


class SignatureHelp(BaseModel):
    signatures: list[SignatureInformation]
    activeSignature: int | None = None
    activeParameter: int | None = None


# ---------------------------------------------------------------------------
# Hover
# ---------------------------------------------------------------------------


class MarkupContent(BaseModel):
    kind: str  # "plaintext" | "markdown"
    value: str


class HoverResult(BaseModel):
    contents: str | dict | list[str | dict] | MarkupContent
    range: Range | None = None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class DiagnosticSeverity(IntEnum):
    Error = 1
    Warning = 2
    Information = 3
    Hint = 4


class Diagnostic(BaseModel):
    range: Range
    severity: int | None = None
    code: int | str | None = None
    source: str | None = None
    message: str


class PublishDiagnosticsParams(BaseModel):
    uri: str
    version: int | None = None
    diagnostics: list[Diagnostic]
