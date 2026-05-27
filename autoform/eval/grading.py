# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LLM jury grading for a single assessment target."""

from __future__ import annotations

import logging
from pathlib import Path

from core.agent import Agent
from core.eval.dtypes import Score
from core.eval.grader import LLMJudgeGrader
from core.eval.rubric import load_lenient_rubrics

from autoform.eval.compilation_grader import LLMJuryGrader

from .types import AssessmentTarget

logger = logging.getLogger(__name__)

_RUBRICS_DIR = Path(__file__).resolve().parent / "rubrics"


def rubric_count() -> int:
    """Return the number of active rubrics."""
    return len(load_lenient_rubrics(_RUBRICS_DIR))


async def grade_statement(
    target: AssessmentTarget,
    agents: list[Agent],
) -> Score:
    """Grade a matched statement using the LLM jury.

    Loads rubrics from this app's ``rubrics/`` directory and pairs each
    with a dedicated agent for concurrent evaluation.
    """
    rubrics = load_lenient_rubrics(_RUBRICS_DIR)
    if len(agents) != len(rubrics):
        raise ValueError(f"Expected {len(rubrics)} agents (one per rubric), got {len(agents)}")
    weights = {r.name: r.spec.weight for r in rubrics}
    jury = LLMJuryGrader(
        members=[(r.name, LLMJudgeGrader(agent=a, rubric=r)) for r, a in zip(rubrics, agents)],
        _weights=weights,
    )
    return await jury.grade(target, "")
