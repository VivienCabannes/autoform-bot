# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for EscalationReader."""

from __future__ import annotations

import json
from pathlib import Path

from autoform.bot.tools.escalate.core import EscalationReader


def _write_escalations(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _make_traces(traces_dir: Path, task_id: str, agents: list[str]) -> None:
    attempt = traces_dir / "tasks" / task_id / "attempt_1"
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "steps.json").write_text("{}")
    for agent in agents:
        (attempt / f"{agent}.json").write_text("{}")


class TestEscalationReader:
    def test_no_escalations_file(self, tmp_path: Path) -> None:
        reader = EscalationReader(tmp_path / "escalations.jsonl", tmp_path / "traces", "task-1")
        result = reader.get_escalations()
        assert "No escalations" in result

    def test_filters_by_task_id(self, tmp_path: Path) -> None:
        """Entries with task_id are filtered by task_id, not agent ID."""
        _write_escalations(
            tmp_path / "escalations.jsonl",
            [
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "warning",
                    "message": "belongs to task-1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "task_id": "task-1",
                },
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "critical",
                    "message": "belongs to task-2",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "task_id": "task-2",
                },
                {
                    "agent_id": "rank0-worker-1",
                    "severity": "warning",
                    "message": "also task-1",
                    "timestamp": "2026-01-01T00:02:00Z",
                    "task_id": "task-1",
                },
            ],
        )

        # No traces needed — task_id filtering doesn't use trace dirs
        reader = EscalationReader(tmp_path / "escalations.jsonl", tmp_path / "traces", "task-1")
        result = reader.get_escalations()

        assert "belongs to task-1" in result
        assert "also task-1" in result
        assert "belongs to task-2" not in result
        assert "2 found" in result

    def test_backward_compat_falls_back_to_agent_ids(self, tmp_path: Path) -> None:
        """Entries without task_id fall back to agent-ID filtering via traces."""
        traces_dir = tmp_path / "traces"
        _make_traces(traces_dir, "task-1", ["rank0-worker-0", "rank0-worker-1"])

        _write_escalations(
            tmp_path / "escalations.jsonl",
            [
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "warning",
                    "message": "old entry no task_id",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                {
                    "agent_id": "rank0-worker-5",
                    "severity": "critical",
                    "message": "different agent old entry",
                    "timestamp": "2026-01-01T00:01:00Z",
                },
            ],
        )

        reader = EscalationReader(tmp_path / "escalations.jsonl", traces_dir, "task-1")
        result = reader.get_escalations()

        assert "old entry no task_id" in result
        assert "different agent old entry" not in result
        assert "1 found" in result

    def test_mixed_old_and_new_entries(self, tmp_path: Path) -> None:
        """Entries with task_id and without task_id both handled correctly."""
        traces_dir = tmp_path / "traces"
        _make_traces(traces_dir, "task-1", ["rank0-worker-0"])

        _write_escalations(
            tmp_path / "escalations.jsonl",
            [
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "warning",
                    "message": "old format",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                {
                    "agent_id": "rank0-worker-2",
                    "severity": "critical",
                    "message": "new format correct task",
                    "timestamp": "2026-01-01T00:01:00Z",
                    "task_id": "task-1",
                },
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "warning",
                    "message": "new format wrong task",
                    "timestamp": "2026-01-01T00:02:00Z",
                    "task_id": "task-2",
                },
            ],
        )

        reader = EscalationReader(tmp_path / "escalations.jsonl", traces_dir, "task-1")
        result = reader.get_escalations()

        assert "old format" in result
        assert "new format correct task" in result
        assert "new format wrong task" not in result
        assert "2 found" in result

    def test_across_multiple_attempts(self, tmp_path: Path) -> None:
        traces_dir = tmp_path / "traces"
        _make_traces(traces_dir, "task-1", ["rank0-worker-0"])
        attempt2 = traces_dir / "tasks" / "task-1" / "attempt_2"
        attempt2.mkdir(parents=True, exist_ok=True)
        (attempt2 / "steps.json").write_text("{}")
        (attempt2 / "rank0-worker-3.json").write_text("{}")

        _write_escalations(
            tmp_path / "escalations.jsonl",
            [
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "warning",
                    "message": "attempt 1 issue",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "task_id": "task-1",
                },
                {
                    "agent_id": "rank0-worker-3",
                    "severity": "critical",
                    "message": "attempt 2 issue",
                    "timestamp": "2026-01-01T01:00:00Z",
                    "task_id": "task-1",
                },
            ],
        )

        reader = EscalationReader(tmp_path / "escalations.jsonl", traces_dir, "task-1")
        result = reader.get_escalations()

        assert "attempt 1 issue" in result
        assert "attempt 2 issue" in result
        assert "2 found" in result

    def test_no_matching_escalations(self, tmp_path: Path) -> None:
        traces_dir = tmp_path / "traces"
        _make_traces(traces_dir, "task-1", ["rank0-worker-0"])

        _write_escalations(
            tmp_path / "escalations.jsonl",
            [
                {
                    "agent_id": "rank0-worker-5",
                    "severity": "warning",
                    "message": "different agent",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "task_id": "task-99",
                },
            ],
        )

        reader = EscalationReader(tmp_path / "escalations.jsonl", traces_dir, "task-1")
        result = reader.get_escalations()
        assert "No escalations" in result

    def test_same_agent_different_tasks_isolated(self, tmp_path: Path) -> None:
        """Same agent ID working on two tasks — escalations stay isolated."""
        _write_escalations(
            tmp_path / "escalations.jsonl",
            [
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "warning",
                    "message": "from task A",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "task_id": "task-A",
                },
                {
                    "agent_id": "rank0-worker-0",
                    "severity": "critical",
                    "message": "from task B",
                    "timestamp": "2026-01-01T01:00:00Z",
                    "task_id": "task-B",
                },
            ],
        )

        reader_a = EscalationReader(tmp_path / "escalations.jsonl", tmp_path / "traces", "task-A")
        result_a = reader_a.get_escalations()
        assert "from task A" in result_a
        assert "from task B" not in result_a

        reader_b = EscalationReader(tmp_path / "escalations.jsonl", tmp_path / "traces", "task-B")
        result_b = reader_b.get_escalations()
        assert "from task B" in result_b
        assert "from task A" not in result_b
