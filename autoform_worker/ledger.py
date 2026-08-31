"""Durable execution state for resumable Autoform runs."""

from __future__ import annotations

import hashlib
import json
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

from autoform_cli.claims import author_claim_key
from autoform_cli.graph import ARTICLE_ID_PATTERN

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


LEDGER_SCHEMA_VERSION = 2
RUN_CONFIG_SCHEMA_VERSION = 1
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
_TASK_RECOVERY_STATUSES = frozenset({"retrying", "blocked", "stopped"})
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
    start_oid: str
    plugin_version: str
    toolchain_fingerprint: str
    coverage_contract_sha256: str
    execution_input_sha256: str
    source_artifacts_sha256: str
    gate_policy_version: str
    schema_version: int = RUN_CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != RUN_CONFIG_SCHEMA_VERSION:
            raise LedgerError(
                f"unsupported run config schema {self.schema_version}; expected {RUN_CONFIG_SCHEMA_VERSION}"
            )
        for field in ("repository_id", "remote", "plugin_version", "gate_policy_version"):
            _validate_nonempty(field.replace("_", " "), getattr(self, field))
        _validate_branch_ref(self.target_ref)
        _validate_identifier("backend", self.backend)
        _validate_oid(self.start_oid)
        for field in (
            "toolchain_fingerprint",
            "coverage_contract_sha256",
            "execution_input_sha256",
            "source_artifacts_sha256",
        ):
            _validate_sha256(getattr(self, field), field)

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
class RunRecord:
    run_id: str
    identity: RunIdentity
    identity_sha256: str
    config: RunConfig
    config_sha256: str
    status: str
    generation: int
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
    claim_token: Mapping[str, object]
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
            self._initialize_schema()
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
        run_id: str | None = None,
    ) -> RunRecord:
        _validate_identity(identity)
        _validate_config_binding(identity, config)
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
                        status, generation, current_oid, stop_requested, detail, created_ns, updated_ns
                    ) VALUES (?, ?, ?, ?, ?, 'created', 0, ?, 0, '', ?, ?)
                    """,
                    (
                        identifier,
                        identity_json,
                        identity.sha256,
                        config_json,
                        config.sha256,
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
                },
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
            if current["status"] == status and current["detail"] == detail:
                return self.get_run(run_id)
            if current["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {current['generation']}, expected {expected_generation}"
                )
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
            if current["status"] in {"complete", "failed"}:
                raise InvalidTransition(f"cannot stop a terminal {current['status']} run")
            if current["stop_requested"]:
                return self.get_run(run_id)
            if current["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {current['generation']}, expected {expected_generation}"
                )
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
            if current["status"] == "running" and not current["stop_requested"]:
                return self.get_run(run_id)
            if current["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {run_id} is at generation {current['generation']}, expected {expected_generation}"
                )
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

    def add_tasks(self, run_id: str, tasks: Iterable[tuple[str, str]]) -> tuple[TaskRecord, ...]:
        canonical = sorted(set(tasks))
        with self._transaction():
            run = self._run_row(run_id)
            if run["status"] != "created":
                raise InvalidTransition(f"cannot add tasks to {run['status']} run")
            now = self._clock_ns()
            for article_id, phase in canonical:
                _validate_article_id(article_id)
                if phase not in _PHASES:
                    raise LedgerError(f"unknown work phase: {phase}")
                try:
                    self._connection.execute(
                        """
                        INSERT INTO tasks(
                            run_id, article_id, phase, status, attempts, generation,
                            blocked_by_json, detail, candidate_oid, integrated_oid
                        ) VALUES (?, ?, ?, 'pending', 0, 0, '[]', '', NULL, NULL)
                        """,
                        (run_id, article_id, phase),
                    )
                except sqlite3.IntegrityError as error:
                    raise LedgerError(f"duplicate task: {article_id}:{phase}") from error
                self._append_event(run_id, "task.created", {"article_id": article_id, "phase": phase}, now)
        return self.tasks(run_id)

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
            task = self._task_row(run_id, article_id, phase)
            if (
                task["status"] == status
                and task["detail"] == detail
                and tuple(json.loads(task["blocked_by_json"])) == blockers
            ):
                return _task_record(task)
            if task["generation"] != expected_generation:
                raise GenerationConflict(
                    f"task {article_id}:{phase} is at generation {task['generation']}, "
                    f"expected {expected_generation}"
                )
            if task["status"] not in _TASK_RECOVERY_SOURCES:
                raise InvalidTransition(f"cannot recover task from {task['status']} to {status}")
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
        expected_task_generation: int,
        worktree_path: str | Path,
        branch: str,
        base_oid: str,
        backend: str,
        claim_key: str,
        claim_token: Mapping[str, object],
        attempt_id: str | None = None,
    ) -> AttemptRecord:
        identifier = attempt_id or uuid.uuid4().hex
        _validate_identifier("attempt id", identifier)
        _validate_identifier("branch", branch)
        _validate_identifier("backend", backend)
        _validate_identifier("claim key", claim_key)
        _validate_article_id(article_id)
        if claim_key != author_claim_key(article_id):
            raise LedgerError(f"claim key is not anchored to durable article id {article_id}")
        _validate_oid(base_oid)
        claim_json = _json_text(dict(claim_token))
        now = self._clock_ns()
        with self._transaction():
            run = self._run_row(run_id)
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
            task = self._task_row(run_id, article_id, phase)
            if task["generation"] != expected_task_generation:
                raise GenerationConflict(
                    f"task {article_id}:{phase} is at generation {task['generation']}, "
                    f"expected {expected_task_generation}"
                )
            if task["status"] not in {"pending", "retrying"}:
                raise InvalidTransition(f"task is not ready for an attempt: {task['status']}")
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
        detail: str = "",
        candidate_oid: str | None = None,
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
            if attempt["status"] != "running":
                if (
                    attempt["status"] == outcome
                    and attempt["detail"] == detail
                    and attempt["candidate_oid"] == candidate_oid
                ):
                    return self.get_attempt(attempt_id)
                raise InvalidTransition(f"attempt is already {attempt['status']}")
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
                """,
                (
                    outcome,
                    candidate_oid,
                    detail,
                    attempt["run_id"],
                    attempt["article_id"],
                    attempt["phase"],
                ),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(
                    f"task changed while finishing attempt: {attempt['article_id']}:{attempt['phase']}"
                )
            self._append_event(
                attempt["run_id"],
                "attempt.finished",
                {"attempt_id": attempt_id, "outcome": outcome, "candidate_oid": candidate_oid, "detail": detail},
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
        """Make every persisted running attempt explicitly retryable or stopped."""
        recovered: list[str] = []
        now = self._clock_ns()
        with self._transaction():
            run = self._run_row(run_id)
            rows = self._connection.execute(
                "SELECT * FROM attempts WHERE run_id = ? AND status = 'running' ORDER BY attempt_id",
                (run_id,),
            ).fetchall()
            task_status = "stopped" if run["stop_requested"] else "retrying"
            for attempt in rows:
                recovered.append(attempt["attempt_id"])
                cursor = self._connection.execute(
                    """
                    UPDATE attempts SET status = 'interrupted', detail = ?, finished_ns = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    ("coordinator exited before recording a terminal result", now, attempt["attempt_id"]),
                )
                if cursor.rowcount != 1:
                    continue
                cursor = self._connection.execute(
                    """
                    UPDATE tasks SET status = ?, detail = ?, generation = generation + 1
                    WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'running'
                    """,
                    (
                        task_status,
                        "previous attempt was interrupted",
                        run_id,
                        attempt["article_id"],
                        attempt["phase"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise GenerationConflict(
                        f"task changed while recovering attempt: {attempt['article_id']}:{attempt['phase']}"
                    )
                self._append_event(
                    run_id,
                    "attempt.interrupted",
                    {"attempt_id": attempt["attempt_id"], "next_task_status": task_status},
                    now,
                )
        return tuple(recovered)

    def record_gate(
        self,
        attempt_id: str,
        name: str,
        passed: bool,
        *,
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
        identifier = queue_item_id or uuid.uuid4().hex
        _validate_identifier("queue item id", identifier)
        now = self._clock_ns()
        with self._transaction():
            attempt = self._attempt_row(attempt_id)
            if attempt["status"] != "candidate" or attempt["candidate_oid"] is None:
                raise InvalidTransition("only a candidate attempt may enter the merge queue")
            run = self._run_row(attempt["run_id"])
            if run["current_oid"] != expected_target_oid:
                raise GenerationConflict(
                    f"run {attempt['run_id']} current OID is {run['current_oid']}, "
                    f"not merge base {expected_target_oid}"
                )
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
                    attempt["candidate_oid"],
                    now,
                    now,
                ),
            )
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = 'queued', generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'candidate'
                """,
                (attempt["run_id"], attempt["article_id"], attempt["phase"]),
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
            if item["status"] == status and item["detail"] == detail:
                return _merge_item_record(item)
            if item["generation"] != expected_generation:
                raise GenerationConflict(
                    f"merge item {queue_item_id} is at generation {item['generation']}, "
                    f"expected {expected_generation}"
                )
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
        detail: str,
    ) -> MergeItemRecord:
        """Record the externally inspected outcome of an interrupted publication."""
        return self.transition_merge_item(
            queue_item_id,
            status,
            expected_generation=expected_generation,
            detail=detail,
        )

    def mark_integrated(
        self,
        queue_item_id: str,
        *,
        integrated_oid: str,
        expected_generation: int,
        expected_item_generation: int | None = None,
    ) -> RunRecord:
        _validate_oid(integrated_oid)
        now = self._clock_ns()
        with self._transaction():
            item = self._merge_item_row(queue_item_id)
            if item["status"] == "integrated" and item["integrated_oid"] == integrated_oid:
                return self.get_run(item["run_id"])
            if item["status"] in _MERGE_ITEM_TERMINAL_STATUSES:
                raise InvalidTransition(f"merge item is already {item['status']}")
            if expected_item_generation is not None and item["generation"] != expected_item_generation:
                raise GenerationConflict(
                    f"merge item {queue_item_id} is at generation {item['generation']}, "
                    f"expected {expected_item_generation}"
                )
            run = self._run_row(item["run_id"])
            if run["generation"] != expected_generation:
                raise GenerationConflict(
                    f"run {item['run_id']} is at generation {run['generation']}, expected {expected_generation}"
                )
            if run["current_oid"] != item["expected_target_oid"]:
                raise GenerationConflict(
                    f"run {item['run_id']} current OID changed from merge base {item['expected_target_oid']} "
                    f"to {run['current_oid']}"
                )
            attempt = self._attempt_row(item["attempt_id"])
            cursor = self._connection.execute(
                """
                UPDATE merge_items SET status = 'integrated', integrated_oid = ?,
                    generation = generation + 1, updated_ns = ?
                WHERE queue_item_id = ? AND generation = ? AND status = ?
                """,
                (integrated_oid, now, queue_item_id, item["generation"], item["status"]),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"merge item changed while integrating: {queue_item_id}")
            cursor = self._connection.execute(
                "UPDATE attempts SET status = 'integrated' WHERE attempt_id = ? AND status = 'candidate'",
                (item["attempt_id"],),
            )
            if cursor.rowcount != 1:
                raise GenerationConflict(f"attempt changed while integrating: {item['attempt_id']}")
            cursor = self._connection.execute(
                """
                UPDATE tasks SET status = 'integrated', integrated_oid = ?, generation = generation + 1
                WHERE run_id = ? AND article_id = ? AND phase = ? AND status = 'queued'
                """,
                (integrated_oid, item["run_id"], attempt["article_id"], attempt["phase"]),
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
        return tuple(
            EventRecord(
                sequence=row["sequence"],
                run_id=row["run_id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                created_ns=row["created_ns"],
            )
            for row in rows
        )

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
        self._connection.execute("PRAGMA foreign_keys = ON")
        mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).casefold() != "wal":
            raise LedgerError(f"SQLite refused WAL mode: {mode}")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA busy_timeout = 5000")

    def _initialize_schema(self) -> None:
        has_metadata = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
        ).fetchone()
        if has_metadata:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise LedgerError("ledger schema version is missing")
            if row["value"] != str(LEDGER_SCHEMA_VERSION):
                raise LedgerError(
                    f"unsupported ledger schema {row['value']}; expected {LEDGER_SCHEMA_VERSION}"
                )
        else:
            existing = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
            if existing is not None:
                raise LedgerError("ledger has tables but no schema metadata")
        script = (
            "BEGIN IMMEDIATE;\n"
            + _SCHEMA
            + "\nINSERT OR IGNORE INTO metadata(key, value) VALUES "
            + f"('schema_version', '{LEDGER_SCHEMA_VERSION}');\nCOMMIT;"
        )
        try:
            self._connection.executescript(script)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
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
        identity_values = json.loads(row["identity_json"])
        config_values = json.loads(row["config_json"])
        if not isinstance(identity_values, dict) or not isinstance(config_values, dict):
            raise TypeError("run identity and config must be JSON objects")
        if _json_text(identity_values) != row["identity_json"]:
            raise ValueError("identity JSON is not canonical")
        if _json_text(config_values) != row["config_json"]:
            raise ValueError("config JSON is not canonical")
        identity = RunIdentity(**identity_values)
        config = RunConfig(**config_values)
    except (KeyError, TypeError, ValueError) as error:
        raise LedgerError(f"run identity or config is invalid: {run_id}") from error
    _validate_identity(identity)
    _validate_config_binding(identity, config)
    if identity.sha256 != row["identity_sha256"]:
        raise LedgerError(f"run identity is inconsistent: {run_id}")
    if config.sha256 != row["config_sha256"]:
        raise LedgerError(f"run config is inconsistent: {run_id}")
    _validate_oid(row["current_oid"])
    return RunRecord(
        run_id=run_id,
        identity=identity,
        identity_sha256=row["identity_sha256"],
        config=config,
        config_sha256=row["config_sha256"],
        status=row["status"],
        generation=row["generation"],
        current_oid=row["current_oid"],
        stop_requested=bool(row["stop_requested"]),
        detail=row["detail"],
        created_ns=row["created_ns"],
        updated_ns=row["updated_ns"],
    )


def _task_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        run_id=row["run_id"],
        article_id=row["article_id"],
        phase=row["phase"],
        status=row["status"],
        attempts=row["attempts"],
        generation=row["generation"],
        blocked_by=tuple(json.loads(row["blocked_by_json"])),
        detail=row["detail"],
        candidate_oid=row["candidate_oid"],
        integrated_oid=row["integrated_oid"],
    )


def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
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
        claim_token=json.loads(row["claim_token_json"]),
        candidate_oid=row["candidate_oid"],
        detail=row["detail"],
        started_ns=row["started_ns"],
        finished_ns=row["finished_ns"],
    )


def _gate_record(row: sqlite3.Row) -> GateRecord:
    return GateRecord(
        attempt_id=row["attempt_id"],
        name=row["name"],
        passed=bool(row["passed"]),
        evidence_sha256=row["evidence_sha256"],
        detail=row["detail"],
        created_ns=row["created_ns"],
    )


def _merge_item_record(row: sqlite3.Row) -> MergeItemRecord:
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


def _validate_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LedgerError(f"{label} is not a lowercase SHA-256 digest")


def _json_bytes(value: object) -> bytes:
    return _json_text(value).encode("utf-8")


def _json_text(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise LedgerError(f"value is not canonical JSON: {error}") from error


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise LedgerError(f"state path is not a real directory: {path}")


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


_SCHEMA = """
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


__all__ = [
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
