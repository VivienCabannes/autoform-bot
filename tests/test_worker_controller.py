from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from autoform_cli.runtime import RuntimeAssertions, RuntimeGraph, RuntimeNode, RuntimeStatus
from autoform_worker.controller import (
    CANDIDATE_GATE_NAME,
    CandidateAdmissionContext,
    ControllerError,
    RecoveryAction,
    REVIEW_GATE_NAME,
    RunStopSignal,
    advance_candidate_admission,
    build_task_specs,
    classify_no_work,
    create_run,
    initialize_created_run,
    plan_candidate_admission,
    plan_recovery,
    select_ready_task,
    status_payload,
)
from autoform_worker.gates import CandidateGateResult
from autoform_worker.ledger import (
    AttemptRecord,
    GateRecord,
    MergeItemRecord,
    MergeReplayRecord,
    RecoverySnapshot,
    RunConfig,
    RunIdentity,
    RunLedger,
    RunRecord,
    TaskRecord,
)
from autoform_worker.scheduler import WorkPhase
from autoform_worker.reviewer import (
    CandidateReviewResult,
    ReviewAdapterFactory,
    ReviewEvidenceBlob,
)

import autoform_worker.controller as controller_module


def _node(
    name: str,
    article_id: str,
    *,
    declaration: str = "theorem",
    state: str = "can_state",
    dispatchable: bool = True,
    formalizable: bool = True,
    mathlib: bool = False,
) -> RuntimeNode:
    stated = state in {"can_prove", "proved", "fully_proved", "defined", "mathlib"}
    proved = state in {"proved", "fully_proved", "defined", "mathlib"}
    return RuntimeNode(
        id=name,
        title=name,
        article_path=f"blueprint/roadmap/{name}.md",
        parent=None,
        depth=0,
        declaration=declaration if formalizable else None,
        formalizable=formalizable,
        dispatchable=dispatchable,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(stated, proved, False),
        status=RuntimeStatus(
            state,
            state == "can_state",
            state == "can_prove",
            stated,
            proved,
            proved,
            state == "not_ready",
        ),
        origin=None,
        source_targets=(),
        lean_targets=(),
        mathlib=mathlib,
        mathlib_declarations=(),
        mathlib_file=None,
        article_id=article_id,
    )


def _runtime(*nodes: RuntimeNode) -> RuntimeGraph:
    return RuntimeGraph(
        "autoform-runtime/v1",
        "markdown-articles",
        "6" * 64,
        "blueprint",
        nodes,
        len(nodes),
        sum(node.formalizable for node in nodes),
        sum(node.dispatchable for node in nodes),
        0,
        0,
    )


def _task(article_id: str, phase: str, status: str = "pending", generation: int = 0) -> TaskRecord:
    return TaskRecord(
        run_id="run",
        article_id=article_id,
        phase=phase,
        status=status,
        attempts=0,
        generation=generation,
        blocked_by=(),
        detail="",
        candidate_oid=None,
        integrated_oid=None,
    )


def _run_inputs(*, runtime_revision: str = "6" * 64) -> tuple[RunConfig, RunIdentity]:
    config = RunConfig(
        repository_id="repository",
        target_ref="refs/heads/main",
        remote="https://example.test/private.git",
        backend="claude",
        reviewer_backend="codex",
        max_attempts=3,
        max_steers=3,
        timeout_seconds=1800.0,
        claim_ttl_seconds=1500.0,
        heartbeat_interval_seconds=300.0,
        start_oid="1" * 40,
        plugin_version="revision",
        toolchain_fingerprint="2" * 64,
        coverage_contract_sha256="3" * 64,
        execution_input_sha256="4" * 64,
        source_artifacts_sha256="5" * 64,
        gate_policy_version="gates-v1",
    )
    identity = RunIdentity(
        repository_id=config.repository_id,
        project_root="/private/project",
        target_ref=config.target_ref,
        base_oid=config.start_oid,
        runtime_revision=runtime_revision,
        coverage_revision=config.coverage_contract_sha256,
        source_artifact_sha256=config.source_artifacts_sha256,
        plugin_revision=config.plugin_version,
        toolchain_fingerprint=config.toolchain_fingerprint,
        execution_input_sha256=config.execution_input_sha256,
        config_sha256=config.sha256,
    )
    return config, identity


