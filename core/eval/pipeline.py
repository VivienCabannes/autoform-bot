# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""EvalPipeline — orchestrates Dataset → AgentRunner → Grader → MetricMonitor.

Serves as a reference implementation for an evaluation pipeline.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Generic

from .dtypes import Datum, Output, Score
from .grader import Grader
from .dtypes import EvalResult
from .metrics import MetricMonitor

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 1


class Dataset(abc.ABC, Generic[Datum]):
    """An iterable collection of evaluation data."""

    @abc.abstractmethod
    def __iter__(self) -> Iterator[Datum]: ...


class AgentRunner(abc.ABC, Generic[Datum, Output]):
    """Protocol for running an agent on a datum.

    This decouples the pipeline from the specific Agent class,
    allowing different agent configurations, multi-agent setups, etc.
    """

    @abc.abstractmethod
    async def run(self, datum: Datum) -> Output:
        """Run the agent on a single datum and return its output."""
        ...


class SimpleAgentRunner(AgentRunner[Datum, str]):
    """Convenience adapter that wraps an Agent into an AgentRunner.

    Translates each datum into a prompt string via ``prompt_fn``,
    calls ``Agent.call()``, and returns the raw string output.
    """

    def __init__(self, agent: Agent, prompt_fn: Callable[[Datum], str]) -> None:
        self.agent = agent
        self.prompt_fn = prompt_fn

    async def run(self, datum: Datum) -> str:
        return await self.agent.call(self.prompt_fn(datum))


class EvalPipeline(Generic[Datum, Output]):
    """Generic evaluation pipeline.

    Orchestrates: ``Dataset[Datum] → AgentRunner[Datum, Output] → Grader[Datum, Output] → MetricMonitor``

    Supports:
    - Sequential or concurrent execution
    - Live monitoring via :class:`MetricMonitor`
    - Error isolation (a single failure doesn't stop the run)
    """

    def __init__(
        self,
        name: str,
        dataset: Dataset[Datum],
        runner: AgentRunner[Datum, Output],
        grader: Grader[Datum, Output],
        monitor: MetricMonitor | None = None,
        *,
        get_id: Callable[[Datum], str] = str,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.name = name
        self.dataset = dataset
        self.runner = runner
        self.grader = grader
        self._get_id = get_id
        self.concurrency = concurrency
        self.monitor: MetricMonitor = monitor or MetricMonitor(name=name)

    async def run(self) -> dict[str, float]:
        """Run the full evaluation pipeline."""
        data = list(self.dataset)
        logger.info(
            "[%s] Starting eval with %d items (concurrency=%d)",
            self.name,
            len(data),
            self.concurrency,
        )

        sem = asyncio.Semaphore(self.concurrency)

        async def process_one(datum: Datum) -> EvalResult[Datum, Output]:
            datum_id = self._get_id(datum)

            async with sem:
                start = time.perf_counter()
                try:
                    output = await self.runner.run(datum)
                    score = await self.grader.grade(datum, output)
                    latency = time.perf_counter() - start
                    result = EvalResult(
                        datum_id=datum_id,
                        score=score,
                        datum=datum,
                        output=output,
                        latency=latency,
                    )
                except Exception as exc:
                    latency = time.perf_counter() - start
                    logger.exception("[%s] Error on %s", self.name, datum_id)
                    result = EvalResult(
                        datum_id=datum_id,
                        datum=datum,
                        score=Score(
                            value=float("nan"),
                            passed=False,
                            feedback=str(exc),
                        ),
                        latency=latency,
                    )

                await self.monitor.record(result)
                return result

        tasks = [process_one(d) for d in data]
        await asyncio.gather(*tasks)

        summary = self.monitor.summary()
        logger.info(
            "[%s] Done: pass_rate=%.1f%%, %d datums, %d results, mean=%.3f, cost=$%.4f",
            self.name,
            summary["pass_rate"] * 100,
            int(summary["total"]),
            int(summary["num_results"]),
            summary["mean_score"],
            summary["total_cost"],
        )
        return summary
