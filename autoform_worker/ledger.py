"""Durable execution state for resumable Autoform runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
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


LEDGER_SCHEMA_VERSION = 3
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
    {"pending", "prepared", "queueing", "queued", "publishing", "integrated", "stale", "uncertain", "failed"}
)
_MERGE_ITEM_TERMINAL_STATUSES = frozenset({"integrated", "stale", "failed"})
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

    @property
    def running_attempts(self) -> tuple[AttemptRecord, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.status == "running")

    @property
    def unresolved_merge_items(self) -> tuple[MergeItemRecord, ...]:
        return tuple(item for item in self.merge_items if item.status not in _MERGE_ITEM_TERMINAL_STATUSES)


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
        if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise LedgerError(f"ledger path is not a regular file: {self.path}")
        created = metadata is None
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure()
            with _initialization_lock(self.path.with_suffix(self.path.suffix + ".initialize.lock")):
                self._initialize_schema()
                self._enable_wal()
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
                UPDATE tasks SET status = 'retrying', generation = generation + 1
                WHERE run_id = ? AND status = 'stopped'
                """,
                (run_id,),
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
            if (
                task["status"] == status
                and task["detail"] == detail
                and _task_record(task).blocked_by == blockers
            ):
                return _task_record(task)
            if task["status"] not in _TASK_RECOVERY_SOURCES:
                raise InvalidTransition(f"cannot recover task from {task['status']} to {status}")
            max_attempts = _run_record(run).config.max_attempts
            if status == "failed":
                if task["status"] != "candidate":
                    raise InvalidTransition("only a rejected candidate may use terminal task failure")
                if task["attempts"] < max_attempts:
                    raise InvalidTransition(
                        f"candidate has used {task['attempts']} of {max_attempts} attempts; retry is required"
                    )
            elif (
                status == "retrying"
                and task["status"] == "candidate"
                and task["attempts"] >= max_attempts
            ):
                raise InvalidTransition(
                    f"candidate exhausted {max_attempts} attempts and must transition to failed"
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
            self._append_event(
                run_id,
                "task.recovered",
                {
                    "from": task["status"],
                    "to": status,
                    "article_id": article_id,
                    "phase": phase,
                    "detail": detail,
                    "blocked_by": blockers,
                },
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
                {"attempt_id": identifier, "article_id": article_id, "phase": phase, "number": number},
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
                    attempt["status"] == outcome
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
                (outcome, candidate_oid, detail, now, attempt_id),
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
                    outcome,
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
                "outcome": outcome,
                "candidate_oid": candidate_oid,
                "detail": detail,
            }
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
                {"attempt_id": attempt_id, "queue_item_id": identifier, "queue_ref": queue_ref},
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
            run = self._run_row(item["run_id"])
            if run["generation"] != expected_run_generation:
                raise GenerationConflict(
                    f"run {item['run_id']} is at generation {run['generation']}, "
                    f"expected {expected_run_generation}"
                )
            if item["generation"] != expected_generation:
                raise GenerationConflict(
                    f"merge item {queue_item_id} is at generation {item['generation']}, "
                    f"expected {expected_generation}"
                )
            if item["status"] == status and item["detail"] == detail:
                return _merge_item_record(item)
            if item["status"] in _MERGE_ITEM_TERMINAL_STATUSES:
                raise InvalidTransition(f"merge item is already {item['status']}")
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
            run = self._run_row(item["run_id"])
            if run["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {item['run_id']} is at generation {run['generation']}, expected {expected_generation}"
                )
            if integrated_oid != item["candidate_oid"]:
                raise InvalidTransition("integrated OID must equal the queued candidate OID")
            if item["status"] == "integrated" and item["integrated_oid"] == integrated_oid:
                return self.get_run(item["run_id"])
            if item["status"] in _MERGE_ITEM_TERMINAL_STATUSES:
                raise InvalidTransition(f"merge item is already {item['status']}")
            if run["current_oid"] != item["expected_target_oid"]:
                raise GenerationConflict(
                    f"run {item['run_id']} current OID changed from merge base {item['expected_target_oid']} "
                    f"to {run['current_oid']}"
                )
            attempt = self._attempt_row(item["attempt_id"])
            if (
                attempt["run_id"] != item["run_id"]
                or attempt["candidate_oid"] != item["candidate_oid"]
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
        return RecoverySnapshot(
            run=run,
            tasks=tasks,
            attempts=attempts,
            gates=gates,
            merge_items=merge_items,
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
        _ensure_private_directory(directory)
        target = directory / digest
        if target.exists():
            _verify_artifact(target, digest, len(content))
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".autoform-artifact-", dir=directory)
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                _fsync_directory(directory)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            _verify_artifact(target, digest, len(content))
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
        return _read_artifact(path, digest, row["size"])

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
            self._connection.execute(
                """
                INSERT INTO events(sequence, run_id, kind, payload_json, created_ns)
                SELECT sequence, run_id, kind, payload_json, created_ns FROM events_v1
                """
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
                max_attempts=3,
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
                ORDER BY created_ns, queue_item_id
                """,
                (run_id,),
            ).fetchall()
            for item in integrated:
                if (
                    item["expected_target_oid"] != current_oid
                    or item["candidate_oid"] != item["integrated_oid"]
                ):
                    raise LedgerError(f"v1 integration history is ambiguous: {run_id}")
                current_oid = item["candidate_oid"]
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
        task_attempts: dict[tuple[str, str, str], int] = {}
        tasks_by_run: dict[str, list[tuple[str, str]]] = {run_id: [] for run_id in run_plans}
        for row in self._connection.execute("SELECT * FROM tasks").fetchall():
            task = _task_record(row)
            task_attempts[(task.run_id, task.article_id, task.phase)] = task.attempts
            tasks_by_run[task.run_id].append((task.article_id, task.phase))
        for run_id, run in run_plans.items():
            task_plan = tuple(sorted(tasks_by_run[run_id]))
            if run.task_count != len(task_plan) or run.task_plan_sha256 != _task_plan_sha256(task_plan):
                raise LedgerError(f"task plan binding is invalid: {run_id}")
        for row in self._connection.execute("SELECT * FROM attempts").fetchall():
            attempt = _attempt_record(row)
            task_key = (attempt.run_id, attempt.article_id, attempt.phase)
            if attempt.number > task_attempts[task_key] or attempt.backend != run_backends[attempt.run_id]:
                raise LedgerError(f"attempt binding is invalid: {attempt.attempt_id}")
        for row in self._connection.execute("SELECT * FROM gates").fetchall():
            _gate_record(row)
        for row in self._connection.execute("SELECT * FROM merge_items").fetchall():
            _merge_item_record(row)
        for row in self._connection.execute("SELECT * FROM events").fetchall():
            _event_record(row)
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
            LIMIT 1
            """
        ).fetchone()
        if bad_merge is not None:
            raise LedgerError(f"merge item binding is invalid: {bad_merge['queue_item_id']}")

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

    def _append_event(self, run_id: str, kind: str, payload: Mapping[str, object], created_ns: int) -> None:
        self._connection.execute(
            "INSERT INTO events(run_id, kind, payload_json, created_ns) VALUES (?, ?, ?, ?)",
            (run_id, kind, _json_text(dict(payload)), created_ns),
        )


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
    if row["status"] in {"candidate", "queued", "integrated"} and row["candidate_oid"] is None:
        raise LedgerError(f"{label} is missing its candidate OID")
    if row["status"] == "integrated" and row["integrated_oid"] != row["candidate_oid"]:
        raise LedgerError(f"{label} integrated OID does not match its candidate")
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
    if row["status"] == "integrated" and row["integrated_oid"] != row["candidate_oid"]:
        raise LedgerError(f"{label} integrated OID does not match its candidate")
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


def _read_artifact(path: Path, digest: str, size: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
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


def _verify_artifact(path: Path, digest: str, size: int) -> None:
    _read_artifact(path, digest, size)


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


__all__ = [
    "ARTICLE_CLAIM_TOKEN_SCHEMA_VERSION",
    "ArticleClaimToken",
    "AttemptRecord",
    "CoordinatorLock",
    "EventRecord",
    "GateRecord",
    "GenerationConflict",
    "InvalidTransition",
    "LEDGER_SCHEMA_VERSION",
    "LedgerBusy",
    "LedgerError",
    "MergeItemRecord",
    "RUN_CONFIG_SCHEMA_VERSION",
    "RecoverySnapshot",
    "RunConfig",
    "RunIdentity",
    "RunLedger",
    "RunRecord",
    "TaskRecord",
]