def test_task_plan_covers_missing_theorem_phases_and_definition_statement() -> None:
    theorem = _node("theorem", "af_000000000000000000000001")
    definition = _node("definition", "af_000000000000000000000002", declaration="def")
    proved = _node("proved", "af_000000000000000000000003", state="fully_proved")
    upstream = _node("upstream", "af_000000000000000000000004", state="mathlib", mathlib=True)
    container = _node(
        "chapter",
        "af_000000000000000000000005",
        formalizable=False,
        dispatchable=False,
    )

    tasks = build_task_specs(_runtime(theorem, definition, proved, upstream, container))

    assert [(task.article_id, task.phase) for task in tasks] == [
        (theorem.article_id, "proof"),
        (theorem.article_id, "statement"),
        (definition.article_id, "statement"),
    ]


def test_task_plan_keeps_not_ready_work_in_completion_denominator() -> None:
    node = _node("deferred", "af_000000000000000000000001", state="not_ready")

    assert [(task.article_id, task.phase) for task in build_task_specs(_runtime(node))] == [
        (node.article_id, "proof"),
        (node.article_id, "statement"),
    ]


def test_select_ready_task_uses_runtime_phase_and_durable_article_id() -> None:
    node = _node("result", "af_000000000000000000000001")
    statement = _task(node.article_id, "statement")
    proof = _task(node.article_id, "proof")

    selected = select_ready_task(_runtime(node), (proof, statement))

    assert selected is not None
    assert selected[0] == statement
    assert selected[1] == node
    assert selected[2] is WorkPhase.STATEMENT


def test_select_ready_task_rejects_regressed_integrated_state() -> None:
    node = _node("result", "af_000000000000000000000001")

    with pytest.raises(ControllerError, match="no longer satisfied"):
        select_ready_task(_runtime(node), (_task(node.article_id, "statement", "integrated"),))


def test_select_ready_task_requires_recovery_before_new_work() -> None:
    first = _node("first", "af_000000000000000000000001", state="can_prove")
    second = _node("second", "af_000000000000000000000002")

    with pytest.raises(ControllerError, match="requires recovery"):
        select_ready_task(
            _runtime(first, second),
            (
                _task(first.article_id, "proof", "candidate"),
                _task(second.article_id, "statement"),
            ),
        )


def test_select_ready_task_detects_incomplete_task_set() -> None:
    node = _node("result", "af_000000000000000000000001")

    with pytest.raises(ControllerError, match="absent from the ledger"):
        select_ready_task(_runtime(node), ())


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("integrated", "integrated"), "complete"),
        (("integrated", "failed"), "failed"),
        (("pending", "stopped"), "stopped"),
        (("candidate", "pending"), "recovery-required"),
        (("pending", "retrying"), "blocked"),
    ],
)
def test_no_work_has_distinct_terminal_classification(statuses: tuple[str, ...], expected: str) -> None:
    tasks = tuple(
        _task(f"af_{index:024x}", "statement", status)
        for index, status in enumerate(statuses, start=1)
    )

    assert classify_no_work(tasks) == expected


def test_planning_requires_durable_article_ids() -> None:
    node = replace(_node("result", "af_000000000000000000000001"), article_id=None)

    with pytest.raises(ControllerError, match="durable article id"):
        build_task_specs(_runtime(node))


def test_create_run_persists_exact_tasks_before_running(tmp_path) -> None:
    runtime = _runtime(_node("result", "af_000000000000000000000001"))
    config, identity = _run_inputs()

    with RunLedger(tmp_path / "run.sqlite3") as ledger:
        run = create_run(ledger, identity, config, runtime, run_id="run")

        assert run.status == "running"
        assert [(task.article_id, task.phase, task.status) for task in ledger.tasks("run")] == [
            ("af_000000000000000000000001", "proof", "pending"),
            ("af_000000000000000000000001", "statement", "pending"),
        ]


def test_created_run_recovers_after_atomic_task_insert(tmp_path) -> None:
    runtime = _runtime(_node("result", "af_000000000000000000000001"))
    config, identity = _run_inputs()
    expected = tuple((spec.article_id, spec.phase) for spec in build_task_specs(runtime))

    with RunLedger(tmp_path / "run.sqlite3") as ledger:
        ledger.create_run(identity, config, tasks=expected, run_id="interrupted")

        assert initialize_created_run(ledger, "interrupted", runtime).status == "running"


