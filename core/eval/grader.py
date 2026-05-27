# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Grader, Rubric, LLMJudgeGrader, and JuryGrader."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic

from .dtypes import Datum, Output, Score

if TYPE_CHECKING:
    from core.agent import Agent


class Grader(abc.ABC, Generic[Datum, Output]):
    """Grades an agent's output against the original datum."""

    @abc.abstractmethod
    async def grade(self, datum: Datum, output: Output) -> Score:
        """Grade a single (datum, output) pair."""
        ...


class Rubric(abc.ABC, Generic[Datum, Output]):
    """One agent's evaluation of an output — possibly across many criteria.

    A rubric is bound to a single LLM judge call: it builds the prompt,
    sends it, and interprets the response into a Score.  That Score may
    cover multiple criteria (accuracy, style, …) packed into its metrics,
    but it always comes from one agent invocation.
    """

    @abc.abstractmethod
    def prompt(self, datum: Datum, output: Output) -> str:
        """Build the evaluation prompt for the judge LLM."""
        ...

    @abc.abstractmethod
    def process_answer(self, response: str) -> Score:
        """Parse the judge's response into a Score with value, passed, and optional metrics."""
        ...


@dataclass
class LLMJudgeGrader(Grader[Datum, Output], Generic[Datum, Output]):
    """Grader that calls a single LLM judge on a single rubric.

    The rubric itself may evaluate multiple criteria in one prompt —
    use a composite rubric to combine several criteria into one call.
    """

    agent: Agent
    rubric: Rubric

    async def grade(self, datum: Datum, output: Output) -> Score:
        response = await self.agent.call(self.rubric.prompt(datum, output))
        return self.rubric.process_answer(response)


@dataclass
class JuryGrader(Grader[Datum, Output], Generic[Datum, Output]):
    """Composes named graders, evaluates in parallel, aggregates scores.

    Accepts any :class:`Grader` (LLM-based or programmatic) and runs
    all members concurrently via ``asyncio.gather``.

    Concrete subclasses define the aggregation strategy.
    """

    members: list[tuple[str, Grader[Datum, Output]]]

    @abc.abstractmethod
    def aggregate(self, scores: dict[str, Score]) -> Score:
        """Combine per-member Scores into a single aggregate Score."""
        ...

    async def grade(self, datum: Datum, output: Output) -> Score:
        """Evaluate all members in parallel, then aggregate."""
        names = [n for n, _ in self.members]
        results = await asyncio.gather(*(g.grade(datum, output) for _, g in self.members))
        return self.aggregate(dict(zip(names, results)))
