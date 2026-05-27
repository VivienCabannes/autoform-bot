# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for EvalPipeline."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.eval.dtypes import Score
from core.eval.grader import Grader
from core.eval.pipeline import AgentRunner, Dataset, EvalPipeline


# -- Fakes -----------------------------------------------------------------


class FakeDataset(Dataset[str]):
    def __init__(self, items: list[str]) -> None:
        self._items = items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)


class FakeRunner(AgentRunner[str, str]):
    async def run(self, datum: str) -> str:
        return f"output_{datum}"


class FailingRunner(AgentRunner[str, str]):
    async def run(self, datum: str) -> str:
        raise RuntimeError("agent failed")


class FakeGrader(Grader[str, str]):
    async def grade(self, datum: str, output: str) -> Score:
        return Score(value=1.0, passed=True, feedback="ok")


# -- Tests ------------------------------------------------------------------


class TestEvalPipeline:
    @pytest.mark.asyncio
    async def test_basic_run(self) -> None:
        pipeline = EvalPipeline(
            name="test",
            dataset=FakeDataset(["a", "b"]),
            runner=FakeRunner(),
            grader=FakeGrader(),
        )
        summary = await pipeline.run()

        assert summary["total"] == 2
        assert summary["pass_rate"] == pytest.approx(1.0)
        assert summary["mean_score"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_error_isolation(self) -> None:
        pipeline = EvalPipeline(
            name="test",
            dataset=FakeDataset(["a"]),
            runner=FailingRunner(),
            grader=FakeGrader(),
        )
        summary = await pipeline.run()

        assert summary["total"] == 1
        assert summary["pass_rate"] == pytest.approx(0.0)
        assert pipeline.monitor.results[0].score.feedback == "agent failed"

    @pytest.mark.asyncio
    async def test_concurrency(self) -> None:
        pipeline = EvalPipeline(
            name="test",
            dataset=FakeDataset(["a", "b", "c"]),
            runner=FakeRunner(),
            grader=FakeGrader(),
            concurrency=3,
        )
        summary = await pipeline.run()
        assert summary["total"] == 3
        assert summary["pass_rate"] == pytest.approx(1.0)
