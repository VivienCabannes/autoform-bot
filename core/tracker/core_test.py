# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for ItemTracker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.tracker.core import ItemFlavor, ItemTracker


@pytest.fixture
def tracker(tmp_path: Path) -> ItemTracker:
    return ItemTracker(tmp_path / "tracker.json")


# ------------------------------------------------------------------
# Add / Get
# ------------------------------------------------------------------


class TestAdd:
    def test_add_returns_incrementing_ids(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("First")
        id2 = tracker.add("Second")
        assert id1 == "1"
        assert id2 == "2"

    def test_add_with_string_id(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", item_id="convex-sets")
        assert item_id == "convex-sets"
        item = tracker.get("convex-sets")
        assert item is not None
        assert item["title"] == "Task"

    def test_add_sets_default_status(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        item = tracker.get(item_id)
        assert item is not None
        assert item["status"] == "pending"

    def test_add_sets_flavor(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        item = tracker.get(item_id)
        assert item is not None
        assert item["flavor"] == "task"

    def test_add_with_custom_flavor(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Bug report", flavor="issue")
        item = tracker.get(item_id)
        assert item["flavor"] == "issue"

    def test_add_with_enum_flavor(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Ship v2", flavor=ItemFlavor.GOAL)
        item = tracker.get(item_id)
        assert item["flavor"] == "goal"

    def test_add_with_metadata(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", metadata={"priority": "high"})
        item = tracker.get(item_id)
        assert item is not None
        assert item["metadata"]["priority"] == "high"

    def test_add_with_dependencies(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("Parent")
        id2 = tracker.add("Child", depends_on=[id1])
        item = tracker.get(id2)
        assert item is not None
        assert item["depends_on"] == [id1]

    def test_add_with_unknown_dependency_raises(self, tracker: ItemTracker) -> None:
        with pytest.raises(ValueError, match="Unknown dependency"):
            tracker.add("Orphan", depends_on=["999"])

    def test_add_with_int_id(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Statement", item_id=42)
        assert item_id == "42"
        assert tracker.get(42) is not None
        assert tracker.get("42") is not None

    def test_add_mixed_ids_no_collision(self, tracker: ItemTracker) -> None:
        """String IDs and auto-increment coexist."""
        tracker.add("Auto 1")  # "1"
        tracker.add("Named", item_id="my-task")
        id3 = tracker.add("Auto 2")  # "2"
        assert id3 == "2"
        assert len(tracker.list()) == 3

    def test_get_nonexistent(self, tracker: ItemTracker) -> None:
        assert tracker.get("999") is None

    def test_add_with_owner(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", owner="agent-1")
        item = tracker.get(item_id)
        assert item["owner"] == "agent-1"

    def test_add_with_active_form(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", active_form="Running tests")
        item = tracker.get(item_id)
        assert item["active_form"] == "Running tests"

    def test_add_default_owner_is_none(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        assert tracker.get(item_id)["owner"] is None


# ------------------------------------------------------------------
# Bidirectional dependencies
# ------------------------------------------------------------------


class TestBidirectionalDeps:
    def test_add_maintains_dependents(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("Parent")
        id2 = tracker.add("Child", depends_on=[id1])
        parent = tracker.get(id1)
        assert id2 in parent["dependents"]

    def test_update_deps_updates_dependents(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("A")
        id2 = tracker.add("B")
        id3 = tracker.add("C", depends_on=[id1])
        # Move C's dependency from A to B
        tracker.update(id3, depends_on=[id2])
        assert id3 not in tracker.get(id1)["dependents"]
        assert id3 in tracker.get(id2)["dependents"]
        assert tracker.get(id3)["depends_on"] == [id2]


# ------------------------------------------------------------------
# Update
# ------------------------------------------------------------------


class TestUpdate:
    def test_update_description(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", "old")
        result = tracker.update(item_id, description="new")
        assert "updated" in result
        assert tracker.get(item_id)["description"] == "new"

    def test_update_metadata_merges(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", metadata={"a": 1})
        tracker.update(item_id, metadata={"b": 2})
        item = tracker.get(item_id)
        assert item["metadata"] == {"a": 1, "b": 2}

    def test_update_depends_on(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("A")
        id2 = tracker.add("B")
        id3 = tracker.add("C", depends_on=[id1])
        tracker.update(id3, depends_on=[id2])
        assert tracker.get(id3)["depends_on"] == [id2]

    def test_update_nonexistent(self, tracker: ItemTracker) -> None:
        result = tracker.update("999", description="nope")
        assert "Error" in result

    def test_update_owner(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        tracker.update(item_id, owner="agent-1")
        assert tracker.get(item_id)["owner"] == "agent-1"

    def test_update_owner_clear(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", owner="agent-1")
        tracker.update(item_id, owner=None)
        assert tracker.get(item_id)["owner"] is None

    def test_update_active_form(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        tracker.update(item_id, active_form="Building")
        assert tracker.get(item_id)["active_form"] == "Building"

    def test_update_owner_sentinel_means_no_change(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", owner="agent-1")
        tracker.update(item_id, description="changed")
        assert tracker.get(item_id)["owner"] == "agent-1"

    def test_update_status(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        result = tracker.update(item_id, status="in_progress")
        assert "updated" in result
        assert tracker.get(item_id)["status"] == "in_progress"

    def test_update_invalid_status(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        result = tracker.update(item_id, status="bogus")
        assert "Error" in result
        assert tracker.get(item_id)["status"] == "pending"

    def test_update_claim_via_owner(self, tracker: ItemTracker) -> None:
        """Setting owner on unowned item auto-transitions to in_progress."""
        item_id = tracker.add("Task")
        result = tracker.update(item_id, owner="agent-1")
        assert "updated" in result
        item = tracker.get(item_id)
        assert item["owner"] == "agent-1"
        assert item["status"] == "in_progress"

    def test_update_claim_already_owned_rejected(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task", owner="agent-1")
        result = tracker.update(item_id, owner="agent-2")
        assert "Error" in result
        assert tracker.get(item_id)["owner"] == "agent-1"

    def test_update_claim_terminal_rejected(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        tracker.update(item_id, status="completed")
        result = tracker.update(item_id, owner="agent-1")
        assert "Error" in result

    def test_reset_failed_on_update(self, tracker: ItemTracker) -> None:
        """Failed items auto-reset to pending on field update."""
        item_id = tracker.add("Task")
        tracker.update(item_id, status="failed")
        assert tracker.get(item_id)["status"] == "failed"

        tracker.update(item_id, description="retry this")
        assert tracker.get(item_id)["status"] == "pending"
        assert tracker.get(item_id)["description"] == "retry this"

    def test_reset_failed_with_explicit_status(self, tracker: ItemTracker) -> None:
        """Explicit status overrides auto-reset."""
        item_id = tracker.add("Task")
        tracker.update(item_id, status="failed")

        tracker.update(item_id, description="retry", status="in_progress")
        assert tracker.get(item_id)["status"] == "in_progress"

    def test_delete_via_status(self, tracker: ItemTracker) -> None:
        """Items are soft-deleted by setting status to deleted."""
        item_id = tracker.add("Task")
        result = tracker.update(item_id, status="deleted")
        assert "updated" in result
        assert tracker.get(item_id)["status"] == "deleted"

    def test_deleted_item_excluded_from_ready(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Task")
        tracker.update(item_id, status="deleted")
        assert len(tracker.ready()) == 0

    def test_deleted_dep_unblocks_dependents(self, tracker: ItemTracker) -> None:
        """Deleting a dependency unblocks its dependents (deleted is terminal)."""
        id1 = tracker.add("Parent")
        id2 = tracker.add("Child", depends_on=[id1])
        tracker.update(id1, status="deleted")
        ready = tracker.ready()
        assert len(ready) == 1
        assert ready[0]["id"] == id2


# ------------------------------------------------------------------
# Bulk operations
# ------------------------------------------------------------------


class TestBulkOps:
    def test_bulk_transition(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("A")
        id2 = tracker.add("B")
        tracker.update(id1, status="in_progress")
        tracker.update(id2, status="in_progress")
        tracker.bulk_transition("in_progress", "failed")
        assert tracker.get(id1)["status"] == "failed"
        assert tracker.get(id2)["status"] == "failed"


# ------------------------------------------------------------------
# List / Ready / Summary
# ------------------------------------------------------------------


class TestQueries:
    def test_list_all(self, tracker: ItemTracker) -> None:
        tracker.add("A")
        tracker.add("B")
        assert len(tracker.list()) == 2

    def test_list_filtered_by_status(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("A")
        tracker.add("B")
        tracker.update(id1, status="completed")
        assert len(tracker.list(status="pending")) == 1
        assert len(tracker.list(status="completed")) == 1

    def test_list_filtered_by_flavor(self, tracker: ItemTracker) -> None:
        tracker.add("Task A")
        tracker.add("Goal B", flavor="goal")
        assert len(tracker.list(flavor="task")) == 1
        assert len(tracker.list(flavor="goal")) == 1

    def test_list_sorted_by_id(self, tracker: ItemTracker) -> None:
        tracker.add("B")
        tracker.add("A")
        items = tracker.list()
        assert items[0]["id"] == "1"
        assert items[1]["id"] == "2"

    def test_list_sorts_numerically_not_lexicographically(self, tracker: ItemTracker) -> None:
        for i in range(1, 12):
            tracker.add(f"Item {i}")
        ids = [item["id"] for item in tracker.list()]
        assert ids == [str(i) for i in range(1, 12)]

    def test_list_sorts_string_ids_after_numeric(self, tracker: ItemTracker) -> None:
        tracker.add("Named", item_id="alpha")
        tracker.add("Auto")  # "1"
        tracker.add("Named 2", item_id="beta")
        ids = [item["id"] for item in tracker.list()]
        assert ids == ["1", "alpha", "beta"]

    def test_ready_no_deps(self, tracker: ItemTracker) -> None:
        tracker.add("A")
        assert len(tracker.ready()) == 1

    def test_ready_blocked_by_pending_dep(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("Parent")
        tracker.add("Child", depends_on=[id1])
        ready = tracker.ready()
        assert len(ready) == 1
        assert ready[0]["id"] == id1

    def test_ready_unblocked_after_dep_completes(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("Parent")
        id2 = tracker.add("Child", depends_on=[id1])
        tracker.update(id1, status="completed")
        ready = tracker.ready()
        assert len(ready) == 1
        assert ready[0]["id"] == id2

    def test_ready_excludes_terminal(self, tracker: ItemTracker) -> None:
        item_id = tracker.add("Done")
        tracker.update(item_id, status="completed")
        assert len(tracker.ready()) == 0

    def test_ready_dag_chain(self, tracker: ItemTracker) -> None:
        """task 3 requires task 1 and task 2."""
        id1 = tracker.add("A")
        id2 = tracker.add("B")
        id3 = tracker.add("C", depends_on=[id1, id2])

        # Only A and B ready
        ready_ids = [i["id"] for i in tracker.ready()]
        assert id1 in ready_ids and id2 in ready_ids
        assert id3 not in ready_ids

        # Complete A — C still blocked by B
        tracker.update(id1, status="completed")
        ready_ids = [i["id"] for i in tracker.ready()]
        assert id2 in ready_ids
        assert id3 not in ready_ids

        # Complete B — C now ready
        tracker.update(id2, status="completed")
        ready_ids = [i["id"] for i in tracker.ready()]
        assert id3 in ready_ids

    def test_summary(self, tracker: ItemTracker) -> None:
        id1 = tracker.add("A")
        tracker.add("B")
        tracker.update(id1, status="completed")
        s = tracker.summary()
        assert s["total"] == 2
        assert s["counts"]["completed"] == 1
        assert s["counts"]["pending"] == 1
        assert s["completion_rate"] == 0.5


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------


class TestPersistence:
    def test_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "tracker.json"
        t1 = ItemTracker(path)
        id1 = t1.add("Task A", depends_on=[])
        t1.add("Task B", depends_on=[id1])
        t1.update(id1, status="completed")

        # Reload from disk
        t2 = ItemTracker(path)
        assert len(t2.list()) == 2
        assert t2.get(id1)["status"] == "completed"
        # Next ID continues from where we left off
        id3 = t2.add("Task C")
        assert id3 == "3"

    def test_saves_kind(self, tmp_path: Path) -> None:
        path = tmp_path / "tracker.json"
        t = ItemTracker(path, default_flavor=ItemFlavor.ISSUE)
        t.add("Bug")
        data = json.loads(path.read_text())
        assert data["kind"] == "issue"

    def test_backfills_flavor_on_load(self, tmp_path: Path) -> None:
        """Items saved without a flavor field get it backfilled on load."""
        path = tmp_path / "tracker.json"
        # Write old-format data without flavor
        data = {
            "kind": "goal",
            "items": [
                {"id": 1, "title": "X", "description": "", "status": "planned", "depends_on": [], "metadata": {}}
            ],
        }
        path.write_text(json.dumps(data))
        t = ItemTracker(path, default_flavor=ItemFlavor.GOAL)
        assert t.get(1)["flavor"] == "goal"

    def test_string_id_roundtrip(self, tmp_path: Path) -> None:
        """String IDs survive save/load."""
        path = tmp_path / "tracker.json"
        t1 = ItemTracker(path)
        t1.add("Task", item_id="my-slug")
        t1.add("Dep", item_id="dep-task", depends_on=["my-slug"])

        t2 = ItemTracker(path)
        assert t2.get("my-slug") is not None
        assert t2.get("dep-task")["depends_on"] == ["my-slug"]

    def test_backfills_new_fields_on_load(self, tmp_path: Path) -> None:
        """Old files without dependents/owner/active_form get them backfilled."""
        path = tmp_path / "tracker.json"
        data = {
            "kind": "task",
            "items": [
                {
                    "id": "1",
                    "title": "A",
                    "description": "",
                    "status": "pending",
                    "flavor": "task",
                    "depends_on": [],
                    "metadata": {},
                },
                {
                    "id": "2",
                    "title": "B",
                    "description": "",
                    "status": "pending",
                    "flavor": "task",
                    "depends_on": ["1"],
                    "metadata": {},
                },
            ],
        }
        path.write_text(json.dumps(data))
        t = ItemTracker(path)
        # New fields backfilled
        assert t.get("1")["owner"] is None
        assert t.get("1")["active_form"] is None
        assert t.get("1")["dependents"] == ["2"]
        assert t.get("2")["dependents"] == []

    def test_high_water_mark_prevents_id_reuse(self, tmp_path: Path) -> None:
        """Deleting an item doesn't reuse its ID."""
        path = tmp_path / "tracker.json"
        t = ItemTracker(path)
        t.add("A")  # 1
        t.add("B")  # 2
        t.add("C")  # 3
        t.update("3", status="deleted")
        id4 = t.add("D")
        assert id4 == "4"

    def test_max_id_in_json(self, tmp_path: Path) -> None:
        path = tmp_path / "tracker.json"
        t = ItemTracker(path)
        t.add("A")
        t.add("B")
        data = json.loads(path.read_text())
        assert data["max_id"] == 2

    def test_dependents_roundtrip(self, tmp_path: Path) -> None:
        """Dependents survive save/load."""
        path = tmp_path / "tracker.json"
        t1 = ItemTracker(path)
        id1 = t1.add("Parent")
        id2 = t1.add("Child", depends_on=[id1])

        t2 = ItemTracker(path)
        assert id2 in t2.get(id1)["dependents"]
        assert t2.get(id2)["depends_on"] == [id1]

    def test_owner_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "tracker.json"
        t1 = ItemTracker(path)
        t1.add("Task", owner="agent-1")

        t2 = ItemTracker(path)
        assert t2.get("1")["owner"] == "agent-1"


# ------------------------------------------------------------------
# sync_statuses
# ------------------------------------------------------------------


class TestSyncStatuses:
    def test_applies_updates(self, tmp_path: Path) -> None:
        t = ItemTracker(tmp_path / "tracker.json")
        t.add("Task A", item_id="1")
        t.add("Task B", item_id="2")

        t.sync_statuses(
            {
                "1": ("in_progress", {"attempts": 1}),
                "2": ("failed", {"attempts": 2}),
            }
        )

        assert t.get("1")["status"] == "in_progress"
        assert t.get("1")["metadata"]["attempts"] == 1
        assert t.get("2")["status"] == "failed"
        assert t.get("2")["metadata"]["attempts"] == 2

    def test_skips_terminal_items(self, tmp_path: Path) -> None:
        t = ItemTracker(tmp_path / "tracker.json")
        t.add("Done task", item_id="1")
        t.update("1", status="completed")

        t.sync_statuses({"1": ("pending", {"attempts": 0})})

        assert t.get("1")["status"] == "completed"

    def test_skips_unknown_ids(self, tmp_path: Path) -> None:
        t = ItemTracker(tmp_path / "tracker.json")
        # Should not raise
        t.sync_statuses({"999": ("pending", {})})

    def test_persists_to_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "tracker.json"
        t = ItemTracker(path)
        t.add("Task A", item_id="1")

        t.sync_statuses({"1": ("in_progress", {"attempts": 3})})

        t2 = ItemTracker(path)
        assert t2.get("1")["status"] == "in_progress"
        assert t2.get("1")["metadata"]["attempts"] == 3


# ------------------------------------------------------------------
# Default flavor
# ------------------------------------------------------------------


class TestDefaultFlavor:
    def test_default_flavor_is_task(self, tmp_path: Path) -> None:
        t = ItemTracker(tmp_path / "tracker.json")
        item_id = t.add("Task")
        assert t.get(item_id)["flavor"] == "task"

    def test_goal_flavor(self, tmp_path: Path) -> None:
        t = ItemTracker(tmp_path / "goals.json", default_flavor=ItemFlavor.GOAL)
        item_id = t.add("Ship v2")
        assert t.get(item_id)["flavor"] == "goal"
        assert t.get(item_id)["status"] == "pending"

        t.update(item_id, status="completed")
        assert t.summary()["completion_rate"] == 1.0

    def test_issue_flavor(self, tmp_path: Path) -> None:
        t = ItemTracker(tmp_path / "issues.json", default_flavor=ItemFlavor.ISSUE)
        item_id = t.add("Login broken")
        assert t.get(item_id)["flavor"] == "issue"
        assert t.get(item_id)["status"] == "pending"

        t.update(item_id, status="completed")
        assert t.summary()["completion_rate"] == 1.0
