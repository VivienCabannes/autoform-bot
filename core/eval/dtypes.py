# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Shared type definitions for the eval pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

Datum = TypeVar("Datum")
Output = TypeVar("Output")


@dataclass
class Score:
    value: float
    passed: bool
    feedback: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult(Generic[Datum, Output]):
    """Result of evaluating a single datum."""

    datum_id: str
    score: Score
    datum: Datum | None = None
    output: Output | None = None
    latency: float | None = None
    cost: float | None = None
