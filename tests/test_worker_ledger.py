from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import autoform_worker.ledger as ledger_module
from autoform_worker.ledger import (
    GenerationConflict,
    InvalidTransition,
    LedgerBusy,
    LedgerError,
    RunIdentity,
    RunLedger,
)


def _identity(project: Path) -> RunIdentity:
    return RunIdentity(
        repository_id="https://example.test/owner/repo.git",
        project_root=str(project.resolve()),
        target_ref="refs/heads/main",
        base_oid="1" * 40,
        runtime_revision="2" * 64,
        coverage_revision="3" * 64,
        source_artifact_sha256="4" * 64,
        plugin_revision="5" * 40,
        toolchain_fingerprint="6" * 64,
        execution_input_sha256="7" * 64,
    )


def _running_ledger(tmp_path: Path) -> tuple[RunLedger, str]:
    ledger = RunLedger(tmp_path / "state/run.sqlite3", clock_ns=iter(range(100, 10_000)).__next__)
    run = ledger.create_run(_identity(tmp_path), run_id="run-1")
    ledger.add_tasks(run.run_id, [("node-a", "statement")])
    run = ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
    return ledger, run.run_id


def _begin(ledger: RunLedger, run_id: str, tmp_path: Path, *, attempt_id: str = "attempt-1"):
    task = ledger.tasks(run_id)[0]
    return ledger.begin_attempt(
        run_id,
        task.node_id,
        task.phase,
        expected_task_generation=task.generation,
        worktree_path=tmp_path / "worktree",
        branch="autoform/run-1/node-a/1",
        base_oid="1" * 40,
        backend="codex",
        claim_key="author/node-a",
        claim_token={"claim_id": "claim-1", "ref_oid": "8" * 40},
        attempt_id=attempt_id,
    )


def test_run_identity_and_events_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    identity = _identity(tmp_path)
    with RunLedger(path, clock_ns=lambda: 123) as ledger:
        created = ledger.create_run(identity, run_id="run-1")
        assert created.identity == identity
        assert created.identity_sha256 == identity.sha256
        assert created.status == "created"
        assert created.generation == 0
        assert [event.kind for event in ledger.events("run-1")] == ["run.created"]
        assert ledger._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert ledger._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert ledger._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with RunLedger(path) as reopened:
        assert reopened.get_run("run-1").identity_sha256 == identity.sha256
        assert [event.payload for event in reopened.events("run-1")] == [
            {"identity_sha256": identity.sha256}
        ]


def test_generation_checks_and_idempotent_stop(tmp_path: Path) -> None:
    with RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(100)).__next__) as ledger:
        run = ledger.create_run(_identity(tmp_path), run_id="run-1")
        running = ledger.transition_run("run-1", "running", expected_generation=run.generation)
        assert running.generation == 1
        with pytest.raises(GenerationConflict):
            ledger.transition_run("run-1", "failed", expected_generation=0)
        stopped = ledger.request_stop("run-1")
        repeated = ledger.request_stop("run-1")
        assert stopped == repeated
        assert stopped.stop_requested
        assert stopped.generation == 2
        with pytest.raises(InvalidTransition):
            ledger.transition_run("run-1", "complete", expected_generation=stopped.generation)


