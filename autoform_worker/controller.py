"""Durable planning primitives for the Autoform execution controller."""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoform_cli.execution_input import load_execution_input
from autoform_cli.runtime import RuntimeGraph, RuntimeNode
from autoform_cli.status import DEFINITION_DECLARATIONS

from .gates import CandidateGateResult, run_candidate_gates
from .ledger import (
    AttemptRecord,
    GateRecord,
    MergeItemRecord,
    RecoverySnapshot,
    RunConfig,
    RunIdentity,
    RunLedger,
    RunRecord,
    TaskRecord,
)
from .reviewer import (
    CandidateReviewRequest,
    CandidateReviewResult,
    ReviewAdapterFactory,
    bind_candidate_review_request,
    load_candidate_review_result,
    review_candidate,
)
from .scheduler import CancellationSignal, WorkItem, WorkPhase, _ready_phase


class ControllerError(RuntimeError):
    """The durable controller cannot safely continue."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


@dataclass(frozen=True, order=True, slots=True)
class TaskSpec:
    """One immutable phase the run must integrate before completion."""

    article_id: str
    phase: str
    node_id: str


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """One deterministic next step derived from a read-only ledger snapshot."""

    kind: str
    identifier: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateAdmissionContext:
    """Exact repositories and work item used to resume candidate admission."""

    repository: Path
    base_worktree: Path
    candidate_worktree: Path
    work_item: WorkItem
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.changed_paths))) != self.changed_paths:
            raise ControllerError(
                "candidate-paths-invalid",
                "candidate changed paths must be unique and sorted",
            )
        if self.work_item.node.article_path not in self.changed_paths:
            raise ControllerError(
                "candidate-article-missing",
                "candidate changed paths must include the selected roadmap article",
            )


EXECUTE_OUTPUT_SCHEMA = "autoform-execute/v1"
CANDIDATE_GATE_NAME = "fixed-gates/v1"
REVIEW_GATE_NAME = "independent-review/v1"

GateRunner = Callable[[str | Path, str | Path, WorkItem], CandidateGateResult]
ReviewRunner = Callable[
    [str | Path, CandidateReviewRequest, ReviewAdapterFactory, CancellationSignal],
    CandidateReviewResult,
]
ExecutionInputReader = Callable[[Path], bytes]
ReviewEvidenceLoader = Callable[[bytes], CandidateReviewResult]


class RunStopSignal:
    """Expose a durable stop request as a thread-safe cancellation signal."""

    def __init__(
        self,
        ledger_path: str | Path,
        run_id: str,
        *,
        poll_interval: float = 0.25,
    ) -> None:
        if not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool):
            raise ValueError("stop poll interval must be a positive finite number")
        normalized_interval = float(poll_interval)
        if not math.isfinite(normalized_interval) or normalized_interval <= 0:
            raise ValueError("stop poll interval must be a positive finite number")
        self.ledger_path = Path(ledger_path).expanduser().resolve()
        self.run_id = run_id
        self.poll_interval = normalized_interval
        self._cancelled = threading.Event()
        self._closed = threading.Event()
        self._failure: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def failure(self) -> str | None:
        """Return a stable fail-closed monitor error, if polling failed."""

        return self._failure

    def is_set(self) -> bool:
        return self._cancelled.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def start(self) -> RunStopSignal:
        if self._thread is not None:
            return self
        thread = threading.Thread(
            target=self._poll,
            name=f"autoform-stop-{self.run_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return self

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._closed.set()
        thread.join()
        self._thread = None

    def __enter__(self) -> RunStopSignal:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _poll(self) -> None:
        try:
            with RunLedger(self.ledger_path) as ledger:
                while not self._closed.is_set():
                    run = ledger.get_run(self.run_id)
                    if run.stop_requested or run.status != "running":
                        self._cancelled.set()
                        return
                    self._closed.wait(self.poll_interval)
        except Exception:
            self._failure = "durable stop monitor could not read the run ledger"
            self._cancelled.set()


def status_payload(
    run: RunRecord,
    tasks: tuple[TaskRecord, ...],
    attempts: tuple[AttemptRecord, ...],
    merge_items: tuple[MergeItemRecord, ...],
) -> dict[str, Any]:
    """Return stable public status without paths, remotes, tokens, or backend output."""

    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return {
        "attempts": [
            {
                "article_id": attempt.article_id,
                "attempt": attempt.number,
                "attempt_id": attempt.attempt_id,
                "phase": attempt.phase,
                "status": attempt.status,
            }
            for attempt in sorted(
                attempts,
                key=lambda value: (value.article_id, value.phase, value.number, value.attempt_id),
            )
        ],
        "current_oid": run.current_oid,
        "generation": run.generation,
        "merge_items": [
            {
                "candidate_oid": item.candidate_oid,
                "queue_item_id": item.queue_item_id,
                "status": item.status,
            }
            for item in sorted(merge_items, key=lambda value: value.queue_item_id)
        ],
        "run_id": run.run_id,
        "schema": EXECUTE_OUTPUT_SCHEMA,
        "status": run.status,
        "stop_requested": run.stop_requested,
        "tasks": {
            "counts": {key: counts[key] for key in sorted(counts)},
            "total": len(tasks),
        },
    }


def plan_recovery(snapshot: RecoverySnapshot) -> RecoveryAction:
    """Choose one safe controller action without changing durable state."""

    if not isinstance(snapshot, RecoverySnapshot):
        raise TypeError("recovery planning requires a RecoverySnapshot")

    replays = snapshot.unresolved_merge_replays
    if len(replays) > 1:
        raise ControllerError(
            "multiple-unresolved-replays",
            "more than one merge replay requires recovery",
        )
    if replays:
        replay = replays[0]
        if replay.status == "uncertain":
            raise ControllerError(
                "replay-outcome-uncertain",
                f"merge replay outcome is uncertain: {replay.replay_id}",
            )
        return RecoveryAction("recover-replay", replay.replay_id)

    merge_items = snapshot.unresolved_merge_items
    if len(merge_items) > 1:
        raise ControllerError(
            "multiple-unresolved-merge-items",
            "more than one merge item requires recovery",
        )
    if merge_items:
        item = merge_items[0]
        if item.status == "uncertain":
            raise ControllerError(
                "publication-outcome-uncertain",
                f"merge publication outcome is uncertain: {item.queue_item_id}",
            )
        kind = "prepare-replay" if item.status == "stale" else "recover-publication"
        return RecoveryAction(kind, item.queue_item_id)

    running_attempts = snapshot.running_attempts
    if len(running_attempts) > 1:
        raise ControllerError(
            "multiple-running-attempts",
            "more than one attempt requires repository recovery",
        )
    if running_attempts:
        return RecoveryAction("recover-attempt", running_attempts[0].attempt_id)

    active_tasks = tuple(
        task for task in snapshot.tasks if task.status in {"running", "candidate", "queued"}
    )
    if len(active_tasks) > 1:
        raise ControllerError(
            "multiple-active-tasks",
            "more than one task is active without matching recovery state",
        )
    if active_tasks:
        task = active_tasks[0]
        if task.status == "candidate":
            candidates = tuple(
                attempt
                for attempt in snapshot.attempts
                if attempt.article_id == task.article_id
                and attempt.phase == task.phase
                and attempt.number == task.attempts
                and attempt.status == "candidate"
                and attempt.candidate_oid == task.candidate_oid
            )
            if len(candidates) != 1:
                raise ControllerError(
                    "candidate-evidence-missing",
                    f"candidate task has no unique attempt evidence: {task.article_id}:{task.phase}",
                )
            if snapshot.run.stop_requested:
                return RecoveryAction("stop-candidate", candidates[0].attempt_id)
            return RecoveryAction("admit-candidate", candidates[0].attempt_id)
        raise ControllerError(
            "active-task-evidence-missing",
            f"{task.status} task has no matching recovery evidence: {task.article_id}:{task.phase}",
        )

    run = snapshot.run
    if run.stop_requested:
        return RecoveryAction("stop-run", run.run_id)
    if run.status == "created":
        return RecoveryAction("initialize-run", run.run_id)
    if run.status == "running":
        return RecoveryAction("schedule", run.run_id)
    if run.status in {"blocked", "stopped"}:
        return RecoveryAction("await-resume", run.run_id)
    return RecoveryAction("terminal", run.run_id)


def plan_candidate_admission(
    snapshot: RecoverySnapshot,
    attempt_id: str,
) -> RecoveryAction:
    """Resume candidate admission from immutable gate evidence without skipping work."""

    if not isinstance(snapshot, RecoverySnapshot):
        raise TypeError("candidate admission planning requires a RecoverySnapshot")
    attempts = tuple(attempt for attempt in snapshot.attempts if attempt.attempt_id == attempt_id)
    if len(attempts) != 1:
        raise ControllerError(
            "candidate-attempt-missing",
            f"candidate admission requires one exact attempt: {attempt_id}",
        )
    attempt = attempts[0]
    if attempt.status != "candidate" or attempt.candidate_oid is None:
        raise ControllerError(
            "candidate-attempt-invalid",
            f"attempt is not an admissible candidate: {attempt_id}",
        )
    tasks = tuple(
        task
        for task in snapshot.tasks
        if task.article_id == attempt.article_id and task.phase == attempt.phase
    )
    if len(tasks) != 1:
        raise ControllerError(
            "candidate-task-missing",
            f"candidate admission requires one exact task: {attempt_id}",
        )
    task = tasks[0]
    if (
        task.status != "candidate"
        or task.attempts != attempt.number
        or task.candidate_oid != attempt.candidate_oid
    ):
        raise ControllerError(
            "candidate-task-mismatch",
            f"candidate task does not match its attempt: {attempt_id}",
        )
    if snapshot.run.status != "running" or snapshot.run.stop_requested:
        return RecoveryAction("stop-candidate", attempt_id)

    gates = tuple(gate for gate in snapshot.gates if gate.attempt_id == attempt_id)
    by_name = {gate.name: gate for gate in gates}
    if len(by_name) != len(gates):
        raise ControllerError(
            "duplicate-candidate-gate",
            f"candidate has duplicate gate evidence: {attempt_id}",
        )
    fixed = by_name.get(CANDIDATE_GATE_NAME)
    review = by_name.get(REVIEW_GATE_NAME)
    if review is not None and (fixed is None or not fixed.passed):
        raise ControllerError(
            "review-without-fixed-gates",
            f"candidate review exists without passing fixed gates: {attempt_id}",
        )
    if fixed is None:
        return RecoveryAction("run-fixed-gates", attempt_id)
    if not fixed.passed:
        return RecoveryAction("reject-candidate", attempt_id)
    if review is None:
        return RecoveryAction("run-independent-review", attempt_id)
    if not review.passed:
        return RecoveryAction("reject-candidate", attempt_id)
    return RecoveryAction("enqueue-candidate", attempt_id)


def advance_candidate_admission(
    ledger: RunLedger,
    snapshot: RecoverySnapshot,
    attempt_id: str,
    context: CandidateAdmissionContext,
    reviewer: ReviewAdapterFactory,
    cancelled: CancellationSignal,
    *,
    gate_runner: GateRunner = run_candidate_gates,
    review_runner: ReviewRunner = review_candidate,
    execution_input_reader: ExecutionInputReader | None = None,
    review_evidence_loader: ReviewEvidenceLoader = load_candidate_review_result,
) -> RecoveryAction:
    """Perform at most one restart-safe gate, review, or enqueue transition."""

    action = plan_candidate_admission(snapshot, attempt_id)
    if action.kind in {"reject-candidate", "stop-candidate"}:
        return action
    attempt, task = _candidate_records(snapshot, attempt_id)
    _validate_candidate_context(snapshot, attempt, context, reviewer)
    read_execution_input = execution_input_reader or _execution_input_bytes

    if action.kind == "run-fixed-gates":
        _require_active_candidate(cancelled, "before fixed gates")
        result = gate_runner(context.base_worktree, context.candidate_worktree, context.work_item)
        if not isinstance(result, CandidateGateResult):
            raise ControllerError("fixed-gate-result-invalid", "fixed gates returned an invalid result")
        _validate_gate_identity(result, context.work_item)
        evidence = result.evidence_bytes()
        if result.passed:
            _candidate_review_request(
                snapshot,
                attempt,
                context,
                evidence,
                read_execution_input,
            )
        _require_active_candidate(cancelled, "after fixed gates")
        digest = ledger.put_artifact("candidate-gate", evidence)
        ledger.record_gate(
            attempt_id,
            CANDIDATE_GATE_NAME,
            result.passed,
            expected_generation=snapshot.run.generation,
            evidence_sha256=digest,
            detail=_gate_detail(result),
        )
        return RecoveryAction(
            "run-independent-review" if result.passed else "reject-candidate",
            attempt_id,
        )

    fixed = _candidate_gate(snapshot, attempt_id, CANDIDATE_GATE_NAME)
    fixed_evidence = ledger.read_artifact(fixed.evidence_sha256)
    request = _candidate_review_request(
        snapshot,
        attempt,
        context,
        fixed_evidence,
        read_execution_input,
    )

    if action.kind == "run-independent-review":
        _require_active_candidate(cancelled, "before independent review")
        result = review_runner(context.repository, request, reviewer, cancelled)
        if not isinstance(result, CandidateReviewResult):
            raise ControllerError(
                "review-result-invalid",
                "independent reviewer returned an invalid result",
            )
        evidence = result.evidence_bytes()
        durable = review_evidence_loader(evidence)
        if durable != result or durable.request != request:
            raise ControllerError(
                "review-evidence-mismatch",
                "independent review evidence does not match the candidate request",
            )
        _require_active_candidate(cancelled, "after independent review")
        digest = ledger.put_artifact("candidate-review", evidence)
        ledger.record_gate(
            attempt_id,
            REVIEW_GATE_NAME,
            result.approved,
            expected_generation=snapshot.run.generation,
            evidence_sha256=digest,
            detail=result.reason,
        )
        return RecoveryAction(
            "enqueue-candidate" if result.approved else "reject-candidate",
            attempt_id,
        )

    if action.kind == "enqueue-candidate":
        _require_active_candidate(cancelled, "before candidate enqueue")
        review_gate = _candidate_gate(snapshot, attempt_id, REVIEW_GATE_NAME)
        durable = review_evidence_loader(ledger.read_artifact(review_gate.evidence_sha256))
        if not durable.approved or durable.request != request:
            raise ControllerError(
                "review-evidence-mismatch",
                "stored review approval does not match the candidate request",
            )
        if (
            durable.reviewer_backend != reviewer.backend
            or durable.reviewer_model != reviewer.model
        ):
            raise ControllerError(
                "reviewer-config-mismatch",
                "stored review approval does not match the configured reviewer",
            )
        queue_item_id, queue_ref = _candidate_queue_identity(snapshot.run.run_id, attempt)
        queued = ledger.enqueue_candidate(
            attempt_id,
            expected_generation=snapshot.run.generation,
            expected_task_generation=task.generation,
            candidate_oid=attempt.candidate_oid,
            required_gates=(CANDIDATE_GATE_NAME, REVIEW_GATE_NAME),
            queue_ref=queue_ref,
            expected_target_oid=attempt.base_oid,
            queue_item_id=queue_item_id,
        )
        return RecoveryAction("publish-candidate", queued)

    return action


def _candidate_records(
    snapshot: RecoverySnapshot,
    attempt_id: str,
) -> tuple[AttemptRecord, TaskRecord]:
    attempts = tuple(attempt for attempt in snapshot.attempts if attempt.attempt_id == attempt_id)
    if len(attempts) != 1:
        raise ControllerError(
            "candidate-attempt-missing",
            f"candidate admission requires one exact attempt: {attempt_id}",
        )
    attempt = attempts[0]
    tasks = tuple(
        task
        for task in snapshot.tasks
        if task.article_id == attempt.article_id and task.phase == attempt.phase
    )
    if len(tasks) != 1:
        raise ControllerError(
            "candidate-task-missing",
            f"candidate admission requires one exact task: {attempt_id}",
        )
    return attempt, tasks[0]


def _validate_candidate_context(
    snapshot: RecoverySnapshot,
    attempt: AttemptRecord,
    context: CandidateAdmissionContext,
    reviewer: ReviewAdapterFactory,
) -> None:
    item = context.work_item
    if (
        item.node.article_id != attempt.article_id
        or item.phase.value != attempt.phase
        or item.attempt != attempt.number
        or item.source_revision != snapshot.run.identity.runtime_revision
        or item.source_contract_sha256 != snapshot.run.config.coverage_contract_sha256
    ):
        raise ControllerError(
            "candidate-context-mismatch",
            f"candidate admission context does not match attempt {attempt.attempt_id}",
        )
    if item.protected_roadmap_sha256 is None:
        raise ControllerError(
            "candidate-context-incomplete",
            "candidate admission requires a protected-roadmap digest",
        )
    if reviewer.backend != snapshot.run.config.reviewer_backend:
        raise ControllerError(
            "reviewer-config-mismatch",
            "reviewer backend does not match the durable run configuration",
        )


def _validate_gate_identity(result: CandidateGateResult, item: WorkItem) -> None:
    if (
        result.node_id != item.node.id
        or result.article_id != item.node.article_id
        or result.phase != item.phase.value
        or result.attempt != item.attempt
        or result.source_revision != item.source_revision
        or result.source_contract_sha256 != item.source_contract_sha256
        or result.protected_roadmap_sha256 != item.protected_roadmap_sha256
    ):
        raise ControllerError(
            "fixed-gate-identity-mismatch",
            "fixed-gate evidence does not match the scheduled work item",
        )


def _candidate_gate(
    snapshot: RecoverySnapshot,
    attempt_id: str,
    name: str,
) -> GateRecord:
    gates = tuple(
        gate for gate in snapshot.gates if gate.attempt_id == attempt_id and gate.name == name
    )
    if len(gates) != 1 or not gates[0].passed:
        raise ControllerError(
            "candidate-gate-missing",
            f"candidate lacks one passing {name} gate: {attempt_id}",
        )
    return gates[0]


def _candidate_review_request(
    snapshot: RecoverySnapshot,
    attempt: AttemptRecord,
    context: CandidateAdmissionContext,
    fixed_evidence: bytes,
    read_execution_input: ExecutionInputReader,
) -> CandidateReviewRequest:
    base_input = read_execution_input(context.base_worktree)
    candidate_input = read_execution_input(context.candidate_worktree)
    return bind_candidate_review_request(
        base_oid=attempt.base_oid,
        candidate_oid=attempt.candidate_oid or "",
        article_id=attempt.article_id,
        node_id=context.work_item.node.id,
        phase=attempt.phase,
        article_path=context.work_item.node.article_path,
        changed_paths=context.changed_paths,
        prover_backend=snapshot.run.config.backend,
        reviewer_backend=snapshot.run.config.reviewer_backend,
        base_execution_input=base_input,
        candidate_execution_input=candidate_input,
        gate_evidence=fixed_evidence,
    )


def _execution_input_bytes(project: Path) -> bytes:
    return load_execution_input(project, lean_root=project).to_json().encode("utf-8")


def _gate_detail(result: CandidateGateResult) -> str:
    if result.passed:
        return "all fixed candidate gates passed"
    if result.checks:
        return result.checks[-1].detail
    return "fixed candidate gates failed without check evidence"


def _require_active_candidate(cancelled: CancellationSignal, stage: str) -> None:
    if cancelled.is_set():
        raise ControllerError(
            "candidate-cancelled",
            f"candidate admission was cancelled {stage}",
        )


def _candidate_queue_identity(
    run_id: str,
    attempt: AttemptRecord,
) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"{run_id}\0{attempt.attempt_id}\0{attempt.candidate_oid}".encode("utf-8")
    ).hexdigest()
    return f"candidate-{digest}", f"refs/autoform/queue/{digest}"


def create_run(
    ledger: RunLedger,
    identity: RunIdentity,
    config: RunConfig,
    runtime: RuntimeGraph,
    *,
    run_id: str,
) -> RunRecord:
    """Persist the complete task plan atomically, then start the run."""

    tasks = tuple((spec.article_id, spec.phase) for spec in build_task_specs(runtime))
    ledger.create_run(identity, config, tasks=tasks, run_id=run_id)
    return initialize_created_run(ledger, run_id, runtime)


def initialize_created_run(
    ledger: RunLedger,
    run_id: str,
    runtime: RuntimeGraph,
) -> RunRecord:
    """Resume the sole safe interruption point between creation and execution."""

    run = ledger.get_run(run_id)
    if run.status != "created":
        raise ControllerError(
            "run-already-initialized",
            f"run is {run.status}, not created: {run_id}",
        )
    if run.identity.runtime_revision != runtime.source_revision:
        raise ControllerError(
            "runtime-revision-changed",
            "the runtime revision does not match the run identity",
        )

    expected = tuple((spec.article_id, spec.phase) for spec in build_task_specs(runtime))
    tasks = ledger.tasks(run_id)
    actual = tuple((task.article_id, task.phase) for task in tasks)
    if actual != expected:
        raise ControllerError(
            "task-set-mismatch",
            "the durable task set does not match the bound runtime",
        )
    if any(task.status != "pending" or task.attempts != 0 for task in tasks):
        raise ControllerError(
            "created-run-mutated",
            "a created run contains task progress",
        )
    return ledger.transition_run(
        run_id,
        "running",
        expected_generation=run.generation,
        detail="",
    )


def build_task_specs(runtime: RuntimeGraph) -> tuple[TaskSpec, ...]:
    """Derive the complete missing task set from one initial runtime snapshot."""

    tasks: list[TaskSpec] = []
    seen_articles: set[str] = set()
    for node in runtime.nodes:
        article_id = _article_id(node)
        if article_id in seen_articles:
            raise ControllerError("duplicate-article-id", f"duplicate durable article id: {article_id}")
        seen_articles.add(article_id)
        if not node.formalizable or not node.dispatchable or node.mathlib:
            continue
        if not node.status.stated:
            tasks.append(TaskSpec(article_id, WorkPhase.STATEMENT.value, node.id))
        if not _is_definition(node) and not node.status.proved:
            tasks.append(TaskSpec(article_id, WorkPhase.PROOF.value, node.id))
    return tuple(sorted(tasks))


def select_ready_task(
    runtime: RuntimeGraph,
    tasks: tuple[TaskRecord, ...],
) -> tuple[TaskRecord, RuntimeNode, WorkPhase] | None:
    """Select the first runtime-ready durable task, or return no work."""

    nodes = {_article_id(node): node for node in runtime.nodes}
    by_key = {(task.article_id, task.phase): task for task in tasks}
    for task in tasks:
        node = nodes.get(task.article_id)
        if node is None:
            raise ControllerError(
                "task-article-missing",
                f"task article is absent from the current runtime: {task.article_id}",
            )
        if task.status == "integrated" and not _phase_satisfied(node, task.phase):
            raise ControllerError(
                "integrated-task-regressed",
                f"integrated task is no longer satisfied: {task.article_id}:{task.phase}",
            )
        if task.status in {"running", "candidate", "queued"}:
            raise ControllerError(
                "recovery-required",
                f"task requires recovery before scheduling: {task.article_id}:{task.phase}",
            )

    for node in runtime.nodes:
        phase = _ready_phase(node)
        if phase is None:
            continue
        article_id = _article_id(node)
        task = by_key.get((article_id, phase.value))
        if task is None:
            if _phase_satisfied(node, phase.value):
                continue
            raise ControllerError(
                "task-set-incomplete",
                f"runtime-ready phase is absent from the ledger: {article_id}:{phase.value}",
            )
        if task.status in {"pending", "retrying"}:
            return task, node, phase
    return None


def classify_no_work(tasks: tuple[TaskRecord, ...]) -> str:
    """Classify a round with no selectable work without conflating outcomes."""

    if all(task.status == "integrated" for task in tasks):
        return "complete"
    if any(task.status == "failed" for task in tasks):
        return "failed"
    if any(task.status == "stopped" for task in tasks):
        return "stopped"
    if any(task.status in {"running", "candidate", "queued"} for task in tasks):
        return "recovery-required"
    return "blocked"


def _phase_satisfied(node: RuntimeNode, phase: str) -> bool:
    if phase == WorkPhase.STATEMENT.value:
        return node.status.stated
    if phase == WorkPhase.PROOF.value:
        return node.status.proved
    raise ControllerError("task-phase-invalid", f"unknown task phase: {phase!r}")


def _is_definition(node: RuntimeNode) -> bool:
    return (node.declaration or "").casefold() in DEFINITION_DECLARATIONS


def _article_id(node: RuntimeNode) -> str:
    if node.article_id is None:
        raise ControllerError(
            "article-id-required",
            f"autonomous execution requires a durable article id: {node.id}",
        )
    return node.article_id


__all__ = [
    "CANDIDATE_GATE_NAME",
    "CandidateAdmissionContext",
    "ControllerError",
    "EXECUTE_OUTPUT_SCHEMA",
    "RecoveryAction",
    "REVIEW_GATE_NAME",
    "RunStopSignal",
    "TaskSpec",
    "advance_candidate_admission",
    "build_task_specs",
    "classify_no_work",
    "create_run",
    "initialize_created_run",
    "plan_candidate_admission",
    "plan_recovery",
    "select_ready_task",
    "status_payload",
]