def test_created_run_rejects_task_or_runtime_drift(tmp_path) -> None:
    runtime = _runtime(_node("result", "af_000000000000000000000001"))
    config, identity = _run_inputs()

    with RunLedger(tmp_path / "run.sqlite3") as ledger:
        ledger.create_run(
            identity,
            config,
            tasks=(("af_000000000000000000000001", "statement"),),
            run_id="task-drift",
        )
        with pytest.raises(ControllerError, match="task set"):
            initialize_created_run(ledger, "task-drift", runtime)

        ledger.create_run(
            identity,
            config,
            tasks=tuple((spec.article_id, spec.phase) for spec in build_task_specs(runtime)),
            run_id="runtime-drift",
        )
        changed = replace(runtime, source_revision="7" * 64)
        with pytest.raises(ControllerError, match="runtime revision"):
            initialize_created_run(ledger, "runtime-drift", changed)


def test_create_run_allows_an_already_complete_task_plan(tmp_path) -> None:
    runtime = _runtime(
        _node(
            "proved",
            "af_000000000000000000000001",
            state="fully_proved",
        )
    )
    config, identity = _run_inputs()

    with RunLedger(tmp_path / "run.sqlite3") as ledger:
        run = create_run(ledger, identity, config, runtime, run_id="run")

        assert run.status == "running"
        assert ledger.tasks("run") == ()
        assert classify_no_work(ledger.tasks("run")) == "complete"


def test_stop_signal_observes_request_from_an_independent_connection(tmp_path) -> None:
    runtime = _runtime(_node("result", "af_000000000000000000000001"))
    config, identity = _run_inputs()
    ledger_path = tmp_path / "run.sqlite3"
    with RunLedger(ledger_path) as ledger:
        create_run(ledger, identity, config, runtime, run_id="run")

    with RunStopSignal(ledger_path, "run", poll_interval=0.01) as signal:
        assert not signal.is_set()
        with RunLedger(ledger_path) as ledger:
            run = ledger.get_run("run")
            ledger.request_stop("run", expected_generation=run.generation)
        assert signal.wait(2.0)
        assert signal.failure is None


def test_stop_signal_fails_closed_when_run_cannot_be_read(tmp_path) -> None:
    config, identity = _run_inputs()
    ledger_path = tmp_path / "run.sqlite3"
    with RunLedger(ledger_path) as ledger:
        ledger.create_run(identity, config, tasks=(), run_id="known")

    with RunStopSignal(ledger_path, "unknown", poll_interval=0.01) as signal:
        assert signal.wait(2.0)
        assert signal.failure == "durable stop monitor could not read the run ledger"


def _recovery_snapshot(
    *,
    run: RunRecord,
    tasks: tuple[TaskRecord, ...] = (),
    attempts: tuple[AttemptRecord, ...] = (),
    gates: tuple[GateRecord, ...] = (),
    merge_items: tuple[MergeItemRecord, ...] = (),
    merge_replays: tuple[MergeReplayRecord, ...] = (),
) -> RecoverySnapshot:
    return RecoverySnapshot(
        run=run,
        tasks=tasks,
        attempts=attempts,
        gates=gates,
        merge_items=merge_items,
        merge_replays=merge_replays,
        target_adoptions=(),
        external_integrations=(),
    )


def _run_record(*, status: str = "running", stop_requested: bool = False) -> RunRecord:
    config, identity = _run_inputs()
    return RunRecord(
        run_id="run",
        identity=identity,
        identity_sha256=identity.sha256,
        config=config,
        config_sha256=config.sha256,
        status=status,
        generation=1,
        task_plan_sha256="8" * 64,
        task_count=1,
        current_oid="1" * 40,
        stop_requested=stop_requested,
        detail="",
        created_ns=1,
        updated_ns=2,
    )


def _attempt(
    task: TaskRecord,
    *,
    status: str,
    candidate_oid: str | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id="attempt",
        run_id="run",
        article_id=task.article_id,
        phase=task.phase,
        number=task.attempts,
        status=status,
        worktree_path="/private/worktree",
        branch="detached/attempt",
        base_oid="1" * 40,
        backend="claude",
        claim_key="author/af_000000000000000000000001",
        claim_token={"opaque": "test"},
        candidate_oid=candidate_oid,
        detail="",
        started_ns=1,
        finished_ns=2,
    )


