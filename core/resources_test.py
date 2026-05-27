# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Unit tests for SubAgentBudget."""

from __future__ import annotations

import asyncio

import pytest

from core.resources import SubAgentBudget


@pytest.fixture
def budget() -> SubAgentBudget:
    return SubAgentBudget(capacity=10)


@pytest.mark.asyncio
async def test_reserve_and_release(budget: SubAgentBudget) -> None:
    await budget.reserve("a1", child_budget=2)
    assert budget.status() == {"capacity": 10, "available": 7, "reserved": 3}

    await budget.release("a1")
    assert budget.status() == {"capacity": 10, "available": 10, "reserved": 0}


@pytest.mark.asyncio
async def test_insufficient_budget(budget: SubAgentBudget) -> None:
    await budget.reserve("a1", child_budget=4)  # costs 5
    assert budget.status()["available"] == 5

    with pytest.raises(ValueError, match="Insufficient sub-agent budget"):
        await budget.reserve("a2", child_budget=5)  # costs 6, only 5 available


@pytest.mark.asyncio
async def test_released_budget_becomes_available(budget: SubAgentBudget) -> None:
    await budget.reserve("a1", child_budget=4)  # costs 5
    await budget.reserve("a2", child_budget=4)  # costs 5, now 0 available

    with pytest.raises(ValueError):
        await budget.reserve("a3", child_budget=0)  # costs 1, none available

    await budget.release("a1")  # frees 5
    await budget.reserve("a3", child_budget=3)  # costs 4, succeeds
    assert budget.status()["available"] == 1


@pytest.mark.asyncio
async def test_concurrent_reservations(budget: SubAgentBudget) -> None:
    async def reserve(agent_id: str) -> bool:
        try:
            await budget.reserve(agent_id, child_budget=0)
            return True
        except ValueError:
            return False

    results = await asyncio.gather(*[reserve(f"a{i}") for i in range(12)])
    successes = sum(results)
    assert successes == 10
    assert budget.status()["available"] == 0


@pytest.mark.asyncio
async def test_release_unknown_agent_is_noop(budget: SubAgentBudget) -> None:
    await budget.release("nonexistent")
    assert budget.status() == {"capacity": 10, "available": 10, "reserved": 0}


@pytest.mark.asyncio
async def test_zero_child_budget_costs_one() -> None:
    b = SubAgentBudget(capacity=1)
    await b.reserve("a1", child_budget=0)
    assert b.status()["available"] == 0

    with pytest.raises(ValueError):
        await b.reserve("a2", child_budget=0)
