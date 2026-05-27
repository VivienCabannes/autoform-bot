# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Aggregators and live monitoring for eval results."""

from __future__ import annotations

import math
from math import comb
from collections.abc import Awaitable, Callable
from typing import Generic

from .dtypes import Datum, EvalResult, Output

GroupedEvalResults = dict[str, list[EvalResult]]
Aggregator = Callable[[GroupedEvalResults], dict[str, float]]
ResultCallback = Callable[[EvalResult], Awaitable[None]]


# -- Built-in aggregators ---------------------------------------------------


def _total(groups: GroupedEvalResults) -> dict[str, float]:
    return {"total": float(len(groups))}


def _pass_rate(groups: GroupedEvalResults) -> dict[str, float]:
    if not groups:
        return {"pass_rate": 0.0}
    # Exclude datums where every result is an error (NaN value).
    valid = {k: g for k, g in groups.items() if any(not math.isnan(r.score.value) for r in g)}
    if not valid:
        return {"pass_rate": 0.0}
    passed = sum(1 for group in valid.values() if any(r.score.passed for r in group))
    return {"pass_rate": passed / len(valid)}


def _mean_score(groups: GroupedEvalResults) -> dict[str, float]:
    if not groups:
        return {"mean_score": 0.0}
    datum_means: list[float] = []
    for group in groups.values():
        values = [r.score.value for r in group if not math.isnan(r.score.value)]
        if values:
            datum_means.append(sum(values) / len(values))
    if not datum_means:
        return {"mean_score": 0.0}
    return {"mean_score": sum(datum_means) / len(datum_means)}


def _num_results(groups: GroupedEvalResults) -> dict[str, float]:
    return {"num_results": float(sum(len(g) for g in groups.values()))}


def _total_cost(groups: GroupedEvalResults) -> dict[str, float]:
    return {"total_cost": sum(r.cost for g in groups.values() for r in g if r.cost is not None)}


def _mean_latency(groups: GroupedEvalResults) -> dict[str, float]:
    latencies: list[float] = [r.latency for g in groups.values() for r in g if r.latency is not None]
    if not latencies:
        return {"mean_latency": 0.0}
    return {"mean_latency": sum(latencies) / len(latencies)}


def _num_errors(groups: GroupedEvalResults) -> dict[str, float]:
    return {"num_errors": float(sum(1 for g in groups.values() for r in g if math.isnan(r.score.value)))}


# -- Composable aggregator factories ----------------------------------------


def pass_at_k(k: int) -> Aggregator:
    """Return an aggregator that computes pass@k (datum-aware).

    Groups results by ``datum_id``, computes the unbiased estimator
    ``1 - C(n-c, k) / C(n, k)`` per datum, then averages across datums.
    Datums with fewer than *k* samples are skipped.
    Returns 0.0 when no datums qualify.
    """

    def _pass_at_k(groups: GroupedEvalResults) -> dict[str, float]:
        estimates: list[float] = []
        for group in groups.values():
            n = len(group)
            if n < k:
                continue
            c = sum(1 for r in group if r.score.passed)
            if n - c < k:
                estimates.append(1.0)
            else:
                estimates.append(1.0 - comb(n - c, k) / comb(n, k))
        if not estimates:
            return {f"pass@{k}": 0.0}
        return {f"pass@{k}": sum(estimates) / len(estimates)}

    return _pass_at_k


def mean_metric(*keys: str) -> Aggregator:
    """Return an aggregator that averages named keys from ``Score.metrics``.

    Computes within-datum means, then averages across datums so each datum
    contributes equally regardless of sample count.  Skips results where a
    key is absent.
    """

    def _mean_metric(groups: GroupedEvalResults) -> dict[str, float]:
        out: dict[str, float] = {}
        for key in keys:
            datum_means: list[float] = []
            for group in groups.values():
                values = [
                    r.score.metrics[key]
                    for r in group
                    if key in r.score.metrics
                    and not (isinstance(r.score.metrics[key], float) and math.isnan(r.score.metrics[key]))
                ]
                if values:
                    datum_means.append(sum(values) / len(values))
            if datum_means:
                out[f"mean_{key}"] = sum(datum_means) / len(datum_means)
        return out

    return _mean_metric


# -- EvalAggregator ---------------------------------------------------------


BUILTIN_AGGREGATORS: list[Aggregator] = [
    _total,
    _pass_rate,
    _mean_score,
    pass_at_k(1),
    _num_results,
    _num_errors,
    _total_cost,
    _mean_latency,
]


class EvalAggregator:
    """Callable aggregator: groups results by datum, runs sub-aggregators."""

    def __init__(self, aggregators: list[Aggregator] | None = None) -> None:
        self._aggregators: list[Aggregator] = list(BUILTIN_AGGREGATORS)
        if aggregators:
            self._aggregators.extend(aggregators)

    def __call__(self, results: list[EvalResult]) -> dict[str, float]:
        groups: GroupedEvalResults = {}
        for r in results:
            groups.setdefault(r.datum_id, []).append(r)

        merged: dict[str, float] = {}
        for agg in self._aggregators:
            merged.update(agg(groups))
        return merged


# -- MetricMonitor -----------------------------------------------------------


class MetricMonitor(Generic[Datum, Output]):
    """Async collector for eval results with callback dispatch.

    Collects results, dispatches async callbacks, and delegates all
    statistics to :class:`EvalAggregator`.
    """

    def __init__(
        self,
        name: str = "",
        aggregators: list[Aggregator] | None = None,
    ) -> None:
        self.name = name
        self.results: list[EvalResult] = []
        self._callbacks: list[ResultCallback] = []
        self._aggregator = EvalAggregator(aggregators)

    def on_result(self, callback: ResultCallback) -> None:
        """Register an async callback invoked for each recorded result."""
        self._callbacks.append(callback)

    async def record(self, result: EvalResult) -> None:
        """Record a single eval result and dispatch callbacks."""
        self.results.append(result)
        for cb in self._callbacks:
            await cb(result)

    def summary(self) -> dict[str, float]:
        """Run all aggregators on collected results and return metrics."""
        return self._aggregator(self.results)