def _merge_item(*, status: str = "stale") -> MergeItemRecord:
    return MergeItemRecord(
        queue_item_id="queue",
        run_id="run",
        attempt_id="attempt",
        queue_ref="refs/autoform/queue/queue",
        expected_target_oid="1" * 40,
        candidate_oid="2" * 40,
        status=status,
        generation=1,
        integrated_oid=None,
        detail="",
        created_ns=1,
        updated_ns=2,
    )


def _merge_replay(*, status: str = "prepared") -> MergeReplayRecord:
    return MergeReplayRecord(
        replay_id="replay",
        queue_item_id="queue",
        ordinal=1,
        target_oid="3" * 40,
        candidate_oid="4" * 40,
        gate_evidence_sha256="5" * 64,
        review_evidence_sha256="6" * 64,
        status=status,
        generation=0,
        publication_evidence_sha256=None,
        detail="",
        created_ns=1,
        updated_ns=2,
    )


def _gate(attempt_id: str, name: str, passed: bool) -> GateRecord:
    return GateRecord(
        attempt_id=attempt_id,
        name=name,
        passed=passed,
        evidence_sha256="9" * 64,
        detail="",
        created_ns=1,
    )


class _AdmissionLedger:
    def __init__(self, artifacts: dict[str, bytes] | None = None) -> None:
        self.artifacts = dict(artifacts or {})
        self.recorded: list[tuple[object, ...]] = []
        self.enqueued: dict[str, object] | None = None

    def put_artifact(self, kind: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self.artifacts[digest] = content
        self.recorded.append(("artifact", kind, digest))
        return digest

    def read_artifact(self, digest: str) -> bytes:
        return self.artifacts[digest]

    def record_gate(
        self,
        attempt_id: str,
        name: str,
        passed: bool,
        *,
        expected_generation: int,
        evidence_sha256: str,
        detail: str,
    ) -> None:
        self.recorded.append(
            ("gate", attempt_id, name, passed, expected_generation, evidence_sha256, detail)
        )

    def enqueue_candidate(self, attempt_id: str, **values: object) -> str:
        self.enqueued = {"attempt_id": attempt_id, **values}
        return str(values["queue_item_id"])


class _ReviewRequestStub:
    reviewer_backend = "codex"

    def as_dict(self) -> dict[str, object]:
        return {"request": "stub"}


def _candidate_admission_case(
    tmp_path,
    *,
    gates: tuple[GateRecord, ...] = (),
) -> tuple[RecoverySnapshot, CandidateAdmissionContext]:
    candidate_oid = "2" * 40
    node = _node(
        "result",
        "af_000000000000000000000001",
        state="can_prove",
    )
    task = replace(
        _task(node.article_id, "proof", "candidate"),
        attempts=1,
        candidate_oid=candidate_oid,
    )
    attempt = _attempt(task, status="candidate", candidate_oid=candidate_oid)
    context = CandidateAdmissionContext(
        repository=tmp_path / "repository",
        base_worktree=tmp_path / "base",
        candidate_worktree=tmp_path / "candidate",
        work_item=controller_module.WorkItem(
            node=node,
            phase=WorkPhase.PROOF,
            attempt=1,
            source_revision="6" * 64,
            source_contract_sha256="3" * 64,
            protected_roadmap_sha256="7" * 64,
        ),
        changed_paths=(node.article_path,),
    )
    return (
        _recovery_snapshot(
            run=_run_record(),
            tasks=(task,),
            attempts=(attempt,),
            gates=gates,
        ),
        context,
    )


def _candidate_gate_result(
    context: CandidateAdmissionContext,
    *,
    passed: bool,
) -> CandidateGateResult:
    return CandidateGateResult(
        passed=passed,
        node_id=context.work_item.node.id,
        article_id=context.work_item.node.article_id,
        phase="proof",
        attempt=1,
        source_revision=context.work_item.source_revision,
        source_contract_sha256=context.work_item.source_contract_sha256,
        protected_roadmap_sha256=context.work_item.protected_roadmap_sha256,
        work_item_sha256="8" * 64,
        base_execution_input_sha256=None,
        candidate_execution_input_sha256=None,
        base_toolchain=None,
        candidate_toolchain=None,
        checks=(),
    )


def test_recovery_plan_prioritizes_replay_over_stale_merge_item() -> None:
    snapshot = _recovery_snapshot(
        run=_run_record(),
        merge_items=(_merge_item(),),
        merge_replays=(_merge_replay(),),
    )

    assert plan_recovery(snapshot) == RecoveryAction("recover-replay", "replay")


def test_recovery_plan_rejects_uncertain_external_outcomes() -> None:
    with pytest.raises(ControllerError, match="replay outcome is uncertain"):
        plan_recovery(
            _recovery_snapshot(
                run=_run_record(),
                merge_items=(_merge_item(),),
                merge_replays=(_merge_replay(status="uncertain"),),
            )
        )
    with pytest.raises(ControllerError, match="publication outcome is uncertain"):
        plan_recovery(
            _recovery_snapshot(
                run=_run_record(),
                merge_items=(_merge_item(status="uncertain"),),
            )
        )


def test_recovery_plan_resumes_exact_candidate_attempt() -> None:
    candidate_oid = "2" * 40
    task = _task("af_000000000000000000000001", "proof", "candidate")
    task = replace(task, attempts=1, candidate_oid=candidate_oid)
    attempt = _attempt(task, status="candidate", candidate_oid=candidate_oid)

    assert plan_recovery(
        _recovery_snapshot(run=_run_record(), tasks=(task,), attempts=(attempt,))
    ) == RecoveryAction("admit-candidate", "attempt")


def test_recovery_plan_does_not_admit_candidate_after_stop_request() -> None:
    candidate_oid = "2" * 40
    task = replace(
        _task("af_000000000000000000000001", "proof", "candidate"),
        attempts=1,
        candidate_oid=candidate_oid,
    )
    attempt = _attempt(task, status="candidate", candidate_oid=candidate_oid)

    assert plan_recovery(
        _recovery_snapshot(
            run=_run_record(stop_requested=True),
            tasks=(task,),
            attempts=(attempt,),
        )
    ) == RecoveryAction("stop-candidate", "attempt")


@pytest.mark.parametrize(
    ("gates", "kind"),
    [
        ((), "run-fixed-gates"),
        ((_gate("attempt", CANDIDATE_GATE_NAME, False),), "reject-candidate"),
        ((_gate("attempt", CANDIDATE_GATE_NAME, True),), "run-independent-review"),
        (
            (
                _gate("attempt", CANDIDATE_GATE_NAME, True),
                _gate("attempt", REVIEW_GATE_NAME, False),
            ),
            "reject-candidate",
        ),
        (
            (
                _gate("attempt", CANDIDATE_GATE_NAME, True),
                _gate("attempt", REVIEW_GATE_NAME, True),
            ),
            "enqueue-candidate",
        ),
    ],
)
def test_candidate_admission_resumes_from_exact_gate_evidence(
    gates: tuple[GateRecord, ...],
    kind: str,
) -> None:
    candidate_oid = "2" * 40
    task = replace(
        _task("af_000000000000000000000001", "proof", "candidate"),
        attempts=1,
        candidate_oid=candidate_oid,
    )
    attempt = _attempt(task, status="candidate", candidate_oid=candidate_oid)
    snapshot = _recovery_snapshot(
        run=_run_record(),
        tasks=(task,),
        attempts=(attempt,),
        gates=gates,
    )

    assert plan_candidate_admission(snapshot, "attempt") == RecoveryAction(kind, "attempt")


def test_candidate_admission_rejects_review_without_passing_fixed_gates() -> None:
    candidate_oid = "2" * 40
    task = replace(
        _task("af_000000000000000000000001", "proof", "candidate"),
        attempts=1,
        candidate_oid=candidate_oid,
    )
    attempt = _attempt(task, status="candidate", candidate_oid=candidate_oid)
    snapshot = _recovery_snapshot(
        run=_run_record(),
        tasks=(task,),
        attempts=(attempt,),
        gates=(_gate("attempt", REVIEW_GATE_NAME, True),),
    )

    with pytest.raises(ControllerError, match="without passing fixed gates"):
        plan_candidate_admission(snapshot, "attempt")


def test_candidate_admission_obeys_stop_before_enqueue() -> None:
    candidate_oid = "2" * 40
    task = replace(
        _task("af_000000000000000000000001", "proof", "candidate"),
        attempts=1,
        candidate_oid=candidate_oid,
    )
    attempt = _attempt(task, status="candidate", candidate_oid=candidate_oid)
    snapshot = _recovery_snapshot(
        run=_run_record(stop_requested=True),
        tasks=(task,),
        attempts=(attempt,),
        gates=(
            _gate("attempt", CANDIDATE_GATE_NAME, True),
            _gate("attempt", REVIEW_GATE_NAME, True),
        ),
    )

    assert plan_candidate_admission(snapshot, "attempt") == RecoveryAction(
        "stop-candidate", "attempt"
    )


def test_candidate_admission_records_failed_fixed_gate_before_rejection(tmp_path) -> None:
    snapshot, context = _candidate_admission_case(tmp_path)
    ledger = _AdmissionLedger()
    gate_result = _candidate_gate_result(context, passed=False)
    reviewer = ReviewAdapterFactory("codex", "review-model", 10, ("test",), lambda _: None)

    action = advance_candidate_admission(
        ledger,  # type: ignore[arg-type]
        snapshot,
        "attempt",
        context,
        reviewer,
        threading.Event(),
        gate_runner=lambda *_: gate_result,
    )

    assert action == RecoveryAction("reject-candidate", "attempt")
    assert ledger.recorded[0][0:2] == ("artifact", "candidate-gate")
    assert ledger.recorded[1][0:5] == (
        "gate",
        "attempt",
        CANDIDATE_GATE_NAME,
        False,
        snapshot.run.generation,
    )
    assert ledger.enqueued is None


def test_candidate_admission_validates_review_request_before_recording_passed_gate(
    tmp_path,
    monkeypatch,
) -> None:
    snapshot, context = _candidate_admission_case(tmp_path)
    context = replace(
        context,
        work_item=replace(context.work_item, source_revision="a" * 64),
    )
    ledger = _AdmissionLedger()
    gate_result = _candidate_gate_result(context, passed=True)
    events: list[str] = []

    def bind_request(**values: object) -> _ReviewRequestStub:
        assert values["gate_evidence"] == gate_result.evidence_bytes()
        events.append("request")
        return _ReviewRequestStub()

    original_put_artifact = ledger.put_artifact

    def put_artifact(kind: str, content: bytes) -> str:
        events.append("artifact")
        return original_put_artifact(kind, content)

    ledger.put_artifact = put_artifact  # type: ignore[method-assign]
    monkeypatch.setattr(controller_module, "bind_candidate_review_request", bind_request)
    reviewer = ReviewAdapterFactory("codex", "review-model", 10, ("test",), lambda _: None)

    action = advance_candidate_admission(
        ledger,  # type: ignore[arg-type]
        snapshot,
        "attempt",
        context,
        reviewer,
        threading.Event(),
        gate_runner=lambda *_: gate_result,
        execution_input_reader=lambda _: b"{}",
    )

    assert action == RecoveryAction("run-independent-review", "attempt")
    assert events == ["request", "artifact"]
    assert ledger.recorded[0][0:2] == ("artifact", "candidate-gate")
    assert ledger.recorded[1][0:5] == (
        "gate",
        "attempt",
        CANDIDATE_GATE_NAME,
        True,
        snapshot.run.generation,
    )


def test_candidate_admission_records_review_rejection_once(tmp_path, monkeypatch) -> None:
    fixed_digest = "9" * 64
    snapshot, context = _candidate_admission_case(
        tmp_path,
        gates=(
            GateRecord("attempt", CANDIDATE_GATE_NAME, True, fixed_digest, "", 1),
        ),
    )
    ledger = _AdmissionLedger({fixed_digest: b"fixed evidence"})
    request = _ReviewRequestStub()
    monkeypatch.setattr(controller_module, "bind_candidate_review_request", lambda **_: request)
    result = CandidateReviewResult(
        status="rejected",
        approved=False,
        reason="source statement changed",
        reviewer_backend="codex",
        reviewer_model="review-model",
        request=request,  # type: ignore[arg-type]
        evidence=tuple(
            ReviewEvidenceBlob(name, b"")
            for name in sorted(
                {
                "base-execution-input.json",
                "candidate-execution-input.json",
                "gate-evidence.json",
                "gate-record.json",
                "protected-roadmap.json",
                "request.json",
                "reviewer-config.json",
                "response.txt",
                "source-contract.json",
                "transcript.json",
                "work-item.json",
                }
            )
        ),
    )
    reviewer = ReviewAdapterFactory("codex", "review-model", 10, ("test",), lambda _: None)

    action = advance_candidate_admission(
        ledger,  # type: ignore[arg-type]
        snapshot,
        "attempt",
        context,
        reviewer,
        threading.Event(),
        review_runner=lambda *_: result,
        execution_input_reader=lambda _: b"{}",
        review_evidence_loader=lambda _: result,
    )

    assert action == RecoveryAction("reject-candidate", "attempt")
    assert ledger.recorded[0][0:2] == ("artifact", "candidate-review")
    assert ledger.recorded[1][0:5] == (
        "gate",
        "attempt",
        REVIEW_GATE_NAME,
        False,
        snapshot.run.generation,
    )
    assert ledger.enqueued is None


def test_candidate_admission_revalidates_both_artifacts_before_enqueue(tmp_path, monkeypatch) -> None:
    fixed_digest = "8" * 64
    review_digest = "9" * 64
    snapshot, context = _candidate_admission_case(
        tmp_path,
        gates=(
            GateRecord("attempt", CANDIDATE_GATE_NAME, True, fixed_digest, "", 1),
            GateRecord("attempt", REVIEW_GATE_NAME, True, review_digest, "", 2),
        ),
    )
    ledger = _AdmissionLedger(
        {fixed_digest: b"fixed evidence", review_digest: b"review evidence"}
    )
    request = _ReviewRequestStub()
    monkeypatch.setattr(controller_module, "bind_candidate_review_request", lambda **_: request)
    reviewer = ReviewAdapterFactory("codex", "review-model", 10, ("test",), lambda _: None)
    durable = SimpleNamespace(
        approved=True,
        request=request,
        reviewer_backend="codex",
        reviewer_model="review-model",
    )

    action = advance_candidate_admission(
        ledger,  # type: ignore[arg-type]
        snapshot,
        "attempt",
        context,
        reviewer,
        threading.Event(),
        execution_input_reader=lambda _: b"{}",
        review_evidence_loader=lambda _: durable,  # type: ignore[arg-type]
    )

    assert action.kind == "publish-candidate"
    assert action.identifier is not None
    assert ledger.enqueued is not None
    assert ledger.enqueued["candidate_oid"] == "2" * 40
    assert ledger.enqueued["expected_target_oid"] == "1" * 40
    assert ledger.enqueued["required_gates"] == (CANDIDATE_GATE_NAME, REVIEW_GATE_NAME)
    assert ledger.enqueued["queue_ref"] == f"refs/autoform/queue/{action.identifier.removeprefix('candidate-')}"


def test_candidate_admission_cancellation_wins_before_enqueue(tmp_path, monkeypatch) -> None:
    fixed_digest = "8" * 64
    review_digest = "9" * 64
    snapshot, context = _candidate_admission_case(
        tmp_path,
        gates=(
            GateRecord("attempt", CANDIDATE_GATE_NAME, True, fixed_digest, "", 1),
            GateRecord("attempt", REVIEW_GATE_NAME, True, review_digest, "", 2),
        ),
    )
    ledger = _AdmissionLedger(
        {fixed_digest: b"fixed evidence", review_digest: b"review evidence"}
    )
    monkeypatch.setattr(
        controller_module,
        "bind_candidate_review_request",
        lambda **_: _ReviewRequestStub(),
    )
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ControllerError, match="cancelled before candidate enqueue"):
        advance_candidate_admission(
            ledger,  # type: ignore[arg-type]
            snapshot,
            "attempt",
            context,
            ReviewAdapterFactory("codex", "review-model", 10, ("test",), lambda _: None),
            cancelled,
            execution_input_reader=lambda _: b"{}",
        )
    assert ledger.enqueued is None


