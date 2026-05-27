# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Constrained task tracker for agents.

Wraps an ItemTracker with guards that prevent agents from performing
status transitions owned by DAGRunner. Only pending or failed tasks
of the configured flavor can be mutated; status changes are not exposed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.tracker import ItemStatus, ItemTracker

MUTABLE_STATUSES = frozenset({ItemStatus.PENDING, ItemStatus.FAILED})


class ConstrainedTracker:
    """ItemTracker wrapper that restricts mutations to pending/failed items.

    Read operations delegate directly.  Mutations check the item's
    current status and flavor before forwarding to the underlying tracker.

    Args:
        tracker: The underlying item tracker.
        mutable_flavors: Which flavors this instance can create and mutate.
            Defaults to ``{"task"}`` (orchestrator). Use ``{"meval"}`` for
            the merge eval triage agent, etc.  Use ``None`` to allow all
            flavors (full access).
        default_flavor: Flavor assigned to newly created items. Falls back
            to the first element of *mutable_flavors*.
    """

    def __init__(
        self,
        tracker: ItemTracker,
        *,
        mutable_flavors: frozenset[str] | None = frozenset({"task"}),
        default_flavor: str | None = None,
    ) -> None:
        self._tracker = tracker
        self._mutable_flavors = mutable_flavors
        if default_flavor is not None:
            self._default_flavor = default_flavor
        elif mutable_flavors is not None:
            self._default_flavor = next(iter(mutable_flavors))
        else:
            self._default_flavor = "task"
        self.dispatch_fn: Callable[[str], str] | None = None
        self.dispatch_ready_fn: Callable[[], int] | None = None

    # ------------------------------------------------------------------
    # Reads — delegate directly
    # ------------------------------------------------------------------

    def get(self, item_id: str) -> dict[str, Any] | None:
        return self._tracker.get(item_id)

    def list(self, *, status: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        return self._tracker.list(status=status, query=query)

    def ready(self) -> list[dict[str, Any]]:
        return self._tracker.ready()

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _check_mutable(self, item: dict[str, Any]) -> str | None:
        """Return an error message if the item cannot be mutated, else None."""
        item_id = item["id"]
        if item["status"] not in MUTABLE_STATUSES:
            return f"Error: task {item_id} is {item['status']} — only pending or failed tasks can be modified."
        if self._mutable_flavors is not None:
            flavor = item.get("flavor", "task")
            if flavor not in self._mutable_flavors:
                return f"Error: item {item_id} (flavor={flavor}) is read-only for this agent."
        return None

    # ------------------------------------------------------------------
    # Mutations — guarded
    # ------------------------------------------------------------------

    def add(
        self,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        item_id: str | None = None,
    ) -> str:
        """Add a new task with this tracker's default flavor."""
        flavor = self._default_flavor
        with self._tracker.lock:
            return self._tracker.add(
                title,
                description,
                depends_on,
                metadata=metadata,
                item_id=item_id,
                flavor=flavor,
            )

    def update(
        self,
        item_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Update a pending or failed task's fields.

        No ``status`` parameter — DAGRunner owns the lifecycle.
        """
        with self._tracker.lock:
            item = self._tracker.get(item_id)
            if item is None:
                return f"Error: task {item_id} not found."
            if err := self._check_mutable(item):
                return err
            return self._tracker.update(
                item_id,
                title=title,
                description=description,
                depends_on=depends_on,
                metadata=metadata,
            )

    def delete(self, item_id: str) -> str:
        """Delete a pending or failed task.

        Tasks with 0 attempts (never dispatched) are physically removed
        from the tracker. Tasks that have been attempted at least once
        are marked as deleted to preserve history.
        """
        with self._tracker.lock:
            item = self._tracker.get(item_id)
            if item is None:
                return f"Error: task {item_id} not found."
            if err := self._check_mutable(item):
                return err

            if item.get("attempts", 0) == 0:
                return self._purge(item_id)
            return self._tracker.update(item_id, status=ItemStatus.DELETED)

    def _purge(self, item_id: str) -> str:
        """Physically remove an item from the tracker, cleaning up DAG edges.

        Caller must hold ``self._tracker.lock``.
        """
        items = self._tracker._items
        item = items.get(item_id)
        if item is None:
            return f"Error: task {item_id} not found."

        for dep_id in item.get("depends_on", []):
            dep = items.get(dep_id)
            if dep is not None:
                dep["dependents"] = [d for d in dep["dependents"] if d != item_id]

        for child_id in item.get("dependents", []):
            child = items.get(child_id)
            if child is not None:
                child["depends_on"] = [d for d in child["depends_on"] if d != item_id]

        del items[item_id]
        self._tracker._save()
        return f"Task {item_id} removed (0 attempts — purged from DAG)."
