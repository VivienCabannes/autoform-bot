# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Smoke test for the full assessment pipeline on a real HighDimensionalStatistics target.

Runs each step individually and prints results so you can inspect:
1. Matching — what declaration was found
2. Axiom extraction — what axioms it uses
3. Grading — each rubric's score and reasoning
"""

import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from autoform.eval.grading import grade_statement
from autoform.eval.lean_checks import AxiomsChecker
from autoform.eval.matching import match_statement
from autoform.eval.types import AssessmentTarget, FormalizationTarget
from core.agent import Agent
from core.agent.loader import load_agent_definition
from core.eval.rubric import load_lenient_rubrics
from core.inference import InferenceProtocol
from core.inference.client import create_inference, lookup_model
from core.trace import AgentTrace, TraceStore
from dotenv import load_dotenv
from tools.files.filesystem.server import filesystem_server, FilesystemConfig
from tools.search.mathlib.server import mathlib_server, MathlibConfig

load_dotenv()

_APP_DIR = Path(__file__).resolve().parent
_REPO_DIR = Path(
    os.environ.get("EVAL_REPO_DIR", str(_APP_DIR.parent.parent / "eval-repo"))
)
_CODE_DIR = _REPO_DIR / "Atlas" / "HighDimensionalStatistics"
_BOOK_DIR = _REPO_DIR / "books" / "HighDimensionalStatistics"
_MATHLIB_DIR = _APP_DIR.parent.parent / "submodules" / "mathlib"
_JUDGE_AGENT_DIR = _APP_DIR / "agents" / "judge"
_RUBRICS_DIR = _APP_DIR / "rubrics"

# Proposition 1.1 — Gaussian tail bound
_STATEMENT = FormalizationTarget(
    name="Proposition 1.1",
    description=(
        "Let X be a Gaussian random variable with mean μ and variance σ² then "
        "for any t > 0, it holds P(X - μ > t) ≤ (1/√(2π)) · e^(-t²/(2σ²)) / t. "
        "By symmetry we also have P(X - μ < -t) ≤ (1/√(2π)) · e^(-t²/(2σ²)) / t, "
        "and P(|X - μ| > t) ≤ √(2/π) · e^(-t²/(2σ²)) / t."
    ),
    kind="proposition",
    location="Chapter 1, Section 1.1",
)


def _separator(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    model_def = lookup_model("Opus 4.6")

    def inference_factory() -> InferenceProtocol:
        return create_inference(model_def)

    trace_store = TraceStore.create_run(_APP_DIR / "output" / "test_traces")
    print(f"Traces: {trace_store.run_dir}")

    # ---------------------------------------------------------------
    # Step 1: Matching
    # ---------------------------------------------------------------
    _separator("Step 1: Matching")
    print(f"Statement: {_STATEMENT.name} ({_STATEMENT.kind})")
    print(f"Location:  {_STATEMENT.location}")
    print(f"Code dir:  {_CODE_DIR}")

    match = await match_statement(
        _STATEMENT,
        _CODE_DIR,
        _BOOK_DIR,
        inference_factory,
        trace_store,
        idx=0,
    )

    print("\nMatch result:")
    print(f"  lean_declaration: {match.lean_declaration}")
    print(f"  lean_file:        {match.lean_file}")
    print(f"  confidence:       {match.confidence}")
    print(f"  reasoning:        {match.reasoning}")

    if match.confidence == "not_found" or match.lean_declaration is None:
        print("\nNo match found — stopping here.")
        return

    # ---------------------------------------------------------------
    # Step 2: Axiom extraction
    # ---------------------------------------------------------------
    _separator("Step 2: Axiom Extraction")
    axiom_checker = AxiomsChecker(_REPO_DIR)
    all_axioms, violations = await axiom_checker.check(
        [match.lean_declaration],
        [match.lean_file] if match.lean_file else None,
    )
    axiom_set = all_axioms.get(match.lean_declaration, frozenset())
    axioms_str = (
        ", ".join(sorted(axiom_set)) if axiom_set else "None (no axioms detected)"
    )

    print(f"Declaration: {match.lean_declaration}")
    print(f"All axioms:  {axiom_set}")
    print(f"Violations:  {violations}")
    print(f"Axioms str:  {axioms_str}")

    # ---------------------------------------------------------------
    # Step 3: Grading
    # ---------------------------------------------------------------
    _separator("Step 3: Grading")

    safe_description = _STATEMENT.description.replace("{", "{{").replace("}", "}}")
    target = AssessmentTarget(
        name=_STATEMENT.name,
        description=safe_description,
        kind=_STATEMENT.kind,
        location=_STATEMENT.location,
        lean_declaration=match.lean_declaration,
        lean_file=match.lean_file,
        axioms=axioms_str,
        book_dir=str(_BOOK_DIR),
        match_confidence=match.confidence,
        match_reasoning=match.reasoning,
    )

    judge_definition = load_agent_definition(_JUDGE_AGENT_DIR)
    mathlib_cfg = mathlib_server(MathlibConfig(repo_root=str(_MATHLIB_DIR)))
    fs_cfg = filesystem_server(
        FilesystemConfig(allowed_dirs=(str(_REPO_DIR), str(_BOOK_DIR)))
    )

    rubrics = load_lenient_rubrics(_RUBRICS_DIR)
    print(f"Rubrics: {[r.name for r in rubrics]}")

    async with AsyncExitStack() as stack:
        agents: list[Agent] = []
        for i in range(len(rubrics)):
            judge_id = f"judge/target_0/{i}"
            agent = await stack.enter_async_context(
                Agent(
                    definition=judge_definition,
                    inference=inference_factory(),
                    server_configs=[mathlib_cfg, fs_cfg],
                    trace_store=trace_store,
                    id=judge_id,
                )
            )
            trace = AgentTrace(id=judge_id)
            agent.set_trace(trace)
            agents.append(agent)

        score = await grade_statement(target, agents)
        for agent in agents:
            agent.finalize_trace()

    print(f"\nOverall score: {score.value:.2f}")
    print(f"Passed:        {score.passed}")
    print(f"Metrics:       {score.metrics}")
    print("\nFeedback:")
    for line in score.feedback.splitlines():
        print(f"  {line}")


if __name__ == "__main__":
    asyncio.run(main())
