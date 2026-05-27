# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for goal tracker standalone functions."""

from __future__ import annotations

from pathlib import Path

from autoform.eval.types import FormalizationTarget
from autoform.eval.utils.tracker import (
    get_formalization_targets,
    populate_tracker,
)
from core.tracker import ItemFlavor, ItemTracker


def _create_test_targets() -> list[FormalizationTarget]:
    return [
        FormalizationTarget(
            name="Complement of a Union",
            description="If B, C are sets then (B \u222a C)^c = B^c \u2229 C^c.",
            lean_declaration="compl_union",
            lean_file="A.lean",
        ),
        FormalizationTarget(
            name="Bernoulli's Inequality",
            description="For all c >= -1, (1+c)^n >= 1 + nc.",
            lean_declaration="bernoulli",
            lean_file="B.lean",
        ),
    ]


def _make_tracker(tmp_path: Path) -> ItemTracker:
    return ItemTracker(tmp_path / "tracker.json", default_flavor=ItemFlavor.GOAL)


class TestPopulateTracker:
    def test_creates_items(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        populate_tracker(tracker, _create_test_targets())

        stmt = tracker.get(0)
        assert stmt is not None
        assert stmt["id"] == "0"
        assert stmt["title"] == "Complement of a Union"
        assert stmt["status"] == "pending"
        assert stmt["flavor"] == "goal"
        assert stmt["metadata"]["lean_file"] == "A.lean"
        assert stmt["metadata"]["lean_declaration"] == "compl_union"

        assert tracker.get(1) is not None

    def test_is_idempotent(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        populate_tracker(tracker, _create_test_targets())

        tracker.update(0, status="completed")

        # Re-populate should not overwrite
        populate_tracker(tracker, _create_test_targets())
        stmt = tracker.get(0)
        assert stmt["status"] == "completed"

    def test_targets_without_lean_info(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        targets = [FormalizationTarget(name="No Lean", description="Just a statement.")]
        populate_tracker(tracker, targets)

        stmt = tracker.get(0)
        assert stmt is not None
        assert "lean_declaration" not in stmt["metadata"]
        assert "lean_file" not in stmt["metadata"]


class TestStatementTracking:
    def test_list_all(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        populate_tracker(tracker, _create_test_targets())
        assert len(tracker.list()) == 2

    def test_list_filtered(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        populate_tracker(tracker, _create_test_targets())
        tracker.update(0, status="completed")

        pending = tracker.list(status="pending")
        assert len(pending) == 1
        assert pending[0]["id"] == "1"

        completed = tracker.list(status="completed")
        assert len(completed) == 1
        assert completed[0]["id"] == "0"

    def test_update_status_completed(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        populate_tracker(tracker, _create_test_targets())

        result = tracker.update(0, status="completed")
        assert "updated" in result

        stmt = tracker.get(0)
        assert stmt["status"] == "completed"

    def test_update_status_not_found(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        result = tracker.update(999, status="completed")
        assert "not found" in result

    def test_empty_tracker(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        assert tracker.list() == []
        assert tracker.list(status="completed") == []


class TestGetFormalizationTargets:
    def test_returns_targets_with_lean_metadata(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        populate_tracker(tracker, _create_test_targets())

        targets = get_formalization_targets(tracker)
        assert len(targets) == 2
        assert targets[0].lean_file == "A.lean"
        assert targets[0].lean_declaration == "compl_union"

    def test_skips_items_without_lean_metadata(self, tmp_path: Path) -> None:
        tracker = _make_tracker(tmp_path)
        tracker.add("No mapping", "desc", item_id="1", metadata={})

        targets = get_formalization_targets(tracker)
        assert targets == []
