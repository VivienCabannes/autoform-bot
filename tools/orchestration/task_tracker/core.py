"""Task tracker helpers — query functions for task-specific views.

These functions provide task-domain views on top of the generic ItemTracker.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.tracker import ItemStatus, ItemTracker

logger = logging.getLogger(__name__)


def get_state(tracker: ItemTracker) -> str:
    """Return the task overview as JSON (titles, statuses, deps — no full descriptions)."""
    overview = []
    for item in tracker.list():
        entry: dict[str, Any] = {
            "id": item["id"],
            "title": item.get("title", ""),
            "status": item["status"],
            "attempts": item.get("attempts", 0),
            "depends_on": item.get("depends_on", []),
            "dependents": item.get("dependents", []),
        }
        if item.get("owner") is not None:
            entry["owner"] = item["owner"]
        meta = {k: v for k, v in item.get("metadata", {}).items() if k != "attempts"}
        if meta:
            entry["metadata"] = meta
        overview.append(entry)
    return json.dumps({"tasks": overview}, indent=2)


def task_summary(tracker: ItemTracker) -> dict[str, Any]:
    """Summary with task-specific field names."""
    items = tracker.list()
    total = len(items)
    completed = sum(1 for i in items if i["status"] == ItemStatus.COMPLETED)
    deleted = sum(1 for i in items if i["status"] == ItemStatus.DELETED)
    failed = sum(1 for i in items if i["status"] == ItemStatus.FAILED)
    pending = sum(1 for i in items if i["status"] == ItemStatus.PENDING)
    return {
        "total_tasks": total,
        "completed": completed,
        "deleted": deleted,
        "failed": failed,
        "pending": pending,
        "success_rate": completed / total if total else 0.0,
    }
