# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for Lean checks (compilation, forbidden keywords, axioms)."""

from __future__ import annotations

from pathlib import Path

from autoform.eval.lean_checks import ForbiddenKeywordChecker, _lean_file_to_import
from tools.execution.lean.proof_checker import parse_axioms_per_decl


# ---------------------------------------------------------------------------
# ForbiddenKeywordChecker
# ---------------------------------------------------------------------------


def test_no_forbidden_keywords(tmp_path: Path) -> None:
    lean_file = tmp_path / "Foo.lean"
    lean_file.write_text("theorem foo : True := trivial\n")

    violations = ForbiddenKeywordChecker(tmp_path).check()
    assert violations == []


def test_forbidden_keyword_elab(tmp_path: Path) -> None:
    lean_file = tmp_path / "Bad.lean"
    lean_file.write_text("elab my_elab : tactic => sorry\n")

    violations = ForbiddenKeywordChecker(tmp_path).check()
    assert len(violations) == 1
    assert violations[0][1] == "elab"


def test_forbidden_keyword_in_comment_not_detected(tmp_path: Path) -> None:
    """The checker strips comments before scanning for forbidden keywords."""
    lean_file = tmp_path / "Comment.lean"
    lean_file.write_text("-- macro should not be detected\ntheorem foo : True := trivial\n")

    violations = ForbiddenKeywordChecker(tmp_path).check()
    assert len(violations) == 0


def test_skips_lake_directory(tmp_path: Path) -> None:
    lake_dir = tmp_path / ".lake" / "packages" / "Foo"
    lake_dir.mkdir(parents=True)
    lean_file = lake_dir / "Bad.lean"
    lean_file.write_text("macro foo : tactic => sorry\n")

    violations = ForbiddenKeywordChecker(tmp_path).check()
    assert violations == []


def test_macro_and_syntax_not_forbidden(tmp_path: Path) -> None:
    lean_file = tmp_path / "Ok.lean"
    lean_file.write_text("macro foo\nsyntax bar\n")

    violations = ForbiddenKeywordChecker(tmp_path).check()
    assert violations == []


# ---------------------------------------------------------------------------
# _lean_file_to_import
# ---------------------------------------------------------------------------


def test_lean_file_to_import() -> None:
    assert _lean_file_to_import("AlgebraicCombinatorics/CauchyBinet.lean") == "AlgebraicCombinatorics.CauchyBinet"


def test_lean_file_to_import_nested() -> None:
    assert (
        _lean_file_to_import("AlgebraicCombinatorics/SignedCounting/InclusionExclusion1.lean")
        == "AlgebraicCombinatorics.SignedCounting.InclusionExclusion1"
    )


# ---------------------------------------------------------------------------
# parse_axioms_per_decl (from tools, tested here for integration)
# ---------------------------------------------------------------------------


def test_parse_axioms_per_decl_basic() -> None:
    output = (
        "'Foo.bar' depends on axioms: [propext, Classical.choice, Quot.sound]\n"
        "'Foo.baz' depends on axioms: [propext, sorryAx]\n"
        "'Foo.qux' does not depend on any axioms\n"
    )
    result = parse_axioms_per_decl(output)
    assert result["Foo.bar"] == {"propext", "Classical.choice", "Quot.sound"}
    assert result["Foo.baz"] == {"propext", "sorryAx"}
    assert result["Foo.qux"] == set()


def test_parse_axioms_per_decl_empty() -> None:
    assert parse_axioms_per_decl("") == {}
