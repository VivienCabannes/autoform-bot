from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import autoform_worker.ledger as ledger_module
from autoform_cli.claims import author_claim_key
from autoform_worker.ledger import (
    AttemptRecord,
    GateRecord,
    GenerationConflict,
    InvalidTransition,
    LedgerBusy,
    LedgerError,
    MergeItemRecord,
    RecoverySnapshot,
    RunConfig,
    RunIdentity,
    RunLedger,
)


_ARTICLE_A = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
_ARTICLE_B = "af_bbbbbbbbbbbbbbbbbbbbbbbb"
_ARTICLE_DEPENDENCY = "af_dddddddddddddddddddddddd"


def _config() -> RunConfig:
    return RunConfig(
        repository_id="https://example.test/owner/repo.git",
        target_ref="refs/heads/main",
        remote="https://example.test/owner/repo.git",
        backend="codex",
        start_oid="1" * 40,
        plugin_version="0.5.0",
        toolchain_fingerprint="6" * 64,
        coverage_contract_sha256="3" * 64,
        execution_input_sha256="7" * 64,
        source_artifacts_sha256="4" * 64,
        gate_policy_version="v1",
    )


def _identity(project: Path, config: RunConfig | None = None) -> RunIdentity:
    config = config or _config()
    return RunIdentity(
        repository_id=config.repository_id,
        project_root=str(project.resolve()),
        target_ref=config.target_ref,
        base_oid=config.start_oid,
        runtime_revision="2" * 64,
        coverage_revision=config.coverage_contract_sha256,
        source_artifact_sha256=config.source_artifacts_sha256,
        plugin_revision="5" * 40,
        toolchain_fingerprint=config.toolchain_fingerprint,
        execution_input_sha256=config.execution_input_sha256,
        config_sha256=config.sha256,
    )


def _running_ledger(tmp_path: Path) -> tuple[RunLedger, str]:
    ledger = RunLedger(tmp_path / "state/run.sqlite3", clock_ns=iter(range(100, 10_000)).__next__)
    config = _config()
    run = ledger.create_run(_identity(tmp_path, config), config, run_id="run-1")
    ledger.add_tasks(run.run_id, [(_ARTICLE_A, "statement")])
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
        branch=f"autoform/run-1/{task.article_id}/1",
        base_oid="1" * 40,
        backend="codex",
        claim_key=author_claim_key(task.article_id),
        claim_token={"claim_id": "claim-1", "ref_oid": "8" * 40},
        attempt_id=attempt_id,
    )


def _begin_task(
    ledger: RunLedger,
    run_id: str,
    tmp_path: Path,
    article_id: str,
    phase: str,
    attempt_id: str,
):
    task = ledger.get_task(run_id, article_id, phase)
    return ledger.begin_attempt(
        run_id,
        article_id,
        phase,
        expected_task_generation=task.generation,
        worktree_path=tmp_path / attempt_id,
        branch=f"autoform/{run_id}/{article_id}/{task.attempts + 1}",
        base_oid=ledger.get_run(run_id).current_oid,
        backend="codex",
        claim_key=author_claim_key(article_id),
        claim_token={"claim_id": attempt_id, "ref_oid": "8" * 40},
        attempt_id=attempt_id,
    )


def test_run_identity_and_events_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    config = _config()
    identity = _identity(tmp_path, config)
    with RunLedger(path, clock_ns=lambda: 123) as ledger:
        created = ledger.create_run(identity, config, run_id="run-1")
        assert created.identity == identity
        assert created.identity_sha256 == identity.sha256
        assert created.config == config
        assert created.config_sha256 == config.sha256 == identity.config_sha256
        assert created.current_oid == config.start_oid
        assert created.status == "created"
        assert created.generation == 0
        assert [event.kind for event in ledger.events("run-1")] == ["run.created"]
        assert ledger._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert ledger._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert ledger._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with RunLedger(path) as reopened:
        assert reopened.get_run("run-1").identity_sha256 == identity.sha256
        assert [event.payload for event in reopened.events("run-1")] == [
            {
                "config_sha256": config.sha256,
                "current_oid": config.start_oid,
                "identity_sha256": identity.sha256,
            }
        ]


