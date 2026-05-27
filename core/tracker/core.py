# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""ItemTracker — generic persistent item tracker with DAG dependencies.

Items form a DAG via ``depends_on`` / ``dependents`` edges, enabling
dependency-aware scheduling. Supports owner-based claim semantics
and a fixed status lifecycle.

No MCP dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ItemStatus(StrEnum):
    """Fixed status lifecycle for all tracked items."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


class ItemFlavor(StrEnum):
    """Item type — distinguishes different kinds of work in one tracker."""

    TASK = "task"
    ISSUE = "issue"
    GOAL = "goal"


TERMINAL = frozenset({ItemStatus.COMPLETED, ItemStatus.DELETED})
RESET_ON_UPDATE = frozenset({ItemStatus.FAILED})
_STATUSES = tuple(ItemStatus)


def _sort_key(item: dict[str, Any]) -> tuple[int, int | str]:
    """Sort numeric IDs first (by value), then string IDs lexicographically."""
    k = item["id"]
    return (0, int(k)) if k.isdigit() else (1, k)


class ItemTracker:
    """Persistent item tracker with DAG dependencies.

    Stores items as a JSON array on disk and auto-saves after every
    mutation. IDs are strings — caller-provided or auto-incremented.
    """

    def __init__(
        self,
        path: Path | str,
        default_flavor: ItemFlavor = ItemFlavor.TASK,
    ) -> None:
        self.path = Path(path)
        self.default_flavor = default_flavor
        self.lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self._max_id_ever = 0
        if self.path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @contextmanager
    def _mutate(self):
        """Context manager for mutations: yields, then saves to disk."""
        yield
        self._save()

    def _load(self) -> None:
        with open(self.path) as f:
            data = json.load(f)
        kind = data.get("kind", self.default_flavor)
        needs_dependents_rebuild = False
        for item in data.get("items", []):
            item.setdefault("flavor", kind)
            item["id"] = str(item["id"])
            item["depends_on"] = [str(d) for d in item.get("depends_on", [])]
            if "dependents" not in item:
                needs_dependents_rebuild = True
            item.setdefault("dependents", [])
            item.setdefault("owner", None)
            item.setdefault("active_form", None)
            item.setdefault("attempts", 0)
            item.setdefault("metadata", {})
        self._items = {item["id"]: item for item in data.get("items", [])}
        # Restore ID counters from file and high-water-mark
        self._max_id_ever = data.get("max_id", 0)
        if self._items:
            int_ids = [int(k) for k in self._items if k.isdigit()]
            if int_ids:
                max_from_items = max(int_ids)
                self._max_id_ever = max(self._max_id_ever, max_from_items)
        self._next_id = self._max_id_ever + 1
        # One-time migration: rebuild dependents from depends_on edges
        if needs_dependents_rebuild:
            for item in self._items.values():
                item["dependents"] = []
            for item in self._items.values():
                for dep_id in item["depends_on"]:
                    dep = self._items.get(dep_id)
                    if dep is not None:
                        dep["dependents"].append(item["id"])
        logger.info("ItemTracker loaded from %s: %d items", self.path, len(self._items))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": self.default_flavor,
            "max_id": self._max_id_ever,
            "items": list(self._items.values()),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Mutations — all auto-save
    # ------------------------------------------------------------------

    def add(
        self,
        title: str,
        description: str = "",
        depends_on: list[str | int] | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        item_id: str | int | None = None,
        owner: str | None = None,
        active_form: str | None = None,
        flavor: str | None = None,
    ) -> str:
        """Add an item and return its ID.

        When ``item_id`` is provided, uses that ID (must not already exist).
        Otherwise auto-increments. ``flavor`` defaults to ``default_flavor``.
        """
        with self._mutate():
            deps = [str(d) for d in (depends_on or [])]
            unknown = [d for d in deps if d not in self._items]
            if unknown:
                raise ValueError(f"Unknown dependency IDs: {unknown}")

            if item_id is not None:
                item_id = str(item_id)
                if item_id in self._items:
                    raise ValueError(f"Item {item_id} already exists")
                if item_id.isdigit():
                    numeric = int(item_id)
                    self._next_id = max(self._next_id, numeric + 1)
                    self._max_id_ever = max(self._max_id_ever, numeric)
            else:
                item_id = str(self._next_id)
                self._max_id_ever = max(self._max_id_ever, self._next_id)
                self._next_id += 1

            self._items[item_id] = {
                "id": item_id,
                "title": title,
                "description": description,
                "status": ItemStatus.PENDING,
                "flavor": flavor or self.default_flavor,
                "depends_on": deps,
                "dependents": [],
                "owner": owner,
                "active_form": active_form,
                "attempts": 0,
                "metadata": metadata or {},
            }
            # Maintain bidirectional edges
            for dep_id in deps:
                self._items[dep_id]["dependents"].append(item_id)
        return item_id

    _SENTINEL = object()

    def update(
        self,
        item_id: str | int,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        depends_on: list[str | int] | None = None,
        metadata: dict[str, Any] | None = None,
        owner: Any = _SENTINEL,
        active_form: Any = _SENTINEL,
    ) -> str:
        """Update an item. Returns a status message.

        Combines field updates, status transitions, and claim semantics:
        - ``status``: transition to a new status (same guards as ``set_status``).
        - ``owner``: when set on an unowned item, acts as a claim — rejects if
          already owned or terminal, and auto-transitions to ``in_progress``.
        - Items in ``RESET_ON_UPDATE`` statuses are reset to ``pending``
          after field updates (but before explicit ``status``).

        Use ``owner=None`` or ``active_form=None`` to explicitly clear
        these fields (the sentinel default means "don't change").
        """
        key = str(item_id)
        item = self._items.get(key)
        if item is None:
            return f"Error: item {item_id} not found."

        # --- Validate before mutating ---
        if owner is not self._SENTINEL and owner is not None:
            if item["owner"] is None and item["status"] in TERMINAL:
                return f"Error: item {item_id} is {item['status']} (terminal)."
            if item["owner"] is not None and item["owner"] != owner:
                return f"Error: item {item_id} is already owned by '{item['owner']}'."

        if depends_on is not None:
            str_deps = [str(d) for d in depends_on]
            unknown = [d for d in str_deps if d not in self._items or d == key]
            if unknown:
                return f"Error: invalid dependency IDs: {unknown}."

        if status is not None and status not in _STATUSES:
            return f"Error: '{status}' is not a valid status. Valid: {', '.join(_STATUSES)}."

        # --- All validation passed, now mutate ---
        with self._mutate():
            if owner is not self._SENTINEL and owner is not None:
                item["owner"] = owner
                if item["status"] not in TERMINAL and status is None:
                    item["status"] = ItemStatus.IN_PROGRESS
            elif owner is not self._SENTINEL:
                item["owner"] = owner

            if depends_on is not None:
                # Update bidirectional edges
                old_deps = set(item["depends_on"])
                new_deps = set(str_deps)
                for removed in old_deps - new_deps:
                    dep = self._items.get(removed)
                    if dep is not None:
                        dep["dependents"] = [d for d in dep["dependents"] if d != key]
                for added in new_deps - old_deps:
                    self._items[added]["dependents"].append(key)
                item["depends_on"] = str_deps
            if title is not None:
                item["title"] = title
            if description is not None:
                item["description"] = description
            if metadata is not None:
                item["metadata"].update(metadata)
            if active_form is not self._SENTINEL:
                item["active_form"] = active_form

            # Auto-reset statuses configured in RESET_ON_UPDATE
            if item["status"] in RESET_ON_UPDATE:
                item["status"] = ItemStatus.PENDING

            # Explicit status transition
            if status is not None:
                item["status"] = status

        return f"Item {item_id} updated."

    def bulk_transition(self, from_status: str, to_status: str) -> None:
        """Bulk-transition items between statuses.

        Used for shutdown cleanup (e.g. marking all in_progress as failed).
        """
        to_update = [i for i in self._items.values() if i["status"] == from_status]
        if not to_update:
            return

        with self._mutate():
            for item in to_update:
                item["status"] = to_status

    def sync_statuses(
        self,
        updates: dict[str, tuple[str, dict[str, Any]]],
    ) -> None:
        """Apply status + metadata from an authoritative source.

        Skips terminal items to prevent downgrading completed/deleted work.

        Args:
            updates: ``{item_id: (status, metadata_patch)}`` to apply.
        """
        applicable = [
            (self._items[str(item_id)], status, meta_patch)
            for item_id, (status, meta_patch) in updates.items()
            if (item := self._items.get(str(item_id))) is not None and item["status"] not in TERMINAL
        ]
        if not applicable:
            return
        with self._mutate():
            for item, status, meta_patch in applicable:
                item["status"] = status
                item["metadata"].update(meta_patch)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, item_id: str | int) -> dict[str, Any] | None:
        """Get a single item by ID, or None if not found."""
        item = self._items.get(str(item_id))
        return dict(item) if item is not None else None

    def list(
        self, *, status: str | None = None, flavor: str | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        """List items, optionally filtered by status, flavor, and/or free-text query."""
        items = sorted(self._items.values(), key=_sort_key)
        if status is not None:
            items = [i for i in items if i["status"] == status]
        if flavor is not None:
            items = [i for i in items if i.get("flavor") == flavor]
        if query is not None:
            q = query.lower()
            items = [i for i in items if q in f"{i['id']} {i.get('title', '')} {i.get('description', '')}".lower()]
        return items

    def ready(self) -> list[dict[str, Any]]:
        """Pending items whose dependencies are all terminal (ready to dispatch)."""
        result = []
        for item in sorted(self._items.values(), key=_sort_key):
            if item["status"] != ItemStatus.PENDING:
                continue
            deps_met = all(self._items.get(d, {}).get("status") in TERMINAL for d in item["depends_on"])
            if deps_met:
                result.append(item)
        return result

    def summary(self) -> dict[str, Any]:
        """Counts per status and overall statistics."""
        counts: dict[str, int] = {s: 0 for s in _STATUSES}
        for item in self._items.values():
            if item["status"] in counts:
                counts[item["status"]] += 1
        total = len(self._items)
        terminal_count = sum(counts.get(s, 0) for s in TERMINAL)
        return {
            "kind": self.default_flavor,
            "total": total,
            "counts": counts,
            "completion_rate": terminal_count / total if total else 0.0,
        }

    # ------------------------------------------------------------------
    # Scheduling — used by DAGRunner
    # ------------------------------------------------------------------

    def mark_in_progress(self, item_id: str) -> None:
        """Mark an item as in-progress and increment its attempt count."""
        item = self._items.get(str(item_id))
        if item is None:
            return
        with self._mutate():
            item["status"] = ItemStatus.IN_PROGRESS
            item["attempts"] = item.get("attempts", 0) + 1

    def record_result(self, item_id: str, success: bool) -> str:
        """Record a result: mark completed on success, failed otherwise."""
        item = self._items.get(str(item_id))
        if item is None:
            return "failed"
        with self._mutate():
            if success:
                item["status"] = ItemStatus.COMPLETED
                return "completed"
            else:
                item["status"] = ItemStatus.FAILED
                return "failed"

    def has_pending(self) -> bool:
        """True if any item still has work remaining (not terminal)."""
        return any(item["status"] not in TERMINAL for item in self._items.values())

    def get_attempts(self, item_id: str) -> int:
        """Return the number of attempts for an item."""
        return self._items.get(str(item_id), {}).get("attempts", 0)
