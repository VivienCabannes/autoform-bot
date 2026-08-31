from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import autoform_worker.ledger as ledger_module
from autoform_cli.claims import CLAIM_REF_PREFIX, author_claim_key
from autoform_worker.ledger import (
    ArticleClaimToken,
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
        reviewer_backend="claude",
        start_oid="1" * 40,
        plugin_version="0.5.0",
        toolchain_fingerprint="6" * 64,
        coverage_contract_sha256="3" * 64,
        execution_input_sha256="7" * 64,
        source_artifacts_sha256="4" * 64,
        gate_policy_version="v1",
        max_attempts=3,
        max_steers=3,
        timeout_seconds=1800.0,
        claim_ttl_seconds=1500.0,
        heartbeat_interval_seconds=300.0,
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


def _claim_token(article_id: str, *, observed_ref_oid: str = "8" * 40) -> ArticleClaimToken:
    claim_key = author_claim_key(article_id)
    return ArticleClaimToken(
        article_id=article_id,
        claim_key=claim_key,
        claim_ref=CLAIM_REF_PREFIX + claim_key,
        lease_id="e" * 64,
        observed_ref_oid=observed_ref_oid,
        object_format="sha1" if len(observed_ref_oid) == 40 else "sha256",
    )


def _task_plan_digest(tasks: list[tuple[str, str]]) -> str:
    payload = [
        {"article_id": article_id, "phase": phase}
        for article_id, phase in sorted(tasks)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _running_ledger(tmp_path: Path, *, config: RunConfig | None = None) -> tuple[RunLedger, str]:
    ledger = RunLedger(tmp_path / "state/run.sqlite3", clock_ns=iter(range(100, 10_000)).__next__)
    config = config or _config()
    run = ledger.create_run(
        _identity(tmp_path, config),
        config,
        tasks=[(_ARTICLE_A, "statement")],
        run_id="run-1",
    )
    run = ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
    return ledger, run.run_id


def _queue_single_candidate(
    ledger: RunLedger,
    run_id: str,
    tmp_path: Path,
    *,
    candidate_oid: str = "9" * 40,
) -> str:
    attempt = _begin(ledger, run_id, tmp_path)
    _finish(ledger, attempt.attempt_id, "candidate", candidate_oid=candidate_oid)
    evidence = ledger.put_artifact("gate", b"passed\n")
    _record_gate(
        ledger,
        attempt.attempt_id,
        "lean-build",
        True,
        evidence_sha256=evidence,
    )
    return _enqueue(
        ledger,
        attempt.attempt_id,
        required_gates=("lean-build",),
        queue_ref=f"refs/autoform/queue/{run_id}/{_ARTICLE_A}",
        expected_target_oid="1" * 40,
        queue_item_id="queue-1",
    )


def _begin(ledger: RunLedger, run_id: str, tmp_path: Path, *, attempt_id: str = "attempt-1"):
    task = ledger.tasks(run_id)[0]
    return ledger.begin_attempt(
        run_id,
        task.node_id,
        task.phase,
        expected_generation=ledger.get_run(run_id).generation,
        expected_task_generation=task.generation,
        worktree_path=tmp_path / "worktree",
        branch=f"autoform/run-1/{task.article_id}/1",
        base_oid="1" * 40,
        backend="codex",
        claim_key=author_claim_key(task.article_id),
        claim_token=_claim_token(task.article_id),
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
        expected_generation=ledger.get_run(run_id).generation,
        expected_task_generation=task.generation,
        worktree_path=tmp_path / attempt_id,
        branch=f"autoform/{run_id}/{article_id}/{task.attempts + 1}",
        base_oid=ledger.get_run(run_id).current_oid,
        backend="codex",
        claim_key=author_claim_key(article_id),
        claim_token=_claim_token(article_id),
        attempt_id=attempt_id,
    )


def _finish(
    ledger: RunLedger,
    attempt_id: str,
    outcome: str,
    *,
    detail: str = "",
    candidate_oid: str | None = None,
):
    attempt = ledger.get_attempt(attempt_id)
    task = ledger.get_task(attempt.run_id, attempt.article_id, attempt.phase)
    return ledger.finish_attempt(
        attempt_id,
        outcome,
        expected_generation=ledger.get_run(attempt.run_id).generation,
        expected_task_generation=task.generation,
        detail=detail,
        candidate_oid=candidate_oid,
    )


def _record_gate(
    ledger: RunLedger,
    attempt_id: str,
    name: str,
    passed: bool,
    *,
    evidence_sha256: str,
    detail: str = "",
):
    attempt = ledger.get_attempt(attempt_id)
    return ledger.record_gate(
        attempt_id,
        name,
        passed,
        expected_generation=ledger.get_run(attempt.run_id).generation,
        evidence_sha256=evidence_sha256,
        detail=detail,
    )


def _enqueue(
    ledger: RunLedger,
    attempt_id: str,
    *,
    required_gates: tuple[str, ...],
    queue_ref: str,
    expected_target_oid: str,
    queue_item_id: str | None = None,
) -> str:
    attempt = ledger.get_attempt(attempt_id)
    task = ledger.get_task(attempt.run_id, attempt.article_id, attempt.phase)
    assert attempt.candidate_oid is not None
    return ledger.enqueue_candidate(
        attempt_id,
        expected_generation=ledger.get_run(attempt.run_id).generation,
        expected_task_generation=task.generation,
        candidate_oid=attempt.candidate_oid,
        required_gates=required_gates,
        queue_ref=queue_ref,
        expected_target_oid=expected_target_oid,
        queue_item_id=queue_item_id,
    )


def _recover_attempt(
    ledger: RunLedger,
    attempt_id: str,
    outcome: str,
    *,
    detail: str,
    candidate_oid: str | None = None,
):
    attempt = ledger.get_attempt(attempt_id)
    task = ledger.get_task(attempt.run_id, attempt.article_id, attempt.phase)
    return ledger.recover_attempt(
        attempt_id,
        outcome,
        expected_generation=ledger.get_run(attempt.run_id).generation,
        expected_task_generation=task.generation,
        observed_worktree_path=attempt.worktree_path,
        observed_base_oid=attempt.base_oid,
        observed_head_oid=candidate_oid or attempt.base_oid,
        detail=detail,
        candidate_oid=candidate_oid,
    )


def test_run_identity_and_events_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    config = _config()
    identity = _identity(tmp_path, config)
    with RunLedger(path, clock_ns=lambda: 123) as ledger:
        created = ledger.create_run(identity, config, tasks=[], run_id="run-1")
        assert created.identity == identity
        assert created.identity_sha256 == identity.sha256
        assert created.config == config
        assert created.config_sha256 == config.sha256 == identity.config_sha256
        assert created.current_oid == config.start_oid
        assert created.status == "created"
        assert created.generation == 0
        assert created.task_count == 0
        assert created.task_plan_sha256 == _task_plan_digest([])
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
                "task_count": 0,
                "task_plan_sha256": _task_plan_digest([]),
            }
        ]