def test_run_config_is_canonical_validated_and_immutable(tmp_path: Path) -> None:
    config = _config()
    assert config.sha256 == _config().sha256
    assert "project_root" not in config.as_dict()
    with pytest.raises(FrozenInstanceError):
        config.backend = "claude"  # type: ignore[misc]
    with pytest.raises(LedgerError, match="full branch ref"):
        replace(config, target_ref="main")
    with pytest.raises(LedgerError, match="lowercase SHA-256"):
        replace(config, coverage_contract_sha256="not-a-digest")
    with pytest.raises(LedgerError, match="unsupported run config schema"):
        replace(config, schema_version=2)

    path = tmp_path / "state/run.sqlite3"
    with RunLedger(path) as ledger:
        identity = _identity(tmp_path, config)
        with pytest.raises(LedgerError, match="does not match the run config digest"):
            ledger.create_run(replace(identity, config_sha256="a" * 64), config, run_id="bad-run")
        assert ledger.list_runs() == ()

        run = ledger.create_run(identity, config, run_id="run-1")
        assert ledger.list_runs() == (run,)
        stored = ledger._connection.execute(
            "SELECT config_json FROM runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
        assert stored == json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":"))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            ledger._connection.execute(
                "UPDATE runs SET config_sha256 = ? WHERE run_id = ?",
                ("b" * 64, run.run_id),
            )


def test_generation_checks_and_idempotent_stop(tmp_path: Path) -> None:
    with RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(100)).__next__) as ledger:
        config = _config()
        run = ledger.create_run(_identity(tmp_path, config), config, run_id="run-1")
        running = ledger.transition_run("run-1", "running", expected_generation=run.generation)
        assert running.generation == 1
        assert ledger.transition_run("run-1", "running", expected_generation=run.generation) == running
        with pytest.raises(GenerationConflict):
            ledger.transition_run("run-1", "failed", expected_generation=0)
        stopped = ledger.request_stop("run-1", expected_generation=running.generation)
        repeated = ledger.request_stop("run-1", expected_generation=running.generation)
        assert stopped == repeated
        assert stopped.stop_requested
        assert stopped.generation == 2
        with pytest.raises(InvalidTransition):
            ledger.transition_run("run-1", "complete", expected_generation=stopped.generation)


