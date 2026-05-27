# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Utilities for extracting declarations from Lean code.

Provides `extract_all_declarations`, `extract_all_sorry_declarations`,
`build_faithfulness_suffix`, `build_axiom_check_snippet`, and header
parsing helpers.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator

from .constant import DECL_KINDS, NONCOMPUTABLE_SUBSUMES, SORRY_PROOFS


# ---------------------------------------------------------------------------
# Comment-aware line iteration
# ---------------------------------------------------------------------------


def _iter_code_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(index, stripped_line)`` for lines outside comments.

    Walks characters with nested ``/- ... -/`` tracking and ``--`` line
    comments. Block-comment depth is carried across lines.
    """
    depth = 0
    for i, line in enumerate(lines):
        out: list[str] = []
        j, n = 0, len(line)
        while j < n:
            if depth > 0:
                if j + 1 < n and line[j] == "/" and line[j + 1] == "-":
                    depth += 1
                    j += 2
                elif j + 1 < n and line[j] == "-" and line[j + 1] == "/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
                continue
            if j + 1 < n and line[j] == "-" and line[j + 1] == "-":
                break
            if j + 1 < n and line[j] == "/" and line[j + 1] == "-":
                depth = 1
                j += 2
                continue
            out.append(line[j])
            j += 1
        stripped = "".join(out).strip()
        if stripped:
            yield i, stripped


# ---------------------------------------------------------------------------
# Declaration extraction
# ---------------------------------------------------------------------------

_SORRY_DECL_RE = re.compile(
    rf"((?:noncomputable\s+)?(?:{'|'.join(DECL_KINDS)}))"
    rf"\s+(\S+)((?:(?!\b(?:{'|'.join(DECL_KINDS)})\b)[\s\S])*?):=\s*(?:by\s+)?(?:{'|'.join(SORRY_PROOFS)})"
)

_DECL_RE = re.compile(
    rf"((?:noncomputable\s+)?(?:{'|'.join(DECL_KINDS)}))"
    rf"\s+(\S+)((?:(?!\b(?:{'|'.join(DECL_KINDS)})\b)[\s\S])*?):="
)


@dataclasses.dataclass(frozen=True)
class Declaration:
    """A declaration extracted from Lean problem code."""

    kind: str  # e.g. "theorem", "noncomputable abbrev"
    name: str  # e.g. "putnam_1971_b2"
    signature: str  # everything between name and `:=` (args + return type)
    namespace: str = ""  # e.g. "Foo.Bar" for declarations inside namespace Foo.Bar


def _build_namespace_map(code: str) -> dict[int, str]:
    """Map code-line indices (0-based) to their enclosing namespace path."""
    lines = code.split("\n")
    ns_stack: list[str] = []
    result: dict[int, str] = {}

    for idx, stripped in _iter_code_lines(lines):
        result[idx] = ".".join(ns_stack) if ns_stack else ""

        ns_match = re.match(r"^namespace\s+(\S+)", stripped)
        if ns_match:
            ns_stack.append(ns_match.group(1))
            continue

        end_match = re.match(r"^end\b", stripped)
        if end_match and ns_stack:
            ns_stack.pop()

    return result


def extract_all_sorry_declarations(code: str) -> list[Declaration]:
    """Extract all declarations ending with `:= sorry` or `:= by sorry`."""
    ns_map = _build_namespace_map(code)
    results = []
    for m in _SORRY_DECL_RE.finditer(code):
        line_idx = code[: m.start()].count("\n")
        namespace = ns_map.get(line_idx, "")
        results.append(
            Declaration(
                kind=m.group(1),
                name=m.group(2),
                signature=m.group(3),
                namespace=namespace,
            )
        )
    return results


def extract_all_declarations(code: str, sorry_only: bool = False) -> list[Declaration]:
    """Extract all declarations (regardless of proof body).

    Args:
        code: Lean source code.
        sorry_only: If True, only extract sorry-terminated declarations.
    """
    if sorry_only:
        return extract_all_sorry_declarations(code)
    ns_map = _build_namespace_map(code)
    results = []
    for m in _DECL_RE.finditer(code):
        line_idx = code[: m.start()].count("\n")
        namespace = ns_map.get(line_idx, "")
        results.append(
            Declaration(
                kind=m.group(1),
                name=m.group(2),
                signature=m.group(3),
                namespace=namespace,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Faithfulness & axiom-check snippet generation
# ---------------------------------------------------------------------------


def _skip_bracket(signature: str, i: int, open_ch: str, close_ch: str) -> int:
    """Advance `i` past a balanced bracket pair."""
    depth = 1
    i += 1
    while i < len(signature) and depth > 0:
        if signature[i] == open_ch:
            depth += 1
        elif signature[i] == close_ch:
            depth -= 1
        i += 1
    return i


def _names_before_colon(content: str) -> str:
    """Return the names portion of a binder (everything before the first top-level `:`)."""
    depth = 0
    for j, c in enumerate(content):
        if c in ("(", "{", "["):
            depth += 1
        elif c in (")", "}", "]"):
            depth -= 1
        elif c == ":" and depth == 0:
            return content[:j].strip()
    return content.strip()


def _extract_explicit_args(signature: str) -> list[str]:
    """Extract names from explicit ``(...)`` binders in a Lean signature.

    Flattens the signature through ``_iter_code_lines`` first so ``--`` and
    ``/- -/`` comments can't confuse the binder walk.
    """
    signature = " ".join(s for _, s in _iter_code_lines(signature.split("\n")))

    args: list[str] = []
    i = 0
    while i < len(signature):
        ch = signature[i]
        if ch == ":":
            break
        if ch in ("{", "["):
            close = "}" if ch == "{" else "]"
            i = _skip_bracket(signature, i, ch, close)
            continue
        if ch == "(":
            start = i + 1
            i = _skip_bracket(signature, i, "(", ")")
            content = signature[start : i - 1]
            names_part = _names_before_colon(content)
            if names_part:
                args.extend(names_part.split())
            continue
        i += 1
    return args


def build_faithfulness_suffix(declarations: list[Declaration]) -> str:
    """Build a Lean code suffix that type-checks declaration faithfulness.

    For each sorry-terminated declaration `theorem foo (args) : T`, emits
    a matching declaration `foo_original (args) : T := Ns.foo args`.
    """
    if not declarations:
        return ""
    lines = []
    for decl in declarations:
        base_kind = decl.kind.split()[-1]
        if base_kind in NONCOMPUTABLE_SUBSUMES:
            prefix = base_kind
        else:
            prefix = f"noncomputable {base_kind}"
        args = _extract_explicit_args(decl.signature)
        arg_str = " ".join(args)
        qualified_name = f"{decl.namespace}.{decl.name}" if decl.namespace else decl.name
        rhs = f"{qualified_name} {arg_str}" if arg_str else qualified_name
        lines.append(f"{prefix} {decl.name}_original{decl.signature}:= {rhs}")
    return "\n".join(lines) + "\n"


def build_axiom_check_snippet(declarations: list[Declaration]) -> str:
    """Build ``#print axioms`` commands for each declaration."""
    if not declarations:
        return ""
    lines = []
    for decl in declarations:
        qualified_name = f"{decl.namespace}.{decl.name}" if decl.namespace else decl.name
        lines.append(f"#print axioms {qualified_name}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Import & open-directive helpers
# ---------------------------------------------------------------------------


def get_imports(code: str, root_only: bool = False) -> list[str] | set[str]:
    """Extract import statements from Lean code.

    Args:
        code: Lean source code.
        root_only: If True, return a ``set[str]`` of root import names
            (e.g. ``{"Mathlib"}`` from ``import Mathlib.Data.Nat``) instead
            of full import lines.
    """
    imports = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            imports.append(stripped)
        elif stripped and not stripped.startswith("--") and not stripped.startswith("/-"):
            break
    if root_only:
        return {stmt[7:].strip().split(".")[0] for stmt in imports}
    return imports


def split_imports_and_body(code: str) -> tuple[list[str], str, int]:
    """Split Lean code into imports and body, tracking line offset.

    Uses ``_iter_code_lines`` to skip comments and blank lines when
    locating the boundary between imports and body.

    Returns:
        A tuple of ``(imports, body, body_start)`` where *imports* is a
        sorted, deduplicated list of import module paths, *body* is the
        remaining code, and *body_start* is the 0-based line index where
        the body begins in the original source.
    """
    lines = code.splitlines()
    import_lines: list[str] = []
    body_start = len(lines)

    for i, stripped in _iter_code_lines(lines):
        if stripped.startswith("import "):
            import_lines.append(stripped[7:].strip())
        else:
            body_start = i
            break

    body = "\n".join(lines[body_start:]).lstrip("\n")
    return sorted(set(import_lines)), body, body_start


def get_open_directives(codes: list[str]) -> list[str]:
    """Extract and deduplicate top-level ``open`` directives from multiple code blocks.

    Collects directives from each code block in order, deduplicates while
    preserving first-seen order, and skips scoped ``open ... in`` forms.
    """
    seen: set[str] = set()
    result: list[str] = []
    for code in codes:
        for _, stripped in _iter_code_lines(code.splitlines()):
            if not stripped.startswith("open ") or stripped.rstrip().endswith(" in"):
                continue
            if stripped not in seen:
                seen.add(stripped)
                result.append(stripped)
    return result
