# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Goal tracker — manage formalization goals via ItemTracker.

Provides ``populate_tracker()`` to fill an ItemTracker from a list of
``FormalizationTarget`` entries, ``get_formalization_targets()`` to
reconstruct targets from tracker items, and ``build_target_index()``
to build an ID-to-target mapping from either source.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.tracker import ItemTracker

from ..types import FormalizationTarget, load_task_list

logger = logging.getLogger(__name__)


def populate_tracker(
    tracker: ItemTracker,
    targets: list[FormalizationTarget],
) -> None:
    """Create items from ``FormalizationTarget`` entries.

    Idempotent — skips targets whose index already exists in the tracker.
    Stores ``lean_declaration`` and ``lean_file`` in item metadata when present.
    """
    for i, target in enumerate(targets):
        if tracker.get(i) is not None:
            continue

        metadata: dict[str, str] = {}
        if target.lean_declaration:
            metadata["lean_declaration"] = target.lean_declaration
        if target.lean_file:
            metadata["lean_file"] = target.lean_file
        if target.kind:
            metadata["kind"] = target.kind
        if target.location:
            metadata["location"] = target.location

        tracker.add(
            title=target.name,
            description=target.description,
            metadata=metadata,
            item_id=i,
            flavor="goal",
        )

    logger.info("Statement tracker populated: %d items", len(tracker.list()))


def get_formalization_targets(
    tracker: ItemTracker,
    *,
    status: str | None = None,
) -> list[FormalizationTarget]:
    """Return ``FormalizationTarget`` entries for items that have a ``lean_declaration``.

    Args:
        status: If given, only return items with this status
            (e.g. ``"completed"`` for grading, ``"pending"`` for assignment).
    """
    targets: list[FormalizationTarget] = []
    for item in tracker.list(status=status):
        meta = item.get("metadata", {})
        ld = meta.get("lean_declaration")
        if ld:
            targets.append(
                FormalizationTarget(
                    name=item["title"],
                    description=item.get("description", ""),
                    lean_declaration=ld,
                    lean_file=meta.get("lean_file"),
                )
            )
    return targets


def build_target_index(
    *,
    task_file: Path | None = None,
    tracker: ItemTracker | None = None,
) -> dict[int, FormalizationTarget]:
    """Build an ID-to-target mapping from a YAML task file or tracker.

    Exactly one of ``task_file`` or ``tracker`` must be provided.
    When using ``task_file``, targets are indexed by their position in the list.
    When using ``tracker``, targets are indexed by their tracker item ID.
    """
    if task_file is not None:
        return {i: t for i, t in enumerate(load_task_list(task_file))}

    if tracker is not None:
        targets: dict[int, FormalizationTarget] = {}
        for item in tracker.list():
            meta = item.get("metadata", {})
            ld = meta.get("lean_declaration")
            if ld:
                targets[int(item["id"])] = FormalizationTarget(
                    name=item["title"],
                    description=item.get("description", ""),
                    lean_declaration=ld,
                    lean_file=meta.get("lean_file"),
                )
        return targets

    raise ValueError("Either task_file or tracker must be provided")
