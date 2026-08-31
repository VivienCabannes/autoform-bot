"""Durable execution state for resumable Autoform runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoform_cli.claims import CLAIM_REF_PREFIX, author_claim_key
from autoform_cli.graph import ARTICLE_ID_PATTERN

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


LEDGER_SCHEMA_VERSION = 4
RUN_CONFIG_SCHEMA_VERSION = 1
ARTICLE_CLAIM_TOKEN_SCHEMA_VERSION = 1
_BACKEND_IDS = frozenset({"claude", "codex", "muse"})
_OBJECT_FORMAT_OID_LENGTHS = {"sha1": 40, "sha256": 64}
_RUN_STATUSES = frozenset({"created", "running", "complete", "blocked", "failed", "stopped"})
_RUN_TRANSITIONS = {
    "created": frozenset({"running", "failed", "stopped"}),
    "running": frozenset({"complete", "blocked", "failed", "stopped"}),
    "blocked": frozenset({"running", "failed", "stopped"}),
    "failed": frozenset(),
    "complete": frozenset(),
    "stopped": frozenset(),
}
_TASK_STATUSES = frozenset(
    {"pending", "running", "retrying", "candidate", "queued", "integrated", "blocked", "failed", "stopped"}
)
_ATTEMPT_OUTCOMES = frozenset({"candidate", "retrying", "failed", "stopped"})
_TASK_RECOVERY_STATUSES = frozenset({"retrying", "blocked", "failed", "stopped"})
_TASK_RECOVERY_SOURCES = frozenset({"running", "candidate", "queued"})
_MERGE_ITEM_STATUSES = frozenset(
    {
        "pending",
        "prepared",
        "queueing",
        "queued",
        "publishing",
        "integrated",
        "stale",
        "uncertain",
        "failed",
    }
)
_MERGE_ITEM_TERMINAL_STATUSES = frozenset({"integrated", "failed"})
_MERGE_REPLAY_STATUSES = frozenset({"prepared", "publishing", "integrated", "stale", "uncertain", "failed"})
_MERGE_REPLAY_TERMINAL_STATUSES = frozenset({"integrated", "stale", "uncertain", "failed"})
_PHASES = frozenset({"statement", "proof"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}$")
_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$|^0{40}$")


class LedgerError(RuntimeError):
    """The durable run ledger is unavailable or internally inconsistent."""


class LedgerBusy(LedgerError):
    """Another coordinator owns this repository's execution lock."""


class GenerationConflict(LedgerError):
    """A caller tried to update state from a stale ledger generation."""