def test_complete_requires_every_persisted_task_to_be_integrated(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        run = ledger.get_run(run_id)
        with pytest.raises(InvalidTransition, match="not integrated"):
            ledger.transition_run(run_id, "complete", expected_generation=run.generation)
        assert ledger.get_run(run_id).status == "running"
    finally:
        ledger.close()


def test_resume_clears_stop_and_is_idempotent(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        stop_requested = ledger.request_stop(run_id)
        ledger.recover_interrupted(run_id)
        stopped = ledger.transition_run(
            run_id,
            "stopped",
            expected_generation=stop_requested.generation,
            detail="operator stop",
        )

        resumed = ledger.resume_run(run_id, expected_generation=stopped.generation)
        repeated = ledger.resume_run(run_id, expected_generation=resumed.generation)
        assert resumed == repeated
        assert resumed.status == "running"
        assert not resumed.stop_requested
        assert ledger.tasks(run_id)[0].status == "retrying"
        assert ledger.get_attempt(attempt.attempt_id).status == "interrupted"
    finally:
        ledger.close()


def test_task_attempt_recovery_is_explicit_and_idempotent(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        assert attempt.status == "running"
        assert ledger.tasks(run_id)[0].status == "running"

        assert ledger.recover_interrupted(run_id) == (attempt.attempt_id,)
        assert ledger.get_attempt(attempt.attempt_id).status == "interrupted"
        assert ledger.tasks(run_id)[0].status == "retrying"
        assert ledger.recover_interrupted(run_id) == ()

        retry = _begin(ledger, run_id, tmp_path, attempt_id="attempt-2")
        result = ledger.finish_attempt(retry.attempt_id, "retrying", detail="backend unavailable")
        assert result.status == "retrying"
        assert result.finished_ns is not None
        assert ledger.tasks(run_id)[0].attempts == 2
        with pytest.raises(InvalidTransition):
            ledger.finish_attempt(retry.attempt_id, "failed", detail="different result")
    finally:
        ledger.close()


def test_stop_request_turns_interrupted_work_into_stopped(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        ledger.request_stop(run_id)

        assert ledger.recover_interrupted(run_id) == (attempt.attempt_id,)
        assert ledger.tasks(run_id)[0].status == "stopped"
    finally:
        ledger.close()


def test_candidate_requires_all_gates_before_queue_and_integration(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        candidate = ledger.finish_attempt(attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        assert candidate.candidate_oid == "9" * 40
        build_evidence = ledger.put_artifact("gate", b"lean build passed\n")
        review_failure = ledger.put_artifact("gate", b"source review failed\n")
        ledger.record_gate(candidate.attempt_id, "lean-build", True, evidence_sha256=build_evidence)
        ledger.record_gate(
            candidate.attempt_id,
            "source-review",
            False,
            evidence_sha256=review_failure,
        )

        with pytest.raises(InvalidTransition, match="source-review"):
            ledger.enqueue_candidate(
                candidate.attempt_id,
                required_gates=("lean-build", "source-review"),
                queue_ref="refs/autoform/queue/run-1/node-a",
                expected_target_oid="1" * 40,
            )
        with pytest.raises(LedgerError, match="already recorded"):
            ledger.record_gate(
                candidate.attempt_id,
                "source-review",
                True,
                evidence_sha256=ledger.put_artifact("gate", b"source review passed\n"),
            )

        with pytest.raises(InvalidTransition, match="cannot add tasks"):
            ledger.add_tasks(run_id, [("node-b", "proof")])
    finally:
        ledger.close()

    ledger = RunLedger(tmp_path / "second/run.sqlite3", clock_ns=iter(range(10_000, 20_000)).__next__)
    try:
        run = ledger.create_run(_identity(tmp_path), run_id="run-2")
        second_task = ledger.add_tasks(run.run_id, [("node-b", "proof")])[0]
        ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
        second = ledger.begin_attempt(
            run.run_id,
            second_task.node_id,
            second_task.phase,
            expected_task_generation=second_task.generation,
            worktree_path=tmp_path / "worktree-2",
            branch="autoform/run-2/node-b/1",
            base_oid="1" * 40,
            backend="codex",
            claim_key="author/node-b",
            claim_token={"claim_id": "claim-2", "ref_oid": "8" * 40},
            attempt_id="attempt-2",
        )
        ledger.finish_attempt(second.attempt_id, "candidate", candidate_oid="c" * 40)
        build_evidence = ledger.put_artifact("gate", b"lean build passed\n")
        review_evidence = ledger.put_artifact("gate", b"source review passed\n")
        ledger.record_gate(second.attempt_id, "lean-build", True, evidence_sha256=build_evidence)
        ledger.record_gate(second.attempt_id, "source-review", True, evidence_sha256=review_evidence)
        item = ledger.enqueue_candidate(
            second.attempt_id,
            required_gates=("source-review", "lean-build"),
            queue_ref="refs/autoform/queue/run-1/node-b",
            expected_target_oid="1" * 40,
            queue_item_id="queue-1",
        )
        assert item == "queue-1"
        assert ledger.tasks(run.run_id)[-1].status == "queued"
        ledger.mark_integrated(item, integrated_oid="f" * 40)
        integrated = ledger.tasks(run.run_id)[-1]
        assert integrated.status == "integrated"
        assert integrated.integrated_oid == "f" * 40
        ledger.mark_integrated(item, integrated_oid="f" * 40)
        current = ledger.get_run(run.run_id)
        complete = ledger.transition_run(
            run.run_id,
            "complete",
            expected_generation=current.generation,
        )
        assert complete.status == "complete"
    finally:
        ledger.close()


def test_failed_multi_task_insert_rolls_back_rows_and_events(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(100)).__next__)
    run_id = ledger.create_run(_identity(tmp_path), run_id="run-1").run_id
    ledger.add_tasks(run_id, [("node-a", "statement")])
    try:
        before = ledger.events(run_id)
        with pytest.raises(LedgerError, match="duplicate task"):
            ledger.add_tasks(run_id, [("node-b", "proof"), ("node-a", "statement")])
        assert [(task.node_id, task.phase) for task in ledger.tasks(run_id)] == [
            ("node-a", "statement")
        ]
        assert ledger.events(run_id) == before
    finally:
        ledger.close()


def test_events_are_append_only_at_the_database_boundary(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        sequence = ledger.events(run_id)[0].sequence
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger._connection.execute("UPDATE events SET kind = 'changed' WHERE sequence = ?", (sequence,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger._connection.execute("DELETE FROM events WHERE sequence = ?", (sequence,))
    finally:
        ledger.close()


def test_gate_evidence_must_exist_and_remain_intact_until_enqueue(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        ledger.finish_attempt(attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        with pytest.raises(LedgerError, match="not in the artifact store"):
            ledger.record_gate(attempt.attempt_id, "lean-build", True, evidence_sha256="a" * 64)

        digest = ledger.put_artifact("gate", b"passed\n")
        ledger.record_gate(attempt.attempt_id, "lean-build", True, evidence_sha256=digest)
        row = ledger._connection.execute(
            "SELECT relative_path FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()
        (ledger.path.parent / row[0]).write_bytes(b"failed\n")
        with pytest.raises(LedgerError, match="artifact (size|content) changed"):
            ledger.enqueue_candidate(
                attempt.attempt_id,
                required_gates=("lean-build",),
                queue_ref="refs/autoform/queue/run-1/node-a",
                expected_target_oid="1" * 40,
            )
        assert ledger.tasks(run_id)[0].status == "candidate"
    finally:
        ledger.close()


def test_artifacts_are_content_addressed_synced_and_reverified(tmp_path: Path) -> None:
    with RunLedger(tmp_path / "state/run.sqlite3") as ledger:
        digest = ledger.put_artifact("review", b"review evidence\n")
        assert digest == ledger.put_artifact("review", b"review evidence\n")
        assert ledger.read_artifact(digest) == b"review evidence\n"

        path = ledger.path.parent / ledger._connection.execute(
            "SELECT relative_path FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()[0]
        path.write_bytes(b"changed\n")
        with pytest.raises(LedgerError, match="artifact (size|content) changed"):
            ledger.read_artifact(digest)


def test_artifact_reader_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    with RunLedger(tmp_path / "state/run.sqlite3") as ledger:
        digest = ledger.put_artifact("gate", b"evidence\n")
        row = ledger._connection.execute(
            "SELECT relative_path FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()
        path = ledger.path.parent / row[0]
        backup = path.with_name("backup")
        path.rename(backup)
        path.symlink_to(backup)
        with pytest.raises(LedgerError, match="cannot be inspected|private regular file"):
            ledger.read_artifact(digest)
        path.unlink()
        os.link(backup, path)
        with pytest.raises(LedgerError, match="private regular file"):
            ledger.read_artifact(digest)


def test_only_one_coordinator_can_hold_the_lock(tmp_path: Path) -> None:
    with RunLedger(tmp_path / "state/run.sqlite3") as ledger:
        first = ledger.coordinator_lock(owner={"run_id": "run-1"})
        second = ledger.coordinator_lock(owner={"run_id": "run-2"})
        with first:
            payload = json.loads(ledger.coordinator_lock_path.read_text(encoding="utf-8"))
            assert payload["run_id"] == "run-1"
            assert payload["pid"] == os.getpid()
            with pytest.raises(LedgerBusy):
                second.acquire()
        with second:
            assert json.loads(ledger.coordinator_lock_path.read_text(encoding="utf-8"))["run_id"] == "run-2"


def test_coordinator_fails_closed_without_advisory_locks(tmp_path: Path, monkeypatch) -> None:
    with RunLedger(tmp_path / "state/run.sqlite3") as ledger:
        monkeypatch.setattr(ledger_module, "fcntl", None)
        with pytest.raises(LedgerError, match="requires filesystem advisory locks"):
            ledger.coordinator_lock().acquire()


def test_stale_process_cannot_advance_run_generation(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    with RunLedger(path) as first:
        created = first.create_run(_identity(tmp_path), run_id="run-1")
        with RunLedger(path) as second:
            advanced = first.transition_run(
                created.run_id,
                "running",
                expected_generation=created.generation,
            )
            with pytest.raises(GenerationConflict):
                second.transition_run(
                    created.run_id,
                    "failed",
                    expected_generation=created.generation,
                )
            assert second.get_run(created.run_id) == advanced


def test_unknown_schema_and_nonregular_ledger_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '99')")
    connection.commit()
    connection.close()

    with pytest.raises(LedgerError, match="unsupported ledger schema"):
        RunLedger(path)

    path.unlink()
    path.mkdir()
    with pytest.raises(LedgerError, match="not a regular file"):
        RunLedger(path)


def test_ledger_rejects_missing_schema_metadata_and_symlink_path(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unexpected(value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerError, match="no schema metadata"):
        RunLedger(path)

    path.unlink()
    target = tmp_path / "target.sqlite3"
    sqlite3.connect(target).close()
    path.symlink_to(target)
    with pytest.raises(LedgerError, match="not a regular file"):
        RunLedger(path)