def test_run_config_is_canonical_validated_and_immutable(tmp_path: Path) -> None:
    config = _config()
    assert config.sha256 == _config().sha256
    assert "project_root" not in config.as_dict()
    assert config.reviewer_backend == "claude"
    with pytest.raises(FrozenInstanceError):
        config.backend = "claude"  # type: ignore[misc]
    with pytest.raises(LedgerError, match="full branch ref"):
        replace(config, target_ref="main")
    with pytest.raises(LedgerError, match="lowercase SHA-256"):
        replace(config, coverage_contract_sha256="not-a-digest")
    with pytest.raises(LedgerError, match="unsupported run config schema"):
        replace(config, schema_version=2)
    with pytest.raises(LedgerError, match="reviewer backend must differ"):
        replace(config, reviewer_backend=config.backend)
    with pytest.raises(LedgerError, match="reviewer backend must differ"):
        replace(config, backend="CODEX", reviewer_backend="codex")
    with pytest.raises(LedgerError, match="reviewer backend"):
        replace(config, reviewer_backend="not a backend")
    canonical = replace(config, backend="CODEX", reviewer_backend="CLAUDE")
    assert canonical.backend == "codex"
    assert canonical.reviewer_backend == "claude"

    path = tmp_path / "state/run.sqlite3"
    with RunLedger(path) as ledger:
        identity = _identity(tmp_path, config)
        with pytest.raises(LedgerError, match="does not match the run config digest"):
            ledger.create_run(
                replace(identity, config_sha256="a" * 64),
                config,
                tasks=[],
                run_id="bad-run",
            )
        assert ledger.list_runs() == ()

        run = ledger.create_run(identity, config, tasks=[], run_id="run-1")
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("max_attempts", True, "max attempts"),
        ("max_attempts", 0, "max attempts"),
        ("max_steers", False, "max steers"),
        ("max_steers", -1, "max steers"),
        ("timeout_seconds", True, "timeout"),
        ("timeout_seconds", float("nan"), "timeout"),
        ("timeout_seconds", float("inf"), "timeout"),
        ("timeout_seconds", 10**400, "timeout"),
        ("timeout_seconds", 0, "timeout"),
        ("claim_ttl_seconds", False, "claim TTL"),
        ("claim_ttl_seconds", float("-inf"), "claim TTL"),
        ("claim_ttl_seconds", 0, "claim TTL"),
        ("heartbeat_interval_seconds", True, "heartbeat interval"),
        ("heartbeat_interval_seconds", float("nan"), "heartbeat interval"),
        ("heartbeat_interval_seconds", 0, "heartbeat interval"),
        ("heartbeat_interval_seconds", 1500, "shorter than claim TTL"),
    ),
)
def test_run_config_rejects_invalid_retry_and_timeout_controls(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(LedgerError, match=message):
        replace(_config(), **{field: value})


def test_generation_checks_and_idempotent_stop(tmp_path: Path) -> None:
    with RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(100)).__next__) as ledger:
        config = _config()
        run = ledger.create_run(_identity(tmp_path, config), config, tasks=[], run_id="run-1")
        running = ledger.transition_run("run-1", "running", expected_generation=run.generation)
        assert running.generation == 1
        with pytest.raises(GenerationConflict):
            ledger.transition_run("run-1", "running", expected_generation=run.generation)
        assert ledger.transition_run(
            "run-1", "running", expected_generation=running.generation
        ) == running
        with pytest.raises(GenerationConflict):
            ledger.transition_run("run-1", "failed", expected_generation=0)
        stopped = ledger.request_stop("run-1", expected_generation=running.generation)
        with pytest.raises(GenerationConflict):
            ledger.request_stop("run-1", expected_generation=running.generation)
        repeated = ledger.request_stop("run-1", expected_generation=stopped.generation)
        assert stopped == repeated
        assert stopped.stop_requested
        assert stopped.generation == 2
        with pytest.raises(InvalidTransition):
            ledger.transition_run("run-1", "complete", expected_generation=stopped.generation)


