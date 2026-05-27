# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for core.coordination.concurrent_agents."""

from core.task import Task
from core.coordination.concurrent_agents import ConcurrentResult


def test_concurrent_result():
    """ConcurrentResult dataclass works."""
    result = ConcurrentResult(success=True, winner_id="agent-0")
    assert result.success
    assert result.winner_id == "agent-0"

    result = ConcurrentResult(success=False, error="All agents failed")
    assert not result.success
    assert result.error == "All agents failed"


def test_task_tree():
    """Task tree structure works."""
    parent = Task(description="Formalize chapter 5")
    child = Task(description="Formalize theorem 5.1")
    parent.add_child(child)

    assert child.parent is parent
    assert len(parent.children) == 1
    assert child.root() is parent
    assert child.context_chain() == ["Formalize chapter 5", "Formalize theorem 5.1"]
