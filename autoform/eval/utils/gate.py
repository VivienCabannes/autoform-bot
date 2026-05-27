# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""EvalGate — per-statement evaluation for merge gating and on-demand assessment.

Wraps the autoformalization eval machinery to support two use cases:

1. **Merge gate**: evaluate a subset of statements against a worker's worktree
2. **Full eval**: run the complete evaluation (repo checks + all statements)

Manages judge agent lifecycle internally via async context manager.
Target loading is external — callers provide a pre-built target index
(see ``build_target_index`` in ``tracker.py``).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.agent import Agent
from core.agent.loader import load_agent_definition
from core.eval.dtypes import Score
from core.eval.rubric import load_json_rubrics
from core.trace import AgentTrace, TraceStore

from ..types import FormalizationTarget
from ..metrics import InferenceFactory
from ..lean_checks import AxiomsChecker, ForbiddenKeywordChecker

from ..compilation_grader import AxiomGrader, CompilationGrader, LLMJuryGrader, RUBRICS_DIR, StatementGrader

logger = logging.getLogger(__name__)

_JUDGE_AGENT_DIR = Path(__file__).parent.parent / "agents" / "judge"


# ---------------------------------------------------------------------------
# Precomputed repo-level check results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoCheckResults:
    """Precomputed repo-level check results, injectable into EvalGate."""

    forbidden_keyword_violations: list[tuple[str, str]] = field(default_factory=list)
    axiom_violations: dict[str, frozenset[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalGateResult:
    """Result of evaluating a set of statements."""

    passed: bool
    statement_scores: dict[int, Score] = field(default_factory=dict)
    feedback: str = ""


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class EvalGate:
    """Evaluates specific statements from a formalization repo.

    Use as an async context manager — creates judge agents on enter,
    cleans them up on exit.

    Target loading is the caller's responsibility. Use
    ``build_target_index()`` from ``tracker.py`` to build the target
    dict from either a YAML task file or an ``ItemTracker``.

    Example::

        targets = build_target_index(task_file=Path("targets.yaml"))
        async with EvalGate(make_inference, targets, repo_dir=repo) as gate:
            result = await gate.evaluate_statements([1, 3, 5], repo)
            if not result.passed:
                print(result.feedback)
    """

    def __init__(
        self,
        inference_factory: InferenceFactory,
        targets: dict[int, FormalizationTarget],
        *,
        repo_dir: Path,
        repo_checks: RepoCheckResults | None = None,
        trace_store: TraceStore | None = None,
        task_id: str | None = None,
    ) -> None:
        self._inference_factory = inference_factory
        self._targets_by_id = dict(targets)
        self._repo_dir = repo_dir
        self._repo_checks = repo_checks
        self._trace_store = trace_store
        self._task_id = task_id
        self._stack: AsyncExitStack | None = None
        self._grader: StatementGrader | None = None

    async def __aenter__(self) -> EvalGate:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        # Repo-level checks: use precomputed results or run them now
        if self._repo_checks is not None:
            forbidden = self._repo_checks.forbidden_keyword_violations
            axiom_violations = self._repo_checks.axiom_violations
        else:
            forbidden = ForbiddenKeywordChecker(self._repo_dir).check()
            checked = [
                (t.lean_declaration, t.lean_file)
                for t in self._targets_by_id.values()
                if t.lean_declaration and t.lean_file
            ]
            decl_names = [d for d, _ in checked]
            lean_files = [f for _, f in checked]
            _, axiom_violations = await AxiomsChecker(repo_dir=self._repo_dir).check(decl_names, lean_files)

        # Create one judge agent per rubric for concurrent evaluation
        definition = load_agent_definition(_JUDGE_AGENT_DIR)
        num_rubrics = len(load_json_rubrics(RUBRICS_DIR))
        agents: list[Agent] = []
        for i in range(num_rubrics):
            agent = await self._stack.enter_async_context(
                Agent(definition=definition, inference=self._inference_factory(), trace_store=self._trace_store)
            )
            if self._trace_store:
                agent.set_trace(AgentTrace(id=f"judge/{i}", task_id=self._task_id))
            agents.append(agent)
        self._grader = StatementGrader(
            CompilationGrader.create(True, forbidden_keyword_violations=forbidden),
            AxiomGrader(axiom_violations),
            LLMJuryGrader.create(agents),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.__aexit__(*exc)
            self._stack = None
        self._grader = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def all_statement_ids(self) -> list[int]:
        """All statement IDs known from the targets."""
        return sorted(self._targets_by_id.keys())

    async def evaluate_statements(
        self,
        statement_ids: list[int],
        repo_dir: Path,
    ) -> EvalGateResult:
        """Grade specific statements.

        Args:
            statement_ids: Which statements to evaluate.
            repo_dir: Repo directory containing the Lean source files
                (e.g. the main workspace or a worker's worktree).
        """
        if self._grader is None:
            raise RuntimeError("EvalGate must be used as an async context manager")

        scores: dict[int, Score] = {}
        feedback_parts: list[str] = []

        for stmt_id in statement_ids:
            target = self._targets_by_id.get(stmt_id)
            if target is None or not target.lean_file:
                logger.info("Statement %d has no lean file mapping yet, skipping eval", stmt_id)
                continue
            resolved = self._resolve_statement(target, repo_dir)
            if resolved is None:
                scores[stmt_id] = Score(value=0.0, passed=False, feedback="Could not resolve statement")
                feedback_parts.append(f"Statement {stmt_id}: could not resolve")
                continue

            target, source = resolved
            score = await self._grader.grade(target, source)

            scores[stmt_id] = score
            status = "PASS" if score.passed else "FAIL"
            feedback_parts.append(f"Statement {stmt_id} ({target.name}): {status} ({score.value:.2f})")
            if score.feedback:
                for line in score.feedback.splitlines():
                    feedback_parts.append(f"  {line}")

        all_passed = all(s.passed for s in scores.values())
        return EvalGateResult(
            passed=all_passed,
            statement_scores=scores,
            feedback="\n".join(feedback_parts),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_statement(
        target: FormalizationTarget,
        repo_dir: Path,
    ) -> tuple[FormalizationTarget, str] | None:
        """Resolve a target to its Lean source text."""
        if not target.lean_file:
            return None

        lean_path = repo_dir / target.lean_file
        if not lean_path.exists():
            logger.warning("Lean file not found: %s", lean_path)
            return None

        return target, lean_path.read_text(encoding="utf-8")