def test_stop_request_rejects_a_stale_controller_generation(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    config = _config()
    with RunLedger(path) as first:
        created = first.create_run(
            _identity(tmp_path, config), config, tasks=[], run_id="run-1"
        )
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
            with pytest.raises(GenerationConflict):
                stale.request_stop("run-1", expected_generation=blocked.generation)
            assert stale.request_stop("run-1", expected_generation=stopped.generation) == stopped
            assert stale.events("run-1") == before_events


def test_create_run_atomically_persists_and_binds_the_complete_task_plan(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    config = _config()
    with RunLedger(path) as first:
        plan = [(_ARTICLE_B, "proof"), (_ARTICLE_A, "statement")]
        created = first.create_run(
            _identity(tmp_path, config),
            config,
            tasks=plan,
            run_id="run-1",
        )
        assert created.generation == 0
        assert created.task_count == 2
        assert created.task_plan_sha256 == _task_plan_digest(plan)
        assert [(task.article_id, task.phase) for task in first.tasks(created.run_id)] == sorted(plan)
        assert [event.kind for event in first.events(created.run_id)] == [
            "run.created",
            "task.created",
            "task.created",
        ]
        assert not hasattr(first, "add_tasks")
        with RunLedger(path) as stale:
            assert stale.get_run(created.run_id).task_plan_sha256 == created.task_plan_sha256
            assert stale.tasks(created.run_id) == first.tasks(created.run_id)
        with pytest.raises(sqlite3.IntegrityError, match="task plan is immutable"):
            first._connection.execute(
                """
                INSERT INTO tasks(
                    run_id, article_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                ) VALUES (?, ?, 'proof', 'pending', 0, 0, '[]', '', NULL, NULL)
                """,
                (created.run_id, _ARTICLE_DEPENDENCY),
            )
        with pytest.raises(sqlite3.IntegrityError, match="task plan is immutable"):
            first._connection.execute(
                "DELETE FROM tasks WHERE run_id = ? AND article_id = ? AND phase = ?",
                (created.run_id, _ARTICLE_A, "statement"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            first._connection.execute(
                "UPDATE runs SET task_count = 3 WHERE run_id = ?", (created.run_id,)
            )


def test_empty_task_plan_is_explicit_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    config = _config()
    with RunLedger(path) as ledger:
        created = ledger.create_run(
            _identity(tmp_path, config), config, tasks=[], run_id="run-1"
        )
        assert created.task_count == 0
        assert created.task_plan_sha256 == _task_plan_digest([])
        assert ledger.tasks(created.run_id) == ()
        with pytest.raises(sqlite3.IntegrityError, match="task plan is immutable"):
            ledger._connection.execute(
                """
                INSERT INTO tasks(
                    run_id, article_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                ) VALUES (?, ?, 'statement', 'pending', 0, 0, '[]', '', NULL, NULL)
                """,
                (created.run_id, _ARTICLE_A),
            )
    with RunLedger(path) as reopened:
        assert reopened.get_run("run-1") == created
        assert reopened.tasks("run-1") == ()


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
        with pytest.raises(LedgerError, match="durable article_id"):
            ledger.create_run(
                _identity(tmp_path, config),
                config,
                tasks=[("chapter/path-derived-id", "proof")],
                run_id="bad-run",
            )
        assert ledger.list_runs() == ()
        run = ledger.create_run(
            _identity(tmp_path, config),
            config,
            tasks=[(_ARTICLE_A, "proof")],
            run_id="run-1",
        )
        task = ledger.tasks(run.run_id)[0]
        assert task.article_id == task.node_id == _ARTICLE_A
        run = ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
        with pytest.raises(LedgerError, match="not anchored"):
            ledger.begin_attempt(
                run.run_id,
                task.article_id,
                task.phase,
                expected_generation=run.generation,
                expected_task_generation=task.generation,
                worktree_path=tmp_path / "attempt",
                branch=f"autoform/run-1/{task.article_id}/1",
                base_oid=run.current_oid,
                backend=config.backend,
                claim_key="author/path-derived-id",
                claim_token=_claim_token(_ARTICLE_A),
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
        assert ledger.recover_interrupted(run_id) == (attempt.attempt_id,)
        _recover_attempt(ledger, attempt.attempt_id, "stopped", detail="operator stop")
        stopped = ledger.transition_run(
            run_id,
            "stopped",
            expected_generation=stop_requested.generation,
            detail="operator stop",
        )

        resumed = ledger.resume_run(run_id, expected_generation=stopped.generation)
        with pytest.raises(GenerationConflict):
            ledger.resume_run(run_id, expected_generation=stopped.generation)
        repeated = ledger.resume_run(run_id, expected_generation=resumed.generation)
        assert resumed == repeated
        assert resumed.status == "running"
        assert not resumed.stop_requested
        assert ledger.tasks(run_id)[0].status == "retrying"
        assert ledger.get_attempt(attempt.attempt_id).status == "stopped"
    finally:
        ledger.close()


def test_task_attempt_recovery_is_explicit_and_idempotent(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        assert attempt.status == "running"
        assert ledger.tasks(run_id)[0].status == "running"
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        with pytest.raises(GenerationConflict, match="task"):
            ledger.finish_attempt(
                attempt.attempt_id,
                "retrying",
                expected_generation=ledger.get_run(run_id).generation,
                expected_task_generation=task.generation - 1,
                detail="stale result",
            )

        assert ledger.recover_interrupted(run_id) == (attempt.attempt_id,)
        assert ledger.get_attempt(attempt.attempt_id).status == "running"
        assert ledger.tasks(run_id)[0].status == "running"
        result = _recover_attempt(
            ledger,
            attempt.attempt_id,
            "retrying",
            detail="repository inspection found no candidate",
        )
        assert result.status == "retrying"
        assert ledger.tasks(run_id)[0].status == "retrying"
        assert ledger.recover_interrupted(run_id) == ()

        retry = _begin(ledger, run_id, tmp_path, attempt_id="attempt-2")
        retry_task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        run_generation = ledger.get_run(run_id).generation
        result = ledger.finish_attempt(
            retry.attempt_id,
            "retrying",
            expected_generation=run_generation,
            expected_task_generation=retry_task.generation,
            detail="backend unavailable",
        )
        assert result.status == "retrying"
        assert result.finished_ns is not None
        assert ledger.tasks(run_id)[0].attempts == 2
        with pytest.raises(GenerationConflict, match="task"):
            ledger.finish_attempt(
                retry.attempt_id,
                "retrying",
                expected_generation=run_generation,
                expected_task_generation=retry_task.generation,
                detail="backend unavailable",
            )
        assert _finish(ledger, retry.attempt_id, "retrying", detail="backend unavailable") == result
        with pytest.raises(InvalidTransition):
            _finish(ledger, retry.attempt_id, "failed", detail="different result")
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


def test_attempt_claim_token_is_typed_canonical_and_durable(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    path = ledger.path
    try:
        token = _claim_token(_ARTICLE_A)
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        with pytest.raises(LedgerError, match="ArticleClaimToken"):
            ledger.begin_attempt(
                run_id,
                task.article_id,
                task.phase,
                expected_generation=ledger.get_run(run_id).generation,
                expected_task_generation=task.generation,
                worktree_path=tmp_path / "untyped",
                branch=f"autoform/{run_id}/{task.article_id}/1",
                base_oid=ledger.get_run(run_id).current_oid,
                backend="codex",
                claim_key=author_claim_key(task.article_id),
                claim_token=token.as_dict(),  # type: ignore[arg-type]
            )
        with pytest.raises(LedgerError, match="does not match"):
            ledger.begin_attempt(
                run_id,
                task.article_id,
                task.phase,
                expected_generation=ledger.get_run(run_id).generation,
                expected_task_generation=task.generation,
                worktree_path=tmp_path / "mismatched",
                branch=f"autoform/{run_id}/{task.article_id}/1",
                base_oid=ledger.get_run(run_id).current_oid,
                backend="codex",
                claim_key=author_claim_key(task.article_id),
                claim_token=_claim_token(_ARTICLE_B),
            )
        attempt = ledger.begin_attempt(
            run_id,
            task.article_id,
            task.phase,
            expected_generation=ledger.get_run(run_id).generation,
            expected_task_generation=task.generation,
            worktree_path=tmp_path / "typed",
            branch=f"autoform/{run_id}/{task.article_id}/1",
            base_oid=ledger.get_run(run_id).current_oid,
            backend="CODEX",
            claim_key=author_claim_key(task.article_id),
            claim_token=token,
            attempt_id="attempt-typed",
        )
        assert attempt.claim_token == token
        assert attempt.backend == "codex"
        renewed_observation = replace(token, observed_ref_oid="9" * 40)
        assert renewed_observation.lease_id == token.lease_id
        assert renewed_observation.claim_ref == token.claim_ref
        assert renewed_observation.observed_ref_oid != token.observed_ref_oid
        stored = ledger._connection.execute(
            "SELECT claim_token_json FROM attempts WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()[0]
        assert stored == json.dumps(token.as_dict(), sort_keys=True, separators=(",", ":"))
    finally:
        ledger.close()

    with RunLedger(path) as reopened:
        assert reopened.get_attempt("attempt-typed").claim_token == token


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"claim_key": "author/wrong"}, "claim key"),
        ({"claim_ref": "refs/autoform/claims/author/wrong"}, "claim ref"),
        ({"lease_id": "short"}, "lease id"),
        ({"object_format": "sha256"}, "object format"),
        ({"object_format": "sha512"}, "object format"),
        ({"schema_version": 2}, "claim token schema"),
    ),
)
def test_article_claim_token_rejects_incomplete_recovery_identity(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(LedgerError, match=message):
        replace(_claim_token(_ARTICLE_A), **changes)


def test_crash_recovery_records_inspected_candidate_without_destroying_it(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        candidate_oid = "9" * 40
        run = ledger.get_run(run_id)
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")

        with pytest.raises(InvalidTransition, match="inspected worktree HEAD"):
            ledger.recover_attempt(
                attempt.attempt_id,
                "candidate",
                expected_generation=run.generation,
                expected_task_generation=task.generation,
                observed_worktree_path=attempt.worktree_path,
                observed_base_oid=attempt.base_oid,
                observed_head_oid="a" * 40,
                detail="repository inspection found a commit",
                candidate_oid=candidate_oid,
            )
        with pytest.raises(InvalidTransition, match="cannot discard"):
            ledger.recover_attempt(
                attempt.attempt_id,
                "retrying",
                expected_generation=run.generation,
                expected_task_generation=task.generation,
                observed_worktree_path=attempt.worktree_path,
                observed_base_oid=attempt.base_oid,
                observed_head_oid=candidate_oid,
                detail="repository inspection found a commit",
            )
        with pytest.raises(GenerationConflict, match="run"):
            ledger.recover_attempt(
                attempt.attempt_id,
                "candidate",
                expected_generation=run.generation - 1,
                expected_task_generation=task.generation,
                observed_worktree_path=attempt.worktree_path,
                observed_base_oid=attempt.base_oid,
                observed_head_oid=candidate_oid,
                detail="repository inspection found a commit",
                candidate_oid=candidate_oid,
            )
        with pytest.raises(GenerationConflict, match="task"):
            ledger.recover_attempt(
                attempt.attempt_id,
                "candidate",
                expected_generation=run.generation,
                expected_task_generation=task.generation - 1,
                observed_worktree_path=attempt.worktree_path,
                observed_base_oid=attempt.base_oid,
                observed_head_oid=candidate_oid,
                detail="repository inspection found a commit",
                candidate_oid=candidate_oid,
            )
        with pytest.raises(GenerationConflict, match="different attempt worktree"):
            ledger.recover_attempt(
                attempt.attempt_id,
                "candidate",
                expected_generation=run.generation,
                expected_task_generation=task.generation,
                observed_worktree_path=tmp_path / "different-worktree",
                observed_base_oid=attempt.base_oid,
                observed_head_oid=candidate_oid,
                detail="repository inspection found a commit",
                candidate_oid=candidate_oid,
            )
        assert ledger.get_attempt(attempt.attempt_id).status == "running"

        recovered = ledger.recover_attempt(
            attempt.attempt_id,
            "candidate",
            expected_generation=run.generation,
            expected_task_generation=task.generation,
            observed_worktree_path=attempt.worktree_path,
            observed_base_oid=attempt.base_oid,
            observed_head_oid=candidate_oid,
            detail="repository inspection found a commit",
            candidate_oid=candidate_oid,
        )
        assert recovered.status == "candidate"
        assert recovered.candidate_oid == candidate_oid
        event = ledger.events(run_id)[-1]
        assert event.kind == "attempt.recovered"
        assert event.payload["repository_inspection"] == {
            "base_oid": attempt.base_oid,
            "head_oid": candidate_oid,
            "worktree_path": attempt.worktree_path,
        }
        evidence = ledger.put_artifact("gate", b"passed\n")
        _record_gate(
            ledger,
            attempt.attempt_id,
            "lean-build",
            True,
            evidence_sha256=evidence,
        )
        _enqueue(
            ledger,
            attempt.attempt_id,
            required_gates=("lean-build",),
            queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
            expected_target_oid=run.current_oid,
            queue_item_id="recovered-candidate",
        )
        assert ledger.get_merge_item("recovered-candidate").candidate_oid == candidate_oid
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
            _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        if source == "queued":
            evidence = ledger.put_artifact("gate", b"passed\n")
            _record_gate(ledger, attempt.attempt_id, "lean-build", True, evidence_sha256=evidence)
            _enqueue(
                ledger,
                attempt.attempt_id,
                required_gates=("lean-build",),
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
                expected_target_oid=ledger.get_run(run_id).current_oid,
                queue_item_id="queue-1",
            )
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        run_generation = ledger.get_run(run_id).generation
        assert task.status == source

        changed = ledger.transition_task(
            run_id,
            task.node_id,
            task.phase,
            target,
            expected_generation=task.generation,
            expected_run_generation=run_generation,
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
                    expected_item_generation=ledger.get_merge_item("queue-1").generation,
                )
            assert ledger.get_run(run_id) == run_before
            assert ledger.get_merge_item("queue-1").status == "pending"
            assert ledger.get_attempt(attempt.attempt_id).status == "candidate"

        before_events = ledger.events(run_id)
        with pytest.raises(GenerationConflict):
            ledger.transition_task(
                run_id,
                task.node_id,
                task.phase,
                target,
                expected_generation=task.generation,
                expected_run_generation=run_generation,
                detail=f"recover {source}",
                blocked_by=blockers,
            )
        assert (
            ledger.transition_task(
                run_id,
                task.node_id,
                task.phase,
                target,
                expected_generation=changed.generation,
                expected_run_generation=run_generation,
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
                expected_run_generation=run_generation,
                detail="stale controller",
            )
    finally:
        ledger.close()


def test_rejected_candidate_retries_then_fails_at_max_attempts_with_evidence(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        run_generation = ledger.get_run(run_id).generation
        candidate_oids = ("9" * 40, "a" * 40, "b" * 40)
        for number, candidate_oid in enumerate(candidate_oids, start=1):
            attempt = _begin(ledger, run_id, tmp_path, attempt_id=f"attempt-{number}")
            _finish(ledger, attempt.attempt_id, "candidate", candidate_oid=candidate_oid)
            evidence = ledger.put_artifact("review", f"candidate {number} rejected\n".encode())
            _record_gate(
                ledger,
                attempt.attempt_id,
                "independent-review",
                False,
                evidence_sha256=evidence,
            )
            candidate = ledger.get_task(run_id, _ARTICLE_A, "statement")
            if number < 3:
                with pytest.raises(InvalidTransition, match="retry is required"):
                    ledger.transition_task(
                        run_id,
                        candidate.article_id,
                        candidate.phase,
                        "failed",
                        expected_generation=candidate.generation,
                        expected_run_generation=run_generation,
                        detail="candidate rejected",
                    )
                ledger.transition_task(
                    run_id,
                    candidate.article_id,
                    candidate.phase,
                    "retrying",
                    expected_generation=candidate.generation,
                    expected_run_generation=run_generation,
                    detail="candidate rejected",
                )
            else:
                failed = ledger.transition_task(
                    run_id,
                    candidate.article_id,
                    candidate.phase,
                    "retrying",
                    expected_generation=candidate.generation,
                    expected_run_generation=run_generation,
                    detail="candidate rejected after maximum attempts",
                )
                assert ledger.events(run_id)[-1].payload["requested_status"] == "retrying"

        assert failed.status == "failed"
        assert failed.attempts == 3
        assert failed.candidate_oid == candidate_oids[-1]
        attempts = ledger.list_attempts(run_id)
        assert [attempt.status for attempt in attempts] == ["candidate"] * 3
        assert [attempt.candidate_oid for attempt in attempts] == list(candidate_oids)
        assert all(
            not ledger.get_gate(attempt.attempt_id, "independent-review").passed
            for attempt in attempts
        )
        with pytest.raises(InvalidTransition, match="not ready"):
            _begin(ledger, run_id, tmp_path, attempt_id="attempt-4")
    finally:
        ledger.close()


@pytest.mark.parametrize("recovery", (False, True))
def test_retry_outcome_at_attempt_limit_is_forced_to_failed(
    tmp_path: Path,
    recovery: bool,
) -> None:
    ledger, run_id = _running_ledger(tmp_path, config=replace(_config(), max_attempts=1))
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        if recovery:
            finished = _recover_attempt(
                ledger,
                attempt.attempt_id,
                "retrying",
                detail="transient failure at retry ceiling",
            )
        else:
            finished = _finish(
                ledger,
                attempt.attempt_id,
                "retrying",
                detail="transient failure at retry ceiling",
            )

        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        assert finished.status == task.status == "failed"
        assert task.attempts == 1
        event = ledger.events(run_id)[-1]
        assert event.payload["outcome"] == "failed"
        assert event.payload["requested_outcome"] == "retrying"
        with pytest.raises(InvalidTransition, match="not ready"):
            _begin(ledger, run_id, tmp_path, attempt_id="attempt-2")
    finally:
        ledger.close()


def test_task_recovery_at_attempt_limit_is_forced_to_failed(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path, config=replace(_config(), max_attempts=1))
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        failed = ledger.transition_task(
            run_id,
            task.article_id,
            task.phase,
            "retrying",
            expected_generation=task.generation,
            expected_run_generation=ledger.get_run(run_id).generation,
            detail="attempt interrupted at retry ceiling",
        )

        assert failed.status == "failed"
        assert ledger.get_attempt(attempt.attempt_id).status == "interrupted"
        assert ledger.events(run_id)[-1].payload["requested_status"] == "retrying"
        path = ledger.path
        ledger.close()
        ledger = RunLedger(path)
        assert ledger.get_task(run_id, _ARTICLE_A, "statement").status == "failed"
    finally:
        ledger.close()


def test_stop_request_turns_interrupted_work_into_stopped(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        stale_run = ledger.get_run(run_id)
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        stopped = ledger.request_stop(run_id, expected_generation=stale_run.generation)

        with pytest.raises(GenerationConflict, match="run"):
            ledger.transition_task(
                run_id,
                task.article_id,
                task.phase,
                "stopped",
                expected_generation=task.generation,
                expected_run_generation=stale_run.generation,
                detail="stale stop recovery",
            )
        assert ledger.get_run(run_id) == stopped
        assert ledger.get_task(run_id, _ARTICLE_A, "statement") == task

        assert ledger.recover_interrupted(run_id) == (attempt.attempt_id,)
        _recover_attempt(ledger, attempt.attempt_id, "stopped", detail="operator stop")
        assert ledger.tasks(run_id)[0].status == "stopped"
    finally:
        ledger.close()


def test_resume_fails_stopped_task_that_exhausted_attempts(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path, config=replace(_config(), max_attempts=1))
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        stop_requested = ledger.request_stop(
            run_id,
            expected_generation=ledger.get_run(run_id).generation,
        )
        _recover_attempt(ledger, attempt.attempt_id, "stopped", detail="operator stop")
        stopped = ledger.transition_run(
            run_id,
            "stopped",
            expected_generation=stop_requested.generation,
        )
        resumed = ledger.resume_run(run_id, expected_generation=stopped.generation)

        assert resumed.status == "running"
        assert ledger.get_task(run_id, _ARTICLE_A, "statement").status == "failed"
        path = ledger.path
        ledger.close()
        ledger = RunLedger(path)
        assert ledger.get_task(run_id, _ARTICLE_A, "statement").status == "failed"
    finally:
        ledger.close()


def test_stop_request_prevents_merge_state_and_integration_mutations(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        queue_item_id = _queue_single_candidate(ledger, run_id, tmp_path)
        item = ledger.get_merge_item(queue_item_id)
        stopped = ledger.request_stop(
            run_id,
            expected_generation=ledger.get_run(run_id).generation,
        )

        with pytest.raises(InvalidTransition, match="pending stop request"):
            ledger.transition_merge_item(
                queue_item_id,
                "publishing",
                expected_generation=item.generation,
                expected_run_generation=stopped.generation,
                detail="must not publish after stop",
            )
        with pytest.raises(InvalidTransition, match="pending stop request"):
            ledger.mark_integrated(
                queue_item_id,
                integrated_oid="9" * 40,
                expected_generation=stopped.generation,
                expected_item_generation=item.generation,
            )

        assert ledger.get_run(run_id) == stopped
        assert ledger.get_merge_item(queue_item_id) == item
        assert ledger.get_task(run_id, _ARTICLE_A, "statement").status == "queued"
    finally:
        ledger.close()


def test_begin_attempt_rejects_a_stale_parent_run_generation(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        stale_run = ledger.get_run(run_id)
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        blocked = ledger.transition_run(
            run_id,
            "blocked",
            expected_generation=stale_run.generation,
            detail="operator review",
        )
        with pytest.raises(GenerationConflict, match="run"):
            ledger.begin_attempt(
                run_id,
                task.article_id,
                task.phase,
                expected_generation=stale_run.generation,
                expected_task_generation=task.generation,
                worktree_path=tmp_path / "stale-attempt",
                branch=f"autoform/{run_id}/{task.article_id}/1",
                base_oid=blocked.current_oid,
                backend="codex",
                claim_key=author_claim_key(task.article_id),
                claim_token=_claim_token(task.article_id),
            )
        assert ledger.list_attempts(run_id) == ()
    finally:
        ledger.close()


@pytest.mark.parametrize("operation", ("finish", "gate", "enqueue"))
def test_stop_request_wins_races_with_attempt_mutations(tmp_path: Path, operation: str) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        evidence = ""
        if operation in {"gate", "enqueue"}:
            _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="9" * 40)
            evidence = ledger.put_artifact("gate", b"passed\n")
        if operation == "enqueue":
            _record_gate(
                ledger,
                attempt.attempt_id,
                "lean-build",
                True,
                evidence_sha256=evidence,
            )
        stale_run = ledger.get_run(run_id)
        task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        with RunLedger(ledger.path) as stopper:
            stopped = stopper.request_stop(run_id, expected_generation=stale_run.generation)

        def mutate(expected_generation: int) -> object:
            if operation == "finish":
                return ledger.finish_attempt(
                    attempt.attempt_id,
                    "retrying",
                    expected_generation=expected_generation,
                    expected_task_generation=task.generation,
                    detail="backend unavailable",
                )
            if operation == "gate":
                return ledger.record_gate(
                    attempt.attempt_id,
                    "lean-build",
                    True,
                    expected_generation=expected_generation,
                    evidence_sha256=evidence,
                )
            return ledger.enqueue_candidate(
                attempt.attempt_id,
                expected_generation=expected_generation,
                expected_task_generation=task.generation,
                candidate_oid="9" * 40,
                required_gates=("lean-build",),
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
                expected_target_oid=stale_run.current_oid,
            )

        with pytest.raises(GenerationConflict):
            mutate(stale_run.generation)
        with pytest.raises(InvalidTransition, match="stop|accepting"):
            mutate(stopped.generation)
        assert ledger.list_merge_items(run_id) == ()
        if operation == "finish":
            assert ledger.get_attempt(attempt.attempt_id).status == "running"
            assert ledger.get_task(run_id, _ARTICLE_A, "statement").status == "running"
        elif operation == "gate":
            assert ledger.list_gates(attempt.attempt_id) == ()
        else:
            assert len(ledger.list_gates(attempt.attempt_id)) == 1
            assert ledger.get_task(run_id, _ARTICLE_A, "statement").status == "candidate"
    finally:
        ledger.close()


def test_candidate_requires_all_gates_before_queue_and_integration(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        candidate = _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        assert candidate.candidate_oid == "9" * 40
        build_evidence = ledger.put_artifact("gate", b"lean build passed\n")
        review_failure = ledger.put_artifact("gate", b"source review failed\n")
        _record_gate(ledger, candidate.attempt_id, "lean-build", True, evidence_sha256=build_evidence)
        _record_gate(
            ledger,
            candidate.attempt_id,
            "source-review",
            False,
            evidence_sha256=review_failure,
        )

        with pytest.raises(InvalidTransition, match="source-review"):
            _enqueue(
                ledger,
                candidate.attempt_id,
                required_gates=("lean-build", "source-review"),
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
                expected_target_oid="1" * 40,
            )
        with pytest.raises(LedgerError, match="already recorded"):
            _record_gate(
                ledger,
                candidate.attempt_id,
                "source-review",
                True,
                evidence_sha256=ledger.put_artifact("gate", b"source review passed\n"),
            )

        assert ledger.get_run(run_id).task_count == 1
    finally:
        ledger.close()


def test_gate_replay_is_idempotent_only_when_every_input_matches(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        attempt = _begin(ledger, run_id, tmp_path)
        candidate = _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        assert (
            _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="9" * 40)
            == candidate
        )
        with pytest.raises(InvalidTransition, match="already candidate"):
            _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="a" * 40)
        first_digest = ledger.put_artifact("gate", b"first evidence\n")
        second_digest = ledger.put_artifact("gate", b"second evidence\n")
        recorded = _record_gate(
            ledger,
            attempt.attempt_id,
            "lean-build",
            True,
            evidence_sha256=first_digest,
            detail="passed",
        )
        assert isinstance(recorded, GateRecord)
        before_events = ledger.events(run_id)
        assert (
            _record_gate(
                ledger,
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
                _record_gate(ledger, attempt.attempt_id, "lean-build", **values)
        assert ledger.get_gate(attempt.attempt_id, "lean-build") == recorded
        assert ledger.list_gates(attempt.attempt_id) == (recorded,)
    finally:
        ledger.close()


def test_old_attempt_cannot_enqueue_after_a_retry_rebinds_the_task(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    try:
        old_attempt = _begin(ledger, run_id, tmp_path, attempt_id="attempt-old")
        _finish(ledger, old_attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        evidence = ledger.put_artifact("gate", b"passed\n")
        _record_gate(
            ledger,
            old_attempt.attempt_id,
            "lean-build",
            True,
            evidence_sha256=evidence,
        )
        old_candidate_task = ledger.get_task(run_id, _ARTICLE_A, "statement")
        ledger.transition_task(
            run_id,
            _ARTICLE_A,
            "statement",
            "retrying",
            expected_generation=old_candidate_task.generation,
            expected_run_generation=ledger.get_run(run_id).generation,
            detail="retry after review",
        )
        new_attempt = _begin(ledger, run_id, tmp_path, attempt_id="attempt-new")
        _finish(ledger, new_attempt.attempt_id, "candidate", candidate_oid="a" * 40)
        current_task = ledger.get_task(run_id, _ARTICLE_A, "statement")

        with pytest.raises(GenerationConflict, match="expected"):
            ledger.enqueue_candidate(
                old_attempt.attempt_id,
                expected_generation=ledger.get_run(run_id).generation,
                expected_task_generation=current_task.generation - 1,
                candidate_oid="9" * 40,
                required_gates=("lean-build",),
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
                expected_target_oid=ledger.get_run(run_id).current_oid,
                queue_item_id="stale-generation",
            )
        with pytest.raises(GenerationConflict, match="no longer names candidate"):
            ledger.enqueue_candidate(
                old_attempt.attempt_id,
                expected_generation=ledger.get_run(run_id).generation,
                expected_task_generation=current_task.generation,
                candidate_oid="9" * 40,
                required_gates=("lean-build",),
                queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_A}",
                expected_target_oid=ledger.get_run(run_id).current_oid,
                queue_item_id="stale-queue",
            )
        assert ledger.list_merge_items(run_id) == ()
        assert ledger.get_task(run_id, _ARTICLE_A, "statement").candidate_oid == "a" * 40
    finally:
        ledger.close()


def test_candidate_can_be_queued_and_integrated(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "second/run.sqlite3", clock_ns=iter(range(10_000, 20_000)).__next__)
    try:
        config = _config()
        run = ledger.create_run(
            _identity(tmp_path, config),
            config,
            tasks=[(_ARTICLE_B, "proof")],
            run_id="run-2",
        )
        second_task = ledger.tasks(run.run_id)[0]
        ledger.transition_run(run.run_id, "running", expected_generation=run.generation)
        second = ledger.begin_attempt(
            run.run_id,
            second_task.node_id,
            second_task.phase,
            expected_generation=ledger.get_run(run.run_id).generation,
            expected_task_generation=second_task.generation,
            worktree_path=tmp_path / "worktree-2",
            branch=f"autoform/run-2/{_ARTICLE_B}/1",
            base_oid="1" * 40,
            backend="codex",
            claim_key=author_claim_key(_ARTICLE_B),
            claim_token=_claim_token(_ARTICLE_B),
            attempt_id="attempt-2",
        )
        _finish(ledger, second.attempt_id, "candidate", candidate_oid="c" * 40)
        build_evidence = ledger.put_artifact("gate", b"lean build passed\n")
        review_evidence = ledger.put_artifact("gate", b"source review passed\n")
        _record_gate(ledger, second.attempt_id, "lean-build", True, evidence_sha256=build_evidence)
        _record_gate(ledger, second.attempt_id, "source-review", True, evidence_sha256=review_evidence)
        item = _enqueue(
            ledger,
            second.attempt_id,
            required_gates=("source-review", "lean-build"),
            queue_ref=f"refs/autoform/queue/run-1/{_ARTICLE_B}",
            expected_target_oid="1" * 40,
            queue_item_id="queue-1",
        )
        assert item == "queue-1"
        assert ledger.tasks(run.run_id)[-1].status == "queued"
        before_integration = ledger.get_run(run.run_id)
        queued_item = ledger.get_merge_item(item)
        with pytest.raises(InvalidTransition, match="must equal the queued candidate"):
            ledger.mark_integrated(
                item,
                integrated_oid="d" * 40,
                expected_generation=before_integration.generation,
                expected_item_generation=queued_item.generation,
            )
        assert ledger.get_run(run.run_id) == before_integration
        assert ledger.get_merge_item(item) == queued_item

        uncertain = ledger.transition_merge_item(
            item,
            "uncertain",
            expected_generation=queued_item.generation,
            expected_run_generation=before_integration.generation,
            detail="publication result was not observed",
        )
        with pytest.raises(GenerationConflict, match="merge item"):
            ledger.mark_integrated(
                item,
                integrated_oid="c" * 40,
                expected_generation=before_integration.generation,
                expected_item_generation=queued_item.generation,
            )
        ready = ledger.record_merge_recovery(
            item,
            "queued",
            expected_generation=uncertain.generation,
            expected_run_generation=before_integration.generation,
            detail="remote ref still names the candidate",
        )
        ledger.mark_integrated(
            item,
            integrated_oid="c" * 40,
            expected_generation=before_integration.generation,
            expected_item_generation=ready.generation,
        )
        integrated = ledger.tasks(run.run_id)[-1]
        assert integrated.status == "integrated"
        assert integrated.integrated_oid == "c" * 40
        integrated_run = ledger.get_run(run.run_id)
        integrated_item = ledger.get_merge_item(item)
        ledger.mark_integrated(
            item,
            integrated_oid="c" * 40,
            expected_generation=integrated_run.generation,
            expected_item_generation=integrated_item.generation,
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
        created = ledger.create_run(
            _identity(tmp_path, config),
            config,
            tasks=[(_ARTICLE_A, "proof"), (_ARTICLE_B, "proof")],
            run_id="run-1",
        )
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
            _finish(ledger, attempt_id, "candidate", candidate_oid=candidate_oid)
            evidence = ledger.put_artifact("gate", f"{node_id} passed\n".encode())
            _record_gate(ledger, attempt_id, "lean-build", True, evidence_sha256=evidence)
            _enqueue(
                ledger,
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
            expected_run_generation=running.generation,
            detail="remote CAS started",
        )
        before_replay_events = ledger.events(created.run_id)
        with pytest.raises(GenerationConflict):
            ledger.transition_merge_item(
                first.queue_item_id,
                "publishing",
                expected_generation=first.generation,
                expected_run_generation=running.generation,
                detail="remote CAS started",
            )
        assert (
            ledger.transition_merge_item(
                first.queue_item_id,
                "publishing",
                expected_generation=publishing.generation,
                expected_run_generation=running.generation,
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
                expected_run_generation=running.generation,
                detail="stale recovery result",
            )
        recovered = ledger.record_merge_recovery(
            first.queue_item_id,
            "queued",
            expected_generation=publishing.generation,
            expected_run_generation=running.generation,
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
        integrated_item = ledger.get_merge_item(first.queue_item_id)
        with pytest.raises(GenerationConflict):
            ledger.mark_integrated(
                first.queue_item_id,
                integrated_oid="9" * 40,
                expected_generation=running.generation,
                expected_item_generation=recovered.generation,
            )
        assert (
            ledger.mark_integrated(
                first.queue_item_id,
                integrated_oid="9" * 40,
                expected_generation=integrated_run.generation,
                expected_item_generation=integrated_item.generation,
            )
            == integrated_run
        )
        assert ledger.events(created.run_id) == replay_events

        second = ledger.get_merge_item("queue-b")
        with pytest.raises(GenerationConflict, match="run"):
            ledger.transition_merge_item(
                second.queue_item_id,
                "publishing",
                expected_generation=second.generation,
                expected_run_generation=running.generation,
                detail="stale parent generation",
            )
        with pytest.raises(GenerationConflict):
            ledger.mark_integrated(
                second.queue_item_id,
                integrated_oid="a" * 40,
                expected_generation=running.generation,
                expected_item_generation=second.generation,
            )
        with pytest.raises(GenerationConflict, match="current OID changed"):
            ledger.mark_integrated(
                second.queue_item_id,
                integrated_oid="a" * 40,
                expected_generation=integrated_run.generation,
                expected_item_generation=second.generation,
            )
        assert ledger.get_merge_item(second.queue_item_id) == second
        assert ledger.get_task(created.run_id, _ARTICLE_B, "proof").status == "queued"
        assert ledger.get_attempt("attempt-b").status == "candidate"
    finally:
        ledger.close()


def test_invalid_task_plan_does_not_create_partial_rows_or_events(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "run.sqlite3", clock_ns=iter(range(100)).__next__)
    config = _config()
    try:
        with pytest.raises(LedgerError, match="duplicate task"):
            ledger.create_run(
                _identity(tmp_path, config),
                config,
                tasks=[(_ARTICLE_A, "statement"), (_ARTICLE_A, "statement")],
                run_id="run-1",
            )
        assert ledger.list_runs() == ()
        assert ledger._connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert ledger._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
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
        _finish(ledger, attempt.attempt_id, "candidate", candidate_oid="9" * 40)
        with pytest.raises(LedgerError, match="not in the artifact store"):
            _record_gate(
                ledger,
                attempt.attempt_id,
                "lean-build",
                True,
                evidence_sha256="a" * 64,
            )

        digest = ledger.put_artifact("gate", b"passed\n")
        _record_gate(
            ledger,
            attempt.attempt_id,
            "lean-build",
            True,
            evidence_sha256=digest,
        )
        row = ledger._connection.execute(
            "SELECT relative_path FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()
        (ledger.path.parent / row[0]).write_bytes(b"failed\n")
        with pytest.raises(LedgerError, match="artifact (size|content) changed"):
            _enqueue(
                ledger,
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


def test_artifact_store_rejects_symlinked_ancestor_on_write_and_read(tmp_path: Path) -> None:
    write_state = tmp_path / "write/state"
    with RunLedger(write_state / "run.sqlite3") as ledger:
        outside = tmp_path / "outside-write"
        outside.mkdir()
        (write_state / "artifacts").symlink_to(outside, target_is_directory=True)
        with pytest.raises(LedgerError, match="missing or unsafe"):
            ledger.put_artifact("gate", b"must not escape\n")
        assert tuple(outside.iterdir()) == ()

    read_state = tmp_path / "read/state"
    with RunLedger(read_state / "run.sqlite3") as ledger:
        digest = ledger.put_artifact("gate", b"evidence\n")
        artifact_root = read_state / "artifacts"
        moved_root = tmp_path / "outside-read"
        artifact_root.rename(moved_root)
        artifact_root.symlink_to(moved_root, target_is_directory=True)
        with pytest.raises(LedgerError, match="missing or unsafe"):
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
        created = first.create_run(
            _identity(tmp_path, config), config, tasks=[], run_id="run-1"
        )
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


def test_one_hundred_concurrent_constructors_initialize_one_fresh_ledger(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    barrier = threading.Barrier(100)

    def construct(_: int) -> tuple[str, str]:
        barrier.wait()
        with RunLedger(path) as ledger:
            version = ledger._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            mode = ledger._connection.execute("PRAGMA journal_mode").fetchone()[0]
            return version, mode

    with ThreadPoolExecutor(max_workers=100) as executor:
        results = tuple(executor.map(construct, range(100)))

    assert results == ((str(ledger_module.LEDGER_SCHEMA_VERSION), "wal"),) * 100
    with RunLedger(path) as reopened:
        assert reopened.list_runs() == ()


@pytest.mark.parametrize(
    ("schema_version", "message"),
    (
        ("1", "schema 1 objects"),
        ("2", "schema 2 objects"),
        ("99", "unsupported ledger schema"),
    ),
)
def test_unknown_schema_and_nonregular_ledger_fail_closed(
    tmp_path: Path,
    schema_version: str,
    message: str,
) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (schema_version,))
    connection.commit()
    connection.close()

    with pytest.raises(LedgerError, match=message):
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


def test_ledger_rejects_hardlinks_and_unsafe_sqlite_sidecars_before_wal(tmp_path: Path) -> None:
    original = tmp_path / "hardlink/original/run.sqlite3"
    with RunLedger(original):
        pass
    alias = tmp_path / "hardlink/alias/run.sqlite3"
    alias.parent.mkdir()
    os.link(original, alias)
    with pytest.raises(LedgerError, match="ledger path is hard-linked"):
        RunLedger(alias)

    for suffix, link_kind in (("-wal", "symlink"), ("-shm", "hardlink")):
        path = tmp_path / f"sidecar-{link_kind}/run.sqlite3"
        with RunLedger(path):
            pass
        target = tmp_path / f"{link_kind}-target"
        target.write_bytes(b"unsafe sqlite state")
        sidecar = Path(f"{path}{suffix}")
        if link_kind == "symlink":
            sidecar.symlink_to(target)
            message = "not a regular file"
        else:
            os.link(target, sidecar)
            message = "hard-linked"
        with pytest.raises(LedgerError, match=message):
            RunLedger(path)


def test_v1_ledger_is_migrated_without_permitting_an_unsafe_resume(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    config = _config()
    legacy_identity = _identity(tmp_path, config).as_dict()
    legacy_identity.pop("config_sha256")
    identity_json = json.dumps(legacy_identity, sort_keys=True, separators=(",", ":"))
    connection = sqlite3.connect(path)
    ledger_module._execute_schema(connection, ledger_module._SCHEMA_V1)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
    connection.execute(
        """
        INSERT INTO runs VALUES (?, ?, ?, 'running', 2, 0, '', 1, 2)
        """,
        ("run-1", identity_json, hashlib.sha256(identity_json.encode()).hexdigest()),
    )
    connection.execute(
        """
        INSERT INTO tasks VALUES (?, ?, 'statement', 'running', 1, 1, '[]', '', NULL, NULL)
        """,
        ("run-1", _ARTICLE_A),
    )
    connection.execute(
        """
        INSERT INTO attempts VALUES (
            'attempt-1', ?, ?, 'statement', 1, 'running', ?, ?, ?, 'codex', ?, ?, NULL, '', 3, NULL
        )
        """,
        (
            "run-1",
            _ARTICLE_A,
            str((tmp_path / "worktree").resolve()),
            f"autoform/run-1/{_ARTICLE_A}/1",
            config.start_oid,
            author_claim_key(_ARTICLE_A),
            json.dumps(_claim_token(_ARTICLE_A).as_dict(), sort_keys=True, separators=(",", ":")),
        ),
    )
    connection.execute(
        "INSERT INTO events(run_id, kind, payload_json, created_ns) VALUES (?, ?, ?, ?)",
        ("run-1", "run.created", "{}", 1),
    )
    connection.commit()
    connection.close()

    with RunLedger(path) as ledger:
        migrated = ledger.get_run("run-1")
        assert migrated.status == "failed"
        assert "controller settings were not persisted" in migrated.detail
        assert migrated.config.backend == "codex"
        assert migrated.config.reviewer_backend == "claude"
        assert migrated.config.max_attempts == 3
        assert migrated.config.timeout_seconds == 1800.0
        assert migrated.task_count == 1
        assert migrated.task_plan_sha256 == _task_plan_digest([(_ARTICLE_A, "statement")])
        assert ledger.get_task("run-1", _ARTICLE_A, "statement").article_id == _ARTICLE_A
        assert ledger.get_attempt("attempt-1").claim_token == _claim_token(_ARTICLE_A)
        migration_event = ledger.events("run-1")[-1]
        assert migration_event.kind == "ledger.migrated"
        assert migration_event.payload["status"] == "failed"
        assert ledger._connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "3"
        assert "article_id" in {
            row["name"] for row in ledger._connection.execute("PRAGMA table_info(tasks)")
        }

    with RunLedger(path) as reopened:
        assert reopened.get_run("run-1").config == migrated.config


def test_v2_ledger_migration_seals_even_a_zero_row_task_plan(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    config = _config()
    identity = _identity(tmp_path, config)
    legacy_config = config.as_dict()
    legacy_config["backend"] = "CODEX"
    legacy_config["reviewer_backend"] = "CLAUDE"
    config_json = json.dumps(legacy_config, sort_keys=True, separators=(",", ":"))
    legacy_config_sha256 = hashlib.sha256(config_json.encode()).hexdigest()
    legacy_identity = identity.as_dict()
    legacy_identity["config_sha256"] = legacy_config_sha256
    identity_json = json.dumps(legacy_identity, sort_keys=True, separators=(",", ":"))
    connection = sqlite3.connect(path)
    ledger_module._execute_schema(connection, ledger_module._SCHEMA_V2)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
    connection.execute(
        """
        INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'created', 0, ?, 0, '', 1, 1)
        """,
        (
            "run-1",
            identity_json,
            hashlib.sha256(identity_json.encode()).hexdigest(),
            config_json,
            legacy_config_sha256,
            config.start_oid,
        ),
    )
    connection.commit()
    connection.close()

    with RunLedger(path) as ledger:
        migrated = ledger.get_run("run-1")
        assert migrated.status == "failed"
        assert "complete task plan was not atomically persisted" in migrated.detail
        assert migrated.config.backend == "codex"
        assert migrated.config.reviewer_backend == "claude"
        assert migrated.task_count == 0
        assert migrated.task_plan_sha256 == _task_plan_digest([])
        assert ledger.tasks("run-1") == ()
        assert ledger.events("run-1")[-1].payload["from_schema"] == 2
        with pytest.raises(sqlite3.IntegrityError, match="task plan is immutable"):
            ledger._connection.execute(
                """
                INSERT INTO tasks(
                    run_id, article_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                ) VALUES ('run-1', ?, 'proof', 'pending', 0, 0, '[]', '', NULL, NULL)
                """,
                (_ARTICLE_A,),
            )


def test_v2_migration_rejects_complete_run_with_pending_task_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    config = _config()
    identity = _identity(tmp_path, config)
    config_json = json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":"))
    identity_json = json.dumps(identity.as_dict(), sort_keys=True, separators=(",", ":"))
    connection = sqlite3.connect(path)
    ledger_module._execute_schema(connection, ledger_module._SCHEMA_V2)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
    connection.execute(
        """
        INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'complete', 1, ?, 0, '', 1, 2)
        """,
        (
            "run-1",
            identity_json,
            hashlib.sha256(identity_json.encode()).hexdigest(),
            config_json,
            config.sha256,
            config.start_oid,
        ),
    )
    connection.execute(
        """
        INSERT INTO tasks VALUES (?, ?, 'statement', 'pending', 0, 0, '[]', '', NULL, NULL)
        """,
        ("run-1", _ARTICLE_A),
    )
    connection.commit()
    connection.close()

    with pytest.raises(LedgerError, match="complete run contains unintegrated tasks"):
        RunLedger(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"
    finally:
        connection.close()


def test_v2_migration_preserves_a_provable_integrated_chain(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    config = _config()
    identity = _identity(tmp_path, config)
    config_json = json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":"))
    identity_json = json.dumps(identity.as_dict(), sort_keys=True, separators=(",", ":"))
    candidate_oid = "9" * 40
    claim_json = json.dumps(
        _claim_token(_ARTICLE_A).as_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = sqlite3.connect(path)
    ledger_module._execute_schema(connection, ledger_module._SCHEMA_V2)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'complete', 2, ?, 0, '', 1, 5)",
        (
            "run-1",
            identity_json,
            hashlib.sha256(identity_json.encode()).hexdigest(),
            config_json,
            config.sha256,
            candidate_oid,
        ),
    )
    connection.execute(
        "INSERT INTO tasks VALUES (?, ?, 'statement', 'integrated', 1, 3, '[]', '', ?, ?)",
        ("run-1", _ARTICLE_A, candidate_oid, candidate_oid),
    )
    connection.execute(
        """
        INSERT INTO attempts VALUES (
            'attempt-1', ?, ?, 'statement', 1, 'integrated', ?, ?, ?, 'codex', ?, ?, ?, '', 2, 4
        )
        """,
        (
            "run-1",
            _ARTICLE_A,
            str((tmp_path / "worktree").resolve()),
            f"autoform/run-1/{_ARTICLE_A}/1",
            config.start_oid,
            author_claim_key(_ARTICLE_A),
            claim_json,
            candidate_oid,
        ),
    )
    connection.execute(
        """
        INSERT INTO merge_items VALUES (
            'queue-1', 'run-1', 'attempt-1', ?, ?, ?, 'integrated', 1, ?, '', 3, 4
        )
        """,
        (
            f"refs/autoform/queue/run-1/{_ARTICLE_A}",
            config.start_oid,
            candidate_oid,
            candidate_oid,
        ),
    )
    integration_payload = json.dumps(
        {
            "expected_target_oid": config.start_oid,
            "integrated_oid": candidate_oid,
            "queue_item_id": "queue-1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "INSERT INTO events(run_id, kind, payload_json, created_ns) VALUES (?, ?, ?, ?)",
        ("run-1", "candidate.integrated", integration_payload, 4),
    )
    connection.commit()
    connection.close()

    with RunLedger(path) as ledger:
        assert ledger.get_run("run-1").current_oid == candidate_oid
        assert ledger.get_task("run-1", _ARTICLE_A, "statement").status == "integrated"
        assert ledger.get_merge_item("queue-1").status == "integrated"


def test_v2_migration_closes_an_exhausted_retry_state(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    config = replace(_config(), max_attempts=1)
    identity = _identity(tmp_path, config)
    config_json = json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":"))
    identity_json = json.dumps(identity.as_dict(), sort_keys=True, separators=(",", ":"))
    claim_json = json.dumps(
        _claim_token(_ARTICLE_A).as_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = sqlite3.connect(path)
    ledger_module._execute_schema(connection, ledger_module._SCHEMA_V2)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'running', 1, ?, 0, '', 1, 4)",
        (
            "run-1",
            identity_json,
            hashlib.sha256(identity_json.encode()).hexdigest(),
            config_json,
            config.sha256,
            config.start_oid,
        ),
    )
    connection.execute(
        "INSERT INTO tasks VALUES (?, ?, 'statement', 'retrying', 1, 2, '[]', 'retry', NULL, NULL)",
        ("run-1", _ARTICLE_A),
    )
    connection.execute(
        """
        INSERT INTO attempts VALUES (
            'attempt-1', ?, ?, 'statement', 1, 'retrying', ?, ?, ?, 'codex', ?, ?, NULL,
            'retry', 2, 3
        )
        """,
        (
            "run-1",
            _ARTICLE_A,
            str((tmp_path / "worktree").resolve()),
            f"autoform/run-1/{_ARTICLE_A}/1",
            config.start_oid,
            author_claim_key(_ARTICLE_A),
            claim_json,
        ),
    )
    connection.commit()
    connection.close()

    with RunLedger(path) as ledger:
        assert ledger.get_run("run-1").status == "failed"
        assert ledger.get_task("run-1", _ARTICLE_A, "statement").status == "failed"
        assert ledger.get_attempt("attempt-1").status == "failed"


def test_malformed_v2_config_is_wrapped_and_migration_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "state/run.sqlite3"
    path.parent.mkdir()
    config = _config()
    malformed_config = config.as_dict()
    malformed_config["unexpected"] = True
    config_json = json.dumps(malformed_config, sort_keys=True, separators=(",", ":"))
    config_sha256 = hashlib.sha256(config_json.encode()).hexdigest()
    identity_values = _identity(tmp_path, config).as_dict()
    identity_values["config_sha256"] = config_sha256
    identity_json = json.dumps(identity_values, sort_keys=True, separators=(",", ":"))
    connection = sqlite3.connect(path)
    ledger_module._execute_schema(connection, ledger_module._SCHEMA_V2)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '2')")
    connection.execute(
        """
        INSERT INTO runs VALUES (?, ?, ?, ?, ?, 'created', 0, ?, 0, '', 1, 1)
        """,
        (
            "run-1",
            identity_json,
            hashlib.sha256(identity_json.encode()).hexdigest(),
            config_json,
            config_sha256,
            config.start_oid,
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(LedgerError, match="v2 run identity or config is invalid"):
        RunLedger(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-trigger",
        "missing-column",
        "missing-index",
        "missing-foreign-key",
    ),
)
def test_full_v3_schema_shape_is_verified(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / f"{mutation}.sqlite3"
    connection = sqlite3.connect(path)
    script = ledger_module._SCHEMA
    if mutation == "missing-trigger":
        script = script.replace(
            "CREATE TRIGGER IF NOT EXISTS tasks_plan_no_delete BEFORE DELETE ON tasks\n"
            "BEGIN SELECT RAISE(ABORT, 'run task plan is immutable'); END;",
            "",
        )
    elif mutation == "missing-column":
        script = script.replace("    detail TEXT NOT NULL,\n    created_ns", "    created_ns", 1)
    elif mutation == "missing-index":
        script = script.replace("    UNIQUE(run_id, article_id, phase, number),\n", "")
    else:
        script = script.replace(
            "    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,",
            "    run_id TEXT NOT NULL,",
            1,
        )
    ledger_module._execute_schema(connection, script)
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '3')")
    connection.commit()
    connection.close()

    with pytest.raises(LedgerError, match="objects, columns, indexes, or foreign keys"):
        RunLedger(path)


def test_reopen_rejects_complete_run_with_unintegrated_tasks(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    path = ledger.path
    ledger._connection.execute(
        "UPDATE runs SET status = 'complete' WHERE run_id = ?",
        (run_id,),
    )
    ledger.close()

    with pytest.raises(LedgerError, match="complete run contains unintegrated tasks"):
        RunLedger(path)


def test_reopen_rejects_current_oid_outside_integrated_event_chain(tmp_path: Path) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    queue_item_id = _queue_single_candidate(ledger, run_id, tmp_path)
    item = ledger.get_merge_item(queue_item_id)
    ledger.mark_integrated(
        queue_item_id,
        integrated_oid="9" * 40,
        expected_generation=ledger.get_run(run_id).generation,
        expected_item_generation=item.generation,
    )
    path = ledger.path
    ledger.close()

    with RunLedger(path) as reopened:
        assert reopened.get_run(run_id).current_oid == "9" * 40
        assert reopened.get_task(run_id, _ARTICLE_A, "statement").status == "integrated"
        reopened._connection.execute(
            "UPDATE runs SET current_oid = ? WHERE run_id = ?",
            ("f" * 40, run_id),
        )

    with pytest.raises(LedgerError, match="current OID does not match its integration history"):
        RunLedger(path)


@pytest.mark.parametrize(
    ("target", "value", "message"),
    (
        ("blockers", '"not-an-array"', "JSON array"),
        ("blockers", "{", "malformed JSON"),
        ("claim", "[]", "JSON object"),
        ("event", "[]", "JSON object"),
        ("event", "{", "malformed JSON"),
    ),
)
def test_malformed_persisted_json_is_wrapped_and_rejected(
    tmp_path: Path,
    target: str,
    value: str,
    message: str,
) -> None:
    ledger, run_id = _running_ledger(tmp_path)
    if target == "blockers":
        ledger._connection.execute(
            "UPDATE tasks SET blocked_by_json = ? WHERE run_id = ?",
            (value, run_id),
        )
    elif target == "claim":
        attempt = _begin(ledger, run_id, tmp_path)
        ledger._connection.execute(
            "UPDATE attempts SET claim_token_json = ? WHERE attempt_id = ?",
            (value, attempt.attempt_id),
        )
    else:
        ledger._connection.execute(
            "INSERT INTO events(run_id, kind, payload_json, created_ns) VALUES (?, ?, ?, ?)",
            (run_id, "corrupt", value, 999_999),
        )
    path = ledger.path
    ledger.close()

    with pytest.raises(LedgerError, match=message):
        RunLedger(path)
