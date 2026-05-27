# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for MetricMonitor and aggregators."""

from __future__ import annotations

import pytest

from core.eval.dtypes import Score
from core.eval.metrics import (
    EvalAggregator,
    EvalResult,
    MetricMonitor,
    mean_metric,
    pass_at_k,
)


def _result(
    value: float,
    passed: bool,
    *,
    datum_id: str = "d1",
    latency: float | None = None,
    cost: float | None = None,
    metrics: dict[str, float] | None = None,
) -> EvalResult[str, str]:
    return EvalResult(
        datum_id=datum_id,
        datum="input",
        output="output",
        score=Score(value=value, passed=passed, metrics=metrics or {}),
        latency=latency,
        cost=cost,
    )


def _group(results: list[EvalResult]) -> dict[str, list[EvalResult]]:
    """Group results by datum_id for aggregator tests."""
    groups: dict[str, list[EvalResult]] = {}
    for r in results:
        groups.setdefault(r.datum_id, []).append(r)
    return groups


# -- MetricMonitor tests ----------------------------------------------------


class TestMetricMonitor:
    @pytest.fixture()
    def monitor(self) -> MetricMonitor[str, str]:
        return MetricMonitor(name="test")

    def test_empty_monitor(self, monitor: MetricMonitor[str, str]) -> None:
        s = monitor.summary()
        assert s["total"] == 0
        assert s["mean_score"] == 0.0
        assert s["pass_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_summary(self, monitor: MetricMonitor[str, str]) -> None:
        await monitor.record(_result(1.0, True, datum_id="d1", cost=0.01))
        await monitor.record(_result(0.0, False, datum_id="d2", cost=0.02))

        s = monitor.summary()
        assert s["total"] == 2
        assert s["num_results"] == 2
        assert s["mean_score"] == pytest.approx(0.5)
        assert s["pass_rate"] == pytest.approx(0.5)
        assert s["pass@1"] == pytest.approx(0.5)
        assert s["total_cost"] == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_async_callback(self, monitor: MetricMonitor[str, str]) -> None:
        collected: list[EvalResult] = []

        async def cb(r: EvalResult) -> None:
            collected.append(r)

        monitor.on_result(cb)
        await monitor.record(_result(0.9, True))
        assert len(collected) == 1


# -- pass_at_k tests --------------------------------------------------------


class TestPassAtK:
    def test_all_pass(self) -> None:
        results = [_result(1.0, True) for _ in range(5)]
        assert pass_at_k(1)(_group(results)) == {"pass@1": 1.0}

    def test_none_pass(self) -> None:
        results = [_result(0.0, False) for _ in range(5)]
        assert pass_at_k(1)(_group(results)) == {"pass@1": pytest.approx(0.0)}

    def test_some_pass(self) -> None:
        # 3 pass out of 5: pass@1 = 1 - C(2,1)/C(5,1) = 1 - 2/5 = 0.6
        results = [_result(1.0, True) for _ in range(3)] + [_result(0.0, False) for _ in range(2)]
        assert pass_at_k(1)(_group(results)) == {"pass@1": pytest.approx(0.6)}

    def test_too_few_results(self) -> None:
        results = [_result(1.0, True)]
        assert pass_at_k(5)(_group(results)) == {"pass@5": 0.0}

    def test_pass_at_k_equals_n(self) -> None:
        # n=3, c=2, k=3: n-c=1 < k=3 → 1.0
        results = [_result(1.0, True), _result(1.0, True), _result(0.0, False)]
        assert pass_at_k(3)(_group(results)) == {"pass@3": 1.0}


# -- mean_metric tests ------------------------------------------------------


class TestMeanMetric:
    def test_single_key(self) -> None:
        results = [
            _result(1.0, True, metrics={"accuracy": 0.9}),
            _result(1.0, True, metrics={"accuracy": 0.7}),
        ]
        out = mean_metric("accuracy")(_group(results))
        assert out == {"mean_accuracy": pytest.approx(0.8)}

    def test_missing_key_skipped(self) -> None:
        results = [
            _result(1.0, True, metrics={"accuracy": 0.9}),
            _result(1.0, True, metrics={}),
        ]
        out = mean_metric("accuracy")(_group(results))
        assert out == {"mean_accuracy": pytest.approx(0.9)}

    def test_all_missing(self) -> None:
        results = [_result(1.0, True), _result(1.0, True)]
        out = mean_metric("accuracy")(_group(results))
        assert out == {}

    def test_multiple_keys(self) -> None:
        results = [
            _result(1.0, True, metrics={"a": 1.0, "b": 2.0}),
            _result(1.0, True, metrics={"a": 3.0, "b": 4.0}),
        ]
        out = mean_metric("a", "b")(_group(results))
        assert out == {"mean_a": pytest.approx(2.0), "mean_b": pytest.approx(3.0)}


# -- Datum-aware aggregation tests ------------------------------------------


class TestDatumAwareAggregation:
    """Tests that EvalAggregator groups by datum_id."""

    def test_groups_by_datum(self) -> None:
        # d1: one pass, one fail → passed; d2: all fail → failed
        results = [
            _result(1.0, True, datum_id="d1"),
            _result(0.0, False, datum_id="d1"),
            _result(0.0, False, datum_id="d2"),
        ]
        s = EvalAggregator()(results)
        assert s["total"] == 2
        assert s["num_results"] == 3
        assert s["pass_rate"] == pytest.approx(0.5)

    def test_mean_score_is_mean_of_datum_means(self) -> None:
        # d1: scores 0.8, 0.4 → mean 0.6
        # d2: scores 1.0 → mean 1.0
        # overall: (0.6 + 1.0) / 2 = 0.8
        results = [
            _result(0.8, True, datum_id="d1"),
            _result(0.4, False, datum_id="d1"),
            _result(1.0, True, datum_id="d2"),
        ]
        s = EvalAggregator()(results)
        assert s["mean_score"] == pytest.approx(0.8)

    def test_cost_and_latency(self) -> None:
        results = [
            _result(1.0, True, datum_id="d1", cost=0.01, latency=1.0),
            _result(0.5, False, datum_id="d1", cost=0.02, latency=2.0),
            _result(0.8, True, datum_id="d2", cost=0.03, latency=3.0),
        ]
        s = EvalAggregator()(results)
        assert s["total_cost"] == pytest.approx(0.06)
        assert s["mean_latency"] == pytest.approx(2.0)