def test_candidate_admission_cancellation_during_evidence_reload_prevents_enqueue(
    tmp_path,
    monkeypatch,
) -> None:
    fixed_digest = "8" * 64
    review_digest = "9" * 64
    snapshot, context = _candidate_admission_case(
        tmp_path,
        gates=(
            GateRecord("attempt", CANDIDATE_GATE_NAME, True, fixed_digest, "", 1),
            GateRecord("attempt", REVIEW_GATE_NAME, True, review_digest, "", 2),
        ),
    )
    ledger = _AdmissionLedger(
        {fixed_digest: b"fixed evidence", review_digest: b"review evidence"}
    )
    request = _ReviewRequestStub()
    monkeypatch.setattr(controller_module, "bind_candidate_review_request", lambda **_: request)
    reviewer = ReviewAdapterFactory("codex", "review-model", 10, ("test",), lambda _: None)
    cancelled = threading.Event()
    durable = SimpleNamespace(
        approved=True,
        request=request,
        reviewer_backend="codex",
        reviewer_model="review-model",
    )

    def cancel_during_reload(_: bytes) -> object:
        cancelled.set()
        return durable

    with pytest.raises(ControllerError, match="cancelled after review evidence validation"):
        advance_candidate_admission(
            ledger,  # type: ignore[arg-type]
            snapshot,
            "attempt",
            context,
            reviewer,
            cancelled,
            execution_input_reader=lambda _: b"{}",
            review_evidence_loader=cancel_during_reload,  # type: ignore[arg-type]
        )
    assert ledger.enqueued is None