class InvalidTransition(LedgerError):
    """A requested lifecycle transition is not allowed."""


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Portable controller inputs that must not drift while a run is resumed."""

    repository_id: str
    target_ref: str
    remote: str
    backend: str
    reviewer_backend: str
    start_oid: str
    plugin_version: str
    toolchain_fingerprint: str
    coverage_contract_sha256: str
    execution_input_sha256: str
    source_artifacts_sha256: str
    gate_policy_version: str
    max_attempts: int
    max_steers: int
    timeout_seconds: float
    claim_ttl_seconds: float
    heartbeat_interval_seconds: float
    schema_version: int = RUN_CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != RUN_CONFIG_SCHEMA_VERSION:
            raise LedgerError(
                f"unsupported run config schema {self.schema_version}; expected {RUN_CONFIG_SCHEMA_VERSION}"
            )
        for field in ("repository_id", "remote", "plugin_version", "gate_policy_version"):
            _validate_nonempty(field.replace("_", " "), getattr(self, field))
        _validate_branch_ref(self.target_ref)
        backend = _canonical_backend("backend", self.backend)
        reviewer_backend = _canonical_backend("reviewer backend", self.reviewer_backend)
        if reviewer_backend == backend:
            raise LedgerError("reviewer backend must differ from the prover backend")
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "reviewer_backend", reviewer_backend)
        _validate_oid(self.start_oid)
        for field in (
            "toolchain_fingerprint",
            "coverage_contract_sha256",
            "execution_input_sha256",
            "source_artifacts_sha256",
        ):
            _validate_sha256(getattr(self, field), field)
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise LedgerError("max attempts must be an integer of at least one")
        if type(self.max_steers) is not int or self.max_steers < 0:
            raise LedgerError("max steers must be a nonnegative integer")
        timeout = _positive_finite_number("timeout", self.timeout_seconds)
        claim_ttl = _positive_finite_number("claim TTL", self.claim_ttl_seconds)
        heartbeat = _positive_finite_number("heartbeat interval", self.heartbeat_interval_seconds)
        if heartbeat >= claim_ttl:
            raise LedgerError("heartbeat interval must be shorter than claim TTL")
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "claim_ttl_seconds", claim_ttl)
        object.__setattr__(self, "heartbeat_interval_seconds", heartbeat)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_json_bytes(self.as_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Immutable inputs that identify one autonomous execution run."""

    repository_id: str
    project_root: str
    target_ref: str
    base_oid: str
    runtime_revision: str
    coverage_revision: str
    source_artifact_sha256: str
    plugin_revision: str
    toolchain_fingerprint: str
    execution_input_sha256: str
    config_sha256: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_json_bytes(self.as_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class ArticleClaimToken:
    """Claim evidence whose lease ID remains stable across heartbeat ref updates."""

    article_id: str
    claim_key: str
    claim_ref: str
    lease_id: str
    observed_ref_oid: str
    object_format: str
    schema_version: int = ARTICLE_CLAIM_TOKEN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != ARTICLE_CLAIM_TOKEN_SCHEMA_VERSION:
            raise LedgerError(
                f"unsupported article claim token schema {self.schema_version}; "
                f"expected {ARTICLE_CLAIM_TOKEN_SCHEMA_VERSION}"
            )
        _validate_article_id(self.article_id)
        expected_key = author_claim_key(self.article_id)
        if self.claim_key != expected_key:
            raise LedgerError(f"article claim key must equal {expected_key}")
        expected_ref = CLAIM_REF_PREFIX + expected_key
        if self.claim_ref != expected_ref:
            raise LedgerError(f"article claim ref must equal {expected_ref}")
        _validate_sha256(self.lease_id, "article claim lease id")
        if self.object_format not in _OBJECT_FORMAT_OID_LENGTHS:
            choices = ", ".join(sorted(_OBJECT_FORMAT_OID_LENGTHS))
            raise LedgerError(f"Git object format must be one of: {choices}")
        _validate_oid(self.observed_ref_oid)
        if len(self.observed_ref_oid) != _OBJECT_FORMAT_OID_LENGTHS[self.object_format]:
            raise LedgerError("observed claim ref OID does not match its Git object format")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    identity: RunIdentity
    identity_sha256: str
    config: RunConfig
    config_sha256: str
    status: str
    generation: int
    task_plan_sha256: str
    task_count: int
    current_oid: str
    stop_requested: bool
    detail: str
    created_ns: int
    updated_ns: int


@dataclass(frozen=True, slots=True)
class TaskRecord:
    run_id: str
    article_id: str
    phase: str
    status: str
    attempts: int
    generation: int
    blocked_by: tuple[str, ...]
    detail: str
    candidate_oid: str | None
    integrated_oid: str | None

    @property
    def node_id(self) -> str:
        """Compatibility spelling for callers written before durable article IDs."""
        return self.article_id


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_id: str
    run_id: str
    article_id: str
    phase: str
    number: int
    status: str
    worktree_path: str
    branch: str
    base_oid: str
    backend: str
    claim_key: str
    claim_token: ArticleClaimToken
    candidate_oid: str | None
    detail: str
    started_ns: int
    finished_ns: int | None

    @property
    def node_id(self) -> str:
        """Compatibility spelling for callers written before durable article IDs."""
        return self.article_id


@dataclass(frozen=True, slots=True)
class GateRecord:
    attempt_id: str
    name: str
    passed: bool
    evidence_sha256: str
    detail: str
    created_ns: int


@dataclass(frozen=True, slots=True)
class MergeItemRecord:
    queue_item_id: str
    run_id: str
    attempt_id: str
    queue_ref: str
    expected_target_oid: str
    candidate_oid: str
    status: str
    generation: int
    integrated_oid: str | None
    detail: str
    created_ns: int
    updated_ns: int


@dataclass(frozen=True, slots=True)
class MergeReplayRecord:
    """One immutable replay candidate and its append-only publication lifecycle."""

    replay_id: str
    queue_item_id: str
    ordinal: int
    target_oid: str
    candidate_oid: str
    gate_evidence_sha256: str
    review_evidence_sha256: str
    status: str
    generation: int
    publication_evidence_sha256: str | None
    detail: str
    created_ns: int
    updated_ns: int


@dataclass(frozen=True, slots=True)
class TargetAdoptionRecord:
    """A verified external first-parent advance accepted by one run."""

    adoption_id: str
    run_id: str
    ordinal: int
    previous_oid: str
    target_oid: str
    evidence_sha256: str
    created_ns: int


@dataclass(frozen=True, slots=True)
class ExternalIntegrationRecord:
    """One task transition proved by a verified target-head adoption."""

    adoption_id: str
    run_id: str
    article_id: str
    phase: str
    integrated_oid: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    sequence: int
    run_id: str
    kind: str
    payload: Mapping[str, object]
    created_ns: int


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """One read-only, transactionally consistent view used during recovery."""

    run: RunRecord
    tasks: tuple[TaskRecord, ...]
    attempts: tuple[AttemptRecord, ...]
    gates: tuple[GateRecord, ...]
    merge_items: tuple[MergeItemRecord, ...]
    merge_replays: tuple[MergeReplayRecord, ...]
    target_adoptions: tuple[TargetAdoptionRecord, ...]
    external_integrations: tuple[ExternalIntegrationRecord, ...]

    @property
    def running_attempts(self) -> tuple[AttemptRecord, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.status == "running")

    @property
    def unresolved_merge_items(self) -> tuple[MergeItemRecord, ...]:
        return tuple(item for item in self.merge_items if item.status not in _MERGE_ITEM_TERMINAL_STATUSES)

    @property
    def unresolved_merge_replays(self) -> tuple[MergeReplayRecord, ...]:
        return tuple(
            replay for replay in self.merge_replays if replay.status not in _MERGE_REPLAY_TERMINAL_STATUSES
        )


class CoordinatorLock:
    """A process-scoped, inode-stable exclusive coordinator lock."""

    def __init__(
        self,
        path: str | Path,
        *,
        owner: Mapping[str, object] | None = None,
        clock_ns: Any = time.time_ns,
    ) -> None:
        self.path = _absolute_path(path)
        self.owner = dict(owner or {})
        self._clock_ns = clock_ns
        self._descriptor: int | None = None

    def acquire(self) -> CoordinatorLock:
        if self._descriptor is not None:
            return self
        if fcntl is None:
            raise LedgerError("durable execution requires filesystem advisory locks")
        _ensure_private_directory(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created = not self.path.exists()
        try:
            descriptor = os.open(self.path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise LedgerError(f"coordinator lock is not a private regular file: {self.path}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LedgerBusy(f"another Autoform coordinator owns {self.path}") from error
            payload = {
                "acquired_ns": self._clock_ns(),
                "pid": os.getpid(),
                "token": uuid.uuid4().hex,
                **self.owner,
            }
            encoded = _json_bytes(payload) + b"\n"
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            if created:
                _fsync_directory(self.path.parent)
            self._descriptor = descriptor
            return self
        except BaseException:
            if "descriptor" in locals():
                os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> CoordinatorLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


class RunLedger:
    """SQLite-backed run state with atomic lifecycle transitions."""

    def __init__(self, path: str | Path, *, clock_ns: Any = time.time_ns) -> None:
        self.path = _absolute_path(path)
        self._clock_ns = clock_ns
        _ensure_private_directory(self.path.parent)
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            _validate_private_regular_file(self.path, metadata, label="ledger")
        created = metadata is None
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        try:
            connected_metadata = self.path.lstat()
            _validate_private_regular_file(self.path, connected_metadata, label="ledger")
            if metadata is not None and (connected_metadata.st_dev, connected_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise LedgerError(f"ledger path changed while it was being opened: {self.path}")
            with _initialization_lock(self.path.with_suffix(self.path.suffix + ".initialize.lock")):
                locked_metadata = self.path.lstat()
                _validate_private_regular_file(self.path, locked_metadata, label="ledger")
                if (locked_metadata.st_dev, locked_metadata.st_ino) != (
                    connected_metadata.st_dev,
                    connected_metadata.st_ino,
                ):
                    raise LedgerError(f"ledger path changed before initialization: {self.path}")
                _validate_sqlite_sidecars(self.path)
                self._configure()
                self._initialize_schema()
                self._enable_wal()
                wal_metadata = self.path.lstat()
                _validate_private_regular_file(self.path, wal_metadata, label="ledger")
                if (wal_metadata.st_dev, wal_metadata.st_ino) != (
                    connected_metadata.st_dev,
                    connected_metadata.st_ino,
                ):
                    raise LedgerError(f"ledger path changed while enabling WAL: {self.path}")
                _validate_sqlite_sidecars(self.path)
            if created:
                _fsync_directory(self.path.parent)
        except BaseException:
            self._connection.close()
            raise

    @property
    def coordinator_lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    @property
    def artifact_root(self) -> Path:
        return self.path.parent / "artifacts" / "sha256"

    def coordinator_lock(self, *, owner: Mapping[str, object] | None = None) -> CoordinatorLock:
        return CoordinatorLock(self.coordinator_lock_path, owner=owner, clock_ns=self._clock_ns)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> RunLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_run(
        self,
        identity: RunIdentity,
        config: RunConfig,
        *,
        tasks: Iterable[tuple[str, str]],
        run_id: str | None = None,
    ) -> RunRecord:
        _validate_identity(identity)
        _validate_config_binding(identity, config)
        task_plan = _canonical_task_plan(tasks)
        task_plan_sha256 = _task_plan_sha256(task_plan)
        identifier = run_id or uuid.uuid4().hex
        _validate_identifier("run id", identifier)
        now = self._clock_ns()
        identity_json = _json_text(identity.as_dict())
        config_json = _json_text(config.as_dict())
        with self._transaction():
            try:
                self._connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, identity_json, identity_sha256, config_json, config_sha256,
                        status, generation, task_plan_sha256, task_count, current_oid,
                        stop_requested, detail, created_ns, updated_ns
                    ) VALUES (?, ?, ?, ?, ?, 'created', 0, ?, ?, ?, 0, '', ?, ?)
                    """,
                    (
                        identifier,
                        identity_json,
                        identity.sha256,
                        config_json,
                        config.sha256,
                        task_plan_sha256,
                        len(task_plan),
                        config.start_oid,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise LedgerError(f"run already exists: {identifier}") from error
            self._append_event(
                identifier,
                "run.created",
                {
                    "config_sha256": config.sha256,
                    "current_oid": config.start_oid,
                    "identity_sha256": identity.sha256,
                    "task_count": len(task_plan),
                    "task_plan_sha256": task_plan_sha256,
                },
                now,
            )
            for article_id, phase in task_plan:
                self._connection.execute(
                    """
                    INSERT INTO tasks(
                        run_id, article_id, phase, status, attempts, generation,
                        blocked_by_json, detail, candidate_oid, integrated_oid
                    ) VALUES (?, ?, ?, 'pending', 0, 0, '[]', '', NULL, NULL)
                    """,
                    (identifier, article_id, phase),
                )
                self._append_event(
                    identifier,
                    "task.created",
                    {"article_id": article_id, "phase": phase},
                    now,
                )
        return self.get_run(identifier)

    def get_run(self, run_id: str) -> RunRecord:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown run: {run_id}")
        return _run_record(row)

    def list_runs(self) -> tuple[RunRecord, ...]:
        """Return all persisted runs in deterministic creation order."""
        rows = self._connection.execute("SELECT * FROM runs ORDER BY created_ns, run_id").fetchall()
        return tuple(_run_record(row) for row in rows)

    def transition_run(
        self,
        run_id: str,
        status: str,
        *,
        expected_generation: int,
        detail: str = "",
    ) -> RunRecord:
        if status not in _RUN_STATUSES:
            raise InvalidTransition(f"unknown run status: {status}")
        with self._transaction():
            current = self._run_row(run_id)
            if current["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {current['generation']}, expected {expected_generation}"
                )
            if current["status"] == status and current["detail"] == detail:
                return self.get_run(run_id)
            if current["stop_requested"] and status != "stopped":
                raise InvalidTransition("a stop-requested run may transition only to stopped")
            if status not in _RUN_TRANSITIONS[current["status"]]:
                raise InvalidTransition(f"cannot move run from {current['status']} to {status}")
            if status == "complete":
                unfinished = self._connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE run_id = ? AND status != 'integrated'",
                    (run_id,),
                ).fetchone()[0]
                if unfinished:
                    raise InvalidTransition(f"run has {unfinished} task(s) that are not integrated")
            now = self._clock_ns()
            cursor = self._connection.execute(
                """
                UPDATE runs SET status = ?, detail = ?, generation = generation + 1, updated_ns = ?
                WHERE run_id = ? AND generation = ?
                """,
                (status, detail, now, run_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"run changed while updating: {run_id}")
            self._append_event(run_id, "run.transition", {"from": current["status"], "to": status, "detail": detail}, now)
        return self.get_run(run_id)

    def request_stop(self, run_id: str, *, expected_generation: int) -> RunRecord:
        with self._transaction():
            current = self._run_row(run_id)
            if current["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {current['generation']}, expected {expected_generation}"
                )
            if current["status"] in {"complete", "failed"}:
                raise InvalidTransition(f"cannot stop a terminal {current['status']} run")
            if current["stop_requested"]:
                return self.get_run(run_id)
            now = self._clock_ns()
            cursor = self._connection.execute(
                """
                UPDATE runs SET stop_requested = 1, generation = generation + 1, updated_ns = ?
                WHERE run_id = ? AND generation = ?
                """,
                (now, run_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"run changed while requesting stop: {run_id}")
            self._append_event(run_id, "run.stop-requested", {}, now)
        return self.get_run(run_id)

    def resume_run(self, run_id: str, *, expected_generation: int) -> RunRecord:
        """Resume an operator-stopped or externally blocked run exactly once."""
        with self._transaction():
            current = self._run_row(run_id)
            if current["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {current['generation']}, expected {expected_generation}"
                )
            if current["status"] == "running" and not current["stop_requested"]:
                return self.get_run(run_id)
            if current["status"] not in {"stopped", "blocked"}:
                raise InvalidTransition(f"cannot resume a {current['status']} run")
            max_attempts = _run_record(current).config.max_attempts
            now = self._clock_ns()
            cursor = self._connection.execute(
                """
                UPDATE runs SET status = 'running', stop_requested = 0, detail = '',
                    generation = generation + 1, updated_ns = ?
                WHERE run_id = ? AND generation = ?
                """,
                (now, run_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"run changed while resuming: {run_id}")
            self._connection.execute(
                """
                UPDATE tasks SET
                    status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'retrying' END,
                    generation = generation + 1
                WHERE run_id = ? AND status = 'stopped'
                """,
                (max_attempts, run_id),
            )
            self._append_event(run_id, "run.resumed", {"from": current["status"]}, now)
        return self.get_run(run_id)

    def tasks(self, run_id: str) -> tuple[TaskRecord, ...]:
        self._run_row(run_id)
        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY article_id, phase", (run_id,)
        ).fetchall()
        return tuple(_task_record(row) for row in rows)

    def get_task(self, run_id: str, article_id: str, phase: str) -> TaskRecord:
        return _task_record(self._task_row(run_id, article_id, phase))

    def transition_task(
        self,
        run_id: str,
        article_id: str,
        phase: str,
        status: str,
        *,
        expected_generation: int,
        expected_run_generation: int,
        detail: str,
        blocked_by: Iterable[str] = (),
    ) -> TaskRecord:
        """Resolve active task state without deleting its attempt or candidate evidence."""
        if status not in _TASK_RECOVERY_STATUSES:
            raise InvalidTransition(f"task recovery cannot transition to {status}")
        if not detail.strip():
            raise InvalidTransition("task recovery requires a nonempty detail")
        blockers = tuple(sorted(set(blocked_by)))
        for blocker in blockers:
            _validate_article_id(blocker)
        if status != "blocked" and blockers:
            raise InvalidTransition("only a blocked task may record blocking nodes")
        now = self._clock_ns()
        with self._transaction():
            run = self._run_row(run_id)
            if run["generation"] != expected_run_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {run['generation']}, "
                    f"expected {expected_run_generation}"
                )
            task = self._task_row(run_id, article_id, phase)
            if task["generation"] != expected_generation:
                raise GenerationConflict(
                    f"task {article_id}:{phase} is at generation {task['generation']}, "
                    f"expected {expected_generation}"
                )
            max_attempts = _run_record(run).config.max_attempts
            requested_status = status
            if status == "retrying" and task["attempts"] >= max_attempts:
                status = "failed"
            if (
                task["status"] == status
                and task["detail"] == detail
                and _task_record(task).blocked_by == blockers
            ):
                return _task_record(task)
            if task["status"] not in _TASK_RECOVERY_SOURCES:
                raise InvalidTransition(f"cannot recover task from {task['status']} to {status}")
            if status == "failed":
                if requested_status == "failed" and task["status"] != "candidate":
                    raise InvalidTransition("only a rejected candidate may use terminal task failure")
                if task["attempts"] < max_attempts:
                    raise InvalidTransition(
                        f"candidate has used {task['attempts']} of {max_attempts} attempts; retry is required"
                    )
            if task["status"] == "running":
                attempts = self._connection.execute(
                    """
                    SELECT attempt_id FROM attempts
                    WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'running'
                    ORDER BY number
                    """,
                    (run_id, article_id, phase),
                ).fetchall()
                if len(attempts) != 1:
                    raise LedgerError(
                        f"running task {article_id}:{phase} has {len(attempts)} running attempts"
                    )
                cursor = self._connection.execute(
                    """
                    UPDATE attempts SET status = 'interrupted', detail = ?, finished_ns = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (detail, now, attempts[0]["attempt_id"]),
                )
                if cursor.rowcount != 1:
                    raise GenerationConflict(
                        f"running attempt changed while recovering task: {article_id}:{phase}"
                    )
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = ?, blocked_by_json = ?, detail = ?, generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND generation = ?
                """,
                (
                    status,
                    _json_text(blockers),
                    detail,
                    run_id,
                    article_id,
                    phase,
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"task changed while recovering: {article_id}:{phase}")
            payload: dict[str, object] = {
                "from": task["status"],
                "to": status,
                "article_id": article_id,
                "phase": phase,
                "detail": detail,
                "blocked_by": blockers,
            }
            if requested_status != status:
                payload["requested_status"] = requested_status
            self._append_event(
                run_id,
                "task.recovered",
                payload,
                now,
            )
        return self.get_task(run_id, article_id, phase)

    def begin_attempt(
        self,
        run_id: str,
        article_id: str,
        phase: str,
        *,
        expected_generation: int,
        expected_task_generation: int,
        worktree_path: str | Path,
        branch: str,
        base_oid: str,
        backend: str,
        claim_key: str,
        claim_token: ArticleClaimToken,
        attempt_id: str | None = None,
    ) -> AttemptRecord:
        identifier = attempt_id or uuid.uuid4().hex
        _validate_identifier("attempt id", identifier)
        _validate_identifier("branch", branch)
        backend = _canonical_backend("backend", backend)
        _validate_identifier("claim key", claim_key)
        _validate_article_id(article_id)
        if claim_key != author_claim_key(article_id):
            raise LedgerError(f"claim key is not anchored to durable article id {article_id}")
        if not isinstance(claim_token, ArticleClaimToken):
            raise LedgerError("attempt claim token must be an ArticleClaimToken")
        if claim_token.article_id != article_id or claim_token.claim_key != claim_key:
            raise LedgerError("attempt claim token does not match the article claim")
        _validate_oid(base_oid)
        claim_json = _json_text(claim_token.as_dict())
        now = self._clock_ns()
        with self._transaction():
            run = self._run_row(run_id)
            if run["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {run['generation']}, expected {expected_generation}"
                )
            task = self._task_row(run_id, article_id, phase)
            if task["generation"] != expected_task_generation:
                raise GenerationConflict(
                    f"task {article_id}:{phase} is at generation {task['generation']}, "
                    f"expected {expected_task_generation}"
                )
            if run["status"] != "running" or run["stop_requested"]:
                raise InvalidTransition(f"run is not accepting attempts: {run['status']}")
            run_record = _run_record(run)
            if base_oid != run_record.current_oid:
                raise GenerationConflict(
                    f"attempt base {base_oid} does not match run current OID {run_record.current_oid}"
                )
            if backend != run_record.config.backend:
                raise LedgerError(
                    f"attempt backend {backend!r} does not match run backend {run_record.config.backend!r}"
                )
            if task["status"] not in {"pending", "retrying"}:
                raise InvalidTransition(f"task is not ready for an attempt: {task['status']}")
            if task["attempts"] >= run_record.config.max_attempts:
                raise InvalidTransition(
                    f"task exhausted its {run_record.config.max_attempts} configured attempts"
                )
            unresolved_merge = self._connection.execute(
                """
                SELECT merge_items.queue_item_id FROM merge_items
                JOIN attempts USING(attempt_id)
                WHERE attempts.run_id = ? AND attempts.article_id = ? AND attempts.phase = ?
                    AND merge_items.status NOT IN ('integrated', 'stale', 'failed')
                ORDER BY merge_items.created_ns LIMIT 1
                """,
                (run_id, article_id, phase),
            ).fetchone()
            if unresolved_merge is not None:
                raise InvalidTransition(
                    f"task has unresolved merge item: {unresolved_merge['queue_item_id']}"
                )
            number = task["attempts"] + 1
            try:
                self._connection.execute(
                    """
                    INSERT INTO attempts(
                        attempt_id, run_id, article_id, phase, number, status,
                        worktree_path, branch, base_oid, backend, claim_key,
                        claim_token_json, candidate_oid, detail, started_ns, finished_ns
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, NULL, '', ?, NULL)
                    """,
                    (
                        identifier,
                        run_id,
                        article_id,
                        phase,
                        number,
                        str(Path(worktree_path).expanduser().resolve()),
                        branch,
                        base_oid,
                        backend,
                        claim_key,
                        claim_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise LedgerError(f"attempt already exists: {identifier}") from error
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = 'running', attempts = ?, generation = generation + 1, detail = ''
                WHERE run_id = ? AND article_id = ? AND phase = ? AND generation = ?
                """,
                (number, run_id, article_id, phase, expected_task_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"task changed while starting attempt: {article_id}:{phase}")
            self._append_event(
                run_id,
                "attempt.started",
                {
                    "article_id": article_id,
                    "attempt_id": identifier,
                    "base_oid": base_oid,
                    "number": number,
                    "phase": phase,
                },
                now,
            )
        return self.get_attempt(identifier)

    def finish_attempt(
        self,
        attempt_id: str,
        outcome: str,
        *,
        expected_generation: int,
        expected_task_generation: int,
        detail: str = "",
        candidate_oid: str | None = None,
    ) -> AttemptRecord:
        return self._finish_attempt(
            attempt_id,
            outcome,
            expected_generation=expected_generation,
            expected_task_generation=expected_task_generation,
            detail=detail,
            candidate_oid=candidate_oid,
            recovery_evidence=None,
        )

    def recover_attempt(
        self,
        attempt_id: str,
        outcome: str,
        *,
        expected_generation: int,
        expected_task_generation: int,
        observed_worktree_path: str | Path,
        observed_base_oid: str,
        observed_head_oid: str,
        detail: str,
        candidate_oid: str | None = None,
    ) -> AttemptRecord:
        """Resolve one interrupted attempt after inspecting its repository worktree."""
        _validate_oid(observed_base_oid)
        _validate_oid(observed_head_oid)
        evidence = {
            "worktree_path": str(Path(observed_worktree_path).expanduser().resolve()),
            "base_oid": observed_base_oid,
            "head_oid": observed_head_oid,
        }
        if not detail.strip():
            raise InvalidTransition("attempt recovery requires a nonempty detail")
        if outcome == "candidate" and candidate_oid != observed_head_oid:
            raise InvalidTransition("a recovered candidate must equal the inspected worktree HEAD")
        if outcome != "candidate" and observed_head_oid != observed_base_oid:
            raise InvalidTransition("a non-candidate recovery cannot discard an inspected commit")
        return self._finish_attempt(
            attempt_id,
            outcome,
            expected_generation=expected_generation,
            expected_task_generation=expected_task_generation,
            detail=detail,
            candidate_oid=candidate_oid,
            recovery_evidence=evidence,
        )

    def _finish_attempt(
        self,
        attempt_id: str,
        outcome: str,
        *,
        expected_generation: int,
        expected_task_generation: int,
        detail: str,
        candidate_oid: str | None,
        recovery_evidence: Mapping[str, str] | None,
    ) -> AttemptRecord:
        if outcome not in _ATTEMPT_OUTCOMES:
            raise InvalidTransition(f"unknown attempt outcome: {outcome}")
        if outcome == "candidate":
            if candidate_oid is None:
                raise InvalidTransition("a candidate attempt requires a commit OID")
            _validate_oid(candidate_oid)
        elif candidate_oid is not None:
            raise InvalidTransition("only a candidate attempt may record a commit OID")
        now = self._clock_ns()
        with self._transaction():
            attempt = self._attempt_row(attempt_id)
            run = self._run_row(attempt["run_id"])
            if run["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {attempt['run_id']} is at generation {run['generation']}, "
                    f"expected {expected_generation}"
                )
            task = self._task_row(attempt["run_id"], attempt["article_id"], attempt["phase"])
            if task["generation"] != expected_task_generation:
                raise GenerationConflict(
                    f"task {attempt['article_id']}:{attempt['phase']} is at generation "
                    f"{task['generation']}, expected {expected_task_generation}"
                )
            effective_outcome = outcome
            if outcome == "retrying" and attempt["number"] >= _run_record(run).config.max_attempts:
                effective_outcome = "failed"
            if recovery_evidence is None:
                if run["status"] != "running" or run["stop_requested"]:
                    raise InvalidTransition(f"run is not accepting attempt results: {run['status']}")
            else:
                if recovery_evidence["worktree_path"] != attempt["worktree_path"]:
                    raise GenerationConflict("recovery inspected a different attempt worktree")
                if recovery_evidence["base_oid"] != attempt["base_oid"]:
                    raise GenerationConflict("recovery inspected a different attempt base")
                if run["stop_requested"] and outcome != "stopped":
                    raise InvalidTransition("a stop-requested run may recover an attempt only as stopped")
                if run["status"] not in {"running", "stopped"}:
                    raise InvalidTransition(f"cannot recover an attempt for a {run['status']} run")
            if attempt["status"] != "running":
                if (
                    attempt["status"] == effective_outcome
                    and attempt["detail"] == detail
                    and attempt["candidate_oid"] == candidate_oid
                ):
                    return self.get_attempt(attempt_id)
                raise InvalidTransition(f"attempt is already {attempt['status']}")
            if task["status"] != "running" or task["attempts"] != attempt["number"]:
                raise GenerationConflict(
                    f"task no longer names running attempt {attempt_id}: "
                    f"{attempt['article_id']}:{attempt['phase']}"
                )
            cursor = self._connection.execute(
                """
                UPDATE attempts SET status = ?, candidate_oid = ?, detail = ?, finished_ns = ?
                WHERE attempt_id = ? AND status = 'running'
                """,
                (effective_outcome, candidate_oid, detail, now, attempt_id),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"attempt changed while finishing: {attempt_id}")
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = ?, candidate_oid = ?, detail = ?, generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'running'
                    AND attempts = ? AND generation = ?
                """,
                (
                    effective_outcome,
                    candidate_oid,
                    detail,
                    attempt["run_id"],
                    attempt["article_id"],
                    attempt["phase"],
                    attempt["number"],
                    expected_task_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(
                    f"task changed while finishing attempt: {attempt['article_id']}:{attempt['phase']}"
                )
            payload: dict[str, object] = {
                "attempt_id": attempt_id,
                "outcome": effective_outcome,
                "candidate_oid": candidate_oid,
                "detail": detail,
            }
            if effective_outcome != outcome:
                payload["requested_outcome"] = outcome
            if recovery_evidence is not None:
                payload["repository_inspection"] = dict(recovery_evidence)
            self._append_event(
                attempt["run_id"],
                "attempt.recovered" if recovery_evidence is not None else "attempt.finished",
                payload,
                now,
            )
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        return _attempt_record(self._attempt_row(attempt_id))

    def list_attempts(self, run_id: str) -> tuple[AttemptRecord, ...]:
        self._run_row(run_id)
        rows = self._connection.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY article_id, phase, number, attempt_id",
            (run_id,),
        ).fetchall()
        return tuple(_attempt_record(row) for row in rows)

    def inspect_interrupted(self, run_id: str) -> tuple[AttemptRecord, ...]:
        """Return running attempts for external inspection without changing their evidence."""
        self._run_row(run_id)
        rows = self._connection.execute(
            "SELECT * FROM attempts WHERE run_id = ? AND status = 'running' ORDER BY attempt_id",
            (run_id,),
        ).fetchall()
        return tuple(_attempt_record(row) for row in rows)

    def recover_interrupted(self, run_id: str) -> tuple[str, ...]:
        """Compatibility alias for read-only interrupted-attempt inspection."""
        return tuple(attempt.attempt_id for attempt in self.inspect_interrupted(run_id))

    def record_gate(
        self,
        attempt_id: str,
        name: str,
        passed: bool,
        *,
        expected_generation: int,
        evidence_sha256: str,
        detail: str = "",
    ) -> GateRecord:
        _validate_identifier("gate name", name)
        if not isinstance(passed, bool):
            raise LedgerError("gate passed value must be a bool")
        _validate_sha256(evidence_sha256, "gate evidence")
        now = self._clock_ns()
        with self._transaction():
            attempt = self._attempt_row(attempt_id)
            self._active_run_row(attempt["run_id"], expected_generation)
            existing = self._connection.execute(
                "SELECT * FROM gates WHERE attempt_id = ? AND name = ?",
                (attempt_id, name),
            ).fetchone()
            if existing is not None:
                if (
                    bool(existing["passed"]) == passed
                    and existing["evidence_sha256"] == evidence_sha256
                    and existing["detail"] == detail
                ):
                    return _gate_record(existing)
                raise LedgerError(f"gate already recorded with different evidence: {attempt_id}:{name}")
            if attempt["status"] != "candidate":
                raise InvalidTransition("gates may be recorded only for a candidate attempt")
            evidence = self._connection.execute(
                "SELECT 1 FROM artifacts WHERE sha256 = ?", (evidence_sha256,)
            ).fetchone()
            if evidence is None:
                raise LedgerError(f"gate evidence is not in the artifact store: {evidence_sha256}")
            self._connection.execute(
                """
                INSERT INTO gates(attempt_id, name, passed, evidence_sha256, detail, created_ns)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, name, int(passed), evidence_sha256, detail, now),
            )
            self._append_event(
                attempt["run_id"],
                "gate.recorded",
                {"attempt_id": attempt_id, "name": name, "passed": passed, "evidence_sha256": evidence_sha256},
                now,
            )
        return self.get_gate(attempt_id, name)

    def get_gate(self, attempt_id: str, name: str) -> GateRecord:
        row = self._connection.execute(
            "SELECT * FROM gates WHERE attempt_id = ? AND name = ?",
            (attempt_id, name),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown gate: {attempt_id}:{name}")
        return _gate_record(row)

    def list_gates(self, attempt_id: str) -> tuple[GateRecord, ...]:
        self._attempt_row(attempt_id)
        rows = self._connection.execute(
            "SELECT * FROM gates WHERE attempt_id = ? ORDER BY name",
            (attempt_id,),
        ).fetchall()
        return tuple(_gate_record(row) for row in rows)

    def enqueue_candidate(
        self,
        attempt_id: str,
        *,
        expected_generation: int,
        expected_task_generation: int,
        candidate_oid: str,
        required_gates: Iterable[str],
        queue_ref: str,
        expected_target_oid: str,
        queue_item_id: str | None = None,
    ) -> str:
        required = tuple(sorted(set(required_gates)))
        if not required:
            raise InvalidTransition("at least one gate is required before enqueue")
        for name in required:
            _validate_identifier("gate name", name)
        _validate_identifier("queue ref", queue_ref)
        _validate_oid(expected_target_oid)
        _validate_oid(candidate_oid)
        identifier = queue_item_id or uuid.uuid4().hex
        _validate_identifier("queue item id", identifier)
        now = self._clock_ns()
        with self._transaction():
            attempt = self._attempt_row(attempt_id)
            run = self._active_run_row(attempt["run_id"], expected_generation)
            task = self._task_row(attempt["run_id"], attempt["article_id"], attempt["phase"])
            if task["generation"] != expected_task_generation:
                raise GenerationConflict(
                    f"task {attempt['article_id']}:{attempt['phase']} is at generation "
                    f"{task['generation']}, expected {expected_task_generation}"
                )
            if attempt["status"] != "candidate" or attempt["candidate_oid"] != candidate_oid:
                raise InvalidTransition("only a candidate attempt may enter the merge queue")
            if attempt["base_oid"] != expected_target_oid:
                raise GenerationConflict(
                    f"candidate attempt base {attempt['base_oid']} for {attempt_id} "
                    f"does not match merge base {expected_target_oid}"
                )
            if run["current_oid"] != expected_target_oid:
                raise GenerationConflict(
                    f"run {attempt['run_id']} current OID is {run['current_oid']}, "
                    f"not merge base {expected_target_oid}"
                )
            if (
                task["status"] != "candidate"
                or task["candidate_oid"] != candidate_oid
                or task["attempts"] != attempt["number"]
            ):
                raise GenerationConflict(f"task no longer names candidate attempt {attempt_id}")
            rows = self._connection.execute(
                "SELECT name, passed, evidence_sha256 FROM gates WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
            observed = {row["name"]: bool(row["passed"]) for row in rows}
            missing = [name for name in required if not observed.get(name, False)]
            if missing:
                raise InvalidTransition("candidate lacks passing gates: " + ", ".join(missing))
            evidence_by_name = {row["name"]: row["evidence_sha256"] for row in rows}
            for name in required:
                self.read_artifact(evidence_by_name[name])
            self._connection.execute(
                """
                INSERT INTO merge_items(
                    queue_item_id, run_id, attempt_id, queue_ref, expected_target_oid,
                    candidate_oid, status, generation, integrated_oid, detail, created_ns, updated_ns
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, '', ?, ?)
                """,
                (
                    identifier,
                    attempt["run_id"],
                    attempt_id,
                    queue_ref,
                    expected_target_oid,
                    candidate_oid,
                    now,
                    now,
                ),
            )
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = 'queued', generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'candidate'
                    AND generation = ? AND attempts = ? AND candidate_oid = ?
                """,
                (
                    attempt["run_id"],
                    attempt["article_id"],
                    attempt["phase"],
                    expected_task_generation,
                    attempt["number"],
                    candidate_oid,
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(
                    f"candidate task changed before enqueue: {attempt['article_id']}:{attempt['phase']}"
                )
            self._append_event(
                attempt["run_id"],
                "candidate.queued",
                {
                    "attempt_id": attempt_id,
                    "candidate_oid": candidate_oid,
                    "expected_target_oid": expected_target_oid,
                    "queue_item_id": identifier,
                    "queue_ref": queue_ref,
                },
                now,
            )
        return identifier

    def get_merge_item(self, queue_item_id: str) -> MergeItemRecord:
        row = self._connection.execute(
            "SELECT * FROM merge_items WHERE queue_item_id = ?", (queue_item_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown merge item: {queue_item_id}")
        return _merge_item_record(row)

    def list_merge_items(self, run_id: str) -> tuple[MergeItemRecord, ...]:
        self._run_row(run_id)
        rows = self._connection.execute(
            "SELECT * FROM merge_items WHERE run_id = ? ORDER BY created_ns, queue_item_id",
            (run_id,),
        ).fetchall()
        return tuple(_merge_item_record(row) for row in rows)

    def prepare_merge_replay(
        self,
        queue_item_id: str,
        *,
        target_oid: str,
        candidate_oid: str,
        gate_evidence_sha256: str,
        review_evidence_sha256: str,
        expected_run_generation: int,
        expected_item_generation: int,
        replay_id: str | None = None,
    ) -> MergeReplayRecord:
        """Record a fresh, admitted replay without rewriting the original attempt."""

        _validate_oid(target_oid)
        _validate_oid(candidate_oid)
        _validate_sha256(gate_evidence_sha256, "replay gate evidence")
        _validate_sha256(review_evidence_sha256, "replay review evidence")
        if gate_evidence_sha256 == review_evidence_sha256:
            raise InvalidTransition("replay gate and review evidence must be distinct")
        identifier = replay_id or uuid.uuid4().hex
        _validate_identifier("replay id", identifier)
        self.read_artifact(gate_evidence_sha256)
        self.read_artifact(review_evidence_sha256)
        now = self._clock_ns()
        with self._transaction():
            item = self._merge_item_row(queue_item_id)
            run = self._active_run_row(item["run_id"], expected_run_generation)
            if item["generation"] != expected_item_generation:
                raise GenerationConflict(
                    f"merge item {queue_item_id} is at generation {item['generation']}, "
                    f"expected {expected_item_generation}"
                )
            existing = self._connection.execute(
                "SELECT * FROM merge_replays WHERE replay_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                replay = _merge_replay_record(existing)
                expected = (
                    queue_item_id,
                    target_oid,
                    candidate_oid,
                    gate_evidence_sha256,
                    review_evidence_sha256,
                )
                observed = (
                    replay.queue_item_id,
                    replay.target_oid,
                    replay.candidate_oid,
                    replay.gate_evidence_sha256,
                    replay.review_evidence_sha256,
                )
                if observed != expected:
                    raise GenerationConflict(f"replay id already names different evidence: {identifier}")
                return replay
            if item["status"] != "stale":
                raise InvalidTransition("only a stale merge item may prepare a replay")
            if run["current_oid"] != target_oid:
                raise GenerationConflict(
                    f"replay target {target_oid} does not match run head {run['current_oid']}"
                )
            if len(target_oid) != len(item["candidate_oid"]) or len(candidate_oid) != len(target_oid):
                raise InvalidTransition("replay object ids do not match the run object format")
            if candidate_oid in {target_oid, item["candidate_oid"]}:
                raise InvalidTransition("replay candidate must be a distinct commit")
            active = self._connection.execute(
                """
                SELECT replay_id FROM merge_replays
                WHERE queue_item_id = ? AND status NOT IN ('integrated','stale','uncertain','failed')
                LIMIT 1
                """,
                (queue_item_id,),
            ).fetchone()
            if active is not None:
                raise InvalidTransition(f"merge item already has an active replay: {active['replay_id']}")
            uncertain = self._connection.execute(
                """
                SELECT replay_id FROM merge_replays
                WHERE queue_item_id = ? AND status = 'uncertain' LIMIT 1
                """,
                (queue_item_id,),
            ).fetchone()
            if uncertain is not None:
                raise InvalidTransition(
                    f"merge item has an uncertain replay outcome: {uncertain['replay_id']}"
                )
            ordinal = self._connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM merge_replays WHERE queue_item_id = ?",
                (queue_item_id,),
            ).fetchone()[0]
            self._connection.execute(
                """
                INSERT INTO merge_replays(
                    replay_id, queue_item_id, ordinal, target_oid, candidate_oid,
                    gate_evidence_sha256, review_evidence_sha256, status, generation,
                    publication_evidence_sha256, detail, created_ns, updated_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', 0, NULL, '', ?, ?)
                """,
                (
                    identifier,
                    queue_item_id,
                    ordinal,
                    target_oid,
                    candidate_oid,
                    gate_evidence_sha256,
                    review_evidence_sha256,
                    now,
                    now,
                ),
            )
            self._append_event(
                item["run_id"],
                "candidate.replay-prepared",
                {
                    "candidate_oid": candidate_oid,
                    "gate_evidence_sha256": gate_evidence_sha256,
                    "queue_item_id": queue_item_id,
                    "replay_id": identifier,
                    "review_evidence_sha256": review_evidence_sha256,
                    "target_oid": target_oid,
                },
                now,
            )
        return self.get_merge_replay(identifier)

    def get_merge_replay(self, replay_id: str) -> MergeReplayRecord:
        return _merge_replay_record(self._merge_replay_row(replay_id))

    def list_merge_replays(self, queue_item_id: str) -> tuple[MergeReplayRecord, ...]:
        self._merge_item_row(queue_item_id)
        rows = self._connection.execute(
            "SELECT * FROM merge_replays WHERE queue_item_id = ? ORDER BY ordinal",
            (queue_item_id,),
        ).fetchall()
        return tuple(_merge_replay_record(row) for row in rows)

    def transition_merge_replay(
        self,
        replay_id: str,
        status: str,
        *,
        expected_generation: int,
        expected_run_generation: int,
        detail: str,
    ) -> MergeReplayRecord:
        if status not in _MERGE_REPLAY_STATUSES or status == "integrated":
            raise InvalidTransition(f"invalid replay recovery status: {status}")
        if not detail.strip():
            raise InvalidTransition("merge replay transitions require a nonempty detail")
        now = self._clock_ns()
        with self._transaction():
            replay = self._merge_replay_row(replay_id)
            item = self._merge_item_row(replay["queue_item_id"])
            run = self._active_run_row(item["run_id"], expected_run_generation)
            if replay["generation"] != expected_generation:
                raise GenerationConflict(
                    f"merge replay {replay_id} is at generation {replay['generation']}, "
                    f"expected {expected_generation}"
                )
            if replay["status"] == status and replay["detail"] == detail:
                return _merge_replay_record(replay)
            if replay["status"] in _MERGE_REPLAY_TERMINAL_STATUSES:
                raise InvalidTransition(f"merge replay is already {replay['status']}")
            if item["status"] != "stale":
                raise InvalidTransition("merge replay no longer belongs to a stale merge item")
            allowed = {
                "prepared": frozenset({"publishing", "stale", "uncertain", "failed"}),
                "publishing": frozenset({"stale", "uncertain", "failed"}),
            }
            if status not in allowed[replay["status"]]:
                raise InvalidTransition(f"cannot move merge replay from {replay['status']} to {status}")
            if status == "publishing" and run["current_oid"] != replay["target_oid"]:
                raise GenerationConflict(
                    f"run {item['run_id']} current OID changed from replay base "
                    f"{replay['target_oid']} to {run['current_oid']}"
                )
            cursor = self._connection.execute(
                """
                UPDATE merge_replays SET status = ?, detail = ?, generation = generation + 1,
                    updated_ns = ? WHERE replay_id = ? AND generation = ?
                """,
                (status, detail, now, replay_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"merge replay changed while updating: {replay_id}")
            self._append_event(
                item["run_id"],
                "merge-replay.transition",
                {"detail": detail, "from": replay["status"], "replay_id": replay_id, "to": status},
                now,
            )
        return self.get_merge_replay(replay_id)

    def mark_replay_integrated(
        self,
        replay_id: str,
        *,
        publication_evidence_sha256: str,
        expected_generation: int,
        expected_item_generation: int,
        expected_replay_generation: int,
    ) -> RunRecord:
        """Integrate one exact replay while retaining the original candidate identity."""

        _validate_sha256(publication_evidence_sha256, "replay publication evidence")
        self.read_artifact(publication_evidence_sha256)
        now = self._clock_ns()
        with self._transaction():
            replay = self._merge_replay_row(replay_id)
            item = self._merge_item_row(replay["queue_item_id"])
            if publication_evidence_sha256 in {
                replay["gate_evidence_sha256"],
                replay["review_evidence_sha256"],
            }:
                raise InvalidTransition("replay publication evidence must be distinct from admission evidence")
            if replay["generation"] != expected_replay_generation:
                raise GenerationConflict(
                    f"merge replay {replay_id} is at generation {replay['generation']}, "
                    f"expected {expected_replay_generation}"
                )
            if item["generation"] != expected_item_generation:
                raise GenerationConflict(
                    f"merge item {item['queue_item_id']} is at generation {item['generation']}, "
                    f"expected {expected_item_generation}"
                )
            run = self._active_run_row(item["run_id"], expected_generation)
            if replay["status"] != "publishing":
                raise InvalidTransition("only a publishing replay may be integrated")
            if item["status"] != "stale":
                raise InvalidTransition("replay integration requires its original stale merge item")
            if run["current_oid"] != replay["target_oid"]:
                raise GenerationConflict(
                    f"run {item['run_id']} current OID changed from replay base "
                    f"{replay['target_oid']} to {run['current_oid']}"
                )
            attempt = self._attempt_row(item["attempt_id"])
            task = self._task_row(attempt["run_id"], attempt["article_id"], attempt["phase"])
            if (
                attempt["status"] != "candidate"
                or attempt["candidate_oid"] != item["candidate_oid"]
                or attempt["base_oid"] != item["expected_target_oid"]
                or task["status"] != "queued"
                or task["candidate_oid"] != item["candidate_oid"]
                or task["attempts"] != attempt["number"]
            ):
                raise GenerationConflict("replayed merge item no longer owns its original candidate task")
            cursor = self._connection.execute(
                """
                UPDATE merge_replays SET status = 'integrated', publication_evidence_sha256 = ?,
                    detail = 'atomic replay publication verified', generation = generation + 1,
                    updated_ns = ?
                WHERE replay_id = ? AND generation = ? AND status = 'publishing'
                """,
                (publication_evidence_sha256, now, replay_id, expected_replay_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"merge replay changed while integrating: {replay_id}")
            cursor = self._connection.execute(
                """
                UPDATE merge_items SET status = 'integrated', integrated_oid = ?,
                    generation = generation + 1, updated_ns = ?
                WHERE queue_item_id = ? AND generation = ? AND status = 'stale'
                """,
                (replay["candidate_oid"], now, item["queue_item_id"], expected_item_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"merge item changed while replaying: {item['queue_item_id']}")
            cursor = self._connection.execute(
                "UPDATE attempts SET status = 'integrated' WHERE attempt_id = ? AND status = 'candidate'",
                (item["attempt_id"],),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"attempt changed while replaying: {item['attempt_id']}")
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = 'integrated', integrated_oid = ?, generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'queued'
                    AND attempts = ? AND candidate_oid = ?
                """,
                (
                    replay["candidate_oid"],
                    item["run_id"],
                    attempt["article_id"],
                    attempt["phase"],
                    attempt["number"],
                    item["candidate_oid"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict("queued task changed while integrating its replay")
            cursor = self._connection.execute(
                """
                UPDATE runs SET current_oid = ?, generation = generation + 1, updated_ns = ?
                WHERE run_id = ? AND generation = ? AND current_oid = ?
                """,
                (
                    replay["candidate_oid"],
                    now,
                    item["run_id"],
                    expected_generation,
                    replay["target_oid"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"run changed while integrating replay: {item['run_id']}")
            self._append_event(
                item["run_id"],
                "candidate.integrated",
                {
                    "expected_target_oid": replay["target_oid"],
                    "integrated_oid": replay["candidate_oid"],
                    "original_candidate_oid": item["candidate_oid"],
                    "publication_evidence_sha256": publication_evidence_sha256,
                    "queue_item_id": item["queue_item_id"],
                    "replay_id": replay_id,
                },
                now,
            )
        return self.get_run(item["run_id"])

    def adopt_target_head(
        self,
        run_id: str,
        *,
        target_oid: str,
        evidence_sha256: str,
        satisfied_tasks: Iterable[tuple[str, str]] = (),
        expected_generation: int,
        adoption_id: str | None = None,
    ) -> RunRecord:
        """Advance to an externally verified target and record satisfied tasks atomically.

        Repository ancestry, changed-path, source-contract, gate, and review checks are
        performed by the controller. The canonical evidence artifact and exact task set
        are retained here so a restart cannot silently relabel the run head.
        """

        _validate_oid(target_oid)
        _validate_sha256(evidence_sha256, "target adoption evidence")
        try:
            raw_tasks = tuple(satisfied_tasks)
        except TypeError as error:
            raise InvalidTransition("target adoption tasks must be iterable") from error
        tasks_list: list[tuple[str, str]] = []
        for item in raw_tasks:
            if not isinstance(item, tuple) or len(item) != 2:
                raise InvalidTransition("target adoption tasks must be (article_id, phase) pairs")
            article_id, phase = item
            if not isinstance(article_id, str) or not isinstance(phase, str):
                raise InvalidTransition("target adoption tasks must contain strings")
            _validate_article_id(article_id)
            if phase not in _PHASES:
                raise InvalidTransition(f"unknown task phase: {phase}")
            tasks_list.append((article_id, phase))
        if len(set(tasks_list)) != len(tasks_list):
            raise InvalidTransition("target adoption contains duplicate tasks")
        tasks = tuple(sorted(tasks_list))
        identifier = adoption_id or uuid.uuid4().hex
        _validate_identifier("target adoption id", identifier)
        self.read_artifact(evidence_sha256)
        now = self._clock_ns()
        with self._transaction():
            run = self._active_run_row(run_id, expected_generation)
            existing = self._connection.execute(
                "SELECT * FROM target_adoptions WHERE adoption_id = ?", (identifier,)
            ).fetchone()
            if existing is not None:
                adoption = _target_adoption_record(existing)
                existing_tasks = tuple(
                    sorted(
                        (row["article_id"], row["phase"])
                        for row in self._connection.execute(
                            """
                            SELECT article_id, phase FROM external_integrations
                            WHERE adoption_id = ?
                            """,
                            (identifier,),
                        ).fetchall()
                    )
                )
                if (
                    adoption.run_id != run_id
                    or adoption.target_oid != target_oid
                    or adoption.evidence_sha256 != evidence_sha256
                    or existing_tasks != tasks
                ):
                    raise GenerationConflict(f"target adoption id names different evidence: {identifier}")
                if run["current_oid"] != target_oid:
                    raise GenerationConflict(
                        f"target adoption {identifier} is no longer the run head"
                    )
                return self.get_run(run_id)
            previous_oid = run["current_oid"]
            if previous_oid == target_oid:
                raise InvalidTransition("target adoption must advance to a different object")
            if len(previous_oid) != len(target_oid):
                raise InvalidTransition("target adoption object id does not match the run object format")
            unsafe_item = self._connection.execute(
                """
                SELECT queue_item_id FROM merge_items
                WHERE run_id = ? AND status NOT IN ('integrated','failed','stale') LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if unsafe_item is not None:
                raise InvalidTransition(
                    f"target head cannot be adopted while merge item is active: {unsafe_item['queue_item_id']}"
                )
            unsafe_replay = self._connection.execute(
                """
                SELECT merge_replays.replay_id FROM merge_replays
                JOIN merge_items USING(queue_item_id)
                WHERE merge_items.run_id = ?
                    AND merge_replays.status NOT IN ('integrated','stale','failed')
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if unsafe_replay is not None:
                raise InvalidTransition(
                    "target head cannot be adopted while merge replay is unresolved: "
                    f"{unsafe_replay['replay_id']}"
                )
            rows: list[sqlite3.Row] = []
            selected = set(tasks)
            for article_id, phase in tasks:
                task = self._task_row(run_id, article_id, phase)
                if task["status"] not in {"pending", "retrying", "blocked"}:
                    raise InvalidTransition(
                        f"externally satisfied task is already active or terminal: {article_id}:{phase}"
                    )
                retained = self._connection.execute(
                    """
                    SELECT merge_items.queue_item_id FROM merge_items
                    JOIN attempts USING(attempt_id)
                    WHERE attempts.run_id = ? AND attempts.article_id = ? AND attempts.phase = ?
                        AND merge_items.status NOT IN ('integrated','failed')
                    LIMIT 1
                    """,
                    (run_id, article_id, phase),
                ).fetchone()
                if retained is not None:
                    raise InvalidTransition(
                        f"externally satisfied task retains merge ownership: {retained['queue_item_id']}"
                    )
                if phase == "proof":
                    statement = self._connection.execute(
                        """
                        SELECT status FROM tasks
                        WHERE run_id = ? AND article_id = ? AND phase = 'statement'
                        """,
                        (run_id, article_id),
                    ).fetchone()
                    if statement is None:
                        raise InvalidTransition(
                            f"external proof integration has no statement task: {article_id}"
                        )
                    if (
                        statement["status"] != "integrated"
                        and (article_id, "statement") not in selected
                    ):
                        raise InvalidTransition(
                            f"external proof integration omits its statement task: {article_id}"
                        )
                rows.append(task)
            ordinal = self._connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM target_adoptions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            self._connection.execute(
                """
                INSERT INTO target_adoptions(
                    adoption_id, run_id, ordinal, previous_oid, target_oid, evidence_sha256, created_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (identifier, run_id, ordinal, previous_oid, target_oid, evidence_sha256, now),
            )
            for task in rows:
                self._connection.execute(
                    """
                    INSERT INTO external_integrations(
                        adoption_id, run_id, article_id, phase, integrated_oid
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (identifier, run_id, task["article_id"], task["phase"], target_oid),
                )
                cursor = self._connection.execute(
                    """
                    UPDATE tasks SET status = 'integrated', integrated_oid = ?,
                        blocked_by_json = '[]', detail = 'verified external integration',
                        generation = generation + 1
                    WHERE run_id = ? AND article_id = ? AND phase = ?
                        AND generation = ? AND status = ?
                    """,
                    (
                        target_oid,
                        run_id,
                        task["article_id"],
                        task["phase"],
                        task["generation"],
                        task["status"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise GenerationConflict(
                        f"task changed during target adoption: {task['article_id']}:{task['phase']}"
                    )
            cursor = self._connection.execute(
                """
                UPDATE runs SET current_oid = ?, generation = generation + 1, updated_ns = ?
                WHERE run_id = ? AND generation = ? AND current_oid = ?
                """,
                (target_oid, now, run_id, expected_generation, previous_oid),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"run changed while adopting target: {run_id}")
            self._append_event(
                run_id,
                "target.adopted",
                {
                    "adoption_id": identifier,
                    "evidence_sha256": evidence_sha256,
                    "ordinal": ordinal,
                    "previous_oid": previous_oid,
                    "satisfied_tasks": [
                        {"article_id": article_id, "phase": phase} for article_id, phase in tasks
                    ],
                    "target_oid": target_oid,
                },
                now,
            )
        return self.get_run(run_id)

    def list_target_adoptions(self, run_id: str) -> tuple[TargetAdoptionRecord, ...]:
        self._run_row(run_id)
        rows = self._connection.execute(
            "SELECT * FROM target_adoptions WHERE run_id = ? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        return tuple(_target_adoption_record(row) for row in rows)

    def list_external_integrations(self, run_id: str) -> tuple[ExternalIntegrationRecord, ...]:
        self._run_row(run_id)
        rows = self._connection.execute(
            """
            SELECT * FROM external_integrations
            WHERE run_id = ? ORDER BY adoption_id, article_id, phase
            """,
            (run_id,),
        ).fetchall()
        return tuple(_external_integration_record(row) for row in rows)

    def transition_merge_item(
        self,
        queue_item_id: str,
        status: str,
        *,
        expected_generation: int,
        expected_run_generation: int,
        detail: str,
    ) -> MergeItemRecord:
        """Persist a recoverable publication state using a merge-item CAS."""
        if status not in _MERGE_ITEM_STATUSES:
            raise InvalidTransition(f"unknown merge item status: {status}")
        if status == "integrated":
            raise InvalidTransition("integrated merge items must use mark_integrated")
        if not detail.strip():
            raise InvalidTransition("merge item transitions require a nonempty detail")
        now = self._clock_ns()
        with self._transaction():
            item = self._merge_item_row(queue_item_id)
            self._active_run_row(item["run_id"], expected_run_generation)
            if item["generation"] != expected_generation:
                raise GenerationConflict(
                    f"merge item {queue_item_id} is at generation {item['generation']}, "
                    f"expected {expected_generation}"
                )
            if item["status"] == status and item["detail"] == detail:
                return _merge_item_record(item)
            if item["status"] in _MERGE_ITEM_TERMINAL_STATUSES:
                raise InvalidTransition(f"merge item is already {item['status']}")
            if item["status"] == "stale" and status != "failed":
                raise InvalidTransition("a stale merge item may only be replayed or failed")
            if item["status"] == "stale" and status == "failed":
                unresolved_replay = self._connection.execute(
                    """
                    SELECT replay_id FROM merge_replays
                    WHERE queue_item_id = ?
                        AND status NOT IN ('integrated','stale','failed')
                    LIMIT 1
                    """,
                    (queue_item_id,),
                ).fetchone()
                if unresolved_replay is not None:
                    raise InvalidTransition(
                        "merge item cannot fail while a replay is unresolved: "
                        f"{unresolved_replay['replay_id']}"
                    )
            cursor = self._connection.execute(
                """
                UPDATE merge_items SET status = ?, detail = ?, generation = generation + 1, updated_ns = ?
                WHERE queue_item_id = ? AND generation = ?
                """,
                (status, detail, now, queue_item_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"merge item changed while updating: {queue_item_id}")
            self._append_event(
                item["run_id"],
                "merge-item.transition",
                {"from": item["status"], "to": status, "queue_item_id": queue_item_id, "detail": detail},
                now,
            )
        return self.get_merge_item(queue_item_id)

    def record_merge_recovery(
        self,
        queue_item_id: str,
        status: str,
        *,
        expected_generation: int,
        expected_run_generation: int,
        detail: str,
    ) -> MergeItemRecord:
        """Record the externally inspected outcome of an interrupted publication."""
        return self.transition_merge_item(
            queue_item_id,
            status,
            expected_generation=expected_generation,
            expected_run_generation=expected_run_generation,
            detail=detail,
        )

    def mark_integrated(
        self,
        queue_item_id: str,
        *,
        integrated_oid: str,
        expected_generation: int,
        expected_item_generation: int,
    ) -> RunRecord:
        _validate_oid(integrated_oid)
        now = self._clock_ns()
        with self._transaction():
            item = self._merge_item_row(queue_item_id)
            if item["generation"] != expected_item_generation:
                raise GenerationConflict(
                    f"merge item {queue_item_id} is at generation {item['generation']}, "
                    f"expected {expected_item_generation}"
                )
            run = self._active_run_row(item["run_id"], expected_generation)
            if integrated_oid != item["candidate_oid"]:
                raise InvalidTransition("integrated OID must equal the queued candidate OID")
            if item["status"] == "integrated" and item["integrated_oid"] == integrated_oid:
                return self.get_run(item["run_id"])
            if item["status"] in _MERGE_ITEM_TERMINAL_STATUSES:
                raise InvalidTransition(f"merge item is already {item['status']}")
            if item["status"] == "stale":
                raise InvalidTransition("a stale merge item must use an admitted replay")
            if run["current_oid"] != item["expected_target_oid"]:
                raise GenerationConflict(
                    f"run {item['run_id']} current OID changed from merge base {item['expected_target_oid']} "
                    f"to {run['current_oid']}"
                )
            attempt = self._attempt_row(item["attempt_id"])
            if (
                attempt["run_id"] != item["run_id"]
                or attempt["candidate_oid"] != item["candidate_oid"]
                or attempt["base_oid"] != item["expected_target_oid"]
            ):
                raise LedgerError(f"merge item candidate disagrees with attempt: {queue_item_id}")
            cursor = self._connection.execute(
                """
                UPDATE merge_items SET status = 'integrated', integrated_oid = ?,
                    generation = generation + 1, updated_ns = ?
                WHERE queue_item_id = ? AND generation = ? AND status = ? AND candidate_oid = ?
                """,
                (
                    integrated_oid,
                    now,
                    queue_item_id,
                    expected_item_generation,
                    item["status"],
                    item["candidate_oid"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"merge item changed while integrating: {queue_item_id}")
            cursor = self._connection.execute(
                """
                UPDATE attempts SET status = 'integrated'
                WHERE attempt_id = ? AND status = 'candidate' AND candidate_oid = ?
                """,
                (item["attempt_id"], item["candidate_oid"]),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"attempt changed while integrating: {item['attempt_id']}")
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = 'integrated', integrated_oid = ?, generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'queued'
                    AND attempts = ? AND candidate_oid = ?
                """,
                (
                    integrated_oid,
                    item["run_id"],
                    attempt["article_id"],
                    attempt["phase"],
                    attempt["number"],
                    item["candidate_oid"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(
                    f"queued task changed while integrating: {attempt['article_id']}:{attempt['phase']}"
                )
            cursor = self._connection.execute(
                """
                UPDATE runs SET current_oid = ?, generation = generation + 1, updated_ns = ?
                WHERE run_id = ? AND generation = ? AND current_oid = ?
                """,
                (
                    integrated_oid,
                    now,
                    item["run_id"],
                    expected_generation,
                    item["expected_target_oid"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"run changed while integrating: {item['run_id']}")
            self._append_event(
                item["run_id"],
                "candidate.integrated",
                {
                    "expected_target_oid": item["expected_target_oid"],
                    "queue_item_id": queue_item_id,
                    "integrated_oid": integrated_oid,
                },
                now,
            )
        return self.get_run(item["run_id"])

    def inspect_recovery(self, run_id: str) -> RecoverySnapshot:
        """Read all controller recovery evidence without changing any lifecycle state."""
        with self._read_transaction():
            run = _run_record(self._run_row(run_id))
            tasks = tuple(
                _task_record(row)
                for row in self._connection.execute(
                    "SELECT * FROM tasks WHERE run_id = ? ORDER BY article_id, phase",
                    (run_id,),
                ).fetchall()
            )
            attempts = tuple(
                _attempt_record(row)
                for row in self._connection.execute(
                    "SELECT * FROM attempts WHERE run_id = ? ORDER BY article_id, phase, number, attempt_id",
                    (run_id,),
                ).fetchall()
            )
            gates = tuple(
                _gate_record(row)
                for row in self._connection.execute(
                    """
                    SELECT gates.* FROM gates
                    JOIN attempts USING(attempt_id)
                    WHERE attempts.run_id = ?
                    ORDER BY gates.attempt_id, gates.name
                    """,
                    (run_id,),
                ).fetchall()
            )
            merge_items = tuple(
                _merge_item_record(row)
                for row in self._connection.execute(
                    "SELECT * FROM merge_items WHERE run_id = ? ORDER BY created_ns, queue_item_id",
                    (run_id,),
                ).fetchall()
            )
            merge_replays = tuple(
                _merge_replay_record(row)
                for row in self._connection.execute(
                    """
                    SELECT merge_replays.* FROM merge_replays
                    JOIN merge_items USING(queue_item_id)
                    WHERE merge_items.run_id = ?
                    ORDER BY merge_replays.queue_item_id, merge_replays.ordinal
                    """,
                    (run_id,),
                ).fetchall()
            )
            target_adoptions = self.list_target_adoptions(run_id)
            external_integrations = self.list_external_integrations(run_id)
        return RecoverySnapshot(
            run=run,
            tasks=tasks,
            attempts=attempts,
            gates=gates,
            merge_items=merge_items,
            merge_replays=merge_replays,
            target_adoptions=target_adoptions,
            external_integrations=external_integrations,
        )

    def events(self, run_id: str, *, after: int = 0) -> tuple[EventRecord, ...]:
        self._run_row(run_id)
        rows = self._connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, after),
        ).fetchall()
        return tuple(_event_record(row) for row in rows)

    def put_artifact(self, kind: str, content: bytes) -> str:
        _validate_identifier("artifact kind", kind)
        digest = hashlib.sha256(content).hexdigest()
        directory = self.artifact_root / digest[:2]
        target = directory / digest
        with _open_private_subdirectory(
            self.path.parent,
            ("artifacts", "sha256", digest[:2]),
            create=True,
        ) as directory_descriptor:
            try:
                os.stat(digest, dir_fd=directory_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                temporary_name = f".autoform-artifact-{uuid.uuid4().hex}"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
                except OSError as error:
                    raise LedgerError(f"artifact temporary file cannot be created: {target}") from error
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(
                        temporary_name,
                        digest,
                        src_dir_fd=directory_descriptor,
                        dst_dir_fd=directory_descriptor,
                    )
                    os.fsync(directory_descriptor)
                finally:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_descriptor)
                    except FileNotFoundError:
                        pass
            _read_artifact_at(directory_descriptor, target, digest, len(content))
        now = self._clock_ns()
        with self._transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO artifacts(sha256, kind, size, relative_path, created_ns) VALUES (?, ?, ?, ?, ?)",
                (digest, kind, len(content), str(target.relative_to(self.path.parent)), now),
            )
            row = self._connection.execute("SELECT * FROM artifacts WHERE sha256 = ?", (digest,)).fetchone()
            if (
                row is None
                or row["kind"] != kind
                or row["size"] != len(content)
                or row["relative_path"] != str(target.relative_to(self.path.parent))
            ):
                raise LedgerError(f"artifact metadata is inconsistent: {digest}")
        return digest

    def read_artifact(self, digest: str) -> bytes:
        _validate_sha256(digest, "artifact")
        row = self._connection.execute("SELECT * FROM artifacts WHERE sha256 = ?", (digest,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown artifact: {digest}")
        path = self.artifact_root / digest[:2] / digest
        if row["relative_path"] != str(path.relative_to(self.path.parent)):
            raise LedgerError(f"artifact path is inconsistent: {digest}")
        with _open_private_subdirectory(
            self.path.parent,
            ("artifacts", "sha256", digest[:2]),
            create=False,
        ) as directory_descriptor:
            return _read_artifact_at(directory_descriptor, path, digest, row["size"])

    def _configure(self) -> None:
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA synchronous = FULL")

    def _enable_wal(self) -> None:
        try:
            mode = self._connection.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).casefold() != "wal":
                mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise LedgerError(f"ledger journal mode could not be configured: {self.path}") from error
        if str(mode).casefold() != "wal":
            raise LedgerError(f"SQLite refused WAL mode: {mode}")

    def _initialize_schema(self) -> None:
        try:
            with self._transaction():
                tables = {
                    row["name"]
                    for row in self._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                if not tables:
                    _execute_schema(self._connection, _SCHEMA)
                    self._connection.execute(
                        "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                        (str(LEDGER_SCHEMA_VERSION),),
                    )
                else:
                    if "metadata" not in tables:
                        raise LedgerError("ledger has tables but no schema metadata")
                    row = self._connection.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone()
                    if row is None:
                        raise LedgerError("ledger schema version is missing")
                    if row["value"] == "1":
                        self._migrate_v1()
                    elif row["value"] == "2":
                        self._migrate_v2()
                    elif row["value"] == "3":
                        self._migrate_v3()
                    elif row["value"] != str(LEDGER_SCHEMA_VERSION):
                        raise LedgerError(
                            f"unsupported ledger schema {row['value']}; expected {LEDGER_SCHEMA_VERSION}"
                        )
                _verify_schema(self._connection, _SCHEMA, version=LEDGER_SCHEMA_VERSION)
                self._validate_content_rows()
        except LedgerError:
            raise
        except sqlite3.DatabaseError as error:
            raise LedgerError(f"ledger database is malformed: {self.path}") from error

    def _migrate_v1(self) -> None:
        renamed = (
            "runs",
            "tasks",
            "attempts",
            "gates",
            "merge_items",
            "events",
            "artifacts",
        )
        with self._transaction():
            version = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version["value"] == str(LEDGER_SCHEMA_VERSION):
                return
            if version is None or version["value"] != "1":
                raise LedgerError("ledger schema changed while preparing the v1 migration")
            _verify_schema(self._connection, _SCHEMA_V1, version=1)
            migrations = self._prepare_v1_run_migrations()
            events = self._prepare_v1_events()
            self._connection.execute("DROP TRIGGER events_no_update")
            self._connection.execute("DROP TRIGGER events_no_delete")
            for table in renamed:
                self._connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
            _execute_schema(self._connection, _SCHEMA)
            for migration in migrations:
                self._connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, identity_json, identity_sha256, config_json, config_sha256,
                        status, generation, task_plan_sha256, task_count, current_oid,
                        stop_requested, detail, created_ns, updated_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    migration,
                )
            self._connection.execute(
                """
                INSERT INTO tasks(
                    run_id, article_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                )
                SELECT run_id, node_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                FROM tasks_v1
                """
            )
            self._connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, run_id, article_id, phase, number, status, worktree_path,
                    branch, base_oid, backend, claim_key, claim_token_json, candidate_oid,
                    detail, started_ns, finished_ns
                )
                SELECT attempt_id, run_id, node_id, phase, number, status, worktree_path,
                    branch, base_oid, lower(backend), claim_key, claim_token_json, candidate_oid,
                    detail, started_ns, finished_ns
                FROM attempts_v1
                """
            )
            self._connection.execute(
                """
                INSERT INTO gates(attempt_id, name, passed, evidence_sha256, detail, created_ns)
                SELECT attempt_id, name, passed, evidence_sha256, detail, created_ns FROM gates_v1
                """
            )
            self._connection.execute(
                """
                INSERT INTO merge_items(
                    queue_item_id, run_id, attempt_id, queue_ref, expected_target_oid,
                    candidate_oid, status, generation, integrated_oid, detail, created_ns, updated_ns
                )
                SELECT queue_item_id, run_id, attempt_id, queue_ref, expected_target_oid,
                    candidate_oid, status, 0, integrated_oid, '', created_ns, updated_ns
                FROM merge_items_v1
                """
            )
            self._connection.executemany(
                "INSERT INTO events(sequence, run_id, kind, payload_json, created_ns) VALUES (?, ?, ?, ?, ?)",
                events,
            )
            self._connection.execute(
                """
                INSERT INTO artifacts(sha256, kind, size, relative_path, created_ns)
                SELECT sha256, kind, size, relative_path, created_ns FROM artifacts_v1
                """
            )
            for row in self._connection.execute(
                "SELECT run_id, status, config_sha256 FROM runs ORDER BY run_id"
            ).fetchall():
                self._append_event(
                    row["run_id"],
                    "ledger.migrated",
                    {
                        "config_sha256": row["config_sha256"],
                        "from_schema": 1,
                        "status": row["status"],
                        "to_schema": LEDGER_SCHEMA_VERSION,
                    },
                    self._clock_ns(),
                )
            for table in reversed(renamed):
                self._connection.execute(f"DROP TABLE {table}_v1")
            self._connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                (str(LEDGER_SCHEMA_VERSION),),
            )
            _verify_schema(self._connection, _SCHEMA, version=LEDGER_SCHEMA_VERSION)
            self._validate_content_rows()

    def _migrate_v3(self) -> None:
        """Add append-only replay and verified target-adoption history."""

        with self._transaction():
            version = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version["value"] == str(LEDGER_SCHEMA_VERSION):
                return
            if version is None or version["value"] != "3":
                raise LedgerError("ledger schema changed while preparing the v3 migration")
            _verify_schema(self._connection, _SCHEMA_V3, version=3)
            _execute_schema(self._connection, _SCHEMA)
            now = self._clock_ns()
            self._connection.execute(
                """
                UPDATE runs SET status = 'failed', stop_requested = 0,
                    detail = 'v3 run cannot resume because attempt bases were not append-only',
                    updated_ns = ?
                WHERE status NOT IN ('complete', 'failed')
                """,
                (now,),
            )
            for row in self._connection.execute(
                "SELECT run_id, status, config_sha256 FROM runs ORDER BY run_id"
            ).fetchall():
                self._append_event(
                    row["run_id"],
                    "ledger.migrated",
                    {
                        "config_sha256": row["config_sha256"],
                        "from_schema": 3,
                        "status": row["status"],
                        "to_schema": LEDGER_SCHEMA_VERSION,
                    },
                    now,
                )
            self._connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                (str(LEDGER_SCHEMA_VERSION),),
            )
            _verify_schema(self._connection, _SCHEMA, version=LEDGER_SCHEMA_VERSION)
            self._validate_content_rows()

    def _prepare_v1_run_migrations(self) -> tuple[tuple[object, ...], ...]:
        prepared: list[tuple[object, ...]] = []
        rows = self._connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        for row in rows:
            run_id = row["run_id"]
            values = _decode_json_object(row["identity_json"], f"v1 run identity {run_id}")
            expected_fields = {
                "repository_id",
                "project_root",
                "target_ref",
                "base_oid",
                "runtime_revision",
                "coverage_revision",
                "source_artifact_sha256",
                "plugin_revision",
                "toolchain_fingerprint",
                "execution_input_sha256",
            }
            if set(values) != expected_fields or any(not isinstance(value, str) for value in values.values()):
                raise LedgerError(f"v1 run identity is invalid: {run_id}")
            legacy_sha = hashlib.sha256(_json_bytes(values)).hexdigest()
            if row["identity_sha256"] != legacy_sha:
                raise LedgerError(f"v1 run identity is inconsistent: {run_id}")
            base_oid = values["base_oid"]
            _validate_oid(base_oid)  # type: ignore[arg-type]
            backends = {
                _canonical_backend("v1 attempt backend", backend_row["backend"])
                for backend_row in self._connection.execute(
                    "SELECT DISTINCT backend FROM attempts WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            }
            if len(backends) > 1:
                raise LedgerError(f"v1 run used more than one prover backend: {run_id}")
            backend = next(iter(backends), "claude")
            reviewer_backend = "claude" if backend != "claude" else "codex"
            observed_attempt_limit = self._connection.execute(
                "SELECT COALESCE(MAX(attempts), 0) FROM tasks WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            _validate_nonnegative_integer("v1 task attempt count", observed_attempt_limit)
            # V1 did not persist these controller inputs. Historical defaults make the
            # archived record readable, while the terminal status below prevents resume.
            config = RunConfig(
                repository_id=values["repository_id"],  # type: ignore[arg-type]
                target_ref=values["target_ref"],  # type: ignore[arg-type]
                remote=values["repository_id"],  # type: ignore[arg-type]
                backend=backend,
                reviewer_backend=reviewer_backend,
                start_oid=base_oid,  # type: ignore[arg-type]
                plugin_version=values["plugin_revision"],  # type: ignore[arg-type]
                toolchain_fingerprint=values["toolchain_fingerprint"],  # type: ignore[arg-type]
                coverage_contract_sha256=values["coverage_revision"],  # type: ignore[arg-type]
                execution_input_sha256=values["execution_input_sha256"],  # type: ignore[arg-type]
                source_artifacts_sha256=values["source_artifact_sha256"],  # type: ignore[arg-type]
                gate_policy_version="legacy-v1",
                max_attempts=max(3, observed_attempt_limit),
                max_steers=3,
                timeout_seconds=1800.0,
                claim_ttl_seconds=1500.0,
                heartbeat_interval_seconds=300.0,
            )
            identity = RunIdentity(**values, config_sha256=config.sha256)  # type: ignore[arg-type]
            _validate_identity(identity)
            _validate_config_binding(identity, config)
            current_oid = base_oid
            integrated = self._connection.execute(
                """
                SELECT expected_target_oid, candidate_oid, integrated_oid
                FROM merge_items
                WHERE run_id = ? AND status = 'integrated'
                """,
                (run_id,),
            ).fetchall()
            current_oid = _reconstruct_integrated_oid(current_oid, integrated, run_id=run_id)
            status = row["status"]
            detail = row["detail"]
            stop_requested = row["stop_requested"]
            if status not in {"complete", "failed"}:
                status = "failed"
                stop_requested = 0
                detail = "v1 run cannot resume because its controller settings were not persisted"
            task_plan = self._stored_task_plan(run_id, "node_id")
            prepared.append(
                (
                    run_id,
                    _json_text(identity.as_dict()),
                    identity.sha256,
                    _json_text(config.as_dict()),
                    config.sha256,
                    status,
                    row["generation"],
                    _task_plan_sha256(task_plan),
                    len(task_plan),
                    current_oid,
                    stop_requested,
                    detail,
                    row["created_ns"],
                    row["updated_ns"],
                )
            )
        return tuple(prepared)

    def _prepare_v1_events(self) -> tuple[tuple[object, ...], ...]:
        """Add v3 chain fields only when a v1 event and merge item jointly prove them."""
        prepared: list[tuple[object, ...]] = []
        rows = self._connection.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        for row in rows:
            payload_json = row["payload_json"]
            if row["kind"] == "candidate.integrated":
                payload = _decode_json_object(
                    payload_json,
                    f"v1 integration event {row['sequence']} payload",
                )
                queue_item_id = payload.get("queue_item_id")
                integrated_oid = payload.get("integrated_oid")
                item = self._connection.execute(
                    """
                    SELECT run_id, expected_target_oid, candidate_oid, integrated_oid, status
                    FROM merge_items WHERE queue_item_id = ?
                    """,
                    (queue_item_id,),
                ).fetchone()
                if (
                    not isinstance(queue_item_id, str)
                    or not isinstance(integrated_oid, str)
                    or item is None
                    or item["run_id"] != row["run_id"]
                    or item["status"] != "integrated"
                    or item["candidate_oid"] != integrated_oid
                    or item["integrated_oid"] != integrated_oid
                    or (
                        "expected_target_oid" in payload
                        and payload["expected_target_oid"] != item["expected_target_oid"]
                    )
                ):
                    raise LedgerError(
                        f"v1 integration event does not match its merge item: {row['run_id']}"
                    )
                payload["expected_target_oid"] = item["expected_target_oid"]
                payload_json = _json_text(payload)
            prepared.append(
                (
                    row["sequence"],
                    row["run_id"],
                    row["kind"],
                    payload_json,
                    row["created_ns"],
                )
            )
        return tuple(prepared)

    def _migrate_v2(self) -> None:
        renamed = (
            "runs",
            "tasks",
            "attempts",
            "gates",
            "merge_items",
            "events",
            "artifacts",
        )
        with self._transaction():
            version = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version["value"] == str(LEDGER_SCHEMA_VERSION):
                return
            if version is None or version["value"] != "2":
                raise LedgerError("ledger schema changed while preparing the v2 migration")
            _verify_schema(self._connection, _SCHEMA_V2, version=2)
            migrations = self._prepare_v2_run_migrations()
            self._connection.execute("DROP TRIGGER events_no_update")
            self._connection.execute("DROP TRIGGER events_no_delete")
            self._connection.execute("DROP TRIGGER runs_identity_no_update")
            for table in renamed:
                self._connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v2")
            _execute_schema(self._connection, _SCHEMA)
            for migration in migrations:
                self._connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, identity_json, identity_sha256, config_json, config_sha256,
                        status, generation, task_plan_sha256, task_count, current_oid,
                        stop_requested, detail, created_ns, updated_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    migration,
                )
            self._connection.execute(
                """
                INSERT INTO tasks(
                    run_id, article_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                )
                SELECT run_id, article_id, phase, status, attempts, generation,
                    blocked_by_json, detail, candidate_oid, integrated_oid
                FROM tasks_v2
                """
            )
            self._connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, run_id, article_id, phase, number, status, worktree_path,
                    branch, base_oid, backend, claim_key, claim_token_json, candidate_oid,
                    detail, started_ns, finished_ns
                )
                SELECT attempt_id, run_id, article_id, phase, number, status, worktree_path,
                    branch, base_oid, lower(backend), claim_key, claim_token_json, candidate_oid,
                    detail, started_ns, finished_ns
                FROM attempts_v2
                """
            )
            self._normalize_v2_exhausted_retries()
            self._connection.execute(
                """
                INSERT INTO gates(attempt_id, name, passed, evidence_sha256, detail, created_ns)
                SELECT attempt_id, name, passed, evidence_sha256, detail, created_ns FROM gates_v2
                """
            )
            self._connection.execute(
                """
                INSERT INTO merge_items(
                    queue_item_id, run_id, attempt_id, queue_ref, expected_target_oid,
                    candidate_oid, status, generation, integrated_oid, detail, created_ns, updated_ns
                )
                SELECT queue_item_id, run_id, attempt_id, queue_ref, expected_target_oid,
                    candidate_oid, status, generation, integrated_oid, detail, created_ns, updated_ns
                FROM merge_items_v2
                """
            )
            self._connection.execute(
                """
                INSERT INTO events(sequence, run_id, kind, payload_json, created_ns)
                SELECT sequence, run_id, kind, payload_json, created_ns FROM events_v2
                """
            )
            self._connection.execute(
                """
                INSERT INTO artifacts(sha256, kind, size, relative_path, created_ns)
                SELECT sha256, kind, size, relative_path, created_ns FROM artifacts_v2
                """
            )
            for row in self._connection.execute(
                "SELECT run_id, status, config_sha256 FROM runs ORDER BY run_id"
            ).fetchall():
                self._append_event(
                    row["run_id"],
                    "ledger.migrated",
                    {
                        "config_sha256": row["config_sha256"],
                        "from_schema": 2,
                        "status": row["status"],
                        "to_schema": LEDGER_SCHEMA_VERSION,
                    },
                    self._clock_ns(),
                )
            for table in reversed(renamed):
                self._connection.execute(f"DROP TABLE {table}_v2")
            self._connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                (str(LEDGER_SCHEMA_VERSION),),
            )
            _verify_schema(self._connection, _SCHEMA, version=LEDGER_SCHEMA_VERSION)
            self._validate_content_rows()

    def _normalize_v2_exhausted_retries(self) -> None:
        """Close retry states that the persisted v2 attempt policy cannot resume."""
        for row in self._connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall():
            run = _run_record(row)
            self._connection.execute(
                """
                UPDATE attempts SET status = 'failed'
                WHERE run_id = ? AND status = 'retrying' AND EXISTS (
                    SELECT 1 FROM tasks
                    WHERE tasks.run_id = attempts.run_id
                        AND tasks.article_id = attempts.article_id
                        AND tasks.phase = attempts.phase
                        AND tasks.status = 'retrying'
                        AND tasks.attempts = attempts.number
                        AND tasks.attempts >= ?
                )
                """,
                (run.run_id, run.config.max_attempts),
            )
            self._connection.execute(
                """
                UPDATE tasks SET status = 'failed'
                WHERE run_id = ? AND status = 'retrying' AND attempts >= ?
                """,
                (run.run_id, run.config.max_attempts),
            )

    def _prepare_v2_run_migrations(self) -> tuple[tuple[object, ...], ...]:
        prepared: list[tuple[object, ...]] = []
        rows = self._connection.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
        for row in rows:
            run_id = row["run_id"]
            identity_values = _decode_json_object(row["identity_json"], f"v2 run identity {run_id}")
            config_values = _decode_json_object(row["config_json"], f"v2 run config {run_id}")
            if hashlib.sha256(_json_bytes(identity_values)).hexdigest() != row["identity_sha256"]:
                raise LedgerError(f"v2 run identity is inconsistent: {run_id}")
            old_config_sha256 = hashlib.sha256(_json_bytes(config_values)).hexdigest()
            if old_config_sha256 != row["config_sha256"]:
                raise LedgerError(f"v2 run config is inconsistent: {run_id}")
            if identity_values.get("config_sha256") != old_config_sha256:
                raise LedgerError(f"v2 run identity does not bind its config: {run_id}")
            try:
                config_values["backend"] = _canonical_backend("backend", config_values.get("backend"))
                config_values["reviewer_backend"] = _canonical_backend(
                    "reviewer backend", config_values.get("reviewer_backend")
                )
                config = RunConfig(**config_values)
                identity_values["config_sha256"] = config.sha256
                identity = RunIdentity(**identity_values)
            except (TypeError, ValueError, LedgerError) as error:
                raise LedgerError(f"v2 run identity or config is invalid: {run_id}") from error
            _validate_identity(identity)
            _validate_config_binding(identity, config)
            status = row["status"]
            detail = row["detail"]
            stop_requested = row["stop_requested"]
            if status not in {"complete", "failed"}:
                status = "failed"
                stop_requested = 0
                detail = "v2 run cannot resume because its complete task plan was not atomically persisted"
            task_plan = self._stored_task_plan(run_id, "article_id")
            prepared.append(
                (
                    run_id,
                    _json_text(identity.as_dict()),
                    identity.sha256,
                    _json_text(config.as_dict()),
                    config.sha256,
                    status,
                    row["generation"],
                    _task_plan_sha256(task_plan),
                    len(task_plan),
                    row["current_oid"],
                    stop_requested,
                    detail,
                    row["created_ns"],
                    row["updated_ns"],
                )
            )
        return tuple(prepared)

    def _stored_task_plan(self, run_id: str, column: str) -> tuple[tuple[str, str], ...]:
        if column not in {"article_id", "node_id"}:
            raise LedgerError(f"unsupported task identity column: {column}")
        rows = self._connection.execute(
            f"SELECT {_quote_identifier(column)}, phase FROM tasks WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        return _canonical_task_plan(tuple((row[column], row["phase"]) for row in rows))

    def _validate_content_rows(self) -> None:
        run_backends: dict[str, str] = {}
        run_plans: dict[str, RunRecord] = {}
        for row in self._connection.execute("SELECT * FROM runs").fetchall():
            run = _run_record(row)
            run_backends[run.run_id] = run.config.backend
            run_plans[run.run_id] = run
        tasks_by_key: dict[tuple[str, str, str], TaskRecord] = {}
        tasks_by_run: dict[str, list[tuple[str, str]]] = {run_id: [] for run_id in run_plans}
        for row in self._connection.execute("SELECT * FROM tasks").fetchall():
            task = _task_record(row)
            tasks_by_key[(task.run_id, task.article_id, task.phase)] = task
            tasks_by_run[task.run_id].append((task.article_id, task.phase))
        for run_id, run in run_plans.items():
            task_plan = tuple(sorted(tasks_by_run[run_id]))
            if run.task_count != len(task_plan) or run.task_plan_sha256 != _task_plan_sha256(task_plan):
                raise LedgerError(f"task plan binding is invalid: {run_id}")
        attempts_by_id: dict[str, AttemptRecord] = {}
        attempts_by_task: dict[tuple[str, str, str], list[AttemptRecord]] = {
            task_key: [] for task_key in tasks_by_key
        }
        for row in self._connection.execute("SELECT * FROM attempts").fetchall():
            attempt = _attempt_record(row)
            task_key = (attempt.run_id, attempt.article_id, attempt.phase)
            if task_key not in tasks_by_key or attempt.backend != run_backends[attempt.run_id]:
                raise LedgerError(f"attempt binding is invalid: {attempt.attempt_id}")
            attempts_by_id[attempt.attempt_id] = attempt
            attempts_by_task[task_key].append(attempt)
        for row in self._connection.execute("SELECT * FROM gates").fetchall():
            gate = _gate_record(row)
            attempt = attempts_by_id[gate.attempt_id]
            if attempt.status not in {"candidate", "integrated"}:
                raise LedgerError(f"gate is bound to a non-candidate attempt: {gate.attempt_id}:{gate.name}")
        merge_items_by_id: dict[str, MergeItemRecord] = {}
        merge_items_by_attempt: dict[str, MergeItemRecord] = {}
        for row in self._connection.execute("SELECT * FROM merge_items").fetchall():
            item = _merge_item_record(row)
            merge_items_by_id[item.queue_item_id] = item
            merge_items_by_attempt[item.attempt_id] = item
        merge_replays_by_id: dict[str, MergeReplayRecord] = {}
        merge_replays_by_item: dict[str, list[MergeReplayRecord]] = {
            queue_item_id: [] for queue_item_id in merge_items_by_id
        }
        for row in self._connection.execute(
            "SELECT * FROM merge_replays ORDER BY queue_item_id, ordinal"
        ).fetchall():
            replay = _merge_replay_record(row)
            if replay.queue_item_id not in merge_items_by_id:
                raise LedgerError(f"merge replay has no merge item: {replay.replay_id}")
            merge_replays_by_id[replay.replay_id] = replay
            merge_replays_by_item[replay.queue_item_id].append(replay)
        target_adoptions_by_id: dict[str, TargetAdoptionRecord] = {}
        for row in self._connection.execute(
            "SELECT * FROM target_adoptions ORDER BY created_ns, adoption_id"
        ).fetchall():
            adoption = _target_adoption_record(row)
            if adoption.run_id not in run_plans:
                raise LedgerError(f"target adoption has no run: {adoption.adoption_id}")
            target_adoptions_by_id[adoption.adoption_id] = adoption
        external_integrations_by_task: dict[tuple[str, str, str], ExternalIntegrationRecord] = {}
        for row in self._connection.execute(
            "SELECT * FROM external_integrations ORDER BY adoption_id, article_id, phase"
        ).fetchall():
            integration = _external_integration_record(row)
            adoption = target_adoptions_by_id.get(integration.adoption_id)
            task_key = (integration.run_id, integration.article_id, integration.phase)
            if (
                adoption is None
                or adoption.run_id != integration.run_id
                or adoption.target_oid != integration.integrated_oid
                or task_key not in tasks_by_key
            ):
                raise LedgerError(
                    f"external integration binding is invalid: {integration.adoption_id}:"
                    f"{integration.article_id}:{integration.phase}"
                )
            external_integrations_by_task[task_key] = integration
        for task_key, integration in external_integrations_by_task.items():
            if integration.phase != "proof":
                continue
            statement = tasks_by_key.get((integration.run_id, integration.article_id, "statement"))
            if statement is None:
                raise LedgerError(
                    f"external proof integration has no statement task: {integration.article_id}"
                )
            if statement.status != "integrated":
                raise LedgerError(
                    f"external proof integration has an unintegrated statement: {integration.article_id}"
                )
        events_by_run: dict[str, list[EventRecord]] = {run_id: [] for run_id in run_plans}
        for row in self._connection.execute("SELECT * FROM events ORDER BY sequence").fetchall():
            event = _event_record(row)
            events_by_run[event.run_id].append(event)
        for row in self._connection.execute("SELECT * FROM artifacts").fetchall():
            _validate_artifact_row(row)
        bad_gate = self._connection.execute(
            """
            SELECT gates.attempt_id, gates.name FROM gates
            LEFT JOIN artifacts ON artifacts.sha256 = gates.evidence_sha256
            WHERE artifacts.sha256 IS NULL LIMIT 1
            """
        ).fetchone()
        if bad_gate is not None:
            raise LedgerError(f"gate evidence is missing: {bad_gate['attempt_id']}:{bad_gate['name']}")
        bad_merge = self._connection.execute(
            """
            SELECT merge_items.queue_item_id FROM merge_items
            JOIN attempts USING(attempt_id)
            WHERE merge_items.run_id != attempts.run_id
                OR merge_items.candidate_oid != attempts.candidate_oid
                OR merge_items.expected_target_oid != attempts.base_oid
            LIMIT 1
            """
        ).fetchone()
        if bad_merge is not None:
            raise LedgerError(f"merge item binding is invalid: {bad_merge['queue_item_id']}")
        _validate_lifecycle_rows(
            run_plans,
            tasks_by_key,
            attempts_by_task,
            attempts_by_id,
            merge_items_by_id,
            merge_items_by_attempt,
            merge_replays_by_id,
            merge_replays_by_item,
            target_adoptions_by_id,
            external_integrations_by_task,
            events_by_run,
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._connection.in_transaction:
            yield
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown run: {run_id}")
        return row

    def _active_run_row(self, run_id: str, expected_generation: int) -> sqlite3.Row:
        run = self._run_row(run_id)
        if run["generation"] != expected_generation:
            raise GenerationConflict(
                f"run {run_id} is at generation {run['generation']}, expected {expected_generation}"
            )
        if run["stop_requested"]:
            raise InvalidTransition("run has a pending stop request")
        if run["status"] != "running":
            raise InvalidTransition(f"run is not accepting mutations: {run['status']}")
        return run

    def _task_row(self, run_id: str, article_id: str, phase: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE run_id = ? AND article_id = ? AND phase = ?",
            (run_id, article_id, phase),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown task: {article_id}:{phase}")
        return row

    def _attempt_row(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown attempt: {attempt_id}")
        return row

    def _merge_item_row(self, queue_item_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM merge_items WHERE queue_item_id = ?", (queue_item_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown merge item: {queue_item_id}")
        return row

    def _merge_replay_row(self, replay_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM merge_replays WHERE replay_id = ?", (replay_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown merge replay: {replay_id}")
        return row

    def _append_event(self, run_id: str, kind: str, payload: Mapping[str, object], created_ns: int) -> None:
        self._connection.execute(
            "INSERT INTO events(run_id, kind, payload_json, created_ns) VALUES (?, ?, ?, ?)",
            (run_id, kind, _json_text(dict(payload)), created_ns),
        )


def _reconstruct_integrated_oid(
    start_oid: str,
    items: Iterable[Mapping[str, object]],
    *,
    run_id: str,
) -> str:
    successors: dict[str, str] = {}
    for item in items:
        expected_target_oid = item["expected_target_oid"]
        candidate_oid = item["candidate_oid"]
        integrated_oid = item["integrated_oid"]
        if not isinstance(expected_target_oid, str) or not isinstance(candidate_oid, str):
            raise LedgerError(f"integration history is malformed: {run_id}")
        _validate_oid(expected_target_oid)
        _validate_oid(candidate_oid)
        if candidate_oid != integrated_oid:
            raise LedgerError(f"integration history is inconsistent: {run_id}")
        if expected_target_oid in successors:
            raise LedgerError(f"integration history is ambiguous: {run_id}")
        successors[expected_target_oid] = candidate_oid
    current_oid = start_oid
    while current_oid in successors:
        current_oid = successors.pop(current_oid)
    if successors:
        raise LedgerError(f"integration history is ambiguous: {run_id}")
    return current_oid


def _validate_attempt_queue_event_history(
    runs: Mapping[str, RunRecord],
    attempts: Mapping[str, AttemptRecord],
    merge_items: Mapping[str, MergeItemRecord],
    events_by_run: Mapping[str, list[EventRecord]],
) -> None:
    """Bind mutable lifecycle rows to their append-only creation events."""

    migrated_runs: set[str] = set()
    for run_id, events in events_by_run.items():
        for event in events:
            if (
                event.kind == "ledger.migrated"
                and event.payload.get("from_schema") in {1, 2, 3}
                and event.payload.get("to_schema") == LEDGER_SCHEMA_VERSION
            ):
                migrated_runs.add(run_id)
    legacy_terminal_runs = {
        run_id
        for run_id in migrated_runs
        if runs[run_id].status in {"complete", "failed"}
    }

    start_sequence: dict[str, int] = {}
    finish_sequence: dict[str, int] = {}
    queue_sequence: dict[str, int] = {}
    integration_sequence: dict[str, int] = {}
    for run_id, events in events_by_run.items():
        allow_legacy = run_id in legacy_terminal_runs
        for event in events:
            if event.kind == "attempt.started":
                attempt_id = event.payload.get("attempt_id")
                attempt = attempts.get(attempt_id) if isinstance(attempt_id, str) else None
                if attempt is None or attempt.run_id != run_id or attempt_id in start_sequence:
                    raise LedgerError(f"attempt start event is invalid: {run_id}")
                expected = {
                    "article_id": attempt.article_id,
                    "attempt_id": attempt.attempt_id,
                    "base_oid": attempt.base_oid,
                    "number": attempt.number,
                    "phase": attempt.phase,
                }
                legacy_payloads = (
                    {
                        "article_id": attempt.article_id,
                        "attempt_id": attempt.attempt_id,
                        "number": attempt.number,
                        "phase": attempt.phase,
                    },
                    {
                        "attempt_id": attempt.attempt_id,
                        "node_id": attempt.article_id,
                        "number": attempt.number,
                        "phase": attempt.phase,
                    },
                )
                if (
                    event.payload != expected
                    and (not allow_legacy or event.payload not in legacy_payloads)
                ) or (not allow_legacy and event.created_ns != attempt.started_ns):
                    raise LedgerError(f"attempt start event disagrees with its row: {attempt_id}")
                start_sequence[attempt_id] = event.sequence
                continue
            if event.kind in {"attempt.finished", "attempt.recovered"}:
                attempt_id = event.payload.get("attempt_id")
                attempt = attempts.get(attempt_id) if isinstance(attempt_id, str) else None
                if attempt is None or attempt.run_id != run_id:
                    raise LedgerError(f"attempt finish event is invalid: {run_id}")
                if event.payload.get("outcome") != "candidate":
                    continue
                if attempt_id in finish_sequence:
                    raise LedgerError(f"attempt has duplicate candidate finish events: {attempt_id}")
                if (
                    event.payload.get("candidate_oid") != attempt.candidate_oid
                    or event.payload.get("detail") != attempt.detail
                    or (not allow_legacy and event.created_ns != attempt.finished_ns)
                ):
                    raise LedgerError(f"candidate finish event disagrees with its row: {attempt_id}")
                finish_sequence[attempt_id] = event.sequence
                continue
            if event.kind == "candidate.queued":
                queue_item_id = event.payload.get("queue_item_id")
                item = merge_items.get(queue_item_id) if isinstance(queue_item_id, str) else None
                if item is None or item.run_id != run_id or queue_item_id in queue_sequence:
                    raise LedgerError(f"candidate queue event is invalid: {run_id}")
                expected = {
                    "attempt_id": item.attempt_id,
                    "candidate_oid": item.candidate_oid,
                    "expected_target_oid": item.expected_target_oid,
                    "queue_item_id": item.queue_item_id,
                    "queue_ref": item.queue_ref,
                }
                legacy = {
                    "attempt_id": item.attempt_id,
                    "queue_item_id": item.queue_item_id,
                    "queue_ref": item.queue_ref,
                }
                if (
                    event.payload != expected and (not allow_legacy or event.payload != legacy)
                ) or (not allow_legacy and event.created_ns != item.created_ns):
                    raise LedgerError(
                        f"candidate queue event disagrees with its row: {queue_item_id}"
                    )
                queue_sequence[queue_item_id] = event.sequence
                continue
            if event.kind == "candidate.integrated":
                queue_item_id = event.payload.get("queue_item_id")
                if isinstance(queue_item_id, str):
                    integration_sequence.setdefault(queue_item_id, event.sequence)

    for attempt_id, attempt in attempts.items():
        if attempt_id not in start_sequence:
            if attempt.run_id in legacy_terminal_runs:
                continue
            raise LedgerError(f"attempt has no append-only start event: {attempt_id}")
    for queue_item_id, item in merge_items.items():
        start = start_sequence.get(item.attempt_id)
        finish = finish_sequence.get(item.attempt_id)
        queued = queue_sequence.get(queue_item_id)
        if item.run_id in legacy_terminal_runs and None in {start, finish, queued}:
            continue
        if start is None:
            raise LedgerError(f"queued candidate has no start event: {item.attempt_id}")
        if finish is None:
            raise LedgerError(f"queued candidate has no finish event: {item.attempt_id}")
        if queued is None:
            raise LedgerError(f"merge item has no append-only queue event: {queue_item_id}")
        if not start < finish < queued:
            raise LedgerError(f"candidate lifecycle event order is invalid: {queue_item_id}")
        integrated = integration_sequence.get(queue_item_id)
        if integrated is not None and integrated <= queued:
            raise LedgerError(f"candidate integration precedes its queue event: {queue_item_id}")


def _validate_replay_event_history(
    merge_items: Mapping[str, MergeItemRecord],
    replays: Mapping[str, MergeReplayRecord],
    events_by_run: Mapping[str, list[EventRecord]],
) -> None:
    relevant: dict[str, list[EventRecord]] = {replay_id: [] for replay_id in replays}
    for run_id, events in events_by_run.items():
        for event in events:
            if event.kind not in {
                "candidate.replay-prepared",
                "merge-replay.transition",
                "candidate.integrated",
            }:
                continue
            replay_id = event.payload.get("replay_id")
            if event.kind == "candidate.integrated" and replay_id is None:
                continue
            if not isinstance(replay_id, str) or replay_id not in relevant:
                raise LedgerError(f"run replay event references an unknown replay: {run_id}")
            item = merge_items[replays[replay_id].queue_item_id]
            if item.run_id != run_id:
                raise LedgerError(f"run replay event has the wrong run: {replay_id}")
            relevant[replay_id].append(event)

    allowed = {
        "prepared": frozenset({"publishing", "stale", "uncertain", "failed"}),
        "publishing": frozenset({"stale", "uncertain", "failed"}),
    }
    for replay_id, replay in replays.items():
        item = merge_items[replay.queue_item_id]
        status = "prepared"
        generation = 0
        detail = ""
        updated_ns = replay.created_ns
        prepared = False
        integrated = False
        for event in relevant[replay_id]:
            if event.kind == "candidate.replay-prepared":
                expected = {
                    "candidate_oid": replay.candidate_oid,
                    "gate_evidence_sha256": replay.gate_evidence_sha256,
                    "queue_item_id": replay.queue_item_id,
                    "replay_id": replay_id,
                    "review_evidence_sha256": replay.review_evidence_sha256,
                    "target_oid": replay.target_oid,
                }
                if prepared or event.payload != expected or event.created_ns != replay.created_ns:
                    raise LedgerError(f"merge replay preparation event is invalid: {replay_id}")
                prepared = True
                continue
            if not prepared or integrated:
                raise LedgerError(f"merge replay event ordering is invalid: {replay_id}")
            if event.kind == "merge-replay.transition":
                if set(event.payload) != {"detail", "from", "replay_id", "to"}:
                    raise LedgerError(f"merge replay transition event is malformed: {replay_id}")
                next_status = event.payload.get("to")
                next_detail = event.payload.get("detail")
                if (
                    event.payload.get("from") != status
                    or not isinstance(next_status, str)
                    or next_status not in allowed.get(status, frozenset())
                    or not isinstance(next_detail, str)
                    or not next_detail.strip()
                ):
                    raise LedgerError(f"merge replay transition event is invalid: {replay_id}")
                status = next_status
                detail = next_detail
                generation += 1
                updated_ns = event.created_ns
                continue
            expected = {
                "expected_target_oid": replay.target_oid,
                "integrated_oid": replay.candidate_oid,
                "original_candidate_oid": item.candidate_oid,
                "publication_evidence_sha256": replay.publication_evidence_sha256,
                "queue_item_id": replay.queue_item_id,
                "replay_id": replay_id,
            }
            if status != "publishing" or event.payload != expected:
                raise LedgerError(f"merge replay integration event is invalid: {replay_id}")
            status = "integrated"
            detail = "atomic replay publication verified"
            generation += 1
            updated_ns = event.created_ns
            integrated = True
        if not prepared:
            raise LedgerError(f"merge replay has no preparation event: {replay_id}")
        if (
            replay.status != status
            or replay.generation != generation
            or replay.detail != detail
            or replay.updated_ns != updated_ns
        ):
            raise LedgerError(f"merge replay row disagrees with its event history: {replay_id}")


def _target_adoption_event_tasks(value: object, *, run_id: str) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        raise LedgerError(f"run target-adoption event has invalid tasks: {run_id}")
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"article_id", "phase"}:
            raise LedgerError(f"run target-adoption event has invalid tasks: {run_id}")
        article_id = item.get("article_id")
        phase = item.get("phase")
        if not isinstance(article_id, str) or not isinstance(phase, str) or phase not in _PHASES:
            raise LedgerError(f"run target-adoption event has invalid tasks: {run_id}")
        _validate_article_id(article_id)
        result.append((article_id, phase))
    if result != sorted(set(result)):
        raise LedgerError(f"run target-adoption event tasks are not canonical: {run_id}")
    return result


def _validate_lifecycle_rows(
    runs: Mapping[str, RunRecord],
    tasks: Mapping[tuple[str, str, str], TaskRecord],
    attempts_by_task: Mapping[tuple[str, str, str], list[AttemptRecord]],
    attempts_by_id: Mapping[str, AttemptRecord],
    merge_items_by_id: Mapping[str, MergeItemRecord],
    merge_items_by_attempt: Mapping[str, MergeItemRecord],
    merge_replays_by_id: Mapping[str, MergeReplayRecord],
    merge_replays_by_item: Mapping[str, list[MergeReplayRecord]],
    target_adoptions_by_id: Mapping[str, TargetAdoptionRecord],
    external_integrations_by_task: Mapping[tuple[str, str, str], ExternalIntegrationRecord],
    events_by_run: Mapping[str, list[EventRecord]],
) -> None:
    _validate_attempt_queue_event_history(
        runs,
        attempts_by_id,
        merge_items_by_id,
        events_by_run,
    )
    _validate_replay_event_history(merge_items_by_id, merge_replays_by_id, events_by_run)
    tasks_for_run: dict[str, list[TaskRecord]] = {run_id: [] for run_id in runs}
    integrated_items_for_run: dict[str, set[str]] = {run_id: set() for run_id in runs}
    for task_key, task in tasks.items():
        run = runs[task.run_id]
        tasks_for_run[task.run_id].append(task)
        external = external_integrations_by_task.get(task_key)
        attempts = sorted(attempts_by_task[task_key], key=lambda attempt: attempt.number)
        numbers = tuple(attempt.number for attempt in attempts)
        if numbers != tuple(range(1, task.attempts + 1)):
            raise LedgerError(f"task attempt history is incomplete: {task.article_id}:{task.phase}")
        if task.attempts > run.config.max_attempts:
            raise LedgerError(f"task exceeds its configured attempt limit: {task.article_id}:{task.phase}")
        if task.status == "pending":
            if attempts:
                raise LedgerError(f"pending task has attempt history: {task.article_id}:{task.phase}")
            continue
        if external is not None:
            if task.status != "integrated" or task.integrated_oid != external.integrated_oid:
                raise LedgerError(
                    f"external integration disagrees with task: {task.article_id}:{task.phase}"
                )
            if any(attempt.status == "running" for attempt in attempts):
                raise LedgerError(
                    f"externally integrated task retains a running attempt: {task.article_id}:{task.phase}"
                )
            continue
        if not attempts:
            raise LedgerError(f"active task has no attempt history: {task.article_id}:{task.phase}")
        latest = attempts[-1]
        expected_latest_statuses = {
            "running": frozenset({"running"}),
            "candidate": frozenset({"candidate"}),
            "queued": frozenset({"candidate"}),
            "integrated": frozenset({"integrated"}),
            "retrying": frozenset({"retrying", "candidate", "stopped", "interrupted"}),
            "blocked": frozenset({"candidate", "interrupted"}),
            "failed": frozenset({"candidate", "failed", "interrupted", "stopped"}),
            "stopped": frozenset({"candidate", "interrupted", "stopped"}),
        }[task.status]
        if latest.status not in expected_latest_statuses:
            raise LedgerError(f"task and latest attempt lifecycle disagree: {task.article_id}:{task.phase}")
        running_attempts = [attempt for attempt in attempts if attempt.status == "running"]
        if task.status == "running":
            if running_attempts != [latest]:
                raise LedgerError(f"running task does not have one current attempt: {task.article_id}:{task.phase}")
        elif running_attempts:
            raise LedgerError(f"inactive task retains a running attempt: {task.article_id}:{task.phase}")
        if task.status == "retrying" and task.attempts >= run.config.max_attempts:
            raise LedgerError(f"retrying task exhausted its configured attempts: {task.article_id}:{task.phase}")
        if (
            task.status == "failed"
            and latest.status != "failed"
            and task.attempts < run.config.max_attempts
        ):
            raise LedgerError(f"task failed before exhausting recoverable attempts: {task.article_id}:{task.phase}")
        if task.status in {"candidate", "queued", "integrated"} and (
            latest.candidate_oid is None or latest.candidate_oid != task.candidate_oid
        ):
            raise LedgerError(f"task candidate does not match its latest attempt: {task.article_id}:{task.phase}")
        if task.status in {"queued", "integrated"}:
            item = merge_items_by_attempt.get(latest.attempt_id)
            if item is None:
                raise LedgerError(f"task is missing its merge item: {task.article_id}:{task.phase}")
            if task.status == "queued" and item.status == "integrated":
                raise LedgerError(f"queued task has an integrated merge item: {task.article_id}:{task.phase}")
            if task.status == "integrated" and item.status != "integrated":
                raise LedgerError(f"integrated task has no integrated merge item: {task.article_id}:{task.phase}")

    for item in merge_items_by_id.values():
        attempt = attempts_by_id[item.attempt_id]
        task = tasks[(attempt.run_id, attempt.article_id, attempt.phase)]
        expected_attempt_status = "integrated" if item.status == "integrated" else "candidate"
        if attempt.status != expected_attempt_status:
            raise LedgerError(f"merge item and attempt lifecycle disagree: {item.queue_item_id}")
        if item.status == "integrated" and task.status != "integrated":
            raise LedgerError(f"integrated merge item is not bound to an integrated task: {item.queue_item_id}")
        if item.status == "integrated":
            integrated_items_for_run[item.run_id].add(item.queue_item_id)
        if item.status not in _MERGE_ITEM_TERMINAL_STATUSES and (
            attempt.number != task.attempts
            or task.status not in {"queued", "retrying", "blocked", "stopped"}
        ):
            raise LedgerError(f"unresolved merge item is not bound to the current task: {item.queue_item_id}")

        replays = merge_replays_by_item[item.queue_item_id]
        for replay in replays:
            if (
                len(replay.target_oid) != len(item.candidate_oid)
                or len(replay.candidate_oid) != len(item.candidate_oid)
                or replay.candidate_oid in {replay.target_oid, item.candidate_oid}
                or replay.gate_evidence_sha256 == replay.review_evidence_sha256
            ):
                raise LedgerError(f"merge replay identity is invalid: {replay.replay_id}")
        if tuple(replay.ordinal for replay in replays) != tuple(range(1, len(replays) + 1)):
            raise LedgerError(f"merge replay history is incomplete: {item.queue_item_id}")
        active = [replay for replay in replays if replay.status not in _MERGE_REPLAY_TERMINAL_STATUSES]
        if len(active) > 1 or (active and active != replays[-1:]):
            raise LedgerError(f"merge replay history overlaps: {item.queue_item_id}")
        if active and item.status != "stale":
            raise LedgerError(f"active merge replay does not belong to a stale item: {item.queue_item_id}")
        integrated_replays = [replay for replay in replays if replay.status == "integrated"]
        if item.status == "integrated" and item.integrated_oid != item.candidate_oid:
            if len(integrated_replays) != 1 or integrated_replays[0].candidate_oid != item.integrated_oid:
                raise LedgerError(f"merge item replay integration is inconsistent: {item.queue_item_id}")
        elif integrated_replays:
            raise LedgerError(f"merge item has unexpected integrated replay: {item.queue_item_id}")
        if integrated_replays and integrated_replays[0] is not replays[-1]:
            raise LedgerError(f"merge replay continued after integration: {item.queue_item_id}")

    for run_id, run in runs.items():
        run_tasks = tasks_for_run[run_id]
        run_adoption_records = sorted(
            (adoption for adoption in target_adoptions_by_id.values() if adoption.run_id == run_id),
            key=lambda adoption: adoption.ordinal,
        )
        if tuple(adoption.ordinal for adoption in run_adoption_records) != tuple(
            range(1, len(run_adoption_records) + 1)
        ):
            raise LedgerError(f"run target-adoption history is incomplete: {run_id}")
        if run.status == "created" and any(task.status != "pending" for task in run_tasks):
            raise LedgerError(f"created run contains active tasks: {run_id}")
        if run.status == "complete" and any(task.status != "integrated" for task in run_tasks):
            raise LedgerError(f"complete run contains unintegrated tasks: {run_id}")
        if run.status in {"complete", "failed"} and run.stop_requested:
            raise LedgerError(f"terminal run retains a stop request: {run_id}")

        current_oid = run.config.start_oid
        seen_items: set[str] = set()
        seen_adoptions: set[str] = set()
        for event in events_by_run[run_id]:
            if event.kind == "target.adopted":
                adoption_id = event.payload.get("adoption_id")
                adoption = (
                    target_adoptions_by_id.get(adoption_id)
                    if isinstance(adoption_id, str)
                    else None
                )
                expected_tasks = sorted(
                    (
                        integration.article_id,
                        integration.phase,
                    )
                    for integration in external_integrations_by_task.values()
                    if integration.adoption_id == adoption_id
                )
                payload_tasks = _target_adoption_event_tasks(
                    event.payload.get("satisfied_tasks"), run_id=run_id
                )
                if (
                    adoption is None
                    or adoption.run_id != run_id
                    or adoption_id in seen_adoptions
                    or set(event.payload)
                    != {
                        "adoption_id",
                        "evidence_sha256",
                        "ordinal",
                        "previous_oid",
                        "satisfied_tasks",
                        "target_oid",
                    }
                    or event.payload.get("previous_oid") != adoption.previous_oid
                    or event.payload.get("target_oid") != adoption.target_oid
                    or event.payload.get("evidence_sha256") != adoption.evidence_sha256
                    or event.payload.get("ordinal") != adoption.ordinal
                    or payload_tasks != expected_tasks
                    or adoption.previous_oid != current_oid
                    or len(adoption.target_oid) != len(current_oid)
                ):
                    raise LedgerError(f"run target-adoption event is invalid: {run_id}")
                current_oid = adoption.target_oid
                seen_adoptions.add(adoption_id)
                continue
            if event.kind != "candidate.integrated":
                continue
            queue_item_id = event.payload.get("queue_item_id")
            expected_target_oid = event.payload.get("expected_target_oid")
            integrated_oid = event.payload.get("integrated_oid")
            if not isinstance(queue_item_id, str) or queue_item_id in seen_items:
                raise LedgerError(f"run integration event is invalid: {run_id}")
            item = merge_items_by_id.get(queue_item_id)
            replay_id = event.payload.get("replay_id")
            replay = merge_replays_by_id.get(replay_id) if isinstance(replay_id, str) else None
            direct = replay_id is None
            expected_matches = (
                item is not None
                and event.payload
                == {
                    "expected_target_oid": item.expected_target_oid,
                    "integrated_oid": item.integrated_oid,
                    "queue_item_id": item.queue_item_id,
                }
                if direct and item is not None
                else item is not None
                and replay is not None
                and replay.queue_item_id == queue_item_id
                and replay.status == "integrated"
                and expected_target_oid == replay.target_oid
                and integrated_oid == replay.candidate_oid
                and event.payload.get("original_candidate_oid") == item.candidate_oid
            )
            if (
                item is None
                or item.run_id != run_id
                or item.status != "integrated"
                or integrated_oid != item.integrated_oid
                or not expected_matches
            ):
                raise LedgerError(f"run integration event does not match its merge item: {run_id}")
            if expected_target_oid != current_oid:
                raise LedgerError(f"run integration history is not linear: {run_id}")
            current_oid = integrated_oid
            seen_items.add(queue_item_id)
        run_adoptions = {adoption.adoption_id for adoption in run_adoption_records}
        if (
            seen_items != integrated_items_for_run[run_id]
            or seen_adoptions != run_adoptions
            or run.current_oid != current_oid
        ):
            raise LedgerError(f"run current OID does not match its integration history: {run_id}")


def _run_record(row: sqlite3.Row) -> RunRecord:
    run_id = row["run_id"]
    try:
        identity_values = _decode_json_object(row["identity_json"], f"run identity {run_id}")
        config_values = _decode_json_object(row["config_json"], f"run config {run_id}")
        identity = RunIdentity(**identity_values)
        config = RunConfig(**config_values)
        if _json_text(identity.as_dict()) != row["identity_json"]:
            raise LedgerError("run identity fields are incomplete")
        if _json_text(config.as_dict()) != row["config_json"]:
            raise LedgerError("run config fields are incomplete")
    except (KeyError, TypeError, ValueError, LedgerError) as error:
        raise LedgerError(f"run identity or config is invalid: {run_id}") from error
    _validate_identity(identity)
    _validate_config_binding(identity, config)
    if identity.sha256 != row["identity_sha256"]:
        raise LedgerError(f"run identity is inconsistent: {run_id}")
    if config.sha256 != row["config_sha256"]:
        raise LedgerError(f"run config is inconsistent: {run_id}")
    _validate_identifier("run id", run_id)
    _validate_oid(row["current_oid"])
    if row["status"] not in _RUN_STATUSES:
        raise LedgerError(f"run status is invalid: {run_id}")
    _validate_nonnegative_integer("run generation", row["generation"])
    _validate_sha256(row["task_plan_sha256"], "task plan")
    _validate_nonnegative_integer("task count", row["task_count"])
    if type(row["stop_requested"]) is not int or row["stop_requested"] not in (0, 1):
        raise LedgerError(f"run stop flag is invalid: {run_id}")
    _validate_timestamp("run creation time", row["created_ns"])
    _validate_timestamp("run update time", row["updated_ns"])
    if row["updated_ns"] < row["created_ns"]:
        raise LedgerError(f"run timestamps are inconsistent: {run_id}")
    return RunRecord(
        run_id=run_id,
        identity=identity,
        identity_sha256=row["identity_sha256"],
        config=config,
        config_sha256=row["config_sha256"],
        status=row["status"],
        generation=row["generation"],
        task_plan_sha256=row["task_plan_sha256"],
        task_count=row["task_count"],
        current_oid=row["current_oid"],
        stop_requested=bool(row["stop_requested"]),
        detail=row["detail"],
        created_ns=row["created_ns"],
        updated_ns=row["updated_ns"],
    )


def _task_record(row: sqlite3.Row) -> TaskRecord:
    label = f"task {row['run_id']}:{row['article_id']}:{row['phase']}"
    blockers = _decode_json_array(row["blocked_by_json"], f"{label} blockers")
    if any(not isinstance(blocker, str) for blocker in blockers):
        raise LedgerError(f"{label} blockers must contain only article IDs")
    canonical_blockers = tuple(sorted(set(blockers)))
    if tuple(blockers) != canonical_blockers:
        raise LedgerError(f"{label} blockers must be sorted and unique")
    for blocker in canonical_blockers:
        _validate_article_id(blocker)
    _validate_article_id(row["article_id"])
    if row["phase"] not in _PHASES or row["status"] not in _TASK_STATUSES:
        raise LedgerError(f"{label} lifecycle value is invalid")
    _validate_nonnegative_integer(f"{label} attempts", row["attempts"])
    _validate_nonnegative_integer(f"{label} generation", row["generation"])
    _validate_optional_oid(row["candidate_oid"])
    _validate_optional_oid(row["integrated_oid"])
    if row["status"] in {"candidate", "queued"} and row["candidate_oid"] is None:
        raise LedgerError(f"{label} is missing its candidate OID")
    if row["status"] == "integrated" and row["integrated_oid"] is None:
        raise LedgerError(f"{label} has no integrated OID")
    return TaskRecord(
        run_id=row["run_id"],
        article_id=row["article_id"],
        phase=row["phase"],
        status=row["status"],
        attempts=row["attempts"],
        generation=row["generation"],
        blocked_by=canonical_blockers,
        detail=row["detail"],
        candidate_oid=row["candidate_oid"],
        integrated_oid=row["integrated_oid"],
    )


def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
    label = f"attempt {row['attempt_id']}"
    claim_values = _decode_json_object(row["claim_token_json"], f"{label} claim token")
    try:
        claim_token = ArticleClaimToken(**claim_values)
    except (KeyError, TypeError, ValueError, LedgerError) as error:
        raise LedgerError(f"{label} claim token schema is invalid") from error
    if _json_text(claim_token.as_dict()) != row["claim_token_json"]:
        raise LedgerError(f"{label} claim token fields are incomplete")
    _validate_article_id(row["article_id"])
    if row["phase"] not in _PHASES or row["status"] not in {
        "running",
        "candidate",
        "retrying",
        "failed",
        "stopped",
        "interrupted",
        "integrated",
    }:
        raise LedgerError(f"{label} lifecycle value is invalid")
    if type(row["number"]) is not int or row["number"] < 1:
        raise LedgerError(f"{label} number is invalid")
    _validate_identifier("attempt id", row["attempt_id"])
    _validate_identifier("attempt branch", row["branch"])
    _validate_identifier("attempt backend", row["backend"])
    if row["claim_key"] != author_claim_key(row["article_id"]):
        raise LedgerError(f"{label} claim key is not anchored to its article ID")
    if claim_token.article_id != row["article_id"] or claim_token.claim_key != row["claim_key"]:
        raise LedgerError(f"{label} claim token does not match its article claim")
    if not Path(row["worktree_path"]).is_absolute():
        raise LedgerError(f"{label} worktree path must be absolute")
    _validate_oid(row["base_oid"])
    _validate_optional_oid(row["candidate_oid"])
    _validate_timestamp(f"{label} start time", row["started_ns"])
    if row["finished_ns"] is not None:
        _validate_timestamp(f"{label} finish time", row["finished_ns"])
        if row["finished_ns"] < row["started_ns"]:
            raise LedgerError(f"{label} timestamps are inconsistent")
    if row["status"] == "running" and row["finished_ns"] is not None:
        raise LedgerError(f"{label} is running with a finish time")
    if row["status"] != "running" and row["finished_ns"] is None:
        raise LedgerError(f"{label} is finished without a finish time")
    if row["status"] in {"candidate", "integrated"} and row["candidate_oid"] is None:
        raise LedgerError(f"{label} is missing its candidate OID")
    return AttemptRecord(
        attempt_id=row["attempt_id"],
        run_id=row["run_id"],
        article_id=row["article_id"],
        phase=row["phase"],
        number=row["number"],
        status=row["status"],
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        base_oid=row["base_oid"],
        backend=row["backend"],
        claim_key=row["claim_key"],
        claim_token=claim_token,
        candidate_oid=row["candidate_oid"],
        detail=row["detail"],
        started_ns=row["started_ns"],
        finished_ns=row["finished_ns"],
    )


def _gate_record(row: sqlite3.Row) -> GateRecord:
    _validate_identifier("gate name", row["name"])
    if type(row["passed"]) is not int or row["passed"] not in (0, 1):
        raise LedgerError(f"gate pass value is invalid: {row['attempt_id']}:{row['name']}")
    _validate_sha256(row["evidence_sha256"], "gate evidence")
    _validate_timestamp("gate creation time", row["created_ns"])
    return GateRecord(
        attempt_id=row["attempt_id"],
        name=row["name"],
        passed=bool(row["passed"]),
        evidence_sha256=row["evidence_sha256"],
        detail=row["detail"],
        created_ns=row["created_ns"],
    )


def _merge_item_record(row: sqlite3.Row) -> MergeItemRecord:
    label = f"merge item {row['queue_item_id']}"
    _validate_identifier("queue item id", row["queue_item_id"])
    _validate_identifier("queue ref", row["queue_ref"])
    _validate_oid(row["expected_target_oid"])
    _validate_oid(row["candidate_oid"])
    _validate_optional_oid(row["integrated_oid"])
    if row["status"] not in _MERGE_ITEM_STATUSES:
        raise LedgerError(f"{label} status is invalid")
    _validate_nonnegative_integer(f"{label} generation", row["generation"])
    if row["status"] == "integrated" and row["integrated_oid"] is None:
        raise LedgerError(f"{label} has no integrated OID")
    if row["status"] != "integrated" and row["integrated_oid"] is not None:
        raise LedgerError(f"{label} has an integrated OID before integration")
    _validate_timestamp(f"{label} creation time", row["created_ns"])
    _validate_timestamp(f"{label} update time", row["updated_ns"])
    if row["updated_ns"] < row["created_ns"]:
        raise LedgerError(f"{label} timestamps are inconsistent")
    return MergeItemRecord(
        queue_item_id=row["queue_item_id"],
        run_id=row["run_id"],
        attempt_id=row["attempt_id"],
        queue_ref=row["queue_ref"],
        expected_target_oid=row["expected_target_oid"],
        candidate_oid=row["candidate_oid"],
        status=row["status"],
        generation=row["generation"],
        integrated_oid=row["integrated_oid"],
        detail=row["detail"],
        created_ns=row["created_ns"],
        updated_ns=row["updated_ns"],
    )


def _merge_replay_record(row: sqlite3.Row) -> MergeReplayRecord:
    label = f"merge replay {row['replay_id']}"
    _validate_identifier("replay id", row["replay_id"])
    _validate_identifier("queue item id", row["queue_item_id"])
    _validate_nonnegative_integer(f"{label} ordinal", row["ordinal"])
    if row["ordinal"] < 1:
        raise LedgerError(f"{label} ordinal must be positive")
    _validate_oid(row["target_oid"])
    _validate_oid(row["candidate_oid"])
    _validate_sha256(row["gate_evidence_sha256"], f"{label} gate evidence")
    _validate_sha256(row["review_evidence_sha256"], f"{label} review evidence")
    if row["status"] not in _MERGE_REPLAY_STATUSES:
        raise LedgerError(f"{label} status is invalid")
    _validate_nonnegative_integer(f"{label} generation", row["generation"])
    publication = row["publication_evidence_sha256"]
    if publication is not None:
        _validate_sha256(publication, f"{label} publication evidence")
    if row["status"] == "integrated" and publication is None:
        raise LedgerError(f"{label} has no publication evidence")
    if row["status"] != "integrated" and publication is not None:
        raise LedgerError(f"{label} has publication evidence before integration")
    _validate_timestamp(f"{label} creation time", row["created_ns"])
    _validate_timestamp(f"{label} update time", row["updated_ns"])
    if row["updated_ns"] < row["created_ns"]:
        raise LedgerError(f"{label} timestamps are inconsistent")
    return MergeReplayRecord(
        replay_id=row["replay_id"],
        queue_item_id=row["queue_item_id"],
        ordinal=row["ordinal"],
        target_oid=row["target_oid"],
        candidate_oid=row["candidate_oid"],
        gate_evidence_sha256=row["gate_evidence_sha256"],
        review_evidence_sha256=row["review_evidence_sha256"],
        status=row["status"],
        generation=row["generation"],
        publication_evidence_sha256=publication,
        detail=row["detail"],
        created_ns=row["created_ns"],
        updated_ns=row["updated_ns"],
    )


def _target_adoption_record(row: sqlite3.Row) -> TargetAdoptionRecord:
    _validate_identifier("target adoption id", row["adoption_id"])
    _validate_identifier("target adoption run id", row["run_id"])
    _validate_nonnegative_integer("target adoption ordinal", row["ordinal"])
    if row["ordinal"] < 1:
        raise LedgerError(f"target adoption ordinal must be positive: {row['adoption_id']}")
    _validate_oid(row["previous_oid"])
    _validate_oid(row["target_oid"])
    if row["previous_oid"] == row["target_oid"]:
        raise LedgerError(f"target adoption does not advance: {row['adoption_id']}")
    _validate_sha256(row["evidence_sha256"], "target adoption evidence")
    _validate_timestamp("target adoption creation time", row["created_ns"])
    return TargetAdoptionRecord(
        adoption_id=row["adoption_id"],
        run_id=row["run_id"],
        ordinal=row["ordinal"],
        previous_oid=row["previous_oid"],
        target_oid=row["target_oid"],
        evidence_sha256=row["evidence_sha256"],
        created_ns=row["created_ns"],
    )


def _external_integration_record(row: sqlite3.Row) -> ExternalIntegrationRecord:
    _validate_identifier("target adoption id", row["adoption_id"])
    _validate_identifier("external integration run id", row["run_id"])
    _validate_article_id(row["article_id"])
    if row["phase"] not in _PHASES:
        raise LedgerError("external integration phase is invalid")
    _validate_oid(row["integrated_oid"])
    return ExternalIntegrationRecord(
        adoption_id=row["adoption_id"],
        run_id=row["run_id"],
        article_id=row["article_id"],
        phase=row["phase"],
        integrated_oid=row["integrated_oid"],
    )


def _event_record(row: sqlite3.Row) -> EventRecord:
    payload = _decode_json_object(row["payload_json"], f"event {row['sequence']} payload")
    if type(row["sequence"]) is not int or row["sequence"] < 1:
        raise LedgerError("event sequence is invalid")
    _validate_nonempty("event run id", row["run_id"])
    _validate_nonempty("event kind", row["kind"])
    _validate_timestamp("event creation time", row["created_ns"])
    return EventRecord(
        sequence=row["sequence"],
        run_id=row["run_id"],
        kind=row["kind"],
        payload=payload,
        created_ns=row["created_ns"],
    )


def _validate_identity(identity: RunIdentity) -> None:
    for field, value in identity.as_dict().items():
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise LedgerError(f"run identity field {field} must be a nonempty string")
    _validate_oid(identity.base_oid)
    if not Path(identity.project_root).is_absolute():
        raise LedgerError("run identity project_root must be absolute")
    for field in (
        "runtime_revision",
        "coverage_revision",
        "source_artifact_sha256",
        "toolchain_fingerprint",
        "execution_input_sha256",
        "config_sha256",
    ):
        _validate_sha256(getattr(identity, field), field)


def _validate_config_binding(identity: RunIdentity, config: RunConfig) -> None:
    if identity.config_sha256 != config.sha256:
        raise LedgerError("run identity does not match the run config digest")
    matching = (
        ("repository_id", identity.repository_id, config.repository_id),
        ("target_ref", identity.target_ref, config.target_ref),
        ("base_oid", identity.base_oid, config.start_oid),
        ("coverage_revision", identity.coverage_revision, config.coverage_contract_sha256),
        ("source_artifact_sha256", identity.source_artifact_sha256, config.source_artifacts_sha256),
        ("toolchain_fingerprint", identity.toolchain_fingerprint, config.toolchain_fingerprint),
        ("execution_input_sha256", identity.execution_input_sha256, config.execution_input_sha256),
    )
    for field, identity_value, config_value in matching:
        if identity_value != config_value:
            raise LedgerError(f"run identity {field} does not match the run config")


def _validate_nonempty(label: str, value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise LedgerError(f"{label} must be a nonempty canonical string")


def _validate_nonnegative_integer(label: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise LedgerError(f"{label} must be a nonnegative integer")


def _validate_timestamp(label: str, value: object) -> None:
    _validate_nonnegative_integer(label, value)


def _canonical_backend(label: str, value: object) -> str:
    if not isinstance(value, str):
        raise LedgerError(f"{label} is not a portable identifier: {value!r}")
    _validate_identifier(label, value)
    canonical = value.casefold()
    if canonical not in _BACKEND_IDS:
        choices = ", ".join(sorted(_BACKEND_IDS))
        raise LedgerError(f"{label} must be one of: {choices}")
    return canonical


def _positive_finite_number(label: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerError(f"{label} must be a finite positive number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise LedgerError(f"{label} must be a finite positive number") from error
    if not math.isfinite(normalized) or normalized <= 0:
        raise LedgerError(f"{label} must be a finite positive number")
    return normalized


def _validate_branch_ref(value: str) -> None:
    _validate_nonempty("target ref", value)
    if not value.startswith("refs/heads/") or not _IDENTIFIER.fullmatch(value):
        raise LedgerError(f"target ref must be a full branch ref: {value!r}")


def _validate_article_id(value: str) -> None:
    if not isinstance(value, str) or ARTICLE_ID_PATTERN.fullmatch(value) is None:
        raise LedgerError(f"task id must be a durable article_id: {value!r}")


def _validate_identifier(label: str, value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LedgerError(f"{label} is not a portable identifier: {value!r}")


def _validate_oid(value: str) -> None:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise LedgerError(f"invalid Git object id: {value!r}")


def _validate_optional_oid(value: object) -> None:
    if value is not None:
        _validate_oid(value)  # type: ignore[arg-type]


def _validate_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LedgerError(f"{label} is not a lowercase SHA-256 digest")


def _canonical_task_plan(tasks: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    plan: list[tuple[str, str]] = []
    for item in tasks:
        if not isinstance(item, tuple) or len(item) != 2:
            raise LedgerError("each task plan entry must be an (article_id, phase) tuple")
        article_id, phase = item
        _validate_article_id(article_id)
        if phase not in _PHASES:
            raise LedgerError(f"unknown work phase: {phase}")
        plan.append((article_id, phase))
    if len(set(plan)) != len(plan):
        raise LedgerError("task plan contains a duplicate task")
    return tuple(sorted(plan))


def _task_plan_sha256(tasks: Iterable[tuple[str, str]]) -> str:
    payload = [{"article_id": article_id, "phase": phase} for article_id, phase in tasks]
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _json_bytes(value: object) -> bytes:
    return _json_text(value).encode("utf-8")


def _json_text(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise LedgerError(f"value is not canonical JSON: {error}") from error


def _decode_json_value(raw: object, label: str) -> object:
    if not isinstance(raw, str):
        raise LedgerError(f"{label} is not stored as JSON text")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise LedgerError(f"{label} is malformed JSON") from error
    if _json_text(value) != raw:
        raise LedgerError(f"{label} is not canonical JSON")
    return value


def _decode_json_object(raw: object, label: str) -> dict[str, object]:
    value = _decode_json_value(raw, label)
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be a JSON object")
    return value


def _decode_json_array(raw: object, label: str) -> list[object]:
    value = _decode_json_value(raw, label)
    if not isinstance(value, list):
        raise LedgerError(f"{label} must be a JSON array")
    return value


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LedgerError(f"state path is not a real directory: {path}")


def _validate_private_regular_file(path: Path, metadata: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LedgerError(f"{label} path is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise LedgerError(f"{label} path is hard-linked: {path}")


def _validate_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        _validate_private_regular_file(sidecar, metadata, label="SQLite state file")


@contextmanager
def _open_private_subdirectory(root: Path, components: tuple[str, ...], *, create: bool) -> Iterator[int]:
    """Pin a real directory path below ``root`` without following component symlinks."""
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise LedgerError(f"state directory cannot be inspected: {root}") from error
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise LedgerError(f"state path is not a real directory: {root}")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    current_path = root
    try:
        try:
            descriptor = os.open(root, flags)
        except OSError as error:
            raise LedgerError(f"state directory cannot be opened safely: {root}") from error
        descriptors.append(descriptor)
        opened_root = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or (opened_root.st_dev, opened_root.st_ino) != (root_metadata.st_dev, root_metadata.st_ino)
        ):
            raise LedgerError(f"state directory changed while it was being opened: {root}")
        for component in components:
            if not component or component in {".", ".."} or "/" in component or "\x00" in component:
                raise LedgerError(f"invalid private state directory component: {component!r}")
            created = False
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                except OSError as error:
                    raise LedgerError(f"state directory cannot be created: {current_path / component}") from error
            try:
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise LedgerError(
                    f"state directory is missing or unsafe: {current_path / component}"
                ) from error
            descriptors.append(child_descriptor)
            child_metadata = os.fstat(child_descriptor)
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise LedgerError(f"state path is not a real directory: {current_path / component}")
            if created:
                os.fsync(descriptor)
            descriptor = child_descriptor
            current_path /= component
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _initialization_lock(path: Path) -> Iterator[None]:
    """Serialize schema discovery, migration, validation, and journal configuration."""
    if fcntl is None:  # pragma: no cover - SQLite remains the fallback on non-POSIX hosts
        yield
        return
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise LedgerError(f"ledger initialization lock cannot be opened: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise LedgerError(f"ledger initialization lock is not a private regular file: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise LedgerError("coordinator lock metadata could not be written")
        view = view[written:]


def _read_artifact_at(directory_descriptor: int, path: Path, digest: str, size: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise LedgerError(f"artifact cannot be inspected: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise LedgerError(f"artifact is not a private regular file: {path}")
        if metadata.st_size != size:
            raise LedgerError(f"artifact size changed: {digest}")
        chunks: list[bytes] = []
        remaining = size + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    except OSError as error:
        raise LedgerError(f"artifact cannot be read: {digest}") from error
    finally:
        os.close(descriptor)
    if len(content) != size:
        raise LedgerError(f"artifact size changed while reading: {digest}")
    if hashlib.sha256(content).hexdigest() != digest:
        raise LedgerError(f"artifact content changed: {digest}")
    return content


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise LedgerError("ledger schema contains an incomplete SQL statement")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_schema_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def _schema_signature(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        (
            row["type"],
            row["name"],
            row["tbl_name"],
            _normalize_schema_sql(row["sql"]),
        )
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    )
    tables = tuple(
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    )
    table_details: list[object] = []
    for table in tables:
        quoted_table = _quote_identifier(table)
        columns = tuple(tuple(row) for row in connection.execute(f"PRAGMA table_xinfo({quoted_table})"))
        foreign_keys = tuple(
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
        )
        indexes: list[object] = []
        for row in connection.execute(f"PRAGMA index_list({quoted_table})").fetchall():
            index_name = row["name"]
            index_columns = tuple(
                tuple(index_row)
                for index_row in connection.execute(
                    f"PRAGMA index_xinfo({_quote_identifier(index_name)})"
                )
            )
            indexes.append(
                (
                    index_name,
                    row["unique"],
                    row["origin"],
                    row["partial"],
                    index_columns,
                )
            )
        table_details.append((table, columns, foreign_keys, tuple(sorted(indexes))))
    return objects, tuple(table_details)


_EXPECTED_SCHEMA_SIGNATURES: dict[str, tuple[object, ...]] = {}


def _expected_schema_signature(script: str) -> tuple[object, ...]:
    signature = _EXPECTED_SCHEMA_SIGNATURES.get(script)
    if signature is not None:
        return signature
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        reference.execute("PRAGMA foreign_keys = ON")
        _execute_schema(reference, script)
        signature = _schema_signature(reference)
    finally:
        reference.close()
    _EXPECTED_SCHEMA_SIGNATURES[script] = signature
    return signature


def _verify_schema(connection: sqlite3.Connection, script: str, *, version: int) -> None:
    if _schema_signature(connection) != _expected_schema_signature(script):
        raise LedgerError(
            f"ledger schema {version} objects, columns, indexes, or foreign keys are invalid"
        )
    integrity = tuple(tuple(row) for row in connection.execute("PRAGMA integrity_check").fetchall())
    if integrity != (("ok",),):
        raise LedgerError(f"ledger schema {version} failed SQLite integrity checking")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise LedgerError(f"ledger schema {version} has broken foreign keys")


def _validate_artifact_row(row: sqlite3.Row) -> None:
    _validate_sha256(row["sha256"], "artifact")
    _validate_identifier("artifact kind", row["kind"])
    _validate_nonnegative_integer("artifact size", row["size"])
    _validate_nonempty("artifact relative path", row["relative_path"])
    expected = str(Path("artifacts") / "sha256" / row["sha256"][:2] / row["sha256"])
    if row["relative_path"] != expected:
        raise LedgerError(f"artifact path is inconsistent: {row['sha256']}")
    _validate_timestamp("artifact creation time", row["created_ns"])


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('created','running','complete','blocked','failed','stopped')),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    stop_requested INTEGER NOT NULL CHECK(stop_requested IN (0,1)),
    detail TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('statement','proof')),
    status TEXT NOT NULL CHECK(status IN ('pending','running','retrying','candidate','queued','integrated','blocked','failed','stopped')),
    attempts INTEGER NOT NULL CHECK(attempts >= 0),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    blocked_by_json TEXT NOT NULL,
    detail TEXT NOT NULL,
    candidate_oid TEXT,
    integrated_oid TEXT,
    PRIMARY KEY(run_id, node_id, phase)
);
CREATE TABLE IF NOT EXISTS attempts(
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    number INTEGER NOT NULL CHECK(number > 0),
    status TEXT NOT NULL CHECK(status IN ('running','candidate','retrying','failed','stopped','interrupted','integrated')),
    worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    backend TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    claim_token_json TEXT NOT NULL,
    candidate_oid TEXT,
    detail TEXT NOT NULL,
    started_ns INTEGER NOT NULL,
    finished_ns INTEGER,
    UNIQUE(run_id, node_id, phase, number),
    FOREIGN KEY(run_id, node_id, phase) REFERENCES tasks(run_id, node_id, phase) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS gates(
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0,1)),
    evidence_sha256 TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    PRIMARY KEY(attempt_id, name)
);
CREATE TABLE IF NOT EXISTS merge_items(
    queue_item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    queue_ref TEXT NOT NULL,
    expected_target_oid TEXT NOT NULL,
    candidate_oid TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','integrated','failed')),
    integrated_oid TEXT,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS artifacts(
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    relative_path TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
"""


_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
    run_id TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL,
    identity_sha256 TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('created','running','complete','blocked','failed','stopped')),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    current_oid TEXT NOT NULL,
    stop_requested INTEGER NOT NULL CHECK(stop_requested IN (0,1)),
    detail TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    article_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('statement','proof')),
    status TEXT NOT NULL CHECK(status IN ('pending','running','retrying','candidate','queued','integrated','blocked','failed','stopped')),
    attempts INTEGER NOT NULL CHECK(attempts >= 0),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    blocked_by_json TEXT NOT NULL,
    detail TEXT NOT NULL,
    candidate_oid TEXT,
    integrated_oid TEXT,
    PRIMARY KEY(run_id, article_id, phase)
);
CREATE TABLE IF NOT EXISTS attempts(
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    number INTEGER NOT NULL CHECK(number > 0),
    status TEXT NOT NULL CHECK(status IN ('running','candidate','retrying','failed','stopped','interrupted','integrated')),
    worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    backend TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    claim_token_json TEXT NOT NULL,
    candidate_oid TEXT,
    detail TEXT NOT NULL,
    started_ns INTEGER NOT NULL,
    finished_ns INTEGER,
    UNIQUE(run_id, article_id, phase, number),
    FOREIGN KEY(run_id, article_id, phase) REFERENCES tasks(run_id, article_id, phase) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS gates(
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0,1)),
    evidence_sha256 TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    PRIMARY KEY(attempt_id, name)
);
CREATE TABLE IF NOT EXISTS merge_items(
    queue_item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    queue_ref TEXT NOT NULL,
    expected_target_oid TEXT NOT NULL,
    candidate_oid TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('pending','prepared','queueing','queued','publishing','integrated','stale','uncertain','failed')
    ),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    integrated_oid TEXT,
    detail TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_identity_no_update
BEFORE UPDATE OF identity_json, identity_sha256, config_json, config_sha256 ON runs
BEGIN SELECT RAISE(ABORT, 'run identity and config are immutable'); END;
CREATE TABLE IF NOT EXISTS artifacts(
    sha256 TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    relative_path TEXT NOT NULL,
    created_ns INTEGER NOT NULL
);
"""


_SCHEMA = _SCHEMA_V2.replace(
    "    generation INTEGER NOT NULL CHECK(generation >= 0),\n    current_oid TEXT NOT NULL,",
    "    generation INTEGER NOT NULL CHECK(generation >= 0),\n"
    "    task_plan_sha256 TEXT NOT NULL,\n"
    "    task_count INTEGER NOT NULL CHECK(task_count >= 0),\n"
    "    current_oid TEXT NOT NULL,",
    1,
).replace(
    "CREATE TRIGGER IF NOT EXISTS runs_identity_no_update\n"
    "BEFORE UPDATE OF identity_json, identity_sha256, config_json, config_sha256 ON runs\n"
    "BEGIN SELECT RAISE(ABORT, 'run identity and config are immutable'); END;",
    "CREATE TRIGGER IF NOT EXISTS runs_identity_no_update\n"
    "BEFORE UPDATE OF identity_json, identity_sha256, config_json, config_sha256, "
    "task_plan_sha256, task_count ON runs\n"
    "BEGIN SELECT RAISE(ABORT, 'run identity, config, and task plan are immutable'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS tasks_plan_no_insert\n"
    "BEFORE INSERT ON tasks\n"
    "WHEN (SELECT COUNT(*) FROM tasks WHERE run_id = NEW.run_id) >=\n"
    "    (SELECT task_count FROM runs WHERE run_id = NEW.run_id)\n"
    "BEGIN SELECT RAISE(ABORT, 'run task plan is immutable'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS tasks_plan_no_delete BEFORE DELETE ON tasks\n"
    "BEGIN SELECT RAISE(ABORT, 'run task plan is immutable'); END;\n"
    "CREATE TRIGGER IF NOT EXISTS tasks_plan_identity_no_update\n"
    "BEFORE UPDATE OF run_id, article_id, phase ON tasks\n"
    "BEGIN SELECT RAISE(ABORT, 'run task plan is immutable'); END;",
    1,
)

_SCHEMA_V3 = _SCHEMA
_SCHEMA = _SCHEMA_V3 + """
CREATE TABLE IF NOT EXISTS merge_replays(
    replay_id TEXT PRIMARY KEY,
    queue_item_id TEXT NOT NULL REFERENCES merge_items(queue_item_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    target_oid TEXT NOT NULL,
    candidate_oid TEXT NOT NULL,
    gate_evidence_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    review_evidence_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    status TEXT NOT NULL CHECK(status IN ('prepared','publishing','integrated','stale','uncertain','failed')),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    publication_evidence_sha256 TEXT REFERENCES artifacts(sha256),
    detail TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL,
    UNIQUE(queue_item_id, ordinal),
    UNIQUE(queue_item_id, candidate_oid)
);
CREATE TABLE IF NOT EXISTS target_adoptions(
    adoption_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    previous_oid TEXT NOT NULL,
    target_oid TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    created_ns INTEGER NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(run_id, target_oid)
);
CREATE TABLE IF NOT EXISTS external_integrations(
    adoption_id TEXT NOT NULL REFERENCES target_adoptions(adoption_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('statement','proof')),
    integrated_oid TEXT NOT NULL,
    PRIMARY KEY(adoption_id, article_id, phase),
    UNIQUE(run_id, article_id, phase),
    FOREIGN KEY(run_id, article_id, phase) REFERENCES tasks(run_id, article_id, phase) ON DELETE CASCADE
);
CREATE TRIGGER IF NOT EXISTS merge_replays_identity_no_update
BEFORE UPDATE OF replay_id, queue_item_id, ordinal, target_oid, candidate_oid,
    gate_evidence_sha256, review_evidence_sha256, created_ns ON merge_replays
BEGIN SELECT RAISE(ABORT, 'merge replay identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS merge_replays_no_delete BEFORE DELETE ON merge_replays
BEGIN SELECT RAISE(ABORT, 'merge replay history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS target_adoptions_no_update BEFORE UPDATE ON target_adoptions
BEGIN SELECT RAISE(ABORT, 'target adoption history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS target_adoptions_no_delete BEFORE DELETE ON target_adoptions
BEGIN SELECT RAISE(ABORT, 'target adoption history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS external_integrations_no_update BEFORE UPDATE ON external_integrations
BEGIN SELECT RAISE(ABORT, 'external integration history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS external_integrations_no_delete BEFORE DELETE ON external_integrations
BEGIN SELECT RAISE(ABORT, 'external integration history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS attempts_identity_no_update
BEFORE UPDATE OF attempt_id, run_id, article_id, phase, number, worktree_path,
    branch, base_oid, backend, claim_key, claim_token_json, started_ns ON attempts
BEGIN SELECT RAISE(ABORT, 'attempt identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS merge_items_identity_no_update
BEFORE UPDATE OF queue_item_id, run_id, attempt_id, queue_ref, expected_target_oid,
    candidate_oid, created_ns ON merge_items
BEGIN SELECT RAISE(ABORT, 'merge item identity is immutable'); END;
"""


__all__ = [
    "ARTICLE_CLAIM_TOKEN_SCHEMA_VERSION",
    "ArticleClaimToken",
    "AttemptRecord",
    "CoordinatorLock",
    "EventRecord",
    "GateRecord",
    "ExternalIntegrationRecord",
    "GenerationConflict",
    "InvalidTransition",
    "LEDGER_SCHEMA_VERSION",
    "LedgerBusy",
    "LedgerError",
    "MergeItemRecord",
    "MergeReplayRecord",
    "RUN_CONFIG_SCHEMA_VERSION",
    "RecoverySnapshot",
    "RunConfig",
    "RunIdentity",
    "RunLedger",
    "RunRecord",
    "TaskRecord",
    "TargetAdoptionRecord",
]
