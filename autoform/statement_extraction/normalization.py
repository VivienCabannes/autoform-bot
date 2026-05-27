# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Name normalization for statement deduplication."""

from __future__ import annotations

import re

_KIND_ABBREVIATIONS = {
    "thm": "theorem",
    "lem": "lemma",
    "prop": "proposition",
    "def": "definition",
    "defn": "definition",
    "cor": "corollary",
    "conj": "conjecture",
    "ax": "axiom",
    "const": "construction",
    "cl": "claim",
}

_KIND_PATTERN = re.compile(
    r"^(theorem|thm|lemma|lem|proposition|prop|definition|defn|def|corollary|cor|conjecture|conj|axiom|ax|construction|const|claim|cl)\.?\s*",
    re.IGNORECASE,
)

_HAS_NUMBER_RE = re.compile(r"\d")
_PARENS_RE = re.compile(r"\s*\(.*\)\s*$")


def normalize_statement_name(name: str) -> str:
    """Normalize a statement name to a canonical form for comparison.

    Strips parenthetical nicknames only when a number is present.
    Expands abbreviations, normalizes whitespace.

    "Theorem 3.2 (Heine-Borel)" → "theorem 3.2"
    "Thm. 3.2" → "theorem 3.2"
    "Def. (Presheaf)" → "definition (presheaf)"
    "Lemma (Noether Normalization)" → "lemma (noether normalization)"
    """
    name = name.strip()
    if _HAS_NUMBER_RE.search(name):
        name = _PARENS_RE.sub("", name)
    name = name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[.:;,]+$", "", name)
    m = _KIND_PATTERN.match(name)
    if m:
        abbrev = m.group(1).lower().rstrip(".")
        full = _KIND_ABBREVIATIONS.get(abbrev, abbrev)
        rest = name[m.end() :].lstrip(". ")
        name = f"{full} {rest}" if rest else full
    return name