def test_recovery_plan_keeps_external_recovery_ahead_of_stop() -> None:
    snapshot = _recovery_snapshot(
        run=_run_record(stop_requested=True),
        merge_items=(_merge_item(status="publishing"),),
    )

    assert plan_recovery(snapshot) == RecoveryAction("recover-publication", "queue")


@pytest.mark.parametrize(
    ("status", "stop_requested", "kind"),
    [
        ("created", False, "initialize-run"),
        ("running", False, "schedule"),
        ("running", True, "stop-run"),
        ("blocked", False, "await-resume"),
        ("complete", False, "terminal"),
    ],
)
def test_recovery_plan_classifies_idle_run_state(
    status: str,
    stop_requested: bool,
    kind: str,
) -> None:
    assert plan_recovery(
        _recovery_snapshot(run=_run_record(status=status, stop_requested=stop_requested))
    ) == RecoveryAction(kind, "run")


def test_status_payload_is_stable_and_redacts_private_controller_data() -> None:
    config = RunConfig(
        repository_id="repository",
        target_ref="refs/heads/main",
        remote="https://example.test/private.git",
        backend="claude",
        reviewer_backend="codex",
        max_attempts=3,
        max_steers=3,
        timeout_seconds=1800.0,
        claim_ttl_seconds=1500.0,
        heartbeat_interval_seconds=300.0,
        start_oid="1" * 40,
        plugin_version="revision",
        toolchain_fingerprint="2" * 64,
        coverage_contract_sha256="3" * 64,
        execution_input_sha256="4" * 64,
        source_artifacts_sha256="5" * 64,
        gate_policy_version="gates-v1",
    )
    identity = RunIdentity(
        repository_id="repository",
        project_root="/secret/project",
        target_ref=config.target_ref,
        base_oid=config.start_oid,
        runtime_revision="runtime",
        coverage_revision="6" * 64,
        source_artifact_sha256=config.source_artifacts_sha256,
        plugin_revision=config.plugin_version,
        toolchain_fingerprint=config.toolchain_fingerprint,
        execution_input_sha256=config.execution_input_sha256,
        config_sha256=config.sha256,
    )
    run = RunRecord(
        run_id="run",
        identity=identity,
        identity_sha256=identity.sha256,
        config=config,
        config_sha256=config.sha256,
        status="running",
        generation=7,
        task_plan_sha256="8" * 64,
        task_count=1,
        current_oid="7" * 40,
        stop_requested=False,
        detail="secret backend output",
        created_ns=1,
        updated_ns=2,
    )
    task = _task("af_000000000000000000000001", "proof", "running")
    attempt = AttemptRecord(
        attempt_id="attempt",
        run_id="run",
        article_id=task.article_id,
        phase="proof",
        number=2,
        status="running",
        worktree_path="/secret/worktree",
        branch="detached/attempt",
        base_oid="7" * 40,
        backend="claude",
        claim_key="secret-claim-key",
        claim_token={"secret": "token"},
        candidate_oid=None,
        detail="private transcript",
        started_ns=1,
        finished_ns=None,
    )
    item = MergeItemRecord(
        queue_item_id="queue",
        run_id="run",
        attempt_id="attempt",
        queue_ref="refs/autoform/queue/private",
        expected_target_oid="7" * 40,
        candidate_oid="8" * 40,
        status="pending",
        generation=0,
        integrated_oid=None,
        detail="private remote detail",
        created_ns=1,
        updated_ns=2,
    )

    payload = status_payload(run, (task,), (attempt,), (item,))
    encoded = str(payload)

    assert payload["schema"] == "autoform-execute/v1"
    assert payload["tasks"] == {"counts": {"running": 1}, "total": 1}
    assert payload["attempts"][0]["attempt_id"] == "attempt"
    assert payload["merge_items"][0]["candidate_oid"] == "8" * 40
    for private in ("/secret/project", "/secret/worktree", "private.git", "secret-claim-key", "token", "transcript"):
        assert private not in encoded
