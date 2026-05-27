# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Task — a work unit with tree structure for hierarchical context.

Tasks form a tree: each task knows its parent (for big-picture context)
and can have children (sub-tasks). `description` is the single text
field — it serves as both the human-readable goal and the text sent
to the LLM. Tasks can also declare dependencies on other tasks for
DAG-based scheduling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.eval.grader import Grader


class TaskStatus(str, Enum):
    """Status of a task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """A work unit dispatched to agents for execution.

    Supports tree structure: parent gives agents big-picture context,
    children track sub-tasks. Also supports flat dependency lists
    for DAG-based scheduling via depends_on.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = ""
    grader: Grader[Any, Any] | None = None
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    parent: Task | None = field(default=None, repr=False)
    children: list[Task] = field(default_factory=list, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_child(self, child: Task) -> Task:
        """Add a child task and set its parent."""
        child.parent = self
        self.children.append(child)
        return child

    def root(self) -> Task:
        """Walk up to the root of the task tree."""
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def context_chain(self) -> list[str]:
        """Get descriptions from root to this task (for context)."""
        chain = []
        node: Task | None = self
        while node is not None:
            if node.description:
                chain.append(node.description)
            node = node.parent
        chain.reverse()
        return chain
