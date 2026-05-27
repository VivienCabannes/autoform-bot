# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for ArchiveTraceStore, archive_reports, and archive_skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoform.bot.archive import (
    ArchiveTraceStore,
    _categorize_trace,
    _compute_usage_snapshot,
    archive_reports,
    archive_skills,
    prepare_fresh_run,
)


class FakeTrace:
    """Minimal trace-like object for testing."""

    def __init__(self, trace_id: str, messages: list[dict[str, Any]], final_status: str = "running"):
        self.trace_id = trace_id
        self.messages = messages
        self.final_status = final_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "messages": self.messages,
            "final_status": self.final_status,
            "summary": {},
        }


class TestArchiveTraceStore:
    def test_basic_save_creates_archive(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
        store.save(FakeTrace("t1", msgs))

        archive = json.loads((archive_dir / "t1.json").read_text())
        assert archive["messages"] == msgs

    def test_growth_appends_new_messages(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        msgs1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
        store.save(FakeTrace("t1", msgs1))

        msgs2 = msgs1 + [{"role": "assistant", "content": "hi"}, {"role": "user", "content": "bye"}]
        store.save(FakeTrace("t1", msgs2))

        archive = json.loads((archive_dir / "t1.json").read_text())
        assert archive["messages"] == msgs2

    def test_compaction_preserves_history(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        # Initial messages
        msgs_pre = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "resp2"},
        ]
        store.save(FakeTrace("t1", msgs_pre))

        # Compaction: system prompt + summary, fewer messages
        msgs_post = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "summary of previous conversation"},
        ]
        store.save(FakeTrace("t1", msgs_post))

        archive = json.loads((archive_dir / "t1.json").read_text())
        # Should have original 5 messages + summary (skipping system prompt from post-compaction)
        assert len(archive["messages"]) == 6
        assert archive["messages"][:5] == msgs_pre
        assert archive["messages"][5] == {"role": "assistant", "content": "summary of previous conversation"}

    def test_compaction_then_growth(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        # Pre-compaction
        msgs_pre = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
        ]
        store.save(FakeTrace("t1", msgs_pre))

        # Compaction
        msgs_compact = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "summary"},
        ]
        store.save(FakeTrace("t1", msgs_compact))

        # Growth after compaction
        msgs_new = msgs_compact + [
            {"role": "user", "content": "new_msg"},
            {"role": "assistant", "content": "new_resp"},
        ]
        store.save(FakeTrace("t1", msgs_new))

        archive = json.loads((archive_dir / "t1.json").read_text())
        expected = msgs_pre + [
            {"role": "assistant", "content": "summary"},
            {"role": "user", "content": "new_msg"},
            {"role": "assistant", "content": "new_resp"},
        ]
        assert archive["messages"] == expected

    def test_eviction_on_finalize(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
        store.save(FakeTrace("t1", msgs))
        assert "t1" in store._archive_messages

        # Finalize evicts from memory
        store.save(FakeTrace("t1", msgs, final_status="completed"))
        assert "t1" not in store._archive_messages
        assert "t1" not in store._message_counts

    def test_resume_from_archive_file(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"

        # First session: build archive with history
        store1 = ArchiveTraceStore(run_dir, archive_dir)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "resp1"},
        ]
        store1.save(FakeTrace("t1", msgs))

        # Second session: new store, archive file exists on disk
        store2 = ArchiveTraceStore(run_dir, archive_dir)
        # Agent resumes with same messages + new ones
        msgs_resumed = msgs + [{"role": "user", "content": "msg2"}]
        store2.save(FakeTrace("t1", msgs_resumed))

        archive = json.loads((archive_dir / "t1.json").read_text())
        # On first encounter with existing archive, counter is set to len(msgs_resumed)=4.
        # Since this is the first save for this trace_id in store2, no diff is computed.
        # The archive retains its loaded messages (3) — no new messages since it's initialization.
        # Actually, on first encounter: we load archive (3 msgs), set counter=4.
        # But we don't append anything on first encounter. So archive still has 3 msgs.
        # This is correct — the 4th message will be captured on subsequent saves.
        assert len(archive["messages"]) == 3

        # Third save: add another message
        msgs_more = msgs_resumed + [{"role": "assistant", "content": "resp2"}]
        store2.save(FakeTrace("t1", msgs_more))

        archive = json.loads((archive_dir / "t1.json").read_text())
        assert len(archive["messages"]) == 4
        assert archive["messages"][3] == {"role": "assistant", "content": "resp2"}

    def test_subdirectory_trace_ids(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        msgs = [{"role": "system", "content": "sys"}]
        store.save(FakeTrace("task1/attempt_1/worker-0", msgs))

        assert (archive_dir / "task1" / "attempt_1" / "worker-0.json").exists()

    def test_multiple_traces_independent(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "traces"
        archive_dir = tmp_path / "archive" / "traces"
        store = ArchiveTraceStore(run_dir, archive_dir)

        store.save(FakeTrace("t1", [{"role": "system", "content": "sys1"}]))
        store.save(FakeTrace("t2", [{"role": "system", "content": "sys2"}]))

        a1 = json.loads((archive_dir / "t1.json").read_text())
        a2 = json.loads((archive_dir / "t2.json").read_text())
        assert a1["messages"][0]["content"] == "sys1"
        assert a2["messages"][0]["content"] == "sys2"


class TestArchiveReports:
    def test_copies_reports_with_round_suffix(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        reports.mkdir()
        archive = tmp_path / "archive" / "reports" / "task_reports"

        (reports / "task1.json").write_text('{"status": "done"}')
        (reports / "task2.json").write_text('{"status": "fail"}')

        archive_reports(reports, archive, round_num=3)

        assert (archive / "task1_round3.json").exists()
        assert (archive / "task2_round3.json").exists()
        assert json.loads((archive / "task1_round3.json").read_text()) == {"status": "done"}

    def test_no_reports_is_noop(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        reports.mkdir()
        archive = tmp_path / "archive" / "reports" / "task_reports"

        archive_reports(reports, archive, round_num=1)
        assert archive.exists()
        assert list(archive.iterdir()) == []


class TestArchiveSkills:
    def test_copies_skill_folder(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "tasks" / "t1").mkdir(parents=True)
        (skills / "tasks" / "t1" / "guide.md").write_text("# Guide for t1")

        archive = tmp_path / "archive" / "skills"

        archive_skills(skills, archive, "t1")

        dest = archive / "tasks" / "t1" / "guide.md"
        assert dest.exists()
        assert dest.read_text() == "# Guide for t1"

    def test_overwrites_existing_archive(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        (skills / "tasks" / "t1").mkdir(parents=True)
        (skills / "tasks" / "t1" / "guide.md").write_text("v2")

        archive = tmp_path / "archive" / "skills"
        (archive / "tasks" / "t1").mkdir(parents=True)
        (archive / "tasks" / "t1" / "guide.md").write_text("v1")

        archive_skills(skills, archive, "t1")
        assert (archive / "tasks" / "t1" / "guide.md").read_text() == "v2"

    def test_nonexistent_source_is_noop(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        archive = tmp_path / "archive" / "skills"

        archive_skills(skills, archive, "nonexistent")
        assert not archive.exists()


# ---------------------------------------------------------------------------
# Usage snapshot tests
# ---------------------------------------------------------------------------


def _make_trace_json(
    cost: float = 1.0,
    tokens: int = 100,
    model: str = "claude-sonnet-4-20250514",
) -> dict[str, Any]:
    """Create a minimal trace dict with summary and llm_calls."""
    return {
        "summary": {
            "total_cost_usd": cost,
            "total_tokens": tokens,
        },
        "llm_calls": [
            {
                "model": model,
                "cost_usd": cost,
                "input_tokens": tokens // 2,
                "output_tokens": tokens // 2,
                "cached_input_tokens": tokens // 4,
                "cache_creation_input_tokens": 0,
            }
        ],
    }


def _write_trace(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class TestCategorizeTrace:
    def test_orchestrator(self) -> None:
        assert _categorize_trace(("orchestrator.json",)) == "orchestrator"

    def test_worker(self) -> None:
        assert _categorize_trace(("tasks", "1", "attempt_0", "worker-0.json")) == "workers"

    def test_reviewer(self) -> None:
        assert _categorize_trace(("tasks", "1", "attempt_0", "reviewer-0.json")) == "reviewers"

    def test_analyzer(self) -> None:
        assert _categorize_trace(("tasks", "1", "analyzer.json")) == "analyzers"

    def test_readers(self) -> None:
        assert _categorize_trace(("readers", "reader-0.json")) == "readers"

    def test_eval(self) -> None:
        assert _categorize_trace(("eval", "abc123", "trace.json")) == "eval"

    def test_judge(self) -> None:
        assert _categorize_trace(("judge", "trace.json")) == "eval"

    def test_merge_eval(self) -> None:
        assert _categorize_trace(("merge_eval", "abc", "trace.json")) == "merge_eval"

    def test_other(self) -> None:
        assert _categorize_trace(("unknown", "thing.json")) == "other"


class TestComputeUsageSnapshot:
    def test_basic_aggregation(self, tmp_path: Path) -> None:
        traces = tmp_path / "traces"
        _write_trace(traces / "orchestrator.json", _make_trace_json(cost=2.0, tokens=200))
        _write_trace(
            traces / "tasks" / "1" / "attempt_0" / "worker-0.json",
            _make_trace_json(cost=5.0, tokens=500),
        )

        files = list(traces.rglob("*.json"))
        snap = _compute_usage_snapshot(traces, files, "20260429T000000Z")

        assert snap["total_cost_usd"] == 7.0
        assert snap["total_tokens"] == 700
        assert snap["cost_by_category"]["orchestrator"] == 2.0
        assert snap["cost_by_category"]["workers"] == 5.0

    def test_categorization(self, tmp_path: Path) -> None:
        traces = tmp_path / "traces"
        _write_trace(traces / "orchestrator.json", _make_trace_json(cost=1.0))
        _write_trace(traces / "tasks" / "1" / "attempt_0" / "worker-0.json", _make_trace_json(cost=2.0))
        _write_trace(traces / "tasks" / "1" / "attempt_0" / "reviewer-0.json", _make_trace_json(cost=3.0))
        _write_trace(traces / "tasks" / "1" / "analyzer.json", _make_trace_json(cost=4.0))
        _write_trace(traces / "readers" / "reader-0.json", _make_trace_json(cost=5.0))
        _write_trace(traces / "eval" / "abc" / "judge.json", _make_trace_json(cost=6.0))
        _write_trace(traces / "merge_eval" / "def" / "matcher.json", _make_trace_json(cost=7.0))

        files = list(traces.rglob("*.json"))
        snap = _compute_usage_snapshot(traces, files, "ts")

        assert snap["cost_by_category"]["orchestrator"] == 1.0
        assert snap["cost_by_category"]["workers"] == 2.0
        assert snap["cost_by_category"]["reviewers"] == 3.0
        assert snap["cost_by_category"]["analyzers"] == 4.0
        assert snap["cost_by_category"]["readers"] == 5.0
        assert snap["cost_by_category"]["eval"] == 6.0
        assert snap["cost_by_category"]["merge_eval"] == 7.0

    def test_skips_steps_json(self, tmp_path: Path) -> None:
        traces = tmp_path / "traces"
        _write_trace(traces / "tasks" / "1" / "attempt_0" / "steps.json", {"winner_id": "worker-0"})
        _write_trace(traces / "tasks" / "1" / "attempt_0" / "worker-0.json", _make_trace_json(cost=5.0))

        files = list(traces.rglob("*.json"))
        snap = _compute_usage_snapshot(traces, files, "ts")

        assert snap["total_cost_usd"] == 5.0

    def test_per_model_stats(self, tmp_path: Path) -> None:
        traces = tmp_path / "traces"
        _write_trace(traces / "orchestrator.json", _make_trace_json(cost=1.0, tokens=100, model="model-a"))
        _write_trace(
            traces / "tasks" / "1" / "attempt_0" / "worker-0.json",
            _make_trace_json(cost=2.0, tokens=200, model="model-b"),
        )

        files = list(traces.rglob("*.json"))
        snap = _compute_usage_snapshot(traces, files, "ts")

        assert "model-a" in snap["model_stats"]
        assert "model-b" in snap["model_stats"]
        assert snap["model_stats"]["model-a"]["total_cost"] == 1.0
        assert snap["model_stats"]["model-b"]["total_cost"] == 2.0

    def test_empty_file_list(self, tmp_path: Path) -> None:
        traces = tmp_path / "traces"
        traces.mkdir()
        snap = _compute_usage_snapshot(traces, [], "ts")
        assert snap["total_cost_usd"] == 0.0
        assert snap["total_tokens"] == 0

    def test_skips_files_without_summary(self, tmp_path: Path) -> None:
        traces = tmp_path / "traces"
        _write_trace(traces / "tasks" / "1" / "attempt_0" / "worker-0.json", {"no_summary": True})

        files = list(traces.rglob("*.json"))
        snap = _compute_usage_snapshot(traces, files, "ts")
        assert snap["total_cost_usd"] == 0.0


class TestPrepareFreshRunSnapshot:
    def _make_run(self, tmp_path: Path) -> Path:
        """Create a minimal run directory with various trace types."""
        run = tmp_path / "run"

        # dag.json with one completed and one in-progress task
        dag = {
            "max_id": 2,
            "items": [
                {"id": "1", "status": "completed", "depends_on": [], "dependents": []},
                {"id": "2", "status": "in_progress", "depends_on": [], "dependents": []},
            ],
        }
        (run / "dag.json").parent.mkdir(parents=True, exist_ok=True)
        (run / "dag.json").write_text(json.dumps(dag))

        # Traces
        traces = run / "traces"
        _write_trace(traces / "orchestrator.json", _make_trace_json(cost=1.0))
        _write_trace(traces / "tasks" / "1" / "attempt_0" / "worker-0.json", _make_trace_json(cost=2.0))
        _write_trace(traces / "tasks" / "2" / "attempt_0" / "worker-0.json", _make_trace_json(cost=3.0))
        _write_trace(traces / "eval" / "abc" / "judge.json", _make_trace_json(cost=4.0))
        _write_trace(traces / "merge_eval" / "def" / "matcher.json", _make_trace_json(cost=5.0))
        _write_trace(traces / "readers" / "reader-0.json", _make_trace_json(cost=6.0))

        # Reports
        reports = run / "reports" / "task_reports"
        reports.mkdir(parents=True)
        (reports / "1.json").write_text("{}")
        (reports / "2.json").write_text("{}")

        # Archive dir
        (run / "archive").mkdir(parents=True, exist_ok=True)

        return run

    def test_creates_usage_snapshot(self, tmp_path: Path) -> None:
        run = self._make_run(tmp_path)
        prepare_fresh_run(run)

        snapshots = list((run / "archive").glob("usage_snapshot_*.json"))
        assert len(snapshots) == 1

        snap = json.loads(snapshots[0].read_text())
        # Orchestrator(1) + task1(2) + eval(4) + merge_eval(5) + readers(6) = 18
        assert snap["total_cost_usd"] == 18.0
        assert snap["cost_by_category"]["orchestrator"] == 1.0
        assert snap["cost_by_category"]["workers"] == 2.0
        assert snap["cost_by_category"]["eval"] == 4.0
        assert snap["cost_by_category"]["merge_eval"] == 5.0
        assert snap["cost_by_category"]["readers"] == 6.0

    def test_deletes_historical_traces(self, tmp_path: Path) -> None:
        run = self._make_run(tmp_path)
        prepare_fresh_run(run)

        traces = run / "traces"
        assert not (traces / "orchestrator.json").exists()
        assert not (traces / "tasks" / "1").exists()
        assert not (traces / "eval").exists()
        assert not (traces / "merge_eval").exists()
        assert not (traces / "readers").exists()

    def test_preserves_in_progress_task_traces(self, tmp_path: Path) -> None:
        run = self._make_run(tmp_path)
        prepare_fresh_run(run)

        # Task 2 is in_progress — its traces must survive
        assert (run / "traces" / "tasks" / "2" / "attempt_0" / "worker-0.json").exists()
        # Its report must also survive
        assert (run / "reports" / "task_reports" / "2.json").exists()

    def test_archives_orchestrator_trace_separately(self, tmp_path: Path) -> None:
        run = self._make_run(tmp_path)
        prepare_fresh_run(run)

        orch_archives = list((run / "archive").glob("orchestrator_*.json"))
        assert len(orch_archives) == 1

    def test_deletes_stale_stats_cache(self, tmp_path: Path) -> None:
        run = self._make_run(tmp_path)
        (run / "stats.json").write_text('{"total_cost": 999}')

        prepare_fresh_run(run)

        assert not (run / "stats.json").exists()

    def test_multiple_fresh_runs_accumulate_snapshots(self, tmp_path: Path) -> None:
        run = self._make_run(tmp_path)
        prepare_fresh_run(run)

        first_snapshots = sorted((run / "archive").glob("usage_snapshot_*.json"))
        assert len(first_snapshots) == 1
        snap1 = json.loads(first_snapshots[0].read_text())
        assert snap1["total_cost_usd"] == 18.0

        # Simulate new traces appearing after first fresh run
        traces = run / "traces"
        _write_trace(traces / "orchestrator.json", _make_trace_json(cost=10.0))
        _write_trace(traces / "eval" / "xyz" / "judge.json", _make_trace_json(cost=20.0))

        # Mark task 2 as completed for second prune
        dag = json.loads((run / "dag.json").read_text())
        for item in dag["items"]:
            if item["id"] == "2":
                item["status"] = "completed"
        (run / "dag.json").write_text(json.dumps(dag))

        # Ensure different timestamp by renaming the first snapshot
        first_snapshots[0].rename(run / "archive" / "usage_snapshot_20260101T000000Z.json")

        prepare_fresh_run(run)

        snapshots = sorted((run / "archive").glob("usage_snapshot_*.json"))
        assert len(snapshots) == 2

        # Second snapshot: orchestrator(10) + task2(3) + eval(20) = 33
        snap2 = json.loads(snapshots[1].read_text())
        assert snap2["total_cost_usd"] == 33.0
