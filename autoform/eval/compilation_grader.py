# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Per-statement graders for autoformalization evaluation.

Contains three layers of graders, each implementing
``Grader[FormalizationTarget, str]``:

- **CompilationGrader** — binary repo-level gate (same score for all statements).
- **AxiomGrader** — per-declaration axiom check (lookup from precomputed violations).
- **LLMJuryGrader** — weighted LLM rubric aggregation (correctness, faithfulness, style).

``StatementGrader`` composes the three into a short-circuit pipeline:
compilation → axioms → jury.  It is used by ``AutoformGrader`` (in
``grader.py``) for concurrent per-statement grading after repo-level
checks pass, and by ``EvalGate`` for on-demand statement evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from core.eval.dtypes import Score
from core.eval.grader import Grader, JuryGrader, LLMJudgeGrader
from core.eval.rubric import load_json_rubrics

from .types import FormalizationTarget

if TYPE_CHECKING:
    from core.agent import Agent

RUBRICS_DIR = Path(__file__).parent / "rubrics"


# ---------------------------------------------------------------------------
# Programmatic graders (precomputed results)
# ---------------------------------------------------------------------------


@dataclass
class CompilationGrader(Grader[FormalizationTarget, str]):
    """Binary pass/fail — same result for all statements.

    Repo-level gate: fails if the project doesn't compile or if forbidden
    keywords (e.g. ``elab``) are detected, since they can bypass proof
    checking.  The score is determined once and returned unchanged for
    every statement.
    """

    score: Score

    @classmethod
    def create(
        cls,
        compiled: bool,
        compilation_output: str = "",
        forbidden_keyword_violations: list[tuple[str, str]] | None = None,
    ) -> CompilationGrader:
        passed = compiled and not forbidden_keyword_violations
        parts: list[str] = []
        if not compiled:
            parts.append(compilation_output[:200])
        if forbidden_keyword_violations:
            kws = ", ".join(f"{f}:{kw}" for f, kw in forbidden_keyword_violations)
            parts.append(f"Forbidden keywords: {kws}")
        return cls(
            score=Score(
                value=1.0 if passed else 0.0,
                passed=passed,
                feedback="; ".join(parts),
                metrics={"compilation": 1 if passed else 0},
            )
        )

    async def grade(self, datum: FormalizationTarget, output: str) -> Score:
        return self.score


@dataclass
class AxiomGrader(Grader[FormalizationTarget, str]):
    """Checks axiom dependencies per declaration.

    Receives precomputed violations (from ``AxiomsChecker.check``)
    and looks up the result for each declaration.
    """

    violations: dict[str, frozenset[str]]

    async def grade(self, datum: FormalizationTarget, output: str) -> Score:
        disallowed = self.violations.get(datum.lean_declaration, frozenset())
        if disallowed:
            return Score(
                value=0.0,
                passed=False,
                feedback=f"Disallowed axioms: {', '.join(sorted(disallowed))}",
                metrics={"axioms": 0},
            )
        return Score(value=1.0, passed=True, feedback="", metrics={"axioms": 1})


# ---------------------------------------------------------------------------
# LLM jury grader
# ---------------------------------------------------------------------------


@dataclass
class LLMJuryGrader(JuryGrader[FormalizationTarget, str]):
    """Jury grader that aggregates LLM rubric scores using weights.

    Each rubric gets its own ``LLMJudgeGrader`` with a dedicated agent,
    enabling truly concurrent evaluation via the parent's ``asyncio.gather``.
    Uses weighted average for the overall score and requires all members
    to pass individually.
    """

    members: list[tuple[str, Grader]] = field(default_factory=list)
    _weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        agents: list[Agent],
        *,
        rubrics_dir: Path = RUBRICS_DIR,
    ) -> LLMJuryGrader:
        """Create a jury grader from active rubrics.

        Args:
            agents: One LLM judge agent per rubric, evaluated concurrently.
            rubrics_dir: Directory containing rubric JSON files.
        """
        rubrics = load_json_rubrics(rubrics_dir)
        if len(agents) != len(rubrics):
            raise ValueError(f"Expected {len(rubrics)} agents (one per rubric), got {len(agents)}")
        members: list[tuple[str, Grader]] = [
            (rubric.name, LLMJudgeGrader(agent=agent, rubric=rubric)) for rubric, agent in zip(rubrics, agents)
        ]
        weights = {r.name: r.spec.weight for r in rubrics}
        return cls(members=members, _weights=weights)

    def aggregate(self, scores: dict[str, Score]) -> Score:
        """Weighted average of scores. Passes only if all members pass."""
        total_weight = 0.0
        weighted_sum = 0.0
        all_passed = True
        combined_metrics: dict[str, object] = {}
        feedback_parts: list[str] = []

        for name, score in scores.items():
            weight = self._weights.get(name, 1.0 / len(scores))
            weighted_sum += score.value * weight
            total_weight += weight
            all_passed = all_passed and score.passed
            combined_metrics.update(score.metrics)

            raw = score.metrics.get(name, "?")
            if isinstance(raw, int):
                feedback_parts.append(f"[{name}={raw}/5] {score.feedback}")
            elif score.feedback:
                feedback_parts.append(f"[{name}] {score.feedback}")

        overall_value = weighted_sum / total_weight if total_weight > 0 else 0.0
        return Score(
            value=overall_value,
            passed=all_passed,
            feedback="\n".join(feedback_parts),
            metrics=combined_metrics,
        )


# ---------------------------------------------------------------------------
# Composite per-statement grader
# ---------------------------------------------------------------------------


class StatementGrader(Grader[FormalizationTarget, str]):
    """Per-statement pipeline: compilation → axioms → jury.

    Short-circuits on programmatic failures — if compilation or axiom
    checks fail, LLM rubrics are skipped entirely.
    """

    def __init__(
        self,
        compilation: CompilationGrader,
        axioms: AxiomGrader,
        jury: LLMJuryGrader,
    ) -> None:
        self._compilation = compilation
        self._axioms = axioms
        self._jury = jury

    async def grade(self, datum: FormalizationTarget, output: str) -> Score:
        compilation_score = await self._compilation.grade(datum, output)
        if not compilation_score.passed:
            return compilation_score

        axiom_score = await self._axioms.grade(datum, output)
        if not axiom_score.passed:
            return axiom_score

        jury_score = await self._jury.grade(datum, output)

        # Merge programmatic metrics into the jury result
        merged_metrics = {
            **compilation_score.metrics,
            **axiom_score.metrics,
            **jury_score.metrics,
        }
        return Score(
            value=jury_score.value,
            passed=jury_score.passed,
            feedback=jury_score.feedback,
            metrics=merged_metrics,
        )
