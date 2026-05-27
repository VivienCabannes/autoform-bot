# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for EvalGate."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from autoform.eval.types import FormalizationTarget
from autoform.eval.utils.gate import EvalGate, EvalGateResult, RepoCheckResults
from autoform.eval.utils.tracker import build_target_index


def _create_test_targets(tmp_path: Path) -> tuple[dict[int, FormalizationTarget], Path]:
    """Create a minimal target index and repo directory for testing."""
    task_file = tmp_path / "targets.yaml"
    task_file.write_text(
        yaml.dump(
            [
                {
                    "name": "Test Theorem",
                    "description": "For all natural numbers n, n + 0 = n.",
                    "lean_declaration": "test_thm",
                    "lean_file": "Test.lean",
                },
            ]
        )
    )

    code_dir = tmp_path / "code"
    code_dir.mkdir()
    (code_dir / "Test.lean").write_text(
        dedent("""\
        theorem test_thm (n : Nat) : n + 0 = n := by simp
    """)
    )

    targets = build_target_index(task_file=task_file)
    return targets, code_dir


class TestEvalGate:
    def test_all_statement_ids(self, tmp_path: Path) -> None:
        targets, code_dir = _create_test_targets(tmp_path)
        gate = EvalGate(lambda: None, targets, repo_dir=code_dir)  # type: ignore[arg-type]
        assert gate.all_statement_ids == [0]

    def test_resolve_statement(self, tmp_path: Path) -> None:
        targets, code_dir = _create_test_targets(tmp_path)
        gate = EvalGate(lambda: None, targets, repo_dir=code_dir)  # type: ignore[arg-type]

        target = gate._targets_by_id[0]
        resolved = gate._resolve_statement(target, code_dir)
        assert resolved is not None
        target, source = resolved
        assert target.name == "Test Theorem"
        assert target.lean_declaration == "test_thm"
        assert "test_thm" in source

    def test_resolve_missing_lean_file(self, tmp_path: Path) -> None:
        targets, code_dir = _create_test_targets(tmp_path)
        gate = EvalGate(lambda: None, targets, repo_dir=code_dir)  # type: ignore[arg-type]

        target_no_file = FormalizationTarget(name="No File", lean_declaration="missing")
        assert gate._resolve_statement(target_no_file, code_dir) is None

    def test_resolve_with_repo_dir_override(self, tmp_path: Path) -> None:
        targets, code_dir = _create_test_targets(tmp_path)
        gate = EvalGate(lambda: None, targets, repo_dir=code_dir)  # type: ignore[arg-type]

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "Test.lean").write_text("theorem test_thm (n : Nat) : n + 0 = n := by omega\n")

        target = gate._targets_by_id[0]
        resolved = gate._resolve_statement(target, worktree)
        assert resolved is not None
        _, source = resolved
        assert "omega" in source

    def test_eval_gate_result_passed(self) -> None:
        from core.eval.dtypes import Score

        result = EvalGateResult(
            passed=True,
            statement_scores={1: Score(value=4.5, passed=True)},
            feedback="Statement 1: PASS",
        )
        assert result.passed
        assert len(result.statement_scores) == 1

    def test_eval_gate_result_failed(self) -> None:
        from core.eval.dtypes import Score

        result = EvalGateResult(
            passed=False,
            statement_scores={
                1: Score(value=4.5, passed=True),
                2: Score(value=1.0, passed=False),
            },
            feedback="Statement 2: FAIL",
        )
        assert not result.passed

    def test_requires_context_manager(self, tmp_path: Path) -> None:
        targets, code_dir = _create_test_targets(tmp_path)
        gate = EvalGate(lambda: None, targets, repo_dir=code_dir)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="async context manager"):
            import asyncio

            asyncio.run(gate.evaluate_statements([0], code_dir))

    def test_repo_check_results_injectable(self, tmp_path: Path) -> None:
        targets, code_dir = _create_test_targets(tmp_path)
        repo_checks = RepoCheckResults(
            forbidden_keyword_violations=[],
            axiom_violations={},
        )
        gate = EvalGate(
            lambda: None,  # type: ignore[arg-type]
            targets,
            repo_dir=code_dir,
            repo_checks=repo_checks,
        )
        # Verify precomputed checks are stored
        assert gate._repo_checks is not None
        assert gate._repo_checks.forbidden_keyword_violations == []


class TestBuildTargetIndex:
    def test_from_task_file(self, tmp_path: Path) -> None:
        task_file = tmp_path / "targets.yaml"
        task_file.write_text(
            yaml.dump(
                [
                    {"name": "Thm1", "description": "desc1", "lean_declaration": "thm1", "lean_file": "A.lean"},
                    {"name": "Thm2", "description": "desc2"},
                ]
            )
        )
        index = build_target_index(task_file=task_file)
        assert len(index) == 2
        assert index[0].name == "Thm1"
        assert index[1].name == "Thm2"

    def test_from_tracker(self, tmp_path: Path) -> None:
        from core.tracker import ItemTracker

        tracker = ItemTracker(tmp_path / "dag.json")
        tracker.add(
            title="Thm1",
            description="desc1",
            metadata={"lean_declaration": "thm1", "lean_file": "A.lean"},
            item_id=0,
            flavor="goal",
        )
        tracker.add(
            title="Thm2",
            description="desc2",
            item_id=1,
            flavor="goal",
        )
        index = build_target_index(tracker=tracker)
        # Only items with lean_declaration are included
        assert len(index) == 1
        assert index[0].name == "Thm1"
        assert index[0].lean_declaration == "thm1"

    def test_requires_one_source(self) -> None:
        with pytest.raises(ValueError, match="Either task_file or tracker"):
            build_target_index()