def test_stop_request_rejects_a_stale_controller_generation(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    config = _config()
    with RunLedger(path) as first:
        created = first.create_run(_identity(tmp_path, config), config, run_id="run-1")
        running = first.transition_run("run-1", "running", expected_generation=created.generation)
        with RunLedger(path) as stale:
            blocked = first.transition_run(
                "run-1",
                "blocked",
                expected_generation=running.generation,
                detail="operator review",
            )
            with pytest.raises(GenerationConflict):
                stale.request_stop("run-1", expected_generation=running.generation)
            assert not stale.get_run("run-1").stop_requested

            stopped = stale.request_stop("run-1", expected_generation=blocked.generation)
            before_events = stale.events("run-1")
            assert stale.request_stop("run-1", expected_generation=blocked.generation) == stopped
            assert stale.events("run-1") == before_events


def test_complete_requires_every_persisted_task_to_be_integrated(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        run = ledger.get_run(run_id)
        with pytest.raises(InvalidTransition, match="not integrated"):
            ledger.transition_run(run_id, "complete", expected_generation=run.generation)
        assert ledger.get_run(run_id).status == "running"
    finally:
        ledger.close()


def test_tasks_and_attempt_claims_require_durable_article_ids(tmp_path: Path) -> None:
    config = _config()
    with RunLedger(tmp_path / "run.sqlite3") as ledger:
        run = ledger.create_run(_identity(tmp_path, config), config, run_id="run-1")
        with pytest.raises(LedgerError, match="durable article_id"):
            ledger.add_tasks(run.run_id, [("chapter/path-derived-id", "proof")])
        task = ledger.add_tasks(run.run_id, [(_ARTICLE_A, "proof")])[0]
        assert task.article_id == task.node_id == _ARTICLE_A
        ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
        with pytest.raises(LedgerError, match="not anchored"):
            ledger.begin_attempt(
                run.run_id,
                task.article_id,
                task.phase,
                expected_task_generation=task.generation,
                worktree_path=tmp_path / "attempt",
                branch=f"autoform/run-1/{task.article_id}/1",
                base_oid=run.current_oid,
                backend=config.backend,
                claim_key="author/path-derived-id",
                claim_token={"claim_id": "claim-1", "ref_oid": "8" * 40},
            )
        assert ledger.list_attempts(run.run_id) == ()


def test_resume_clears_stop_and_is_idempotent(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        stop_requested = ledger.request_stop(
            run_id,
            expected_generation=ledger.get_run(run_id).generation,
        )
        ledger.recover_interrupted(run_id)
        stopped = ledger.transition_run(
            run_id,
            "stopped",
            expected_generation=stop_requested.generation,
            detail="operator stop",
        )

        resumed = ledger.resume_run(run_id, expected_generation=stopped.generation)
        repeated = ledger.resume_run(run_id, expected_generation=stopped.generation)
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


def test_recovery_inspection_is_read_only_and_keeps_running_attempt_evidence(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        before_events = ledger.events(run_id)
        snapshot = ledger.inspect_recovery(run_id)

        assert isinstance(snapshot, RecoverySnapshot)
        assert snapshot.run == ledger.get_run(run_id)
        assert snapshot.tasks == ledger.tasks(run_id)
        assert snapshot.attempts == (attempt,)
        assert snapshot.running_attempts == (attempt,)
        assert snapshot.gates == ()
        assert snapshot.merge_items == snapshot.unresolved_merge_items == ()
        assert ledger.inspect_interrupted(run_id) == (attempt,)
        assert ledger.get_attempt(attempt.attempt_id).status == "running"
        assert ledger.events(run_id) == before_events
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("source", "target", "blockers"),
    (
        ("running", "retrying", ()),
        ("candidate", "blocked", (_ARTICLE_DEPENDENCY,)),
        ("queued", "retrying", ()),
    ),
)
def test_active_tasks_have_cas_safe_recovery_transitions(
    tmp_path: Path,
    source: str,
    target: str,
    blockers: tuple[str, ...],
) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        if source in {"candidate", "queued"}:
            ledger.finish_attempt(attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        if source == "queued":
            evidence = ledger.put_artifact("gate", b"passed\n")
            ledger.record_gate(attempt.attempt_id, "lean-build", True, evidence_sha256=evidence)
            ledger.enqueue_candidate(
                attempt.attempt_id,
                required_gates=("lean-build",),
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
                expected_target_oid=ledger.get_run(run_id).current_oid,
                queue_item_id="queue-1",
            )
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        assert task.status == source

        changed = ledger.transition_task(
            run_id,
            task.node_id,
            task.phase,
            target,
            expected_generation=task.generation,
            detail=f"recover {source}",
            blocked_by=blockers,
        )
        assert changed.status == target
        assert changed.detail == f"recover {source}"
        assert changed.blocked_by == blockers
        assert changed.generation == task.generation + 1
        attempt_after = ledger.get_attempt(attempt.attempt_id)
        assert attempt_after.status == ("interrupted" if source == "running" else "candidate")
        assert attempt_after.candidate_oid == (None if source == "running" else "9" * 40)
        if source == "queued":
            assert ledger.get_merge_item("queue-1").status == "pending"
            with pytest.raises(InvalidTransition, match="unresolved merge item"):
                _begin_task(ledger, run_id, tmp_path, _ARTICLE_A, "statement", "attempt-2")
            run_before = ledger.get_run(run_id)
            with pytest.raises(GenerationConflict, match="queued task changed"):
                ledger.mark_integrated(
                    "queue-1",
                    integrated_oid="9" * 40,
                    expected_generation=run_before.generation,
                )
            assert ledger.get_run(run_id) == run_before
            assert ledger.get_merge_item("queue-1").status == "pending"
            assert ledger.get_attempt(attempt.attempt_id).status == "candidate"

        before_events = ledger.events(run_id)
        assert (
            ledger.transition_task(
                run_id,
                task.node_id,
                task.phase,
                target,
                expected_generation=task.generation,
                detail=f"recover {source}",
                blocked_by=blockers,
            )
            == changed
        )
        assert ledger.events(run_id) == before_events
        with pytest.raises(GenerationConflict):
            ledger.transition_task(
                run_id,
                task.node_id,
                task.phase,
                "stopped" if target != "stopped" else "retrying",
                expected_generation=task.generation,
                detail="stale controller",
            )
    finally:
        ledger.close()


def test_stop_request_turns_interrupted_work_into_stopped(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        ledger.request_stop(run_id, expected_generation=ledger.get_run(run_id).generation)

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
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
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
            ledger.add_tasks(run_id, [(_ARTICLE_B, "proof")])
    finally:
        ledger.close()


def test_gate_replay_is_idempotent_only_when_every_input_matches(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        candidate = ledger.finish_attempt(attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        assert (
            ledger.finish_attempt(attempt.attempt_id, "candidate", candidate_oid="9" * 40)
            == candidate
        )
        with pytest.raises(InvalidTransition, match="already candidate"):
            ledger.finish_attempt(attempt.attempt_id, "candidate", candidate_oid="a" * 40)
        first_digest = ledger.put_artifact("gate", b"first evidence\n")
        second_digest = ledger.put_artifact("gate", b"second evidence\n")
        recorded = ledger.record_gate(
            attempt.attempt_id,
            "lean-build",
            True,
            evidence_sha256=first_digest,
            detail="passed",
        )
        assert isinstance(recorded, GateRecord)
        before_events = ledger.events(run_id)
        assert (
            ledger.record_gate(
                attempt.attempt_id,
                "lean-build",
                True,
                evidence_sha256=first_digest,
                detail="passed",
            )
            == recorded
        )
        assert ledger.events(run_id) == before_events

        mismatches = (
            {"passed": False, "evidence_sha256": first_digest, "detail": "passed"},
            {"passed": True, "evidence_sha256": second_digest, "detail": "passed"},
            {"passed": True, "evidence_sha256": first_digest, "detail": "different"},
        )
        for values in mismatches:
            with pytest.raises(LedgerError, match="different evidence"):
                ledger.record_gate(attempt.attempt_id, "lean-build", **values)
        assert ledger.get_gate(attempt.attempt_id, "lean-build") == recorded
        assert ledger.list_gates(attempt.attempt_id) == (recorded,)
    finally:
        ledger.close()


def test_candidate_can_be_queued_and_integrated(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "second/run.sqlite3", clock_ns=iter(range(10_000, 20_000)).__next__)
    try:
        config = _config()
        run = ledger.create_run(_identity(tmp_path, config), config, run_id="run-2")
        second_task = ledger.add_tasks(run.run_id, [(_ARTICLE_B, "proof")])[0]
        ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
        second = ledger.begin_attempt(
            run.run_id,
            second_task.node_id,
            second_task.phase,
            expected_task_generation=second_task.generation,
            worktree_path=tmp_path / "worktree-2",
            branch=f"autoform/run-2/{_ARTICLE_B}/1",
            base_oid="1" * 40,
            backend="codex",
            claim_key=author_claim_key(_ARTICLE_B),
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
            queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_B}",
            expected_target_oid="1" * 40,
            queue_item_id="queue-1",
        )
        assert item == "queue-1"
        assert ledger.tasks(run.run_id)[-1].status == "queued"
        before_integration = ledger.get_run(run.run_id)
        ledger.mark_integrated(
            item,
            integrated_oid="f" * 40,
            expected_generation=before_integration.generation,
        )
        integrated = ledger.tasks(run.run_id)[-1]
        assert integrated.status == "integrated"
        assert integrated.integrated_oid == "f" * 40
        ledger.mark_integrated(
            item,
            integrated_oid="f" * 40,
            expected_generation=before_integration.generation,
        )
        current = ledger.get_run(run.run_id)
        complete = ledger.transition_run(
            run.run_id,
            "complete",
            expected_generation=current.generation,
        )
        assert complete.status == "complete"
    finally:
        ledger.close()


def test_merge_status_recovery_and_integration_are_cas_safe(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(20_000)).__next__)
    try:
        config = _config()
        created = ledger.create_run(_identity(tmp_path, config), config, run_id="run-1")
        ledger.add_tasks(created.run_id, [(_ARTICLE_A, "proof"), (_ARTICLE_B, "proof")])
        running = ledger.transition_run(
            created.run_id,
            "running",
            expected_generation=created.generation,
        )
        for node_id, attempt_id, candidate_oid, queue_item_id in (
            (_ARTICLE_A, "attempt-a", "9" * 40, "queue-a"),
            (_ARTICLE_B, "attempt-b", "a" * 40, "queue-b"),
        ):
            _begin_task(ledger, created.run_id, tmp_path, node_id, "proof", attempt_id)
            ledger.finish_attempt(attempt_id, "candidate", candidate_oid=candidate_oid)
            evidence = ledger.put_artifact("gate", f"{node_id} passed\n".encode())
            ledger.record_gate(attempt_id, "lean-build", True, evidence_sha256=evidence)
            ledger.enqueue_candidate(
                attempt_id,
                required_gates=("lean-build",),
                queue_ref=f"refs/autoform/queue/{queue_item_id}",
                expected_target_oid=running.current_oid,
                queue_item_id=queue_item_id,
            )

        first = ledger.get_merge_item("queue-a")
        assert isinstance(first, MergeItemRecord)
        publishing = ledger.transition_merge_item(
            first.queue_item_id,
            "publishing",
            expected_generation=first.generation,
            detail="remote CAS started",
        )
        before_replay_events = ledger.events(created.run_id)
        assert (
            ledger.transition_merge_item(
                first.queue_item_id,
                "publishing",
                expected_generation=first.generation,
                detail="remote CAS started",
            )
            == publishing
        )
        assert ledger.events(created.run_id) == before_replay_events
        with pytest.raises(GenerationConflict):
            ledger.transition_merge_item(
                first.queue_item_id,
                "queued",
                expected_generation=first.generation,
                detail="stale recovery result",
            )
        recovered = ledger.record_merge_recovery(
            first.queue_item_id,
            "queued",
            expected_generation=publishing.generation,
            detail="remote queue ref verified",
        )

        attempts = ledger.list_attempts(created.run_id)
        assert len(attempts) == 2 and all(isinstance(attempt, AttemptRecord) for attempt in attempts)
        items = ledger.list_merge_items(created.run_id)
        assert [item.queue_item_id for item in items] == ["queue-a", "queue-b"]
        snapshot = ledger.inspect_recovery(created.run_id)
        assert len(snapshot.gates) == 2
        assert snapshot.merge_items == snapshot.unresolved_merge_items == items

        integrated_run = ledger.mark_integrated(
            first.queue_item_id,
            integrated_oid="9" * 40,
            expected_generation=running.generation,
            expected_item_generation=recovered.generation,
        )
        assert integrated_run.current_oid == "9" * 40
        assert integrated_run.generation == running.generation + 1
        assert ledger.get_task(created.run_id, _ARTICLE_A, "proof").status == "integrated"
        integrated_item = ledger.get_merge_item(first.queue_item_id)
        assert integrated_item.status == "integrated"
        assert integrated_item.detail == "remote queue ref verified"

        replay_events = ledger.events(created.run_id)
        assert (
            ledger.mark_integrated(
                first.queue_item_id,
                integrated_oid="9" * 40,
                expected_generation=running.generation,
                expected_item_generation=recovered.generation,
            )
            == integrated_run
        )
        assert ledger.events(created.run_id) == replay_events

        second = ledger.get_merge_item("queue-b")
        with pytest.raises(GenerationConflict):
            ledger.mark_integrated(
                second.queue_item_id,
                integrated_oid="a" * 40,
                expected_generation=running.generation,
            )
        with pytest.raises(GenerationConflict, match="current OID changed"):
            ledger.mark_integrated(
                second.queue_item_id,
                integrated_oid="a" * 40,
                expected_generation=integrated_run.generation,
            )
        assert ledger.get_merge_item(second.queue_item_id) == second
        assert ledger.get_task(created.run_id, _ARTICLE_B, "proof").status == "queued"
        assert ledger.get_attempt("attempt-b").status == "candidate"
    finally:
        ledger.close()


def test_failed_multi_task_insert_rolls_back_rows_and_events(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(100)).__next__)
    config = _config()
    run_id = ledger.create_run(_identity(tmp_path, config), config, run_id="run-1").run_id
    ledger.add_tasks(run_id, [(_ARTICLE_A, "statement")])
    try:
        before = ledger.events(run_id)
        with pytest.raises(LedgerError, match="duplicate task"):
            ledger.add_tasks(run_id, [(_ARTICLE_B, "proof"), (_ARTICLE_A, "statement")])
        assert [(task.node_id, task.phase) for task in ledger.tasks(run_id)] == [
            (_ARTICLE_A, "statement")
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
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
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
        config = _config()
        created = first.create_run(_identity(tmp_path, config), config, run_id="run-1")
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


@pytest.mark.parametrize("schema_version", ("1", "99"))
def test_unknown_schema_and_nonregular_ledger_fail_closed(tmp_path: Path, schema_version: str) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (schema_version,))
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
