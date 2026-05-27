# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Version-control workflow trace records.

Generic dataclasses for recording build attempts, merge attempts,
review rejections, and no-commit feedback during agent workflows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class BuildAttempt:
    """Record of a build attempt."""

    agent_id: str
    timestamp: float
    duration_ms: float
    success: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MergeAttempt:
    """Record of a merge attempt."""

    agent_id: str
    timestamp: float
    duration_ms: float
    success: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NoCommitFeedback:
    """Record of a no-commit feedback given to an agent."""

    agent_id: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewRejection:
    """Record of a review rejection."""

    agent_id: str
    timestamp: float
    feedback: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
