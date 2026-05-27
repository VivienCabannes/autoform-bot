# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Repo-level autoformalization grader.

Runs programmatic checks (compilation, forbidden keywords, axioms) then
grades all statements concurrently, short-circuiting on per-statement
axiom violations before invoking the per-statement grader.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from logging import getLogger
from pathlib import Path

from core.eval.dtypes import EvalResult, Score
from core.eval.grader import Grader

from .lean_checks import AxiomsChecker, CompilationChecker, ForbiddenKeywordChecker
from .compilation_grader import AxiomGrader, CompilationGrader
from .types import FormalizationTarget

logger = getLogger(__name__)


class AutoformGrader:
    """Repo-level orchestrator: compilation → axioms → concurrent statement grading.

    Runs repo-level checks once, then fans out per-statement grading via
    ``asyncio.gather``.  Each concurrent coroutine calls ``make_grader``
    to obtain its own grader instance — no shared mutable state.

    After ``grade()`` completes, repo-level check results are available
    as attributes for report building.
    """

    def __init__(
        self,
        repo_dir: Path,
        targets: list[FormalizationTarget],
        make_grader: Callable[[], Awaitable[Grader[FormalizationTarget, str]]],
        *,
        allowed_axioms: frozenset[str] = frozenset(),
    ) -> None:
        self._repo_dir = repo_dir
        self._targets = targets
        self._make_grader = make_grader
        self._allowed_axioms = allowed_axioms

        # Set after grade() — available for build_report()
        self.compiled: bool = False
        self.compilation_output: str = ""
        self.forbidden_keyword_violations: list[tuple[str, str]] = []
        self.axiom_violations: dict[str, frozenset[str]] = {}

    async def _repo_checks(self) -> CompilationGrader:
        """Run compilation, forbidden keywords, and axiom checks.

        Populates ``compiled``, ``compilation_output``,
        ``forbidden_keyword_violations``, and ``axiom_violations``.
        """
        self.compiled, self.compilation_output = await CompilationChecker(self._repo_dir).check()
        self.forbidden_keyword_violations = ForbiddenKeywordChecker(self._repo_dir).check()
        compilation = CompilationGrader.create(
            self.compiled,
            self.compilation_output,
            self.forbidden_keyword_violations,
        )

        if compilation.score.passed:
            checked = [(t.lean_declaration, t.lean_file) for t in self._targets if t.lean_declaration and t.lean_file]
            decl_names = [d for d, _ in checked]
            lean_files = [f for _, f in checked]
            _, self.axiom_violations = await AxiomsChecker(self._repo_dir, allowed_axioms=self._allowed_axioms).check(
                decl_names, lean_files
            )

        return compilation

    async def grade(self) -> list[EvalResult]:
        """Run the full evaluation pipeline and return per-statement results."""
        compilation = await self._repo_checks()

        if not compilation.score.passed:
            return [
                EvalResult(
                    datum_id=t.lean_declaration or t.name,
                    score=compilation.score,
                    datum=t,
                )
                for t in self._targets
            ]

        axioms = AxiomGrader(self.axiom_violations)

        async def grade_one(target: FormalizationTarget) -> EvalResult:
            lean_path = self._repo_dir / target.lean_file
            source = lean_path.read_text(encoding="utf-8") if lean_path.exists() else ""
            datum_id = target.lean_declaration or target.name

            # Per-statement axiom short-circuit
            axiom_score = await axioms.grade(target, source)
            if not axiom_score.passed:
                return EvalResult(datum_id=datum_id, score=axiom_score, datum=target, output=source)

            grader = await self._make_grader()
            grader_score = await grader.grade(target, source)
            merged_metrics = {
                **compilation.score.metrics,
                **axiom_score.metrics,
                **grader_score.metrics,
            }
            return EvalResult(
                datum_id=datum_id,
                score=Score(
                    value=grader_score.value,
                    passed=grader_score.passed,
                    feedback=grader_score.feedback,
                    metrics=merged_metrics,
                ),
                datum=target,
                output=source,
            )

        gradeable = [t for t in self._targets if t.lean_file]
        return list(await asyncio.gather(*(grade_one(t) for t in gradeable)))
