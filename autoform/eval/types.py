# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Types and loaders for the autoformalization eval."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from logging import getLogger
from pathlib import Path

import yaml

from core.config import build_with_type_check

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormalizationTarget:
    """A single item from a YAML task list."""

    name: str
    description: str = ""
    kind: str = ""
    location: str = ""
    lean_declaration: str | None = None
    lean_file: str | None = None


@dataclass(frozen=True)
class AutoformConfig:
    """Top-level configuration for the autoformalization eval."""

    repo_dir: Path
    task_file: Path | None = None


@dataclass(frozen=True)
class AssessmentTarget:
    """A book statement enriched with its matched Lean declaration and axiom info.

    Used as the datum for rubric grading — all fields are available as
    template variables in rubric prompt templates via ``dataclasses.asdict``.
    """

    idx: int
    name: str = ""
    description: str = ""
    kind: str = ""
    location: str = ""
    lean_declaration: str | None = None
    lean_file: str | None = None
    lean_source: str | None = None
    axioms: str = ""
    deps: str = ""
    book_dir: str = ""
    match_confidence: str = ""
    match_reasoning: str = ""


@dataclass(frozen=True)
class MatchResult:
    """Structured output from the matching agent."""

    lean_declaration: str | None
    lean_file: str | None
    confidence: str  # "high", "medium", "low", "not_found"
    reasoning: str
    axioms: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# YAML task list
# ---------------------------------------------------------------------------


def load_task_list(path: Path) -> list[FormalizationTarget]:
    """Load a YAML task list into typed ``FormalizationTarget`` entries."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return build_with_type_check(list[FormalizationTarget], raw)


def save_task_list(targets: list[FormalizationTarget], path: Path) -> None:
    """Write a list of ``FormalizationTarget`` entries to a YAML file."""
    data = [{k: v for k, v in asdict(t).items() if v is not None and v != ""} for t in targets]
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
