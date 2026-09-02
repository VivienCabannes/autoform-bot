"""Isolated Git worktrees and a durable compare-and-swap merge queue."""

from __future__ import annotations

import hashlib
import inspect
import json
import copy
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Iterable, Mapping, Set
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Protocol
from urllib.parse import unquote, urlsplit

import autoform_cli.claims as claims_module
from autoform_cli.claims import (
    CLAIM_HEARTBEAT_S,
    CLAIM_REF_PREFIX,
    CLAIM_TTL_S,
    RECOVERY_BLOCK_SCHEMA,
    ClaimBoard,
    ClaimFence,
    claim_handoff_ref,
    target_resolution_ref,
)

from ._paths import GENERATED_DIRECTORY_NAMES
from .ledger import CoordinatorLock


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CANDIDATE_INDEX_STAGE_NAME = re.compile(r"^\.autoform-candidate-index-[0-9a-f]{32}\.stage$")
_CANDIDATE_INDEX_BACKUP_NAME = re.compile(r"^\.autoform-candidate-index-[0-9a-f]{32}\.base$")
_CANDIDATE_BLOB_CHUNK_BYTES = 1024 * 1024
_CANDIDATE_BATCH_HEADER_BYTES = 256
_CANDIDATE_BATCH_STDERR_BYTES = 500
_CANDIDATE_BATCH_TIMEOUT_S = 120
_PRIMARY_PACK_VERIFY_MAX_BYTES = 256 * 1024 * 1024
_PRIMARY_PACK_VERIFY_MAX_LINE_BYTES = 4096
_PRIMARY_PACK_VERIFY_MAX_OBJECTS = 2_000_000
_PRIMARY_PACK_TRACE_MAX_BYTES = 64 * 1024
_MAX_CANDIDATE_BLOB_BYTES = 16 * 1024 * 1024
_MAX_CANDIDATE_TOTAL_BLOB_BYTES = 64 * 1024 * 1024
_MAX_CANDIDATE_ABANDONED_INDEX_STAGES = 8
_WORKTREE_SCHEMA = "autoform-worktree/v2"
_CANDIDATE_SCHEMA = "autoform-candidate/v5"
_READY_CANDIDATE_INDEX_TOPOLOGY = frozenset({"stage", "index", "displaced-backup"})
_PUBLICATION_SCHEMA = "autoform-merge-publication/v6"
_LEGACY_PUBLICATION_SCHEMAS = frozenset(
    {"autoform-merge-publication/v3", "autoform-merge-publication/v4", "autoform-merge-publication/v5"}
)
_UNBOUND_PUBLICATION_SCHEMA = "autoform-merge-publication/v2"
_REPLAY_INTENT_SCHEMA = "autoform-merge-replay-intent/v1"
_RESOLUTION_INTENT_SCHEMA = "autoform-publication-resolution-intent/v1"
_RESOLUTION_SCHEMA = "autoform-target-resolution/v1"
_TRANSPORT_SCHEMA = "autoform-git-transport/v2"
_TRANSPORT_INTENT_SCHEMA = "autoform-git-transport-intent/v1"
_PUBLICATION_STATES = frozenset(
    {
        "prepared",
        "queueing",
        "queued",
        "publishing",
        "replaying",
        "integrated",
        "aborted",
        "stale",
        "uncertain",
    }
)
_REPLAY_EVENT_STATES = frozenset(
    {"prepared", "publishing", "pin-retry", "retry", "stale", "integrated", "uncertain"}
)
_REPLAY_TERMINAL_STATES = frozenset({"pin-retry", "retry", "stale", "integrated", "uncertain"})
_MAX_REPLAY_ATTEMPTS = 128
_MAX_REPLAY_EVENTS = 512
_MAX_PUBLICATION_HISTORY = 1024
_MAX_PUBLICATION_BYTES = 1024 * 1024
_PUBLICATION_TERMINAL_RESERVE_BYTES = 16 * 1024
_REPLAY_EXECUTION_RESERVE_BYTES = 32 * 1024
_MAX_PUBLICATION_DETAIL_BYTES = 2048
_ZERO_OIDS = frozenset({"0" * 40, "0" * 64})
_CAS_REJECTIONS = (
    "stale info",
    "fetch first",
    "remote ref updated since checkout",
    "cannot lock ref",
    "failed to push some refs",
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_REMOTE = re.compile(r"^(?:[^/@:]+@)?(?:\[[^\]]+\]|[^/:]+):.+$")
_GIT_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LANGUAGE",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class RepositoryError(RuntimeError):
    """Repository execution state is invalid or cannot be verified."""


class WorktreeConflict(RepositoryError):
    """An attempt path is occupied by state Autoform does not own."""


class WorktreeUncertain(RepositoryError):
    """An interrupted worktree operation cannot be recovered automatically."""


class CandidateError(RepositoryError):
    """A candidate commit is absent, conflicted, or cannot be verified."""


class CandidateNotFound(CandidateError):
    """No durable candidate intent exists for the attempt."""


class CandidateUncertain(CandidateError):
    """Candidate evidence is incomplete or conflicts with repository state."""


class MergeQueueError(RepositoryError):
    """A merge-queue operation failed before integration was verified."""


class MergeQueueBusy(MergeQueueError):
    """Another publisher owns the target-ref lease."""


class RemoteDrift(MergeQueueError):
    """The target ref no longer equals the queue item's expected object."""


class PublicationUncertain(MergeQueueError):
    """Remote evidence cannot determine whether publication is safe to retry."""


class _ClaimBoardLike(Protocol):
    repo_url: str

    def acquire(
        self,
        key: str,
        ttl: int | float = CLAIM_TTL_S,
        steal: bool = False,
        note: str = "",
        *,
        expected_resolution_oid: str | None = None,
    ) -> bool: ...

    def holds(self, key: str) -> bool: ...

    def held_claim_fence(self, key: str) -> ClaimFence | None: ...

    def release(self, key: str) -> bool: ...

    def adopt_recovery_block(
        self,
        key: str,
        *,
        queue_item_id: str,
        target_resolution_ref: str,
        resolution_oid: str,
        ttl: int | float = CLAIM_TTL_S,
        note: str = "",
    ) -> bool: ...

    def heartbeat(
        self,
        key: str,
        *,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class WorktreeReceipt:
    """Verified identity and current state of one isolated attempt worktree."""

    run_id: str
    attempt_id: str
    repository_id: str
    path: str
    base_oid: str
    head_oid: str
    state: str
    identity_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateReceipt:
    """Verified durable evidence for one deterministic attempt candidate."""

    run_id: str
    attempt_id: str
    repository_id: str
    path: str
    base_oid: str
    tree_oid: str
    candidate_oid: str
    state: str
    allowed_paths: tuple[str, ...]
    author_name: str
    author_email: str
    message_sha256: str
    identity_sha256: str


@dataclass(frozen=True, slots=True)
class _CandidateSnapshot:
    entries: tuple[tuple[str, tuple[str, str]], ...]
    blobs: tuple[tuple[str, bytes], ...]
    git_identity: tuple[int, int, str]
    file_identities: tuple[tuple[str, tuple[int, ...]], ...]
    directory_identities: tuple[tuple[str, tuple[int, ...]], ...]


@dataclass(frozen=True, slots=True)
class _CandidateFileSnapshot:
    identity: tuple[int, ...]
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _CandidateAdminBinding:
    path: Path
    identity: tuple[int, int]
    index_path: Path
    head_snapshot: _CandidateFileSnapshot
    static_sha256: str


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Durable evidence for one remote queue publication attempt."""

    queue_item_id: str
    remote_id: str
    remote_kind: str
    remote_device: int | None
    remote_inode: int | None
    target_ref: str
    queue_ref: str
    expected_target_oid: str
    candidate_oid: str
    status: str
    observed_target_oid: str | None
    observed_queue_oid: str | None
    article_claim_key: str
    article_claim_ref: str
    article_claim_oid: str
    article_claim_lease_id: str
    observed_article_claim_oid: str | None
    article_handoff_ref: str
    observed_article_handoff_oid: str | None
    target_resolution_ref: str
    initial_resolution_oid: str
    resolution_oid: str
    observed_resolution_oid: str | None
    claim_key: str
    claim_ref: str
    claim_oid: str | None
    observed_claim_oid: str | None
    claim_lease_id: str | None
    detail: str
    history: tuple[Mapping[str, object], ...]
    replay_intents: tuple[Mapping[str, object], ...] = ()
    replay_events: tuple[Mapping[str, object], ...] = ()
    resolution_intents: tuple[Mapping[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["history"] = [dict(item) for item in self.history]
        value["replay_intents"] = [dict(item) for item in self.replay_intents]
        value["replay_events"] = [dict(item) for item in self.replay_events]
        value["resolution_intents"] = [dict(item) for item in self.resolution_intents]
        return value

    def evidence_bytes(self) -> bytes:
        """Return canonical bytes suitable for :meth:`RunLedger.put_artifact`."""
        return _json_bytes({"schema": _PUBLICATION_SCHEMA, **self.as_dict()})

    @property
    def article_claim(self) -> ClaimFence:
        """Return the exact article-claim fence recorded for publication."""

        return ClaimFence(
            key=self.article_claim_key,
            ref=self.article_claim_ref,
            oid=self.article_claim_oid,
            lease_id=self.article_claim_lease_id,
        )

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()


class AttemptWorktrees:
    """Create and recover attempt worktrees without touching checkout files."""

    def __init__(self, repository_root: str | Path, state_root: str | Path) -> None:
        self.repository_root = _existing_real_directory(repository_root, label="repository root")
        self._repository_identity = _directory_identity(self.repository_root)
        self._coordinator_git_identity = _coordinator_git_entry_identity(self.repository_root)
        top_level = self._run_git(["rev-parse", "--show-toplevel"]).stdout.strip()
        if _canonical_existing_directory(top_level) != self.repository_root:
            raise RepositoryError("repository root must be the exact Git worktree top level")
        common = self._run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
        self.common_git_dir = _existing_real_directory(common, label="common Git directory")
        self._common_git_identity = _directory_identity(self.common_git_dir)
        self.object_dir = _existing_real_directory(
            self.common_git_dir / "objects", label="primary Git object directory"
        )
        self._object_dir_identity = _directory_identity(self.object_dir)
        self.object_format = self._run_git(["rev-parse", "--show-object-format"]).stdout.strip()
        if self.object_format not in {"sha1", "sha256"}:
            raise RepositoryError(f"unsupported Git object format: {self.object_format}")

        state_path = _absolute_path(state_root)
        if _paths_overlap(self.repository_root, state_path):
            raise RepositoryError("attempt state must be outside the coordinator checkout")
        if _paths_overlap(self.common_git_dir, state_path):
            raise RepositoryError("attempt state must be outside the common Git directory")
        self.state_root = _prepare_private_root(state_path)
        self.worktree_root = self.state_root / "worktrees"
        self.lock_root = self.state_root / "locks"
        _ensure_private_directory(self.worktree_root)
        _ensure_private_directory(self.lock_root)
        self._state_identity = _directory_identity(self.state_root)
        self._worktree_root_identity = _directory_identity(self.worktree_root)
        self._lock_root_identity = _directory_identity(self.lock_root)

        digest_input = f"{self.repository_root}\0{self.common_git_dir}".encode()
        self.repository_id = hashlib.sha256(digest_input).hexdigest()

    def prepare(
        self,
        run_id: str,
        attempt_id: str,
        *,
        base_oid: str,
    ) -> WorktreeReceipt:
        """Create or resume an isolated, detached worktree at ``base_oid``."""
        _validate_name("run id", run_id)
        _validate_name("attempt id", attempt_id)
        _validate_oid(base_oid)
        self._verify_repository()
        self._verify_state()
        self._verify_commit(base_oid)
        attempt_root, tree, marker = self._attempt_paths(run_id, attempt_id)
        lock = self._attempt_lock(run_id, attempt_id)
        with lock:
            self._verify_state()
            if attempt_root.exists() or attempt_root.is_symlink():
                if not marker.exists() and not marker.is_symlink():
                    self._remove_empty_attempt_scaffold(attempt_root, tree)
                else:
                    record = self._read_attempt_marker(marker)
                    self._validate_attempt_record(record, run_id, attempt_id, attempt_root, tree, base_oid)
                    if record["state"] == "cleaning":
                        raise WorktreeUncertain("attempt cleanup must finish before this attempt can be reused")
                    return self._resume_preparation(record, marker, tree)
            if attempt_root.exists() or attempt_root.is_symlink():
                raise WorktreeConflict("empty attempt scaffold could not be recovered")

            _ensure_private_directory(attempt_root.parent)
            attempt_root.mkdir(mode=0o700)
            _checkpoint("worktree-scaffold-created")
            root_identity = _directory_identity(attempt_root)
            tree.mkdir(mode=0o700)
            _checkpoint("worktree-tree-created")
            tree_identity = _directory_identity(tree)
            record: dict[str, object] = {
                "schema": _WORKTREE_SCHEMA,
                "repository_id": self.repository_id,
                "repository_root": str(self.repository_root),
                "common_git_dir": str(self.common_git_dir),
                "run_id": run_id,
                "attempt_id": attempt_id,
                "base_oid": base_oid,
                "path": str(tree),
                "state": "preparing",
                "root_device": root_identity[0],
                "root_inode": root_identity[1],
                "tree_device": tree_identity[0],
                "tree_inode": tree_identity[1],
                "git_entry_device": None,
                "git_entry_inode": None,
                "git_entry_sha256": None,
                "created_ns": time.time_ns(),
                "ready_ns": None,
            }
            self._write_attempt_marker(marker, record)
            _checkpoint("worktree-intent-recorded")
            try:
                self._run_git(["worktree", "add", "--detach", str(tree), base_oid])
            except BaseException:
                # The preparing marker deliberately survives. A later call inspects Git's
                # registration and either completes the exact attempt or fails closed.
                raise
            _checkpoint("worktree-added")
            return self._finalize_preparation(record, marker, tree)

    def inspect(self, run_id: str, attempt_id: str) -> WorktreeReceipt:
        """Inspect an attempt without repairing or changing it."""
        _validate_name("run id", run_id)
        _validate_name("attempt id", attempt_id)
        self._verify_state()
        attempt_root, tree, marker = self._attempt_paths(run_id, attempt_id)
        if not attempt_root.exists() and not attempt_root.is_symlink():
            raise WorktreeConflict(f"attempt does not exist: {run_id}/{attempt_id}")
        record = self._read_attempt_marker(marker)
        self._validate_attempt_record(
            record,
            run_id,
            attempt_id,
            attempt_root,
            tree,
            str(record.get("base_oid", "")),
        )
        if record["state"] == "cleaning":
            raise WorktreeUncertain("attempt cleanup is in progress")
        if record["state"] == "preparing":
            return self._inspect_preparing(record, tree)
        return self._ready_receipt(record, tree)

    def candidate_oid(self, run_id: str, attempt_id: str) -> str:
        """Return a clean candidate commit descended from the recorded base."""
        receipt = self.inspect(run_id, attempt_id)
        if receipt.state != "ready":
            raise WorktreeUncertain(f"attempt worktree is {receipt.state}")
        tree = Path(receipt.path)
        self._assert_canonical_index(tree)
        status = self._run_tree_git(tree, ["status", "--porcelain=v1", "--untracked-files=all"])
        if status.stdout:
            raise WorktreeConflict("candidate worktree contains uncommitted changes")
        if not self._is_ancestor(receipt.base_oid, receipt.head_oid):
            raise WorktreeConflict("candidate commit is not descended from its recorded base")
        return receipt.head_oid

    def commit_candidate(
        self,
        run_id: str,
        attempt_id: str,
        *,
        allowed_paths: Set[str],
        message: str,
        author_name: str,
        author_email: str,
    ) -> CandidateReceipt:
        """Commit exactly the allowed regular-file changes without Git's porcelain."""
        _validate_name("run id", run_id)
        _validate_name("attempt id", attempt_id)
        paths = _validate_candidate_paths(allowed_paths)
        message_bytes = _validate_candidate_message(message)
        _validate_candidate_identity(author_name, author_email)
        message_sha256 = hashlib.sha256(message_bytes).hexdigest()
        attempt_root, tree, marker = self._attempt_paths(run_id, attempt_id)
        journal = attempt_root / "candidate.json"

        with self._attempt_lock(run_id, attempt_id):
            worktree = self.inspect(run_id, attempt_id)
            if worktree.state != "ready":
                raise CandidateUncertain(f"attempt worktree is {worktree.state}")
            marker_record = self._read_attempt_marker(marker)
            self._validate_attempt_record(
                marker_record,
                run_id,
                attempt_id,
                attempt_root,
                tree,
                worktree.base_oid,
            )
            expected_identity = {
                "repository_id": self.repository_id,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "base_oid": worktree.base_oid,
                "path": str(tree),
                "worktree_identity_sha256": worktree.identity_sha256,
                "allowed_paths": list(paths),
                "author_name": author_name,
                "author_email": author_email,
                "message_sha256": message_sha256,
            }
            if journal.exists() or journal.is_symlink():
                record = self._read_candidate_journal(journal)
                self._validate_candidate_record(record, marker_record, expected_identity=expected_identity)
                return self._finish_candidate(
                    record,
                    journal,
                    tree,
                    message_bytes=message_bytes,
                )
            _remove_atomic_write_orphans(journal)
            if worktree.head_oid != worktree.base_oid:
                raise CandidateUncertain("attempt HEAD advanced without a durable candidate intent")

            base_entries = self._candidate_base_entries(worktree.base_oid)
            admin = self._candidate_admin_binding(tree)
            if _candidate_head_oid(admin.head_snapshot) != worktree.base_oid:
                raise CandidateUncertain("attempt Git HEAD does not match the recorded base")
            config_snapshot = self._candidate_config_snapshot(admin)
            config_sha256 = hashlib.sha256(_json_bytes(config_snapshot)).hexdigest()
            base_index = _candidate_private_file_snapshot(admin.index_path, label="attempt Git index")
            self._assert_candidate_index_lock_absent(admin)
            self._assert_candidate_index(
                tree,
                base_entries,
                label="recorded base",
                index_path=admin.index_path,
                index_snapshot=base_index,
            )
            snapshot = self._candidate_snapshot(tree, base_entries, paths)
            tree_oid, objects = _candidate_tree_objects(snapshot.entries, snapshot.blobs, self.object_format)
            commit_content = _candidate_commit_content(
                tree_oid,
                worktree.base_oid,
                author_name,
                author_email,
                message_bytes,
            )
            candidate_oid = _git_object_oid("commit", commit_content, self.object_format)
            self._write_candidate_objects((*objects, ("commit", candidate_oid, commit_content)))
            candidate_index = self._candidate_index_image(
                tree,
                candidate_oid,
                dict(snapshot.entries),
            )
            self._assert_candidate_config_snapshot(admin, config_sha256)
            if _candidate_private_file_snapshot(admin.index_path, label="attempt Git index") != base_index:
                raise CandidateUncertain("attempt Git index changed before candidate intent was recorded")
            self._assert_candidate_index_lock_absent(admin)
            stage_name = self._new_candidate_index_stage_name(admin)
            backup_name = stage_name.removesuffix(".stage") + ".base"
            root_device, root_inode = _directory_identity(attempt_root)
            record: dict[str, object] = {
                "schema": _CANDIDATE_SCHEMA,
                **expected_identity,
                "state": "prepared",
                "tree_oid": tree_oid,
                "candidate_oid": candidate_oid,
                "git_dir": str(admin.path),
                "git_dir_device": admin.identity[0],
                "git_dir_inode": admin.identity[1],
                "git_admin_static_sha256": admin.static_sha256,
                "base_head_identity": list(admin.head_snapshot.identity),
                "base_head_sha256": admin.head_snapshot.sha256,
                "candidate_head_identity": None,
                "base_index_identity": list(base_index.identity),
                "base_index_sha256": base_index.sha256,
                "candidate_index_size": len(candidate_index.content),
                "candidate_index_sha256": candidate_index.sha256,
                "candidate_index_identity": None,
                "candidate_index_stage_name": stage_name,
                "candidate_index_backup_name": backup_name,
                "candidate_index_stage_device": None,
                "candidate_index_stage_inode": None,
                "candidate_index_abandoned_stages": [],
                "config_snapshot_sha256": config_sha256,
                "root_device": root_device,
                "root_inode": root_inode,
                "created_ns": time.time_ns(),
                "ready_ns": None,
            }
            self._write_candidate_journal(journal, record)
            _checkpoint("candidate-intent-recorded")
            self._assert_candidate_config_snapshot(admin, config_sha256)
            return self._finish_candidate(
                record,
                journal,
                tree,
                message_bytes=message_bytes,
                snapshot=snapshot,
                objects=objects,
                commit_content=commit_content,
                candidate_index=candidate_index,
            )

    def inspect_candidate(self, run_id: str, attempt_id: str) -> CandidateReceipt:
        """Inspect durable candidate evidence without repairing or cleaning it."""
        _validate_name("run id", run_id)
        _validate_name("attempt id", attempt_id)
        attempt_root, tree, marker = self._attempt_paths(run_id, attempt_id)
        if not attempt_root.exists() and not attempt_root.is_symlink():
            raise CandidateNotFound(f"candidate does not exist: {run_id}/{attempt_id}")
        worktree = self.inspect(run_id, attempt_id)
        if worktree.state != "ready":
            raise CandidateUncertain(f"attempt worktree is {worktree.state}")
        journal = attempt_root / "candidate.json"
        if not journal.exists() and not journal.is_symlink():
            raise CandidateNotFound(f"candidate does not exist: {run_id}/{attempt_id}")
        marker_record = self._read_attempt_marker(marker)
        self._validate_attempt_record(
            marker_record,
            run_id,
            attempt_id,
            attempt_root,
            tree,
            worktree.base_oid,
        )
        record = self._read_candidate_journal(journal)
        self._validate_candidate_record(record, marker_record)
        admin = self._assert_candidate_admin_binding(tree, record)
        config_sha256 = (
            hashlib.sha256(_json_bytes(self._candidate_config_snapshot(admin))).hexdigest()
            if record["state"] == "ready"
            else str(record["config_snapshot_sha256"])
        )
        paths = tuple(str(path) for path in record["allowed_paths"])
        base_entries = self._candidate_base_entries(worktree.base_oid)
        snapshot = self._candidate_snapshot(tree, base_entries, paths)
        self._assert_candidate_config_snapshot(admin, config_sha256)
        tree_oid, objects = _candidate_tree_objects(snapshot.entries, snapshot.blobs, self.object_format)
        if tree_oid != record["tree_oid"]:
            raise CandidateUncertain("candidate worktree no longer matches the recorded tree")
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(record)
        self._assert_candidate_tree_closure(snapshot.entries, admin, config_sha256)
        candidate_entries = dict(snapshot.entries)
        candidate_index = self._candidate_index_image(tree, str(record["candidate_oid"]), candidate_entries)
        self._assert_recorded_candidate_index(record, candidate_index)
        observed_index = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        topology = self._candidate_index_stage_topology(admin, record, candidate_index)

        candidate_oid = str(record["candidate_oid"])
        head_oid = self._tree_head(tree)
        admin = self._assert_candidate_admin_binding(tree, record)
        if _candidate_head_oid(admin.head_snapshot) != head_oid:
            raise CandidateUncertain("attempt Git HEAD changed during candidate inspection")
        if head_oid != candidate_oid:
            if record["state"] == "prepared" and head_oid == worktree.base_oid:
                raise CandidateUncertain("candidate intent exists but its commit is not current")
            raise CandidateUncertain("attempt HEAD conflicts with the candidate journal")
        candidate_head_identity = record.get("candidate_head_identity")
        if candidate_head_identity is not None and list(admin.head_snapshot.identity) != candidate_head_identity:
            raise CandidateUncertain("candidate Git HEAD identity changed")
        if record["state"] == "ready":
            if topology != _READY_CANDIDATE_INDEX_TOPOLOGY:  # pragma: no cover - checked by topology validator
                raise CandidateUncertain("ready candidate has unresolved Git index staging")
            if self._candidate_index_state(record, observed_index) != "candidate":
                raise CandidateUncertain("ready candidate Git index does not match its tree")
            self._assert_candidate_index(
                tree,
                candidate_entries,
                label="candidate",
                index_path=admin.index_path,
                index_snapshot=observed_index,
            )
            self._assert_candidate_config_snapshot(admin, config_sha256)
            return self._candidate_receipt(record, state="ready")
        index_state = self._candidate_index_state(record, observed_index)
        expected_entries = base_entries if index_state == "base" else candidate_entries
        self._assert_candidate_index(
            tree,
            expected_entries,
            label=f"{index_state} tree",
            index_path=admin.index_path,
            index_snapshot=observed_index,
        )
        self._assert_candidate_config_snapshot(admin, config_sha256)
        return self._candidate_receipt(record, state="recoverable")

    def _finish_candidate(
        self,
        record: dict[str, object],
        journal: Path,
        tree: Path,
        *,
        message_bytes: bytes,
        snapshot: _CandidateSnapshot | None = None,
        objects: tuple[tuple[str, str, bytes], ...] | None = None,
        commit_content: bytes | None = None,
        candidate_index: _CandidateFileSnapshot | None = None,
    ) -> CandidateReceipt:
        base_oid = str(record["base_oid"])
        paths = tuple(str(path) for path in record["allowed_paths"])
        admin = self._assert_candidate_admin_binding(tree, record)
        config_sha256 = (
            hashlib.sha256(_json_bytes(self._candidate_config_snapshot(admin))).hexdigest()
            if record["state"] == "ready"
            else str(record["config_snapshot_sha256"])
        )
        base_entries = self._candidate_base_entries(base_oid)
        if snapshot is None:
            snapshot = self._candidate_snapshot(tree, base_entries, paths)
        self._assert_candidate_config_snapshot(admin, config_sha256)
        tree_oid, rebuilt_objects = _candidate_tree_objects(snapshot.entries, snapshot.blobs, self.object_format)
        if tree_oid != record["tree_oid"]:
            raise CandidateUncertain("candidate worktree differs from its durable intent")
        if commit_content is None:
            commit_content = _candidate_commit_content(
                tree_oid,
                base_oid,
                str(record["author_name"]),
                str(record["author_email"]),
                message_bytes,
            )
        candidate_oid = _git_object_oid("commit", commit_content, self.object_format)
        if candidate_oid != record["candidate_oid"]:
            raise CandidateUncertain("candidate inputs do not reproduce the journaled commit")
        if objects is None:
            objects = rebuilt_objects
        self._write_candidate_objects((*objects, ("commit", candidate_oid, commit_content)))
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(record, expected_content=commit_content)
        self._assert_candidate_tree_closure(snapshot.entries, admin, config_sha256)
        _, base_tree_objects = _candidate_tree_objects(
            tuple(sorted(base_entries.items())),
            (),
            self.object_format,
        )
        durable_objects = {(base_oid, "commit"), (candidate_oid, "commit")}
        durable_objects.update((oid, "blob") for _, oid in base_entries.values())
        durable_objects.update((oid, "blob") for _, (_, oid) in snapshot.entries)
        durable_objects.update((oid, object_type) for object_type, oid, _ in base_tree_objects)
        durable_objects.update((oid, object_type) for object_type, oid, _ in objects)
        candidate_entries = dict(snapshot.entries)
        if candidate_index is None:
            candidate_index = self._candidate_index_image(tree, candidate_oid, candidate_entries)
        self._assert_recorded_candidate_index(record, candidate_index)
        observed_index = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        topology = self._candidate_index_stage_topology(admin, record, candidate_index)

        head_oid = self._tree_head(tree)
        admin = self._assert_candidate_admin_binding(tree, record)
        if _candidate_head_oid(admin.head_snapshot) != head_oid:
            raise CandidateUncertain("attempt Git HEAD changed during candidate recovery")
        if record["state"] == "ready":
            if head_oid != candidate_oid:
                raise CandidateUncertain("ready candidate HEAD no longer matches its journal")
            if list(admin.head_snapshot.identity) != record.get("candidate_head_identity"):
                raise CandidateUncertain("ready candidate Git HEAD identity changed")
            if topology != _READY_CANDIDATE_INDEX_TOPOLOGY:  # pragma: no cover - checked by topology validator
                raise CandidateUncertain("ready candidate has unresolved Git index staging")
            if self._candidate_index_state(record, observed_index) != "candidate":
                raise CandidateUncertain("ready candidate Git index does not match its tree")
            self._assert_candidate_index(
                tree,
                candidate_entries,
                label="candidate",
                index_path=admin.index_path,
                index_snapshot=observed_index,
            )
            self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
            self._assert_candidate_config_snapshot(admin, config_sha256)
            return self._candidate_receipt(record, state="ready")
        if record["state"] != "prepared":  # pragma: no cover - validated before dispatch
            raise CandidateUncertain("candidate journal has an unknown state")
        if head_oid not in {base_oid, candidate_oid}:
            raise CandidateUncertain("attempt HEAD conflicts with the prepared candidate")
        if head_oid == base_oid and (
            list(admin.head_snapshot.identity) != record.get("base_head_identity")
            or admin.head_snapshot.sha256 != record.get("base_head_sha256")
        ):
            raise CandidateUncertain("attempt Git HEAD changed before candidate installation")
        candidate_head_identity = record.get("candidate_head_identity")
        if (
            head_oid == candidate_oid
            and candidate_head_identity is not None
            and (
                not isinstance(candidate_head_identity, list)
                or list(admin.head_snapshot.identity) != candidate_head_identity
            )
        ):
            raise CandidateUncertain("candidate Git HEAD identity changed during recovery")
        index_state = self._candidate_index_state(record, observed_index)
        expected_entries = base_entries if index_state == "base" else candidate_entries
        self._assert_candidate_index(
            tree,
            expected_entries,
            label=f"{index_state} tree",
            index_path=admin.index_path,
            index_snapshot=observed_index,
        )
        if head_oid == base_oid and index_state != "base":
            raise CandidateUncertain("candidate Git index advanced before its HEAD")
        if head_oid == base_oid and topology:
            raise CandidateUncertain("candidate Git index staging advanced before its HEAD")
        self._assert_candidate_config_snapshot(admin, config_sha256)

        _checkpoint("candidate-objects-written")
        self._import_candidate_closure(tuple(sorted(durable_objects)))
        _checkpoint("candidate-closure-imported")
        self._assert_candidate_snapshot_identity(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(admin, config_sha256)
        self._verify_candidate_object(record, expected_content=commit_content)
        if self._candidate_index_snapshot(admin, record, label="attempt Git index") != observed_index:
            raise CandidateUncertain("attempt Git index changed before candidate HEAD update")
        if self._candidate_index_stage_topology(admin, record, candidate_index) != topology:
            raise CandidateUncertain("attempt Git index staging changed before candidate HEAD update")
        admin = self._assert_candidate_admin_binding(tree, record)
        if _candidate_head_oid(admin.head_snapshot) != head_oid:
            raise CandidateUncertain("attempt Git HEAD changed before candidate HEAD update")
        if head_oid == base_oid and (
            list(admin.head_snapshot.identity) != record.get("base_head_identity")
            or admin.head_snapshot.sha256 != record.get("base_head_sha256")
        ):
            raise CandidateUncertain("attempt Git HEAD changed before candidate installation")
        if (
            head_oid == candidate_oid
            and record.get("candidate_head_identity") is not None
            and list(admin.head_snapshot.identity) != record.get("candidate_head_identity")
        ):
            raise CandidateUncertain("candidate Git HEAD identity changed before recovery")

        if head_oid == base_oid:
            updated = self._run_tree_git(
                tree,
                ["update-ref", "--no-deref", "HEAD", candidate_oid, base_oid],
                check=False,
            )
            if updated.returncode != 0:
                observed = self._tree_head(tree)
                if observed != candidate_oid:
                    raise CandidateUncertain(_git_failure("git update-ref", updated))
        _checkpoint("candidate-head-updated")
        if self._tree_head(tree) != candidate_oid:
            raise CandidateUncertain("candidate HEAD update could not be verified")
        admin = self._assert_candidate_admin_binding(tree, record)
        if _candidate_head_oid(admin.head_snapshot) != candidate_oid:
            raise CandidateUncertain("candidate Git HEAD update could not be bound")
        if record.get("candidate_head_identity") is None:
            updated = dict(record)
            updated["candidate_head_identity"] = list(admin.head_snapshot.identity)
            self._write_candidate_journal(journal, updated)
            record.clear()
            record.update(updated)
            _checkpoint("candidate-head-recorded")
        elif list(admin.head_snapshot.identity) != record.get("candidate_head_identity"):
            raise CandidateUncertain("candidate Git HEAD identity changed after installation")
        self._assert_candidate_config_snapshot(admin, config_sha256)

        observed_index = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        index_state = self._candidate_index_state(record, observed_index)
        if index_state == "base" or topology != _READY_CANDIDATE_INDEX_TOPOLOGY:
            observed_index = self._replace_candidate_index(record, journal, admin, observed_index, candidate_index)
        _checkpoint("candidate-index-updated")
        admin = self._assert_candidate_admin_binding(tree, record)
        if _candidate_head_oid(admin.head_snapshot) != candidate_oid or list(
            admin.head_snapshot.identity
        ) != record.get("candidate_head_identity"):
            raise CandidateUncertain("candidate Git HEAD changed before candidate result recording")
        if self._candidate_index_state(record, observed_index) != "candidate":  # pragma: no cover - invariant
            raise CandidateUncertain("candidate Git index update could not be verified")
        self._assert_candidate_index(
            tree,
            candidate_entries,
            label="candidate",
            index_path=admin.index_path,
            index_snapshot=observed_index,
        )
        self._assert_candidate_config_snapshot(admin, config_sha256)
        self._assert_candidate_tree_closure(snapshot.entries, admin, config_sha256)

        ready = dict(record)
        ready.update(
            {
                "state": "ready",
                "candidate_index_identity": list(observed_index.identity),
                "ready_ns": time.time_ns(),
            }
        )
        self._write_candidate_journal(journal, ready)
        _checkpoint("candidate-result-recorded")
        admin = self._assert_candidate_admin_binding(tree, ready)
        if self._tree_head(tree) != candidate_oid or _candidate_head_oid(admin.head_snapshot) != candidate_oid:
            raise CandidateUncertain("candidate HEAD changed after its result was recorded")
        if list(admin.head_snapshot.identity) != ready.get("candidate_head_identity"):
            raise CandidateUncertain("candidate Git HEAD identity changed after its result was recorded")
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(ready, expected_content=commit_content)
        final_index = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        if self._candidate_index_stage_topology(admin, ready, candidate_index) != _READY_CANDIDATE_INDEX_TOPOLOGY:
            raise CandidateUncertain("ready candidate has unresolved Git index staging")
        if self._candidate_index_state(ready, final_index) != "candidate":
            raise CandidateUncertain("ready candidate Git index identity could not be verified")
        self._assert_candidate_index(
            tree,
            candidate_entries,
            label="candidate",
            index_path=admin.index_path,
            index_snapshot=final_index,
        )
        self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(admin, config_sha256)
        return self._candidate_receipt(ready, state="ready")

    def _candidate_base_entries(self, base_oid: str) -> dict[str, tuple[str, str]]:
        tree_oid = self._verified_candidate_commit_tree_oid(base_oid)
        proc = self._run_git_bytes(["--no-replace-objects", "ls-tree", "-r", "-z", "--full-tree", tree_oid])
        entries: dict[str, tuple[str, str]] = {}
        for entry in proc.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, separator, encoded_path = entry.partition(b"\t")
            fields = metadata.split()
            try:
                path = encoded_path.decode("utf-8")
                mode, object_type, oid = (field.decode("ascii") for field in fields)
            except (UnicodeDecodeError, ValueError):
                raise CandidateUncertain("base commit tree output is malformed") from None
            if not separator or not _safe_candidate_path(path) or len(fields) != 3:
                raise CandidateUncertain("base commit tree output is malformed")
            if object_type == "commit" or mode == "160000":
                raise CandidateUncertain(f"candidate commits reject submodules: {path}")
            if object_type != "blob" or mode not in {"100644", "100755"}:
                kind = "symbolic links" if mode == "120000" else "unsupported entries"
                raise CandidateUncertain(f"candidate commits reject {kind}: {path}")
            _validate_oid(oid)
            if path in entries:
                raise CandidateUncertain("base commit tree contains duplicate paths")
            entries[path] = (mode, oid)
        reconstructed_tree_oid = _candidate_tree_oid(tuple(sorted(entries.items())), self.object_format)
        if reconstructed_tree_oid != tree_oid:
            raise CandidateUncertain("base commit tree object has an invalid identity")
        self._verify_candidate_blob_batch(tuple(sorted({oid for _, oid in entries.values()})))
        return entries

    def _candidate_snapshot(
        self,
        tree: Path,
        base_entries: Mapping[str, tuple[str, str]],
        allowed_paths: tuple[str, ...],
    ) -> _CandidateSnapshot:
        allowed = frozenset(allowed_paths)
        git_identity = _git_entry_identity(tree)
        files, contents, identities, directories = _snapshot_regular_tree(
            tree,
            self.object_format,
            retained_paths=allowed,
            required_paths=frozenset(base_entries) | allowed,
        )
        if _git_entry_identity(tree) != git_identity:
            raise CandidateUncertain("attempt worktree .git entry changed during candidate inspection")
        current_paths = frozenset(files)
        changed_outside = sorted(
            path
            for path in current_paths | frozenset(base_entries)
            if path not in allowed and files.get(path) != base_entries.get(path)
        )
        if changed_outside:
            raise CandidateUncertain(f"candidate changed path outside the allowed set: {changed_outside[0]}")
        extra = sorted(current_paths - frozenset(base_entries) - allowed)
        if extra:  # pragma: no cover - included in changed_outside, retained for a precise invariant
            raise CandidateUncertain(f"candidate contains foreign output: {extra[0]}")
        added_allowed = sorted((current_paths - frozenset(base_entries)) & allowed)
        if added_allowed:
            payload = b"".join(b"./" + path.encode("utf-8") + b"\0" for path in added_allowed)
            ignored = self._run_tree_git_bytes(
                tree,
                ["check-ignore", "--no-index", "--stdin", "-z"],
                check=False,
                input_bytes=payload,
            )
            if ignored.returncode not in {0, 1}:
                detail = ignored.stderr.decode("utf-8", errors="replace").strip()[:500]
                raise CandidateUncertain(f"candidate ignore classification failed: {detail}")
            if ignored.stdout:
                first = ignored.stdout.split(b"\0", 1)[0].decode("utf-8", errors="replace")
                raise CandidateUncertain(f"candidate allowed path is ignored: {first}")
        entries = dict(base_entries)
        for path in allowed:
            value = files.get(path)
            if value is None:
                entries.pop(path, None)
            else:
                entries[path] = value
        blobs: dict[str, bytes] = {}
        for path in allowed:
            if path not in entries:
                continue
            _, oid = entries[path]
            content = contents[path]
            if _git_blob_oid(content, self.object_format) != oid:  # pragma: no cover - snapshot invariant
                raise CandidateUncertain(f"candidate blob identity changed while collecting {path}")
            blobs[oid] = content
        return _CandidateSnapshot(
            entries=tuple(sorted(entries.items())),
            blobs=tuple(sorted(blobs.items())),
            git_identity=git_identity,
            file_identities=tuple(sorted(identities.items())),
            directory_identities=tuple(sorted(directories.items())),
        )

    def _assert_candidate_snapshot(
        self,
        tree: Path,
        base_entries: Mapping[str, tuple[str, str]],
        allowed_paths: tuple[str, ...],
        expected: _CandidateSnapshot,
    ) -> None:
        self._verify_tree_binding(tree, require_head=True)
        observed = self._candidate_snapshot(tree, base_entries, allowed_paths)
        if observed != expected:
            raise CandidateUncertain("candidate path was replaced or changed during commit creation")

    def _assert_candidate_snapshot_identity(
        self,
        tree: Path,
        base_entries: Mapping[str, tuple[str, str]],
        allowed_paths: tuple[str, ...],
        expected: _CandidateSnapshot,
    ) -> None:
        """Reject a changed checkout without rereading every file before installation."""
        self._verify_tree_binding(tree, require_head=True)
        git_identity = _git_entry_identity(tree)
        _, _, identities, directories = _snapshot_regular_tree(
            tree,
            self.object_format,
            retained_paths=frozenset(),
            required_paths=frozenset(base_entries) | frozenset(allowed_paths),
            hash_files=False,
        )
        observed = (
            git_identity,
            tuple(sorted(identities.items())),
            tuple(sorted(directories.items())),
        )
        wanted = (expected.git_identity, expected.file_identities, expected.directory_identities)
        if observed != wanted:
            raise CandidateUncertain("candidate path was replaced or changed during commit creation")

    def _candidate_admin_binding(self, tree: Path) -> _CandidateAdminBinding:
        dot_git = tree / ".git"
        try:
            info = dot_git.stat(follow_symlinks=False)
        except OSError as error:
            raise CandidateUncertain("attempt worktree .git entry cannot be inspected safely") from error
        content, _ = _read_candidate_regular_file(dot_git, info, ".git")
        try:
            line = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CandidateUncertain("attempt worktree .git entry is not UTF-8") from error
        if "\0" in line or len(line.splitlines()) != 1 or not line.startswith("gitdir: "):
            raise CandidateUncertain("attempt worktree .git entry is malformed")
        value = line.removeprefix("gitdir: ").strip()
        if not value:
            raise CandidateUncertain("attempt worktree .git entry has no Git directory")
        raw = Path(value)
        if not raw.is_absolute():
            raw = tree / raw
        try:
            git_dir = _existing_real_directory(raw, label="attempt Git directory")
            worktrees = _existing_real_directory(
                self.common_git_dir / "worktrees",
                label="linked-worktree Git directory",
            )
        except RepositoryError as error:
            raise CandidateUncertain(str(error)) from error
        if git_dir.parent != worktrees:
            raise CandidateUncertain("attempt Git directory is outside the repository worktree administration area")
        head_snapshot, static_sha256 = self._candidate_admin_control_snapshot(tree, dot_git, git_dir)
        binding = _CandidateAdminBinding(
            path=git_dir,
            identity=_directory_identity(git_dir),
            index_path=git_dir / "index",
            head_snapshot=head_snapshot,
            static_sha256=static_sha256,
        )
        self._verify_tree_binding(tree, require_head=True)
        final_head, final_static_sha256 = self._candidate_admin_control_snapshot(tree, dot_git, git_dir)
        if final_head != head_snapshot or final_static_sha256 != static_sha256:
            raise CandidateUncertain("attempt Git administration changed during inspection")
        if _directory_identity(git_dir) != binding.identity:
            raise CandidateUncertain("attempt Git directory changed during inspection")
        return binding

    def _candidate_admin_control_snapshot(
        self,
        tree: Path,
        dot_git: Path,
        git_dir: Path,
    ) -> tuple[_CandidateFileSnapshot, str]:
        """Validate linked-worktree control files without following aliases."""
        head = _candidate_private_file_snapshot(git_dir / "HEAD", label="attempt Git HEAD")
        _candidate_head_oid(head)
        head_lock = git_dir / "HEAD.lock"
        if head_lock.exists() or head_lock.is_symlink():
            raise CandidateUncertain("attempt Git HEAD lock contains foreign state")
        commondir_path = git_dir / "commondir"
        commondir = _candidate_private_file_snapshot(commondir_path, label="attempt Git commondir")
        common_value = _candidate_control_path(commondir.content, base=git_dir, label="attempt Git commondir")
        try:
            observed_common = _existing_real_directory(common_value, label="attempt Git common directory")
        except RepositoryError as error:
            raise CandidateUncertain(str(error)) from error
        if observed_common != self.common_git_dir:
            raise CandidateUncertain("attempt Git commondir points outside the recorded repository")

        gitdir_path = git_dir / "gitdir"
        gitdir = _candidate_private_file_snapshot(gitdir_path, label="attempt Git worktree pointer")
        worktree_value = _candidate_control_path(gitdir.content, base=git_dir, label="attempt Git worktree pointer")
        if worktree_value != dot_git:
            raise CandidateUncertain("attempt Git worktree pointer names a different checkout")

        directory_values: list[tuple[str, tuple[int, int] | None]] = []
        for name in ("logs", "refs"):
            path = git_dir / name
            if not path.exists() and not path.is_symlink():
                directory_values.append((name, None))
                continue
            try:
                directory = _existing_real_directory(path, label=f"attempt Git {name} directory")
            except RepositoryError as error:
                raise CandidateUncertain(str(error)) from error
            directory_values.append((name, _directory_identity(directory)))
        logs_head = git_dir / "logs" / "HEAD"
        if logs_head.exists() or logs_head.is_symlink():
            _candidate_private_file_snapshot(logs_head, label="attempt Git HEAD reflog")

        static = (
            (str(commondir_path), commondir.identity, commondir.sha256),
            (str(gitdir_path), gitdir.identity, gitdir.sha256),
            *directory_values,
        )
        return head, hashlib.sha256(_json_bytes(static)).hexdigest()

    def _assert_candidate_admin_binding(
        self,
        tree: Path,
        record: Mapping[str, object],
    ) -> _CandidateAdminBinding:
        observed = self._candidate_admin_binding(tree)
        expected = (
            record.get("git_dir"),
            record.get("git_dir_device"),
            record.get("git_dir_inode"),
            record.get("git_admin_static_sha256"),
        )
        if (str(observed.path), *observed.identity, observed.static_sha256) != expected:
            raise CandidateUncertain("attempt Git directory changed after candidate preparation")
        return observed

    def _candidate_index_entries(
        self,
        tree: Path,
        *,
        index_path: Path,
        index_snapshot: _CandidateFileSnapshot,
    ) -> dict[str, tuple[str, str]]:
        if (
            _candidate_bound_file_snapshot(
                index_path,
                expected=(int(index_snapshot.identity[0]), int(index_snapshot.identity[1])),
                label="attempt Git index",
            )
            != index_snapshot
        ):
            raise CandidateUncertain("attempt Git index changed before inspection")
        flags = self._run_tree_git_with_index(tree, index_path, ["ls-files", "-v", "-z", "--cached"])
        flagged_paths: set[str] = set()
        for entry in flags.stdout.split("\0"):
            if not entry:
                continue
            tag, separator, path = entry.partition(" ")
            if not separator or not _safe_candidate_path(path) or tag != "H" or path in flagged_paths:
                raise CandidateUncertain("candidate index has noncanonical flags or entries")
            flagged_paths.add(path)
        proc = self._run_tree_git_with_index(
            tree,
            index_path,
            ["ls-files", "--stage", "-z", "--cached"],
        )
        entries: dict[str, tuple[str, str]] = {}
        for entry in proc.stdout.split("\0"):
            if not entry:
                continue
            metadata, separator, path = entry.partition("\t")
            fields = metadata.split()
            if not separator or not _safe_candidate_path(path) or len(fields) != 3:
                raise CandidateUncertain("candidate index output is malformed")
            mode, oid, stage = fields
            if mode not in {"100644", "100755"} or stage != "0":
                raise CandidateUncertain(f"candidate index contains an unsupported entry: {path}")
            _validate_oid(oid)
            if path in entries:
                raise CandidateUncertain("candidate index contains duplicate paths")
            entries[path] = (mode, oid)
        if flagged_paths != set(entries):
            raise CandidateUncertain("candidate index flag and stage inventories differ")
        if (
            _candidate_bound_file_snapshot(
                index_path,
                expected=(int(index_snapshot.identity[0]), int(index_snapshot.identity[1])),
                label="attempt Git index",
            )
            != index_snapshot
        ):
            raise CandidateUncertain("attempt Git index changed during inspection")
        return entries

    def _assert_candidate_index(
        self,
        tree: Path,
        expected: Mapping[str, tuple[str, str]],
        *,
        label: str,
        index_path: Path,
        index_snapshot: _CandidateFileSnapshot,
    ) -> None:
        if self._candidate_index_entries(
            tree,
            index_path=index_path,
            index_snapshot=index_snapshot,
        ) != dict(expected):
            raise CandidateUncertain(f"candidate index does not match the {label}")

    def _candidate_index_image(
        self,
        tree: Path,
        candidate_oid: str,
        expected_entries: Mapping[str, tuple[str, str]],
    ) -> _CandidateFileSnapshot:
        with tempfile.TemporaryDirectory(prefix="autoform-candidate-index-") as scratch:
            scratch_path = _existing_real_directory(
                Path(scratch).resolve(strict=True),
                label="candidate index scratch directory",
            )
            index_path = scratch_path / "index"
            self._run_tree_git_with_index(
                tree,
                index_path,
                [
                    "-c",
                    "index.version=2",
                    "-c",
                    "core.splitIndex=false",
                    "-c",
                    "index.sparse=false",
                    "read-tree",
                    "--no-sparse-checkout",
                    candidate_oid,
                ],
            )
            snapshot = _candidate_private_file_snapshot(index_path, label="generated candidate Git index")
            self._assert_candidate_index(
                tree,
                expected_entries,
                label="candidate tree",
                index_path=index_path,
                index_snapshot=snapshot,
            )
            return snapshot

    def _replace_candidate_index(
        self,
        record: dict[str, object],
        journal: Path,
        admin: _CandidateAdminBinding,
        expected_base: _CandidateFileSnapshot,
        candidate: _CandidateFileSnapshot,
    ) -> _CandidateFileSnapshot:
        if _directory_identity(admin.path) != admin.identity:
            raise CandidateUncertain("attempt Git directory changed before index update")
        observed = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        topology = self._candidate_index_stage_topology(admin, record, candidate)
        transitioning = topology in {
            frozenset({"stage", "index", "displaced-lock"}),
            frozenset({"stage", "index", "displaced-backup"}),
        }
        if not transitioning and observed != expected_base:
            raise CandidateUncertain("attempt Git index does not match its recorded base before update")
        self._ensure_candidate_index_stage(record, journal, admin, candidate)
        topology = self._candidate_index_stage_topology(admin, record, candidate)
        stage = self._candidate_index_stage_path(admin, record)
        backup = self._candidate_index_backup_path(admin, record)
        lock = admin.index_path.with_name("index.lock")
        if topology == frozenset({"stage"}):
            try:
                os.link(stage, lock, follow_symlinks=False)
            except OSError as error:
                raise CandidateUncertain("candidate Git index lock could not be acquired atomically") from error
            _fsync_directory(admin.path)
            _checkpoint("candidate-index-locked")
            topology = self._candidate_index_stage_topology(admin, record, candidate)
        if topology == frozenset({"stage", "lock"}):
            _checkpoint("candidate-index-stage-retained")
            topology = self._candidate_index_stage_topology(admin, record, candidate)
        if topology == frozenset({"stage", "lock"}):
            if _directory_identity(admin.path) != admin.identity:
                raise CandidateUncertain("attempt Git directory changed during index staging")
            current = _candidate_private_file_snapshot(admin.index_path, label="attempt Git index")
            if current != expected_base or not _candidate_snapshot_matches_recorded_base(record, current):
                raise CandidateUncertain("attempt Git index changed during index staging")
            _checkpoint("candidate-index-before-exchange")
            try:
                _exchange_paths(lock, admin.index_path)
                _fsync_directory(admin.path)
            except OSError as error:
                raise CandidateUncertain("candidate Git index could not be exchanged atomically") from error
            _checkpoint("candidate-index-exchanged")
            try:
                topology = self._candidate_index_stage_topology(admin, record, candidate)
            except CandidateUncertain:
                self._restore_displaced_candidate_index(admin, record, candidate, lock)
                raise
        if topology == frozenset({"stage", "index", "displaced-lock"}):
            _checkpoint("candidate-index-before-displace")
            try:
                topology = self._candidate_index_stage_topology(admin, record, candidate)
            except CandidateUncertain:
                self._restore_displaced_candidate_index(admin, record, candidate, lock)
                raise
            try:
                _rename_noreplace(lock, backup)
                _fsync_directory(admin.path)
            except OSError as error:
                self._restore_displaced_candidate_index(admin, record, candidate, lock)
                raise CandidateUncertain("displaced Git index could not be preserved") from error
            _checkpoint("candidate-index-displaced")
            try:
                topology = self._candidate_index_stage_topology(admin, record, candidate)
            except CandidateUncertain:
                self._restore_displaced_candidate_index(admin, record, candidate, backup)
                raise
        if topology != _READY_CANDIDATE_INDEX_TOPOLOGY:
            raise CandidateUncertain("candidate Git index staging has an invalid durable state")
        installed = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        if (
            installed.identity[:2]
            != (record.get("candidate_index_stage_device"), record.get("candidate_index_stage_inode"))
            or installed.sha256 != candidate.sha256
            or installed.content != candidate.content
        ):
            raise CandidateUncertain("candidate Git index replacement could not be verified")
        return installed

    def _restore_displaced_candidate_index(
        self,
        admin: _CandidateAdminBinding,
        record: Mapping[str, object],
        candidate: _CandidateFileSnapshot,
        displaced: Path,
    ) -> None:
        """Restore a displaced index after a failed post-exchange validation."""
        expected = (int(record["candidate_index_stage_device"]), int(record["candidate_index_stage_inode"]))
        installed = _candidate_bound_file_snapshot(
            admin.index_path,
            expected=expected,
            label="installed candidate Git index",
        )
        if installed.sha256 != candidate.sha256 or installed.content != candidate.content:
            raise CandidateUncertain("candidate Git index changed before rollback")
        try:
            _exchange_paths(admin.index_path, displaced)
            _fsync_directory(admin.path)
        except OSError as error:
            raise CandidateUncertain("displaced Git index could not be restored") from error
        if displaced != admin.index_path.with_name("index.lock"):
            lock = admin.index_path.with_name("index.lock")
            try:
                _rename_noreplace(displaced, lock)
                _fsync_directory(admin.path)
            except OSError as error:
                raise CandidateUncertain("candidate Git index lock could not be restored") from error

    def _new_candidate_index_stage_name(self, admin: _CandidateAdminBinding) -> str:
        for _ in range(128):
            name = f".autoform-candidate-index-{secrets.token_hex(16)}.stage"
            path = admin.path / name
            backup = path.with_suffix(".base")
            if not path.exists() and not path.is_symlink() and not backup.exists() and not backup.is_symlink():
                return name
        raise CandidateUncertain("candidate Git index stage name could not be allocated")

    def _ensure_candidate_index_stage(
        self,
        record: dict[str, object],
        journal: Path,
        admin: _CandidateAdminBinding,
        candidate: _CandidateFileSnapshot,
    ) -> None:
        stage = self._candidate_index_stage_path(admin, record)
        stage_device = record.get("candidate_index_stage_device")
        stage_inode = record.get("candidate_index_stage_inode")
        if _is_integer(stage_device) and _is_integer(stage_inode):
            topology = self._candidate_index_stage_topology(admin, record, candidate)
            if topology == frozenset({"stage"}):
                staged = _candidate_bound_file_snapshot(
                    stage,
                    expected=(int(stage_device), int(stage_inode)),
                    label="candidate Git index stage",
                )
                if staged.sha256 != candidate.sha256 or staged.content != candidate.content:
                    self._write_candidate_index_stage(stage, (int(stage_device), int(stage_inode)), candidate)
                    _fsync_directory(admin.path)
                    _checkpoint("candidate-index-stage-written")
                _checkpoint("candidate-index-staged")
            return
        if stage_device is not None or stage_inode is not None:
            raise CandidateUncertain("candidate journal has incomplete index staging identity")
        self._assert_candidate_index_lock_absent(admin)
        if stage.exists() or stage.is_symlink():
            self._abandon_unbound_candidate_index_stage(record, journal, admin, stage)
            stage = self._candidate_index_stage_path(admin, record)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(stage, flags, 0o600)
        except OSError as error:
            raise CandidateUncertain("candidate Git index stage could not be created safely") from error
        open_descriptor = descriptor
        try:
            created = os.fstat(descriptor)
            if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
                raise CandidateUncertain("candidate Git index stage was not created as a private regular file")
            os.fsync(descriptor)
            _fsync_directory(admin.path)
            _checkpoint("candidate-index-stage-created-before-journal")
            updated = dict(record)
            updated.update(
                {
                    "candidate_index_stage_device": created.st_dev,
                    "candidate_index_stage_inode": created.st_ino,
                }
            )
            self._write_candidate_journal(journal, updated)
            record.clear()
            record.update(updated)
            _checkpoint("candidate-index-stage-created")
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                open_descriptor = -1
                stream.write(candidate.content)
                stream.flush()
                os.fsync(stream.fileno())
            staged = _candidate_private_file_snapshot(stage, label="candidate Git index stage")
            if staged.sha256 != candidate.sha256 or staged.content != candidate.content:
                raise CandidateUncertain("candidate Git index stage differs from its durable intent")
            _fsync_directory(admin.path)
            _checkpoint("candidate-index-stage-written")
            _checkpoint("candidate-index-staged")
        except BaseException:
            if open_descriptor >= 0:
                os.close(open_descriptor)
            raise

    def _abandon_unbound_candidate_index_stage(
        self,
        record: dict[str, object],
        journal: Path,
        admin: _CandidateAdminBinding,
        stage: Path,
    ) -> None:
        """Preserve an unbound pre-journal stage and durably choose a fresh name."""
        abandoned = record.get("candidate_index_abandoned_stages")
        if not isinstance(abandoned, list) or len(abandoned) >= _MAX_CANDIDATE_ABANDONED_INDEX_STAGES:
            raise CandidateUncertain("candidate Git index abandoned stage limit was reached")
        try:
            info = stage.stat(follow_symlinks=False)
        except OSError as error:
            raise CandidateUncertain("unbound candidate Git index stage cannot be inspected") from error
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise CandidateUncertain("unbound candidate Git index stage is preserved as foreign state")
        name = self._new_candidate_index_stage_name(admin)
        updated = dict(record)
        updated.update(
            {
                "candidate_index_stage_name": name,
                "candidate_index_backup_name": name.removesuffix(".stage") + ".base",
                "candidate_index_abandoned_stages": [
                    *abandoned,
                    {"name": stage.name, "device": info.st_dev, "inode": info.st_ino},
                ],
            }
        )
        self._write_candidate_journal(journal, updated)
        record.clear()
        record.update(updated)
        _checkpoint("candidate-index-stage-abandoned")

    @staticmethod
    def _write_candidate_index_stage(
        stage: Path,
        expected: tuple[int, int],
        candidate: _CandidateFileSnapshot,
    ) -> None:
        flags = os.O_WRONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(stage, flags)
        except OSError as error:
            raise CandidateUncertain("candidate Git index stage cannot be resumed safely") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != expected:
                raise CandidateUncertain("candidate Git index stage changed before recovery")
            os.ftruncate(descriptor, 0)
            remaining = memoryview(candidate.content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:  # pragma: no cover - operating-system write invariant
                    raise CandidateUncertain("candidate Git index stage could not be completed")
                remaining = remaining[written:]
            os.fsync(descriptor)
        except OSError as error:
            raise CandidateUncertain("candidate Git index stage could not be completed") from error
        finally:
            os.close(descriptor)
        staged = _candidate_bound_file_snapshot(stage, expected=expected, label="candidate Git index stage")
        if staged.sha256 != candidate.sha256 or staged.content != candidate.content:
            raise CandidateUncertain("candidate Git index stage differs from its durable intent")

    def _candidate_index_stage_path(self, admin: _CandidateAdminBinding, record: Mapping[str, object]) -> Path:
        name = record.get("candidate_index_stage_name")
        if not isinstance(name, str) or not _CANDIDATE_INDEX_STAGE_NAME.fullmatch(name):
            raise CandidateUncertain("candidate journal has an invalid index stage name")
        return admin.path / name

    @staticmethod
    def _candidate_index_snapshot(
        admin: _CandidateAdminBinding,
        record: Mapping[str, object],
        *,
        label: str,
    ) -> _CandidateFileSnapshot:
        device = record.get("candidate_index_stage_device")
        inode = record.get("candidate_index_stage_inode")
        if _is_integer(device) and _is_integer(inode):
            try:
                info = admin.index_path.stat(follow_symlinks=False)
            except OSError as error:
                raise CandidateUncertain(f"{label} cannot be inspected safely") from error
            if (info.st_dev, info.st_ino) == (int(device), int(inode)):
                return _candidate_bound_file_snapshot(
                    admin.index_path,
                    expected=(int(device), int(inode)),
                    label=label,
                )
        return _candidate_private_file_snapshot(admin.index_path, label=label)

    def _candidate_index_backup_path(self, admin: _CandidateAdminBinding, record: Mapping[str, object]) -> Path:
        name = record.get("candidate_index_backup_name")
        if not isinstance(name, str) or not _CANDIDATE_INDEX_BACKUP_NAME.fullmatch(name):
            raise CandidateUncertain("candidate journal has an invalid index backup name")
        return admin.path / name

    def _candidate_index_stage_topology(
        self,
        admin: _CandidateAdminBinding,
        record: Mapping[str, object],
        candidate: _CandidateFileSnapshot,
    ) -> frozenset[str]:
        device = record.get("candidate_index_stage_device")
        inode = record.get("candidate_index_stage_inode")
        lock = admin.index_path.with_name("index.lock")
        stage = self._candidate_index_stage_path(admin, record)
        backup = self._candidate_index_backup_path(admin, record)
        abandoned = _candidate_abandoned_index_stages(record)
        reserved_names = {stage.name, backup.name, *(name for name, _ in abandoned)}
        try:
            before = admin.path.stat(follow_symlinks=False)
            with os.scandir(admin.path) as iterator:
                foreign_stages = sorted(
                    entry.name
                    for entry in iterator
                    if entry.name.startswith(".autoform-candidate-index-")
                    and (entry.name.endswith(".stage") or entry.name.endswith(".base"))
                    and entry.name not in reserved_names
                )
            after = admin.path.stat(follow_symlinks=False)
        except OSError as error:
            raise CandidateUncertain("candidate Git index stage inventory cannot be inspected") from error
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(after.st_mode)
            or (before.st_dev, before.st_ino) != admin.identity
            or _candidate_stat_identity(after) != _candidate_stat_identity(before)
        ):
            raise CandidateUncertain("attempt Git directory changed during index stage inspection")
        if foreign_stages:
            raise CandidateUncertain("attempt Git directory contains a foreign candidate index stage")
        for name, expected in abandoned:
            path = admin.path / name
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as error:
                raise CandidateUncertain("abandoned candidate Git index stage cannot be inspected") from error
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != expected
            ):
                raise CandidateUncertain("abandoned candidate Git index stage changed and is preserved")
        if device is None and inode is None:
            if record.get("state") == "ready":
                raise CandidateUncertain("ready candidate has no durable index staging identity")
            if lock.exists() or lock.is_symlink():
                raise CandidateUncertain("attempt Git index lock contains foreign state")
            if backup.exists() or backup.is_symlink():
                raise CandidateUncertain("attempt Git index backup contains foreign state")
            if stage.exists() or stage.is_symlink():
                try:
                    info = stage.stat(follow_symlinks=False)
                except OSError as error:
                    raise CandidateUncertain("candidate Git index stage cannot be inspected") from error
                if stat.S_ISDIR(info.st_mode):
                    raise CandidateUncertain("candidate Git index stage is a directory")
                return frozenset({"pending"})
            return frozenset()
        if not _is_integer(device) or not _is_integer(inode):
            raise CandidateUncertain("candidate journal has an invalid index staging identity")

        locations = {
            "stage": stage,
            "lock": lock,
            "index": admin.index_path,
            "backup": backup,
        }
        present: dict[str, _CandidateFileSnapshot] = {}
        expected = (int(device), int(inode))
        base_identity = record.get("base_index_identity")
        if not isinstance(base_identity, list) or len(base_identity) != 7:
            raise CandidateUncertain("candidate journal has an invalid base index identity")
        expected_base = (int(base_identity[0]), int(base_identity[1]))
        for label, path in locations.items():
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                if path.is_symlink():
                    raise CandidateUncertain(f"candidate Git index {label} is a dangling symbolic link")
                continue
            except OSError as error:
                raise CandidateUncertain(f"candidate Git index {label} cannot be inspected") from error
            if (info.st_dev, info.st_ino) != expected:
                if label == "index":
                    continue
                if label in {"lock", "backup"} and (info.st_dev, info.st_ino) == expected_base:
                    displaced = _candidate_bound_file_snapshot(
                        path,
                        expected=expected_base,
                        label=f"displaced candidate Git index {label}",
                    )
                    if not _candidate_snapshot_matches_recorded_base(record, displaced):
                        raise CandidateUncertain("displaced candidate Git index differs from its recorded base")
                    present[f"displaced-{label}"] = displaced
                    continue
                raise CandidateUncertain(f"candidate Git index {label} is foreign state")
            present[label] = _candidate_bound_file_snapshot(
                path, expected=expected, label=f"candidate Git index {label}"
            )
        topology = frozenset(present)
        if topology not in {
            frozenset({"stage"}),
            frozenset({"stage", "lock"}),
            frozenset({"stage", "index", "displaced-lock"}),
            frozenset({"stage", "index", "displaced-backup"}),
        }:
            raise CandidateUncertain("candidate Git index stage has an invalid link topology")
        candidate_locations = {label for label in topology if not label.startswith("displaced-")}
        for label, snapshot in present.items():
            if label.startswith("displaced-"):
                continue
            if snapshot.identity[3] != len(candidate_locations):
                raise CandidateUncertain("candidate Git index stage has an external hard link")
            if snapshot.sha256 != candidate.sha256 or snapshot.content != candidate.content:
                if record.get("state") != "prepared" or topology != frozenset({"stage"}):
                    raise CandidateUncertain("candidate Git index stage differs from its durable intent")
        if record.get("state") == "ready" and topology != _READY_CANDIDATE_INDEX_TOPOLOGY:
            raise CandidateUncertain("ready candidate has unresolved Git index staging")
        return topology

    def _assert_candidate_index_lock_absent(self, admin: _CandidateAdminBinding) -> None:
        lock = admin.index_path.with_name("index.lock")
        if lock.exists() or lock.is_symlink():
            raise CandidateUncertain("attempt Git index lock already exists")

    def _assert_recorded_candidate_index(
        self,
        record: Mapping[str, object],
        candidate: _CandidateFileSnapshot,
    ) -> None:
        if len(candidate.content) != record.get("candidate_index_size") or candidate.sha256 != record.get(
            "candidate_index_sha256"
        ):
            raise CandidateUncertain("generated candidate Git index differs from its durable intent")

    def _candidate_index_state(
        self,
        record: Mapping[str, object],
        observed: _CandidateFileSnapshot,
    ) -> str:
        base_identity = record.get("base_index_identity")
        if (
            isinstance(base_identity, list)
            and list(observed.identity) == base_identity
            and observed.sha256 == record.get("base_index_sha256")
        ):
            return "base"
        if len(observed.content) == record.get("candidate_index_size") and observed.sha256 == record.get(
            "candidate_index_sha256"
        ):
            if record.get("state") == "ready":
                expected_identity = record.get("candidate_index_identity")
                if not isinstance(expected_identity, list) or list(observed.identity) != expected_identity:
                    raise CandidateUncertain("ready candidate Git index identity changed")
            else:
                stage_device = record.get("candidate_index_stage_device")
                stage_inode = record.get("candidate_index_stage_inode")
                if not _is_integer(stage_device) or not _is_integer(stage_inode):
                    raise CandidateUncertain("candidate Git index advanced without a durable staged identity")
                if observed.identity[:2] != (stage_device, stage_inode):
                    raise CandidateUncertain("candidate Git index does not match its durable staged identity")
            return "candidate"
        raise CandidateUncertain("attempt Git index conflicts with the durable candidate transition")

    def _candidate_config_snapshot(self, admin: _CandidateAdminBinding) -> tuple[tuple[str, object], ...]:
        if _directory_identity(admin.path) != admin.identity:
            raise CandidateUncertain("attempt Git directory changed during configuration inspection")
        config_paths = (self.common_git_dir / "config", admin.path / "config.worktree")
        values: list[tuple[str, object]] = []
        for path in config_paths:
            snapshot = _optional_candidate_private_file_snapshot(path, label="repository configuration")
            if snapshot is not None:
                _assert_candidate_config_is_self_contained(self, snapshot.content)
                value: object = [*snapshot.identity, snapshot.sha256]
            else:
                value = None
            values.append((str(path), value))
        try:
            info_dir = _existing_real_directory(self.common_git_dir / "info", label="repository info directory")
        except RepositoryError as error:
            raise CandidateUncertain(str(error)) from error
        values.append((str(info_dir), list(_directory_identity(info_dir))))
        exclude_path = info_dir / "exclude"
        exclude = _optional_candidate_private_file_snapshot(exclude_path, label="repository exclude file")
        values.append(
            (
                str(exclude_path),
                None if exclude is None else [*exclude.identity, exclude.sha256],
            )
        )
        if _directory_identity(admin.path) != admin.identity:
            raise CandidateUncertain("attempt Git directory changed during configuration inspection")
        return tuple(values)

    def _assert_candidate_config_snapshot(
        self,
        admin: _CandidateAdminBinding,
        expected_sha256: str,
    ) -> None:
        observed = hashlib.sha256(_json_bytes(self._candidate_config_snapshot(admin))).hexdigest()
        if observed != expected_sha256:
            raise CandidateUncertain("repository configuration changed during candidate creation")

    def _assert_candidate_tree_closure(
        self,
        entries: tuple[tuple[str, tuple[str, str]], ...],
        admin: _CandidateAdminBinding,
        expected_config_sha256: str,
    ) -> None:
        """Require every blob in the exact candidate tree to be available and hash-valid."""
        oids = tuple(sorted({oid for _, (_, oid) in entries}))
        for oid in oids:
            _validate_oid(oid)
            if len(oid) != hashlib.new(self.object_format).digest_size * 2:
                raise CandidateUncertain("candidate blob id does not match the repository object format")
        self._assert_candidate_config_snapshot(admin, expected_config_sha256)
        self._verify_candidate_blob_batch(oids)
        self._assert_candidate_config_snapshot(admin, expected_config_sha256)

    def _verify_candidate_blob_batch(self, oids: tuple[str, ...]) -> None:
        """Stream and independently hash exact blobs through one hardened Git process."""
        self._verify_candidate_object_batch(
            tuple((oid, "blob") for oid in oids),
            label="candidate tree closure",
        )

    def _verified_candidate_commit_tree_oid(self, oid: str) -> str:
        """Return the tree named by an independently hash-verified commit object."""
        (tree_oid,) = self._verify_candidate_object_batch(((oid, "commit"),), label="base commit")
        if tree_oid is None:  # pragma: no cover - commit parser invariant
            raise CandidateUncertain("base commit has an invalid commit tree header")
        return tree_oid

    def _verify_candidate_object_batch(
        self,
        objects: tuple[tuple[str, str], ...],
        *,
        label: str,
        expected_contents: tuple[bytes, ...] | None = None,
        object_directory: Path | None = None,
    ) -> tuple[str | None, ...]:
        """Stream and independently hash exact objects through one hardened Git process."""
        if expected_contents is not None and len(expected_contents) != len(objects):
            raise CandidateUncertain(f"{label} has an invalid expected object inventory")
        self._verify_repository()
        process: subprocess.Popen[bytes] | None = None
        stderr_thread: threading.Thread | None = None
        watchdog_thread: threading.Thread | None = None
        timed_out = threading.Event()
        stderr_seen = threading.Event()
        stderr_prefix = bytearray()
        stderr_errors: list[BaseException] = []
        cleanup_error: BaseException | None = None
        watchdog_condition = threading.Condition()
        watchdog_finished = False
        watchdog_deadline = time.monotonic() + _CANDIDATE_BATCH_TIMEOUT_S

        def note_progress() -> None:
            nonlocal watchdog_deadline
            with watchdog_condition:
                watchdog_deadline = time.monotonic() + _CANDIDATE_BATCH_TIMEOUT_S
                watchdog_condition.notify_all()

        def finish_watchdog() -> None:
            nonlocal watchdog_finished
            with watchdog_condition:
                watchdog_finished = True
                watchdog_condition.notify_all()

        try:
            with tempfile.TemporaryFile() as requests:
                for oid, _ in objects:
                    requests.write(f"{oid}\n".encode("ascii"))
                requests.seek(0)
                environment = _git_environment()
                if object_directory is not None:
                    environment["GIT_OBJECT_DIRECTORY"] = str(object_directory)
                try:
                    process = subprocess.Popen(
                        _git_command(
                            [
                                "--no-replace-objects",
                                "cat-file",
                                "--batch=%(objectname) %(objecttype) %(objectsize)",
                            ]
                        ),
                        cwd=self.repository_root,
                        env=environment,
                        stdin=requests,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                except OSError as error:
                    raise CandidateUncertain(f"{label} Git process failed: {error}") from error
                if process.stdout is None or process.stderr is None:  # pragma: no cover - PIPE invariant
                    raise CandidateUncertain(f"{label} Git process has no output pipes")

                def drain_stderr() -> None:
                    assert process is not None and process.stderr is not None
                    try:
                        while chunk := process.stderr.read(64 * 1024):
                            stderr_seen.set()
                            remaining = _CANDIDATE_BATCH_STDERR_BYTES - len(stderr_prefix)
                            if remaining > 0:
                                stderr_prefix.extend(chunk[:remaining])
                    except (OSError, ValueError) as error:  # pragma: no cover - operating-system pipe failure
                        stderr_errors.append(error)

                stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
                stderr_thread.start()

                def watch_for_inactivity() -> None:
                    assert process is not None
                    with watchdog_condition:
                        while not watchdog_finished:
                            remaining = watchdog_deadline - time.monotonic()
                            if remaining > 0:
                                watchdog_condition.wait(timeout=remaining)
                                continue
                            timed_out.set()
                            break
                        if watchdog_finished:
                            return
                    try:
                        process.kill()
                    except OSError:
                        pass

                note_progress()
                watchdog_thread = threading.Thread(target=watch_for_inactivity, daemon=True)
                watchdog_thread.start()
                try:
                    result = _verify_candidate_object_batch_output(
                        process.stdout,
                        objects,
                        self.object_format,
                        label=label,
                        progress=note_progress,
                        expected_contents=expected_contents,
                    )
                except CandidateUncertain as error:
                    if timed_out.is_set():
                        raise CandidateUncertain(f"{label} Git process timed out") from error
                    raise
                finally:
                    finish_watchdog()
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired as error:
                    raise CandidateUncertain(f"{label} Git process did not exit") from error
                stderr_thread.join(timeout=5)
                if timed_out.is_set():
                    raise CandidateUncertain(f"{label} Git process timed out")
                if stderr_thread.is_alive() or stderr_errors:
                    raise CandidateUncertain(f"{label} Git stderr could not be drained")
                if returncode != 0 or stderr_seen.is_set():
                    detail = stderr_prefix.decode("utf-8", errors="replace").strip()
                    raise CandidateUncertain(f"{label} is incomplete: {detail}")
                return result
        finally:
            finish_watchdog()
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=1)
                if watchdog_thread.is_alive():  # pragma: no cover - operating-system thread failure
                    cleanup_error = CandidateUncertain(f"{label} timeout thread did not stop")
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as error:  # pragma: no cover - operating-system process failure
                        cleanup_error = error
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - OS process failure
                    cleanup_error = error
                if process.stdout is not None:
                    try:
                        process.stdout.close()
                    except OSError as error:  # pragma: no cover - operating-system pipe failure
                        cleanup_error = error
                if stderr_thread is not None:
                    stderr_thread.join(timeout=5)
                if process.stderr is not None:
                    try:
                        process.stderr.close()
                    except OSError as error:  # pragma: no cover - operating-system pipe failure
                        cleanup_error = error
                if stderr_thread is not None and stderr_thread.is_alive():
                    stderr_thread.join(timeout=1)
                    if stderr_thread.is_alive():  # pragma: no cover - operating-system thread failure
                        cleanup_error = CandidateUncertain(f"{label} stderr thread did not stop")
            self._verify_repository()
            if cleanup_error is not None:
                raise CandidateUncertain(f"{label} Git process could not be reaped") from cleanup_error

    def _write_candidate_objects(self, objects: tuple[tuple[str, str, bytes], ...]) -> None:
        """Write candidate objects in a constant number of Git invocations."""
        grouped: dict[str, list[tuple[str, bytes]]] = {"blob": [], "tree": [], "commit": []}
        for object_type, expected_oid, content in objects:
            if object_type not in grouped or _git_object_oid(object_type, content, self.object_format) != expected_oid:
                raise CandidateUncertain("candidate object inventory has an invalid identity")
            grouped[object_type].append((expected_oid, content))

        with tempfile.TemporaryDirectory(prefix="autoform-candidate-objects-") as scratch:
            scratch_path = Path(scratch)
            for object_type, values in grouped.items():
                if not values:
                    continue
                paths: list[Path] = []
                for index, (_, content) in enumerate(values):
                    path = scratch_path / f"{object_type}-{index}"
                    path.write_bytes(content)
                    paths.append(path)
                payload = b"".join(os.fsencode(path) + b"\n" for path in paths)
                proc = self._run_git_bytes(
                    ["hash-object", "-t", object_type, "-w", "--stdin-paths", "--no-filters"],
                    input_bytes=payload,
                )
                try:
                    observed = proc.stdout.decode("ascii").splitlines()
                except UnicodeDecodeError as error:  # pragma: no cover - Git object IDs are ASCII
                    raise CandidateUncertain("git hash-object returned malformed object ids") from error
                if observed != [oid for oid, _ in values]:
                    raise CandidateUncertain(f"git wrote unexpected {object_type} objects")
        self._verify_candidate_objects(objects)

    def _verify_candidate_objects(self, objects: tuple[tuple[str, str, bytes], ...]) -> None:
        self._verify_candidate_object_batch(
            tuple((expected_oid, object_type) for object_type, expected_oid, _ in objects),
            label="candidate object set",
            expected_contents=tuple(content for _, _, content in objects),
        )

    def _verify_candidate_object(
        self,
        record: Mapping[str, object],
        *,
        expected_content: bytes | None = None,
    ) -> None:
        candidate_oid = str(record["candidate_oid"])
        proc = self._run_git_bytes(["cat-file", "commit", candidate_oid], check=False)
        if proc.returncode != 0:
            raise CandidateUncertain("candidate commit object is missing")
        content = proc.stdout
        if _git_object_oid("commit", content, self.object_format) != candidate_oid:
            raise CandidateUncertain("candidate commit object has an invalid identity")
        if expected_content is not None:
            if content != expected_content:
                raise CandidateUncertain("candidate commit bytes differ from the durable intent")
        else:
            prefix = _candidate_commit_prefix(
                str(record["tree_oid"]),
                str(record["base_oid"]),
                str(record["author_name"]),
                str(record["author_email"]),
            )
            if not content.startswith(prefix):
                raise CandidateUncertain("candidate commit metadata differs from the durable intent")
            if hashlib.sha256(content[len(prefix) :]).hexdigest() != record["message_sha256"]:
                raise CandidateUncertain("candidate commit message differs from the durable intent")

    def _import_candidate_closure(self, closure_objects: tuple[tuple[str, str], ...]) -> None:
        """Copy the exact candidate/base tree closure into owned primary storage."""
        closure: dict[str, str] = {}
        for oid, object_type in closure_objects:
            try:
                _validate_oid(oid)
            except RepositoryError as error:
                raise CandidateUncertain("candidate closure inventory is malformed") from error
            if len(oid) != hashlib.new(self.object_format).digest_size * 2:
                raise CandidateUncertain("candidate closure object id has the wrong format")
            if object_type not in {"blob", "tree", "commit"}:
                raise CandidateUncertain("candidate closure inventory has an invalid object type")
            previous = closure.setdefault(oid, object_type)
            if previous != object_type:
                raise CandidateUncertain("candidate closure inventory has conflicting object types")
        if not closure:
            raise CandidateUncertain("candidate closure inventory is empty")
        loose = {oid for oid in closure if self._primary_loose_object_exists(oid)}
        packed = self._primary_packed_object_oids(tuple((oid, closure[oid]) for oid in sorted(set(closure) - loose)))
        missing = tuple(oid for oid in closure if oid not in loose and oid not in packed)
        if not missing:
            return

        self._verify_repository()
        with tempfile.TemporaryFile() as pack:
            try:
                packed = subprocess.run(
                    _git_command(
                        [
                            "--no-replace-objects",
                            "pack-objects",
                            "--stdout",
                            "--no-reuse-delta",
                            "--no-reuse-object",
                            "--no-thin",
                            "--no-include-tag",
                            "--window=0",
                        ]
                    ),
                    cwd=self.repository_root,
                    env=_git_environment(),
                    input=b"".join(f"{oid}\n".encode("ascii") for oid in missing),
                    stdout=pack,
                    stderr=subprocess.PIPE,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CandidateUncertain(f"candidate closure pack failed: {error}") from error
            self._verify_repository()
            if packed.returncode != 0:
                detail = packed.stderr.decode("utf-8", errors="replace").strip()[:500]
                raise CandidateUncertain(f"candidate closure pack failed: {detail}")
            pack.flush()
            os.fsync(pack.fileno())
            pack.seek(0)
            try:
                installed = subprocess.run(
                    _git_command(["--no-replace-objects", "index-pack", "--stdin", "--strict"]),
                    cwd=self.repository_root,
                    env=_git_environment(),
                    stdin=pack,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CandidateUncertain(f"candidate closure installation failed: {error}") from error
        self._verify_repository()
        if installed.returncode != 0:
            detail = installed.stderr.decode("utf-8", errors="replace").strip()[:500]
            raise CandidateUncertain(f"candidate closure installation failed: {detail}")
        match = re.fullmatch(rb"pack\t([0-9a-f]+)\n", installed.stdout)
        if match is None or len(match.group(1)) != hashlib.new(self.object_format).digest_size * 2:
            raise CandidateUncertain("candidate closure installation returned a malformed pack id")
        pack_oid = match.group(1).decode("ascii")
        pack_dir = _existing_real_directory(self.object_dir / "pack", label="primary Git pack directory")
        installed_pack = pack_dir / f"pack-{pack_oid}.pack"
        installed_index = pack_dir / f"pack-{pack_oid}.idx"
        _fsync_primary_pack_file(installed_pack, label="installed candidate closure pack")
        _fsync_primary_pack_file(installed_index, label="installed candidate closure pack index")
        try:
            _fsync_directory(pack_dir)
        except OSError as error:
            raise CandidateUncertain("candidate closure pack directory could not be synchronized") from error
        verified = self._verified_primary_pack_objects(installed_pack, installed_index)
        expected = {(oid, closure[oid]) for oid in missing}
        if verified != expected:
            raise CandidateUncertain("installed candidate closure pack does not match its exact inventory")
        self._verify_repository()

    def _primary_loose_object_exists(self, oid: str) -> bool:
        path = self.object_dir / oid[:2] / oid[2:]
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            if path.is_symlink():
                raise CandidateUncertain("primary Git object is a dangling symbolic link")
            return False
        except OSError as error:
            raise CandidateUncertain("primary Git object cannot be inspected") from error
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise CandidateUncertain("primary Git object is not a regular file")
        return True

    def _primary_packed_object_oids(self, wanted: tuple[tuple[str, str], ...]) -> frozenset[str]:
        if not wanted:
            return frozenset()
        pack_dir = _existing_real_directory(self.object_dir / "pack", label="primary Git pack directory")
        try:
            packs = sorted(pack_dir.glob("pack-*.pack"))
        except OSError as error:
            raise CandidateUncertain("primary Git pack inventory cannot be inspected") from error
        if not packs:
            return frozenset()

        self._verify_repository()
        bindings = {
            pack: (
                _primary_pack_file_identity(pack, label="primary Git pack"),
                _primary_pack_file_identity(pack.with_suffix(".idx"), label="primary Git pack index"),
            )
            for pack in packs
        }
        process: subprocess.Popen[bytes] | None = None
        timer: threading.Timer | None = None
        timed_out = threading.Event()
        found: set[str] = set()
        trace_offset = 0
        with tempfile.TemporaryFile() as trace, tempfile.TemporaryFile() as stderr:
            environment = _git_environment()
            environment["GIT_TRACE_PACK_ACCESS"] = _inherited_descriptor_path(trace.fileno())
            try:
                process = subprocess.Popen(
                    _git_command(
                        ["--no-replace-objects", "cat-file", "--batch=%(objectname) %(objecttype) %(objectsize)"]
                    ),
                    cwd=self.repository_root,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    pass_fds=(trace.fileno(),),
                )
            except OSError as error:
                raise CandidateUncertain(f"primary Git packed object inspection failed: {error}") from error

            def expire() -> None:
                timed_out.set()
                if process is not None:
                    try:
                        process.kill()
                    except OSError:
                        pass

            timer = threading.Timer(_CANDIDATE_BATCH_TIMEOUT_S, expire)
            timer.daemon = True
            timer.start()
            try:
                if process.stdin is None or process.stdout is None:  # pragma: no cover - PIPE invariant
                    raise CandidateUncertain("primary Git packed object inspection has no data pipes")
                for oid, object_type in wanted:
                    try:
                        process.stdin.write(f"{oid}\n".encode("ascii"))
                        process.stdin.flush()
                    except OSError as error:
                        raise CandidateUncertain("primary Git packed object request failed") from error
                    _verify_candidate_object_batch_output(
                        process.stdout,
                        ((oid, object_type),),
                        self.object_format,
                        label="primary Git packed object",
                        require_eof=False,
                    )
                    trace_size = os.fstat(trace.fileno()).st_size
                    if trace_size < trace_offset:
                        raise CandidateUncertain("primary Git pack trace changed during inspection")
                    if trace_size - trace_offset > _PRIMARY_PACK_TRACE_MAX_BYTES:
                        raise CandidateUncertain("primary Git pack trace exceeded its per-object limit")
                    try:
                        trace_bytes = os.pread(trace.fileno(), trace_size - trace_offset, trace_offset)
                    except OSError as error:
                        raise CandidateUncertain("primary Git pack trace could not be read") from error
                    trace_offset = trace_size
                    accesses = _primary_pack_trace_paths(trace_bytes, repository_root=self.repository_root)
                    primary_accesses = {path for path in accesses if path in bindings}
                    if primary_accesses and primary_accesses != set(accesses):
                        raise CandidateUncertain("primary Git pack lookup fell back to alternate storage")
                    if primary_accesses:
                        found.add(oid)
                process.stdin.close()
                if process.stdout.read(1):
                    raise CandidateUncertain("primary Git packed object inspection returned trailing output")
                returncode = process.wait()
            finally:
                if timer is not None:
                    timer.cancel()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            stderr.seek(0)
            detail_bytes = stderr.read(_CANDIDATE_BATCH_STDERR_BYTES + 1)
        if timed_out.is_set():
            raise CandidateUncertain("primary Git packed object inspection timed out")
        if returncode != 0 or detail_bytes:
            detail = detail_bytes.decode("utf-8", errors="replace").strip()[:_CANDIDATE_BATCH_STDERR_BYTES]
            raise CandidateUncertain(f"primary Git packed object inspection failed: {detail}")
        for pack, (pack_identity, index_identity) in bindings.items():
            if _primary_pack_file_identity(pack, label="primary Git pack") != pack_identity or (
                _primary_pack_file_identity(pack.with_suffix(".idx"), label="primary Git pack index") != index_identity
            ):
                raise CandidateUncertain("primary Git pack storage changed during inspection")
        self._verify_repository()
        return frozenset(found)

    def _verified_primary_pack_objects(
        self,
        pack: Path,
        index: Path,
    ) -> frozenset[tuple[str, str]]:
        """Verify one exact primary pack and cache its object inventory."""
        self._verify_repository()
        pack_stat = _primary_pack_file_identity(pack, label="primary Git pack")
        index_stat = _primary_pack_file_identity(index, label="primary Git pack index")
        pack_identity, pack_sha256 = _primary_pack_file_digest(pack, label="primary Git pack")
        index_identity, index_sha256 = _primary_pack_file_digest(index, label="primary Git pack index")
        if pack_identity != pack_stat or index_identity != index_stat:
            raise CandidateUncertain("primary Git pack storage changed before verification")
        objects = self._verify_primary_pack_output(pack, index)
        if _primary_pack_file_digest(pack, label="primary Git pack") != (pack_identity, pack_sha256) or (
            _primary_pack_file_digest(index, label="primary Git pack index") != (index_identity, index_sha256)
        ):
            raise CandidateUncertain("primary Git pack storage changed during verification")
        self._verify_repository()
        return objects

    def _verify_primary_pack_output(self, pack: Path, index: Path) -> frozenset[tuple[str, str]]:
        """Run Git's pack verifier with bounded, streamed output."""
        process: subprocess.Popen[bytes] | None = None
        timed_out = threading.Event()
        timer: threading.Timer | None = None
        objects: set[tuple[str, str]] = set()
        total_bytes = 0
        ok_line = os.fsencode(pack) + b": ok"
        saw_ok = False
        failure: str | None = None
        with tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    _git_command(["--no-replace-objects", "verify-pack", "-v", "--", str(index)]),
                    cwd=self.repository_root,
                    env=_git_environment(),
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                )
            except OSError as error:
                raise CandidateUncertain(f"primary Git pack verification failed: {error}") from error

            def expire() -> None:
                timed_out.set()
                if process is not None:
                    try:
                        process.kill()
                    except OSError:
                        pass

            timer = threading.Timer(_CANDIDATE_BATCH_TIMEOUT_S, expire)
            timer.daemon = True
            timer.start()
            try:
                if process.stdout is None:  # pragma: no cover - subprocess invariant
                    raise CandidateUncertain("primary Git pack verifier has no output stream")
                while True:
                    line = process.stdout.readline(_PRIMARY_PACK_VERIFY_MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    total_bytes += len(line)
                    if total_bytes > _PRIMARY_PACK_VERIFY_MAX_BYTES:
                        failure = "primary Git pack verification exceeded its output limit"
                        process.kill()
                        break
                    if len(line) > _PRIMARY_PACK_VERIFY_MAX_LINE_BYTES or not line.endswith(b"\n"):
                        failure = "primary Git pack verification returned a malformed line"
                        process.kill()
                        break
                    value = line[:-1]
                    match = re.fullmatch(
                        rb"([0-9a-f]{40}|[0-9a-f]{64}) +(commit|tree|blob|tag) +"
                        rb"[0-9]+ +[0-9]+ +[0-9]+(?: +[0-9]+ +[0-9a-f]{40,64})?",
                        value,
                    )
                    if match is not None:
                        oid = match.group(1).decode("ascii")
                        object_type = match.group(2).decode("ascii")
                        if len(oid) != hashlib.new(self.object_format).digest_size * 2:
                            failure = "primary Git pack verification returned an invalid object id"
                            process.kill()
                            break
                        objects.add((oid, object_type))
                        if len(objects) > _PRIMARY_PACK_VERIFY_MAX_OBJECTS:
                            failure = "primary Git pack verification exceeded its object limit"
                            process.kill()
                            break
                    elif re.fullmatch(rb"non delta: [0-9]+ objects?", value) or re.fullmatch(
                        rb"chain length = [0-9]+: [0-9]+ objects?", value
                    ):
                        continue
                    elif value == ok_line:
                        saw_ok = True
                    else:
                        failure = "primary Git pack verification returned malformed output"
                        process.kill()
                        break
                returncode = process.wait()
            finally:
                if timer is not None:
                    timer.cancel()
                if process.stdout is not None:
                    process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            stderr.seek(0)
            detail_bytes = stderr.read(_CANDIDATE_BATCH_STDERR_BYTES + 1)
        if timed_out.is_set():
            raise CandidateUncertain("primary Git pack verification timed out")
        if failure is not None:
            raise CandidateUncertain(failure)
        if returncode != 0 or detail_bytes:
            detail = detail_bytes.decode("utf-8", errors="replace").strip()[:_CANDIDATE_BATCH_STDERR_BYTES]
            raise CandidateUncertain(f"primary Git pack verification failed: {detail}")
        if not saw_ok:
            raise CandidateUncertain("primary Git pack verification returned no success marker")
        return frozenset(objects)

    def _assert_ready_candidate_tree(self, record: Mapping[str, object], tree: Path) -> None:
        if record["state"] != "ready":
            raise CandidateUncertain("candidate recovery must finish before cleanup")
        admin = self._assert_candidate_admin_binding(tree, record)
        config_sha256 = hashlib.sha256(_json_bytes(self._candidate_config_snapshot(admin))).hexdigest()
        base_entries = self._candidate_base_entries(str(record["base_oid"]))
        paths = tuple(str(path) for path in record["allowed_paths"])
        snapshot = self._candidate_snapshot(tree, base_entries, paths)
        tree_oid, objects = _candidate_tree_objects(snapshot.entries, snapshot.blobs, self.object_format)
        if tree_oid != record["tree_oid"]:
            raise CandidateUncertain("candidate worktree no longer matches the recorded tree")
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(record)
        self._assert_candidate_tree_closure(snapshot.entries, admin, config_sha256)
        if (
            self._tree_head(tree) != record["candidate_oid"]
            or _candidate_head_oid(admin.head_snapshot) != record["candidate_oid"]
            or list(admin.head_snapshot.identity) != record.get("candidate_head_identity")
        ):
            raise CandidateUncertain("ready candidate HEAD no longer matches its journal")
        index = self._candidate_index_snapshot(admin, record, label="attempt Git index")
        candidate_index = self._candidate_index_image(tree, str(record["candidate_oid"]), dict(snapshot.entries))
        self._assert_recorded_candidate_index(record, candidate_index)
        if self._candidate_index_stage_topology(admin, record, candidate_index) != _READY_CANDIDATE_INDEX_TOPOLOGY:
            raise CandidateUncertain("ready candidate has unresolved Git index staging")
        if self._candidate_index_state(record, index) != "candidate":
            raise CandidateUncertain("ready candidate Git index identity changed")
        self._assert_candidate_index(
            tree,
            dict(snapshot.entries),
            label="candidate",
            index_path=admin.index_path,
            index_snapshot=index,
        )
        self._assert_candidate_config_snapshot(admin, config_sha256)

    def _candidate_receipt(self, record: Mapping[str, object], *, state: str) -> CandidateReceipt:
        identity = {
            key: record[key]
            for key in (
                "schema",
                "repository_id",
                "run_id",
                "attempt_id",
                "base_oid",
                "path",
                "worktree_identity_sha256",
                "allowed_paths",
                "author_name",
                "author_email",
                "message_sha256",
                "tree_oid",
                "candidate_oid",
                "git_dir",
                "git_dir_device",
                "git_dir_inode",
                "git_admin_static_sha256",
                "base_head_identity",
                "base_head_sha256",
                "candidate_head_identity",
                "base_index_identity",
                "base_index_sha256",
                "candidate_index_size",
                "candidate_index_sha256",
                "candidate_index_identity",
                "candidate_index_stage_name",
                "candidate_index_backup_name",
                "candidate_index_stage_device",
                "candidate_index_stage_inode",
                "candidate_index_abandoned_stages",
                "config_snapshot_sha256",
            )
        }
        return CandidateReceipt(
            run_id=str(record["run_id"]),
            attempt_id=str(record["attempt_id"]),
            repository_id=self.repository_id,
            path=str(record["path"]),
            base_oid=str(record["base_oid"]),
            tree_oid=str(record["tree_oid"]),
            candidate_oid=str(record["candidate_oid"]),
            state=state,
            allowed_paths=tuple(str(path) for path in record["allowed_paths"]),
            author_name=str(record["author_name"]),
            author_email=str(record["author_email"]),
            message_sha256=str(record["message_sha256"]),
            identity_sha256=hashlib.sha256(_json_bytes(identity)).hexdigest(),
        )

    def _read_candidate_journal(self, journal: Path) -> dict[str, object]:
        return _read_json_file(journal, label="candidate journal")

    def _write_candidate_journal(self, journal: Path, record: Mapping[str, object]) -> None:
        if _canonical_existing_directory(journal.parent) != journal.parent or _directory_identity(journal.parent) != (
            record.get("root_device"),
            record.get("root_inode"),
        ):
            raise CandidateUncertain("attempt directory changed before its candidate journal update")
        _write_json_file(journal, record)

    def _validate_candidate_record(
        self,
        record: Mapping[str, object],
        marker_record: Mapping[str, object],
        *,
        expected_identity: Mapping[str, object] | None = None,
    ) -> None:
        expected = {
            "schema": _CANDIDATE_SCHEMA,
            "repository_id": self.repository_id,
            "run_id": marker_record["run_id"],
            "attempt_id": marker_record["attempt_id"],
            "base_oid": marker_record["base_oid"],
            "path": marker_record["path"],
            "worktree_identity_sha256": self._receipt(
                marker_record,
                head_oid=str(marker_record["base_oid"]),
                state="ready",
            ).identity_sha256,
        }
        if expected_identity is not None:
            expected.update(expected_identity)
        for key, value in expected.items():
            if record.get(key) != value:
                raise CandidateUncertain(f"candidate journal has a different {key}")
        if record.get("state") not in {"prepared", "ready"}:
            raise CandidateUncertain("candidate journal has an unknown state")
        paths = record.get("allowed_paths")
        try:
            validated_paths = _validate_candidate_paths(frozenset(paths)) if isinstance(paths, list) else None
        except (RepositoryError, TypeError) as error:
            raise CandidateUncertain("candidate journal has an invalid allowed path set") from error
        if validated_paths is None or tuple(paths) != validated_paths:
            raise CandidateUncertain("candidate journal has an invalid allowed path set")
        if not isinstance(record.get("author_name"), str) or not isinstance(record.get("author_email"), str):
            raise CandidateUncertain("candidate journal has an invalid author identity")
        try:
            _validate_candidate_identity(str(record["author_name"]), str(record["author_email"]))
        except RepositoryError as error:
            raise CandidateUncertain("candidate journal has an invalid author identity") from error
        for key in (
            "message_sha256",
            "git_admin_static_sha256",
            "base_head_sha256",
            "base_index_sha256",
            "candidate_index_sha256",
            "config_snapshot_sha256",
        ):
            if not isinstance(record.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", str(record.get(key))):
                raise CandidateUncertain(f"candidate journal has an invalid {key}")
        if not isinstance(record.get("git_dir"), str) or not Path(str(record["git_dir"])).is_absolute():
            raise CandidateUncertain("candidate journal has an invalid Git directory")
        for key in ("git_dir_device", "git_dir_inode", "candidate_index_size"):
            if not _is_integer(record.get(key)) or int(record[key]) < 0:
                raise CandidateUncertain(f"candidate journal has an invalid {key}")
        for key in ("base_head_identity", "base_index_identity"):
            identity = record.get(key)
            if (
                not isinstance(identity, list)
                or len(identity) != 7
                or any(not _is_integer(value) or int(value) < 0 for value in identity)
            ):
                raise CandidateUncertain(f"candidate journal has an invalid {key}")
        candidate_head_identity = record.get("candidate_head_identity")
        if candidate_head_identity is not None and (
            not isinstance(candidate_head_identity, list)
            or len(candidate_head_identity) != 7
            or any(not _is_integer(value) or int(value) < 0 for value in candidate_head_identity)
        ):
            raise CandidateUncertain("candidate journal has an invalid candidate HEAD identity")
        candidate_index_identity = record.get("candidate_index_identity")
        if record.get("state") == "ready":
            if (
                candidate_head_identity is None
                or not isinstance(candidate_index_identity, list)
                or len(candidate_index_identity) != 7
                or any(not _is_integer(value) or int(value) < 0 for value in candidate_index_identity)
            ):
                raise CandidateUncertain("ready candidate journal has an invalid candidate index identity")
        elif candidate_index_identity is not None:
            raise CandidateUncertain("prepared candidate journal has a premature candidate index identity")
        stage_name = record.get("candidate_index_stage_name")
        backup_name = record.get("candidate_index_backup_name")
        stage_device = record.get("candidate_index_stage_device")
        stage_inode = record.get("candidate_index_stage_inode")
        if not isinstance(stage_name, str) or not _CANDIDATE_INDEX_STAGE_NAME.fullmatch(stage_name):
            raise CandidateUncertain("candidate journal has an invalid index staging identity")
        if not isinstance(backup_name, str) or not _CANDIDATE_INDEX_BACKUP_NAME.fullmatch(backup_name):
            raise CandidateUncertain("candidate journal has an invalid index backup name")
        if backup_name.removesuffix(".base") != stage_name.removesuffix(".stage"):
            raise CandidateUncertain("candidate journal has mismatched index transition names")
        _candidate_abandoned_index_stages(record)
        if stage_device is None and stage_inode is None:
            if record.get("state") == "ready":
                raise CandidateUncertain("ready candidate journal has no index staging identity")
        elif (
            not _is_integer(stage_device)
            or not _is_integer(stage_inode)
            or int(stage_device) < 0
            or int(stage_inode) < 0
        ):
            raise CandidateUncertain("candidate journal has an invalid index staging identity")
        if (
            isinstance(candidate_index_identity, list)
            and stage_device is not None
            and (candidate_index_identity[:2] != [stage_device, stage_inode])
        ):
            raise CandidateUncertain("ready candidate index does not match its staged identity")
        try:
            for key in ("base_oid", "tree_oid", "candidate_oid"):
                _validate_oid(str(record.get(key, "")))
        except RepositoryError as error:
            raise CandidateUncertain("candidate journal has an invalid object id") from error
        if not _is_integer(record.get("root_device")) or not _is_integer(record.get("root_inode")):
            raise CandidateUncertain("candidate journal has an invalid attempt directory identity")
        if (record["root_device"], record["root_inode"]) != (
            marker_record["root_device"],
            marker_record["root_inode"],
        ):
            raise CandidateUncertain("candidate journal belongs to a different attempt directory")
        if not _is_integer(record.get("created_ns")):
            raise CandidateUncertain("candidate journal has an invalid creation time")
        if record["state"] == "ready" and not _is_integer(record.get("ready_ns")):
            raise CandidateUncertain("ready candidate journal has no valid completion time")

    def cleanup(self, run_id: str, attempt_id: str) -> None:
        """Remove only a worktree whose durable identity is still exact."""
        _validate_name("run id", run_id)
        _validate_name("attempt id", attempt_id)
        attempt_root, tree, marker = self._attempt_paths(run_id, attempt_id)
        with self._attempt_lock(run_id, attempt_id):
            self._verify_state()
            if not attempt_root.exists() and not attempt_root.is_symlink():
                self._remove_empty_run_root(attempt_root.parent)
                return
            if not marker.exists() and not marker.is_symlink():
                self._remove_empty_attempt_scaffold(attempt_root, tree)
                self._remove_empty_run_root(attempt_root.parent)
                return
            record = self._read_attempt_marker(marker)
            self._validate_attempt_record(
                record,
                run_id,
                attempt_id,
                attempt_root,
                tree,
                str(record.get("base_oid", "")),
            )
            if record["state"] == "preparing":
                self._resume_preparation(record, marker, tree)
                record = self._read_attempt_marker(marker)
                self._validate_attempt_record(
                    record,
                    run_id,
                    attempt_id,
                    attempt_root,
                    tree,
                    str(record.get("base_oid", "")),
                )
            candidate_journal = attempt_root / "candidate.json"
            candidate_record: dict[str, object] | None = None
            if candidate_journal.exists() or candidate_journal.is_symlink():
                candidate_record = self._read_candidate_journal(candidate_journal)
                self._validate_candidate_record(candidate_record, record)
                if record["state"] != "cleaning":
                    candidate = self.inspect_candidate(run_id, attempt_id)
                    if candidate.state != "ready":  # pragma: no cover - inspect returns or raises
                        raise CandidateUncertain("candidate recovery must finish before cleanup")
                elif candidate_record["state"] != "ready" or candidate_record["candidate_oid"] != record.get(
                    "cleanup_head_oid"
                ):
                    raise CandidateUncertain("interrupted cleanup has unresolved candidate evidence")
            cleanup_tree = attempt_root / "tree-cleaning"
            if record["state"] != "cleaning":
                if tree not in self._registered_worktrees():
                    if tree.exists() or tree.is_symlink():
                        _remove_bound_empty_directory(
                            tree,
                            (int(record["tree_device"]), int(record["tree_inode"])),
                            label="unregistered attempt path",
                            checkpoint="worktree-cleanup-before-empty-tree-quarantine",
                        )
                    else:
                        raise WorktreeConflict("registered attempt worktree disappeared before cleanup")
                else:
                    self._verify_tree_binding(tree, require_head=False)
                    if candidate_record is None:
                        self._assert_canonical_index(tree)
                        status = self._run_tree_git(
                            tree,
                            ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
                        )
                        if status.stdout:
                            raise WorktreeConflict(
                                "attempt worktree contains tracked, untracked, or ignored state; cleanup stopped"
                            )
                        foreign = self._foreign_tree_entry(tree)
                        if foreign is not None:
                            raise WorktreeConflict(f"attempt worktree contains unowned path {foreign}; cleanup stopped")
                    else:
                        self._assert_ready_candidate_tree(candidate_record, tree)
                    if cleanup_tree.exists() or cleanup_tree.is_symlink():
                        raise WorktreeConflict("attempt cleanup quarantine already exists")
                    cleanup_identity = _directory_identity(tree)
                    cleanup_parent_mode = stat.S_IMODE(attempt_root.stat(follow_symlinks=False).st_mode)
                    if cleanup_parent_mode != 0o700:
                        raise WorktreeConflict("attempt directory permissions changed before cleanup")
                    record.update(
                        {
                            "state": "cleaning",
                            "cleanup_path": str(cleanup_tree),
                            "cleanup_device": cleanup_identity[0],
                            "cleanup_inode": cleanup_identity[1],
                            "cleanup_head_oid": self._tree_head(tree),
                            "cleanup_parent_mode": cleanup_parent_mode,
                        }
                    )
                    self._write_attempt_marker(marker, record)
                    _checkpoint("worktree-cleanup-intent-recorded")
            if record["state"] == "cleaning":
                self._continue_cleanup(record, marker, tree, cleanup_tree, candidate_record=candidate_record)
            elif tree.exists() or tree.is_symlink():
                self._verify_tree_binding(tree, require_head=False)
                _remove_bound_empty_directory(
                    tree,
                    (int(record["tree_device"]), int(record["tree_inode"])),
                    label="unregistered attempt path",
                    checkpoint="worktree-cleanup-before-empty-tree-quarantine",
                )
            if candidate_record is not None:
                if not candidate_journal.exists() and not candidate_journal.is_symlink():
                    raise CandidateUncertain("candidate journal disappeared during cleanup")
                final_candidate_record = self._read_candidate_journal(candidate_journal)
                self._validate_candidate_record(final_candidate_record, record)
                if final_candidate_record["state"] != "ready" or final_candidate_record["candidate_oid"] != record.get(
                    "cleanup_head_oid"
                ):
                    raise CandidateUncertain("candidate journal changed during cleanup")
                candidate_snapshot = _candidate_private_file_snapshot(
                    candidate_journal,
                    label="candidate journal",
                )
                if candidate_snapshot.content != _json_bytes(final_candidate_record) + b"\n":
                    raise CandidateUncertain("candidate journal changed during cleanup")
                _remove_bound_private_file(
                    candidate_journal,
                    candidate_snapshot,
                    label="candidate journal",
                    checkpoint="candidate-cleanup-before-journal-quarantine",
                )
                _checkpoint("candidate-journal-removed")
            if marker.exists() or marker.is_symlink():
                marker_snapshot = _candidate_private_file_snapshot(marker, label="attempt marker")
                if marker_snapshot.content != _json_bytes(record) + b"\n":
                    raise WorktreeConflict("attempt marker changed during cleanup")
                _remove_bound_private_file(
                    marker,
                    marker_snapshot,
                    label="attempt marker",
                    checkpoint="worktree-cleanup-before-marker-quarantine",
                )
                _checkpoint("worktree-marker-removed")
            _remove_bound_empty_directory(
                attempt_root,
                (int(record["root_device"]), int(record["root_inode"])),
                label="attempt directory",
                checkpoint="worktree-cleanup-before-attempt-quarantine",
            )
            self._remove_empty_run_root(attempt_root.parent)

    def _continue_cleanup(
        self,
        record: dict[str, object],
        marker: Path,
        tree: Path,
        cleanup_tree: Path,
        *,
        candidate_record: Mapping[str, object] | None,
    ) -> None:
        cleanup_parent_mode = int(record["cleanup_parent_mode"])
        current_parent_mode = stat.S_IMODE(marker.parent.stat(follow_symlinks=False).st_mode)
        if current_parent_mode == 0o500:
            marker.parent.chmod(cleanup_parent_mode)
            _fsync_directory(marker.parent)
        elif current_parent_mode != cleanup_parent_mode:
            raise WorktreeConflict("attempt cleanup directory permissions changed")
        if tree.exists() or tree.is_symlink():
            if cleanup_tree.exists() or cleanup_tree.is_symlink():
                raise WorktreeConflict("attempt cleanup has both live and quarantined worktree paths")
            self._verify_tree_binding(tree, require_head=False)
            if candidate_record is None:
                self._assert_canonical_index(tree)
                status = self._run_tree_git(
                    tree,
                    ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
                )
                if status.stdout or self._foreign_tree_entry(tree) is not None:
                    raise WorktreeConflict("attempt worktree changed after cleanup intent was recorded")
            else:
                self._assert_ready_candidate_tree(candidate_record, tree)
            _checkpoint("worktree-cleanup-before-quarantine")
            try:
                _rename_noreplace(tree, cleanup_tree)
                _fsync_directory(cleanup_tree.parent)
            except OSError as error:
                raise WorktreeConflict("attempt worktree could not be quarantined safely") from error
            if cleanup_tree.is_symlink() or _directory_identity(cleanup_tree) != (
                record["cleanup_device"],
                record["cleanup_inode"],
            ):
                raise WorktreeConflict("attempt worktree was replaced; its quarantine is preserved")
            _checkpoint("worktree-quarantined")

        if cleanup_tree.exists() or cleanup_tree.is_symlink():
            if cleanup_tree.is_symlink() or _directory_identity(cleanup_tree) != (
                record["cleanup_device"],
                record["cleanup_inode"],
            ):
                raise WorktreeConflict("attempt cleanup quarantine was replaced")
            self._remove_quarantined_worktree(record, cleanup_tree)
            _checkpoint("worktree-quarantine-removed")

        if tree in self._listed_worktree_paths():
            try:
                marker.parent.chmod(0o500)
                _fsync_directory(marker.parent)
                if tree.exists() or tree.is_symlink():
                    raise WorktreeConflict("attempt worktree path reappeared during cleanup")
                proc = self._run_git(["worktree", "remove", str(tree)], check=False)
            finally:
                marker.parent.chmod(cleanup_parent_mode)
                _fsync_directory(marker.parent)
            if proc.returncode != 0 and tree in self._listed_worktree_paths():
                raise WorktreeConflict(_git_failure("git worktree remove", proc))
            _checkpoint("worktree-removed")

    def _remove_quarantined_worktree(self, record: Mapping[str, object], cleanup_tree: Path) -> None:
        dot_git = cleanup_tree / ".git"
        if not dot_git.exists() and not dot_git.is_symlink():
            _remove_bound_empty_directory(
                cleanup_tree,
                (int(record["cleanup_device"]), int(record["cleanup_inode"])),
                label="attempt cleanup quarantine",
                checkpoint="worktree-cleanup-before-final-quarantine",
            )
            return
        if _git_entry_identity(cleanup_tree) != (
            record["git_entry_device"],
            record["git_entry_inode"],
            record["git_entry_sha256"],
        ):
            raise WorktreeConflict("attempt cleanup Git identity was replaced")
        entries = self._cleanup_head_entries(str(record["cleanup_head_oid"]))
        tracked_directories: set[Path] = set()
        for relative, (mode, oid) in entries.items():
            path = cleanup_tree.joinpath(*relative.split("/"))
            parent = path.parent
            while parent != cleanup_tree:
                tracked_directories.add(parent)
                parent = parent.parent
            if mode == "160000":
                tracked_directories.add(path)
                continue
            if not path.exists() and not path.is_symlink():
                continue
            before = path.lstat()
            if mode in {"100644", "100755"}:
                expected_executable = mode == "100755"
                if not stat.S_ISREG(before.st_mode) or bool(before.st_mode & stat.S_IXUSR) != expected_executable:
                    raise WorktreeConflict(f"tracked cleanup path type or mode changed: {relative}")
            elif mode == "120000":
                if not stat.S_ISLNK(before.st_mode):
                    raise WorktreeConflict(f"tracked cleanup path type or mode changed: {relative}")
            else:  # pragma: no cover - modes are constrained while parsing the tree
                raise WorktreeConflict(f"tracked cleanup path has unsupported mode: {relative}")
            if mode == "120000":
                try:
                    link_target = os.fsencode(os.readlink(path))
                except OSError as error:
                    raise WorktreeConflict(f"tracked cleanup symlink changed: {relative}") from error
                hashed = _git_blob_oid(link_target, self.object_format)
            else:
                try:
                    content, _ = _read_candidate_regular_file(path, before, relative)
                except CandidateUncertain as error:
                    raise WorktreeConflict(f"tracked cleanup path changed: {relative}") from error
                hashed = _git_blob_oid(content, self.object_format)
            after = path.lstat()
            before_snapshot = _candidate_stat_identity(before)
            after_snapshot = _candidate_stat_identity(after)
            if before_snapshot != after_snapshot or hashed != oid:
                raise WorktreeConflict(f"tracked cleanup path changed or no longer matches HEAD: {relative}")
            quarantine = _quarantine_bound_path(
                path,
                (before.st_dev, before.st_ino),
                label=f"tracked cleanup path {relative}",
                checkpoint=f"worktree-cleanup-before-path-quarantine:{relative}",
                kind="symlink" if mode == "120000" else "regular",
            )

            def verify_quarantined_path(candidate: Path) -> None:
                try:
                    moved = candidate.lstat()
                    moved_snapshot = _candidate_stat_identity(moved)
                    if moved_snapshot[:6] != before_snapshot[:6]:
                        raise WorktreeConflict(f"tracked cleanup path changed while being quarantined: {relative}")
                    if mode == "120000":
                        moved_content = os.fsencode(os.readlink(candidate))
                    else:
                        moved_content, _ = _read_candidate_regular_file(candidate, moved, relative)
                    if _git_blob_oid(moved_content, self.object_format) != oid:
                        raise WorktreeConflict(f"tracked cleanup path changed while being quarantined: {relative}")
                except OSError as error:
                    raise WorktreeConflict(f"tracked cleanup path could not be verified safely: {relative}") from error

            verify_quarantined_path(quarantine)
            quarantine = _final_removal_quarantine(
                quarantine,
                (before.st_dev, before.st_ino),
                label=f"tracked cleanup path {relative}",
                checkpoint=f"worktree-cleanup-before-path-final-isolation:{relative}",
                kind="symlink" if mode == "120000" else "regular",
            )
            try:
                verify_quarantined_path(quarantine)
                quarantine.unlink()
                _fsync_directory(path.parent)
            except OSError as error:
                raise WorktreeConflict(f"tracked cleanup path could not be removed safely: {relative}") from error
        for directory in sorted(tracked_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                identity = _directory_identity(directory)
            except FileNotFoundError:
                continue
            except RepositoryError as error:
                raise WorktreeConflict(f"cleanup quarantine preserves foreign state at {directory}") from error
            _remove_bound_empty_directory(
                directory,
                identity,
                label=f"tracked cleanup directory {directory}",
                checkpoint="worktree-cleanup-before-directory-quarantine",
            )
        remaining = [entry.name for entry in os.scandir(cleanup_tree) if entry.name != ".git"]
        if remaining:
            raise WorktreeConflict(f"cleanup quarantine preserves foreign state at {remaining[0]}")
        expected_git_identity = (
            record["git_entry_device"],
            record["git_entry_inode"],
            record["git_entry_sha256"],
        )
        if _git_entry_identity(cleanup_tree) != expected_git_identity:
            raise WorktreeConflict("attempt cleanup Git identity changed")
        git_snapshot = _candidate_private_file_snapshot(dot_git, label="attempt cleanup Git file")
        if (git_snapshot.identity[0], git_snapshot.identity[1], git_snapshot.sha256) != expected_git_identity:
            raise WorktreeConflict("attempt cleanup Git identity changed")
        _remove_bound_private_file(
            dot_git,
            git_snapshot,
            label="attempt cleanup Git file",
            checkpoint="worktree-cleanup-before-git-quarantine",
        )
        _remove_bound_empty_directory(
            cleanup_tree,
            (int(record["cleanup_device"]), int(record["cleanup_inode"])),
            label="attempt cleanup quarantine",
            checkpoint="worktree-cleanup-before-final-quarantine",
        )

    def _cleanup_head_entries(self, head_oid: str) -> dict[str, tuple[str, str]]:
        _validate_oid(head_oid)
        proc = self._run_git(["ls-tree", "-r", "-z", "--full-tree", head_oid])
        entries: dict[str, tuple[str, str]] = {}
        for entry in proc.stdout.split("\0"):
            if not entry:
                continue
            metadata, separator, path = entry.partition("\t")
            fields = metadata.split()
            if not separator or not _safe_git_path(path) or len(fields) != 3:
                raise WorktreeConflict("cleanup commit tree output is malformed")
            mode, object_type, oid = fields
            if object_type not in {"blob", "commit"} or not _OID.fullmatch(oid):
                raise WorktreeConflict("cleanup commit tree contains an unsupported entry")
            valid_mode = (object_type == "commit" and mode == "160000") or (
                object_type == "blob" and mode in {"100644", "100755", "120000"}
            )
            if not valid_mode or path in entries:
                raise WorktreeConflict("cleanup commit tree contains an invalid entry")
            entries[path] = (mode, oid)
        return entries

    def _remove_empty_attempt_scaffold(self, attempt_root: Path, tree: Path) -> None:
        if attempt_root.is_symlink() or _canonical_existing_directory(attempt_root) != attempt_root:
            raise WorktreeConflict("attempt path without a marker is not a safe empty scaffold")
        _remove_atomic_write_orphans(attempt_root / "attempt.json")
        entries = list(attempt_root.iterdir())
        if entries == [tree] and not tree.is_symlink() and _canonical_existing_directory(tree) == tree:
            tree_identity = _directory_identity(tree)
            _remove_bound_empty_directory(
                tree,
                tree_identity,
                label="unowned empty attempt tree",
                checkpoint="worktree-scaffold-before-tree-quarantine",
            )
            entries = list(attempt_root.iterdir())
        if entries:
            raise WorktreeConflict("attempt path exists without a durable ownership marker")
        _remove_bound_empty_directory(
            attempt_root,
            _directory_identity(attempt_root),
            label="unowned empty attempt scaffold",
            checkpoint="worktree-scaffold-before-attempt-quarantine",
        )

    @staticmethod
    def _remove_empty_run_root(run_root: Path) -> None:
        try:
            info = run_root.stat(follow_symlinks=False)
        except OSError:
            return
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return
        try:
            with os.scandir(run_root) as iterator:
                if next(iterator, None) is not None:
                    return
        except OSError:
            return
        _remove_bound_empty_directory(
            run_root,
            (info.st_dev, info.st_ino),
            label="empty run directory",
            checkpoint="worktree-cleanup-before-run-quarantine",
        )

    def _resume_preparation(
        self,
        record: dict[str, object],
        marker: Path,
        tree: Path,
    ) -> WorktreeReceipt:
        if record["state"] == "ready":
            return self._ready_receipt(record, tree)
        return self._finalize_preparation(record, marker, tree)

    def _finalize_preparation(
        self,
        record: dict[str, object],
        marker: Path,
        tree: Path,
    ) -> WorktreeReceipt:
        registered = self._registered_worktrees()
        if tree not in registered:
            if not tree.exists() or tree.is_symlink():
                raise WorktreeUncertain("the pinned preparing directory is no longer available")
            if _directory_identity(tree) != (record["tree_device"], record["tree_inode"]):
                raise WorktreeUncertain("the preparing directory was replaced")
            try:
                next(tree.iterdir())
            except StopIteration:
                pass
            else:
                raise WorktreeUncertain("unregistered preparing directory is not empty")
            self._run_git(["worktree", "add", "--detach", str(tree), str(record["base_oid"])])
            _checkpoint("worktree-added")
        self._verify_tree_binding(tree, require_head=True)
        head = self._tree_head(tree)
        if head != record["base_oid"]:
            raise WorktreeUncertain("interrupted worktree preparation no longer has the requested base")
        status = self._run_tree_git(
            tree,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"],
        )
        if status.stdout:
            self._repair_missing_preparing_paths(record, tree, status.stdout)
        tree_identity = _directory_identity(tree)
        if tree_identity != (record["tree_device"], record["tree_inode"]):
            raise WorktreeUncertain("worktree directory changed during preparation")
        git_device, git_inode, git_digest = _git_entry_identity(tree)
        record.update(
            {
                "state": "ready",
                "git_entry_device": git_device,
                "git_entry_inode": git_inode,
                "git_entry_sha256": git_digest,
                "ready_ns": time.time_ns(),
            }
        )
        _checkpoint("worktree-verified")
        self._write_attempt_marker(marker, record)
        return self._ready_receipt(record, tree)

    def _repair_missing_preparing_paths(
        self,
        record: Mapping[str, object],
        tree: Path,
        porcelain: str,
    ) -> None:
        """Restore only absent tracked paths from an interrupted initial checkout."""
        try:
            self._assert_canonical_index_flags(tree)
        except WorktreeConflict as error:
            raise WorktreeUncertain("interrupted worktree preparation has a noncanonical index") from error
        base_oid = str(record["base_oid"])
        index = self._run_tree_git(tree, ["diff-index", "--cached", "--quiet", base_oid, "--"], check=False)
        if index.returncode != 0:
            raise WorktreeUncertain("interrupted worktree preparation changed its index")
        missing: list[str] = []
        for entry in porcelain.split("\0"):
            if not entry:
                continue
            relative = entry[3:] if entry.startswith(" D ") else ""
            if not _safe_git_path(relative) or relative in missing:
                raise WorktreeUncertain("interrupted worktree preparation has changes beyond absent tracked paths")
            missing.append(relative)
        if not missing:
            raise WorktreeUncertain("interrupted worktree preparation is not clean")
        git_identity = _git_entry_identity(tree)
        for relative in missing:
            path = tree.joinpath(*relative.split("/"))
            _assert_missing_worktree_path(tree, path)
            _checkpoint("worktree-missing-path-verified")
            restored = self._run_tree_git(
                tree,
                ["checkout-index", "--stdin", "-z"],
                check=False,
                input_text=f"{relative}\0",
            )
            if restored.returncode != 0:
                raise WorktreeUncertain(f"interrupted checkout path could not be restored: {relative}")
            if _git_entry_identity(tree) != git_identity:
                raise WorktreeUncertain("interrupted worktree Git identity changed during repair")
            _checkpoint("worktree-missing-path-restored")
        try:
            self._assert_canonical_index(tree)
        except WorktreeConflict as error:
            raise WorktreeUncertain("interrupted worktree repair did not restore tracked content") from error
        status = self._run_tree_git(
            tree,
            ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
        )
        if status.stdout:
            raise WorktreeUncertain("interrupted worktree repair did not produce a clean checkout")

    def _inspect_preparing(self, record: dict[str, object], tree: Path) -> WorktreeReceipt:
        if tree not in self._registered_worktrees():
            if not tree.exists() or tree.is_symlink():
                raise WorktreeUncertain("the pinned preparing directory is no longer available")
            if _directory_identity(tree) != (record["tree_device"], record["tree_inode"]):
                raise WorktreeUncertain("the preparing directory was replaced")
            try:
                next(tree.iterdir())
            except StopIteration:
                pass
            else:
                raise WorktreeUncertain("unregistered preparing directory is not empty")
            return self._receipt(record, head_oid=str(record["base_oid"]), state="preparing")
        self._verify_tree_binding(tree, require_head=True)
        return self._receipt(record, head_oid=self._tree_head(tree), state="preparing")

    def _ready_receipt(self, record: dict[str, object], tree: Path) -> WorktreeReceipt:
        tree_identity = _directory_identity(tree)
        if tree_identity != (record["tree_device"], record["tree_inode"]):
            raise WorktreeConflict("attempt worktree directory was replaced")
        if _git_entry_identity(tree) != (
            record["git_entry_device"],
            record["git_entry_inode"],
            record["git_entry_sha256"],
        ):
            raise WorktreeConflict("attempt worktree Git identity was replaced")
        if tree not in self._registered_worktrees():
            raise WorktreeConflict("attempt worktree is no longer registered")
        self._verify_tree_binding(tree, require_head=True)
        head = self._tree_head(tree)
        if not self._is_ancestor(str(record["base_oid"]), head):
            raise WorktreeConflict("attempt HEAD is not descended from its recorded base")
        return self._receipt(record, head_oid=head, state="ready")

    def _receipt(self, record: Mapping[str, object], *, head_oid: str, state: str) -> WorktreeReceipt:
        identity = {
            key: record[key]
            for key in (
                "schema",
                "repository_id",
                "run_id",
                "attempt_id",
                "base_oid",
                "path",
                "root_device",
                "root_inode",
                "tree_device",
                "tree_inode",
                "git_entry_device",
                "git_entry_inode",
                "git_entry_sha256",
            )
        }
        return WorktreeReceipt(
            run_id=str(record["run_id"]),
            attempt_id=str(record["attempt_id"]),
            repository_id=self.repository_id,
            path=str(record["path"]),
            base_oid=str(record["base_oid"]),
            head_oid=head_oid,
            state=state,
            identity_sha256=hashlib.sha256(_json_bytes(identity)).hexdigest(),
        )

    def _validate_attempt_record(
        self,
        record: Mapping[str, object],
        run_id: str,
        attempt_id: str,
        attempt_root: Path,
        tree: Path,
        base_oid: str,
    ) -> None:
        _validate_oid(base_oid)
        expected = {
            "schema": _WORKTREE_SCHEMA,
            "repository_id": self.repository_id,
            "repository_root": str(self.repository_root),
            "common_git_dir": str(self.common_git_dir),
            "run_id": run_id,
            "attempt_id": attempt_id,
            "base_oid": base_oid,
            "path": str(tree),
        }
        for key, value in expected.items():
            if record.get(key) != value:
                raise WorktreeConflict(f"attempt marker has a different {key}")
        if record.get("state") not in {"preparing", "ready", "cleaning"}:
            raise WorktreeConflict("attempt marker has an unknown state")
        for key in ("root_device", "root_inode", "tree_device", "tree_inode", "created_ns"):
            if not _is_integer(record.get(key)):
                raise WorktreeConflict(f"attempt marker has an invalid {key}")
        if record.get("state") in {"ready", "cleaning"} and not _is_integer(record.get("ready_ns")):
            raise WorktreeConflict("ready attempt marker has no valid completion time")
        if record.get("state") in {"ready", "cleaning"}:
            if not _is_integer(record.get("git_entry_device")) or not _is_integer(record.get("git_entry_inode")):
                raise WorktreeConflict("ready attempt marker has an invalid Git entry identity")
            if not isinstance(record.get("git_entry_sha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(record.get("git_entry_sha256"))
            ):
                raise WorktreeConflict("ready attempt marker has an invalid Git entry digest")
        cleanup_tree = attempt_root / "tree-cleaning"
        if record.get("state") == "cleaning":
            if record.get("cleanup_path") != str(cleanup_tree):
                raise WorktreeConflict("cleaning attempt marker has a different cleanup_path")
            if not _is_integer(record.get("cleanup_device")) or not _is_integer(record.get("cleanup_inode")):
                raise WorktreeConflict("cleaning attempt marker has an invalid cleanup identity")
            if record.get("cleanup_parent_mode") != 0o700:
                raise WorktreeConflict("cleaning attempt marker has an invalid parent mode")
            _validate_oid(str(record.get("cleanup_head_oid", "")))
        root_identity = _directory_identity(attempt_root)
        if root_identity != (record.get("root_device"), record.get("root_inode")):
            raise WorktreeConflict("attempt directory was replaced")
        if _canonical_existing_directory(attempt_root) != attempt_root:
            raise WorktreeConflict("attempt path changed through a symbolic link")
        if tree.exists() or tree.is_symlink():
            if tree.is_symlink() or _directory_identity(tree) != (
                record.get("tree_device"),
                record.get("tree_inode"),
            ):
                raise WorktreeConflict("attempt worktree directory was replaced")
            if record.get("state") in {"ready", "cleaning"} and _git_entry_identity(tree) != (
                record.get("git_entry_device"),
                record.get("git_entry_inode"),
                record.get("git_entry_sha256"),
            ):
                raise WorktreeConflict("attempt worktree Git identity was replaced")
        if record.get("state") == "cleaning" and (cleanup_tree.exists() or cleanup_tree.is_symlink()):
            if tree.exists() or tree.is_symlink():
                raise WorktreeConflict("cleaning attempt has both live and quarantined paths")
            if cleanup_tree.is_symlink() or _directory_identity(cleanup_tree) != (
                record.get("cleanup_device"),
                record.get("cleanup_inode"),
            ):
                raise WorktreeConflict("attempt cleanup quarantine was replaced")
            cleanup_git = cleanup_tree / ".git"
            if cleanup_git.exists() or cleanup_git.is_symlink():
                if _git_entry_identity(cleanup_tree) != (
                    record.get("git_entry_device"),
                    record.get("git_entry_inode"),
                    record.get("git_entry_sha256"),
                ):
                    raise WorktreeConflict("attempt cleanup Git identity was replaced")
            else:
                try:
                    next(cleanup_tree.iterdir())
                except StopIteration:
                    pass
                else:
                    raise WorktreeConflict("cleanup quarantine without Git identity contains foreign state")

    def _write_attempt_marker(self, marker: Path, record: Mapping[str, object]) -> None:
        if _directory_identity(marker.parent) != (
            record.get("root_device"),
            record.get("root_inode"),
        ):
            raise WorktreeConflict("attempt directory changed before its marker was recorded")
        if _canonical_existing_directory(marker.parent) != marker.parent:
            raise WorktreeConflict("attempt path changed through a symbolic link")
        _write_json_file(marker, record)

    def _read_attempt_marker(self, marker: Path) -> dict[str, object]:
        return _read_json_file(marker, label="attempt marker")

    def _verify_tree_binding(self, tree: Path, *, require_head: bool) -> None:
        if _canonical_existing_directory(tree) != tree:
            raise WorktreeConflict("attempt worktree path changed through a symbolic link")
        dot_git = tree / ".git"
        try:
            info = dot_git.lstat()
        except OSError as error:
            raise WorktreeConflict("attempt worktree has no inspectable .git file") from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorktreeConflict("attempt worktree .git entry is not a private regular file")
        top = self._run_tree_git(tree, ["rev-parse", "--show-toplevel"]).stdout.strip()
        if _canonical_existing_directory(top) != tree:
            raise WorktreeConflict("attempt path resolves to a different Git worktree")
        common = self._run_tree_git(tree, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
        if _canonical_existing_directory(common) != self.common_git_dir:
            raise WorktreeConflict("attempt worktree belongs to a different repository")
        symbolic_head = self._run_tree_git(tree, ["symbolic-ref", "--quiet", "HEAD"], check=False)
        if symbolic_head.returncode == 0:
            raise WorktreeConflict("attempt worktree HEAD is attached to a shared branch")
        if symbolic_head.returncode != 1:
            raise WorktreeConflict(_git_failure("git symbolic-ref", symbolic_head))
        if require_head:
            self._tree_head(tree)

    def _registered_worktrees(self) -> frozenset[Path]:
        paths: set[Path] = set()
        for raw in self._listed_worktree_paths():
            try:
                paths.add(_canonical_existing_directory(raw))
            except RepositoryError:
                continue
        return frozenset(paths)

    def _listed_worktree_paths(self) -> frozenset[Path]:
        proc = self._run_git(["worktree", "list", "--porcelain", "-z"])
        paths: set[Path] = set()
        for field in proc.stdout.split("\0"):
            if field.startswith("worktree "):
                paths.add(_absolute_path(field.removeprefix("worktree ")))
        return frozenset(paths)

    def _tree_head(self, tree: Path) -> str:
        oid = self._run_tree_git(tree, ["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
        _validate_oid(oid)
        return oid

    def _foreign_tree_entry(self, tree: Path) -> str | None:
        proc = self._run_tree_git(tree, ["ls-files", "-z", "--cached"])
        tracked_files = {path for path in proc.stdout.split("\0") if path}
        tracked_directories: set[str] = set()
        for tracked in tracked_files:
            parent = Path(tracked).parent
            while parent != Path("."):
                tracked_directories.add(parent.as_posix())
                parent = parent.parent

        pending = [tree]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as error:
                raise WorktreeConflict("attempt worktree inventory could not be read safely") from error
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(tree).as_posix()
                if relative == ".git":
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if relative in tracked_files:
                            return relative
                        if relative not in tracked_directories:
                            return relative
                        pending.append(path)
                    elif relative not in tracked_files:
                        return relative
                except OSError as error:
                    raise WorktreeConflict("attempt worktree inventory changed while being inspected") from error
        return None

    def _assert_canonical_index(self, tree: Path) -> None:
        self._assert_canonical_index_flags(tree)
        refreshed = self._run_tree_git(tree, ["update-index", "--really-refresh"], check=False)
        if refreshed.returncode != 0:
            raise WorktreeConflict("attempt worktree tracked content does not match its index")

    def _assert_canonical_index_flags(self, tree: Path) -> None:
        flags = self._run_tree_git(tree, ["ls-files", "-v", "-z", "--cached"])
        flagged_paths: set[str] = set()
        for entry in flags.stdout.split("\0"):
            if not entry:
                continue
            tag, separator, path = entry.partition(" ")
            if not separator or not _safe_git_path(path) or tag != "H" or path in flagged_paths:
                raise WorktreeConflict("attempt worktree index has noncanonical flags or entries")
            flagged_paths.add(path)

    def _verify_commit(self, oid: str) -> None:
        try:
            self._verified_candidate_commit_tree_oid(oid)
        except CandidateUncertain as error:
            raise RepositoryError(f"base object does not resolve exactly to a valid commit: {oid}") from error

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self._run_git(["merge-base", "--is-ancestor", ancestor, descendant], check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise RepositoryError(_git_failure("git merge-base", proc))

    def _attempt_paths(self, run_id: str, attempt_id: str) -> tuple[Path, Path, Path]:
        attempt_root = self.worktree_root / run_id / attempt_id
        return attempt_root, attempt_root / "tree", attempt_root / "attempt.json"

    def _attempt_lock(self, run_id: str, attempt_id: str) -> CoordinatorLock:
        self._verify_state()
        digest = hashlib.sha256(f"{run_id}\0{attempt_id}".encode()).hexdigest()
        return CoordinatorLock(self.lock_root / f"worktree-{digest}.lock")

    def _run_tree_git(
        self,
        tree: Path,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_git(["-C", str(tree), *args], check=check, input_text=input_text)

    def _run_tree_git_bytes(
        self,
        tree: Path,
        args: list[str],
        *,
        check: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run_git_bytes(["-C", str(tree), *args], check=check, input_bytes=input_bytes)

    def _run_tree_git_with_index(
        self,
        tree: Path,
        index_path: Path,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._verify_repository()
        environment = _git_environment()
        environment["GIT_INDEX_FILE"] = str(index_path)
        try:
            proc = subprocess.run(
                _git_command(["-C", str(tree), *args]),
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CandidateUncertain(f"Git index operation failed: {error}") from error
        self._verify_repository()
        if check and proc.returncode != 0:
            raise CandidateUncertain(_git_failure(f"git {args[0] if args else ''}", proc))
        return proc

    def _run_git(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if hasattr(self, "_repository_identity"):
            self._verify_repository()
        try:
            proc = subprocess.run(
                _git_command(args),
                cwd=self.repository_root,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=120,
                env=_git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RepositoryError(f"Git operation failed: {error}") from error
        if hasattr(self, "_repository_identity"):
            self._verify_repository()
        if check and proc.returncode != 0:
            raise RepositoryError(_git_failure(f"git {args[0] if args else ''}", proc))
        return proc

    def _run_git_bytes(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if hasattr(self, "_repository_identity"):
            self._verify_repository()
        try:
            proc = subprocess.run(
                _git_command(args),
                cwd=self.repository_root,
                capture_output=True,
                input=input_bytes,
                timeout=120,
                env=_git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RepositoryError(f"Git operation failed: {error}") from error
        if hasattr(self, "_repository_identity"):
            self._verify_repository()
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()[:500]
            raise RepositoryError(f"git {args[0] if args else ''} failed: {detail}")
        return proc

    def _verify_repository(self) -> None:
        if _directory_identity(self.repository_root) != self._repository_identity:
            raise RepositoryError("coordinator checkout directory was replaced")
        if _coordinator_git_entry_identity(self.repository_root) != self._coordinator_git_identity:
            raise RepositoryError("coordinator checkout .git entry was replaced")
        if hasattr(self, "_common_git_identity") and (
            _directory_identity(self.common_git_dir) != self._common_git_identity
        ):
            raise RepositoryError("common Git directory was replaced")
        if hasattr(self, "_object_dir_identity") and _directory_identity(self.object_dir) != self._object_dir_identity:
            raise RepositoryError("primary Git object directory was replaced")

    def _verify_state(self) -> None:
        for path, expected, label in (
            (self.state_root, self._state_identity, "attempt state root"),
            (self.worktree_root, self._worktree_root_identity, "worktree state root"),
            (self.lock_root, self._lock_root_identity, "attempt lock root"),
        ):
            if _canonical_existing_directory(path) != path or _directory_identity(path) != expected:
                raise RepositoryError(f"{label} was replaced")


class RemoteMergeQueue:
    """Publish candidate commits under a remote lease and an exact ref CAS."""

    def __init__(
        self,
        repository: AttemptWorktrees,
        *,
        remote_url: str | os.PathLike[str],
        state_root: str | Path,
        worker_id: str,
        claim_board: _ClaimBoardLike | None = None,
        claim_ttl: int | float = CLAIM_TTL_S,
        heartbeat_interval: float = CLAIM_HEARTBEAT_S,
    ) -> None:
        _validate_name("worker id", worker_id)
        self.repository = repository
        self.remote_url = _normalize_remote(remote_url)
        self.remote_id = hashlib.sha256(self.remote_url.encode()).hexdigest()
        remote_path = Path(self.remote_url) if _remote_is_local(self.remote_url) else None
        state_path = _absolute_path(state_root)
        if _paths_overlap(repository.repository_root, state_path):
            raise RepositoryError("merge-queue state must be outside the coordinator checkout")
        if _paths_overlap(repository.common_git_dir, state_path):
            raise RepositoryError("merge-queue state must be outside the common Git directory")
        if _paths_overlap(repository.state_root, state_path):
            raise RepositoryError("merge-queue state must be outside the attempt state")
        if remote_path is not None:
            for protected, label in (
                (repository.repository_root, "coordinator checkout"),
                (repository.common_git_dir, "common Git directory"),
                (repository.state_root, "attempt state"),
                (state_path, "merge-queue state"),
            ):
                if _paths_overlap(remote_path, protected):
                    raise RepositoryError(f"local publication remote must be disjoint from the {label}")
        claim_provider: object = ClaimBoard if claim_board is None else claim_board
        if not callable(getattr(claim_provider, "held_claim_fence", None)):
            raise RepositoryError("merge claim board must expose coherent ownership fences")
        if claim_board is not None and _normalize_remote(claim_board.repo_url) != self.remote_url:
            raise RepositoryError("merge claim board must use the publication remote")
        self.state_root = _prepare_private_root(state_path)
        self.publication_root = self.state_root / "publications"
        self.lock_root = self.state_root / "locks"
        _ensure_private_directory(self.publication_root)
        _ensure_private_directory(self.lock_root)
        self._state_identity = _directory_identity(self.state_root)
        self._publication_root_identity = _directory_identity(self.publication_root)
        self._lock_root_identity = _directory_identity(self.lock_root)
        self.worker_id = worker_id
        self.claim_ttl = claim_ttl
        self.heartbeat_interval = heartbeat_interval
        self._remote_path = remote_path
        self._remote_identity = _directory_identity(self._remote_path) if self._remote_path is not None else None
        self._remote_descriptor = (
            _open_directory(self._remote_path, self._remote_identity, label="local publication remote")
            if self._remote_path is not None
            else None
        )
        self._descriptor_finalizer = (
            weakref.finalize(self, os.close, self._remote_descriptor) if self._remote_descriptor is not None else None
        )
        self._transport_python = _existing_executable(sys.executable)
        self._transport_python_identity = _executable_identity(self._transport_python)
        self._transport_helper = Path(__file__).with_name("_git_fd_transport.py").resolve()
        self._transport_helper_identity = _regular_file_identity(
            self._transport_helper,
            label="local transport helper",
        )
        self.transport_root = self.state_root / "transport.git"
        self.transport_marker = self.state_root / "transport.json"
        self.transport_staging = self.state_root / "transport.git.preparing"
        self.transport_intent = self.state_root / "transport.preparing.json"
        with CoordinatorLock(self.lock_root / "transport.lock"):
            self._initialize_transport()
        scratch = self.state_root / "claims" / hashlib.sha256(worker_id.encode()).hexdigest()
        if claim_board is None:
            claim_options: dict[str, object] = {
                "expected_object_format": self.repository.object_format,
            }
            if "expected_repo_identity" in inspect.signature(ClaimBoard).parameters:
                claim_options["expected_repo_identity"] = self._remote_identity
            self.claim_board = ClaimBoard(
                self.remote_url,
                worker_id,
                scratch,
                **claim_options,
            )
        else:
            self.claim_board = claim_board
        board_remote = _normalize_remote(self.claim_board.repo_url)
        if board_remote != self.remote_url:
            raise RepositoryError("merge claim board must use the publication remote")

    def _remote_record_identity(self) -> dict[str, object]:
        return {
            "remote_kind": "local" if self._remote_identity is not None else "network",
            "remote_device": self._remote_identity[0] if self._remote_identity is not None else None,
            "remote_inode": self._remote_identity[1] if self._remote_identity is not None else None,
        }

    def close(self) -> None:
        """Release the pinned local-remote descriptor, if any."""
        if self._descriptor_finalizer is not None and self._descriptor_finalizer.alive:
            self._descriptor_finalizer()
        self._remote_descriptor = None

    def __enter__(self) -> RemoteMergeQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def publish(
        self,
        queue_item_id: str,
        *,
        target_ref: str,
        queue_ref: str,
        expected_target_oid: str,
        candidate_oid: str,
        article_claim: ClaimFence,
    ) -> PublicationReceipt:
        """Consume one article claim and publish its descendant candidate."""
        self._validate_publication_identity(queue_item_id, target_ref, queue_ref, expected_target_oid, candidate_oid)
        self._validate_article_claim(article_claim)
        article_handoff_ref = claim_handoff_ref(article_claim.key)
        self._verify_state()
        claim_key = _merge_claim_key(target_ref)
        resolution_ref = target_resolution_ref(claim_key)
        lock_digest = hashlib.sha256(f"{self.remote_id}\0{target_ref}".encode()).hexdigest()
        with CoordinatorLock(self.lock_root / f"publish-{lock_digest}.lock"):
            self._verify_state()
            directory, journal_path = self._publication_paths(queue_item_id)
            staging = self.publication_root / f".{queue_item_id}.preparing"
            if not (journal_path.exists() or directory.exists() or staging.exists()):
                self._verify_candidate(expected_target_oid, candidate_oid)
                if self._held_claim_fence(article_claim.key) != article_claim:
                    raise PublicationUncertain(
                        "article claim is not owned by this exact controller session"
                    )
                self._import_original_objects(expected_target_oid, candidate_oid, owner=queue_item_id)
            record, journal = self._load_or_create(
                queue_item_id=queue_item_id,
                target_ref=target_ref,
                queue_ref=queue_ref,
                expected_target_oid=expected_target_oid,
                candidate_oid=candidate_oid,
                article_claim_key=article_claim.key,
                article_claim_ref=article_claim.ref,
                article_claim_oid=article_claim.oid,
                article_claim_lease_id=article_claim.lease_id,
                article_handoff_ref=article_handoff_ref,
                claim_key=claim_key,
                claim_ref=CLAIM_REF_PREFIX + claim_key,
                target_resolution_ref=resolution_ref,
            )
            if record["status"] == "integrated":
                return self._receipt(record)
            if record["status"] == "aborted":
                raise MergeQueueError("publication was explicitly aborted")
            if record.get("resolution_intents"):
                resolved = self._recover_resolution_intent(record, journal)
                if resolved is not None and resolved["status"] == "integrated":
                    return self._receipt(resolved)
                raise PublicationUncertain("publication has a pending explicit resolution decision")
            self._ensure_migrated_resolution(record, journal)
            if record["status"] not in {"integrated", "aborted"}:
                self._ensure_original_objects(record)
                self._verify_candidate(expected_target_oid, candidate_oid, use_transport=True)
            recovered = self._recover_current_record(record, journal)
            if recovered["status"] == "integrated":
                return self._receipt(recovered)
            if recovered["status"] == "aborted":
                raise MergeQueueError("publication was explicitly aborted")
            if recovered["status"] == "stale":
                raise RemoteDrift(str(recovered["detail"]))
            if recovered["status"] == "uncertain":
                raise PublicationUncertain(str(recovered["detail"]))
            if recovered["status"] == "replaying":
                raise MergeQueueBusy("a stale publication replay is already in progress")
            acquired = self.claim_board.acquire(
                claim_key,
                ttl=self.claim_ttl,
                note=f"merge queue item {queue_item_id}",
                expected_resolution_oid=(
                    str(recovered["resolution_oid"])
                    if recovered["status"] in {"queued", "publishing"}
                    else None
                ),
            )
            if not acquired:
                raise MergeQueueBusy(f"another publisher owns {target_ref}")
            merge_fence: ClaimFence | None = None
            release_warning = ""
            result: PublicationReceipt | None = None
            pending_error: BaseException | None = None
            try:
                ready_to_publish = False
                with self.claim_board.heartbeat(
                    claim_key,
                    interval=self.heartbeat_interval,
                    ttl=self.claim_ttl,
                ) as heartbeat:
                    record = self._read_journal(journal)
                    merge_fence = self._held_claim_fence(claim_key)
                    if merge_fence is None:
                        raise PublicationUncertain("publication claim has no coherent ownership fence")
                    record["claim_oid"] = merge_fence.oid
                    record["claim_lease_id"] = merge_fence.lease_id
                    self._assert_lease(claim_key, heartbeat)
                    record = self._recover_current_record(record, journal)
                    if record["status"] == "integrated":
                        result = self._receipt(record)
                    elif record["status"] == "aborted":
                        raise MergeQueueError("publication was explicitly aborted")
                    elif record["status"] == "stale":
                        raise RemoteDrift(str(record["detail"]))
                    elif record["status"] == "uncertain":
                        raise PublicationUncertain(str(record["detail"]))
                    else:
                        record = self._ensure_queue_ref(record, journal, heartbeat)
                        if record["status"] == "uncertain":
                            raise PublicationUncertain(str(record["detail"]))
                        if record["status"] != "queued":
                            raise MergeQueueBusy(str(record["detail"]))
                        self._assert_lease(claim_key, heartbeat)
                        ready_to_publish = True
                if ready_to_publish or result is not None:
                    merge_fence = self._held_claim_fence(claim_key)
                    if merge_fence is None:
                        raise PublicationUncertain("publication claim has no coherent ownership fence")
                if ready_to_publish:
                    record["claim_oid"] = merge_fence.oid
                    record["claim_lease_id"] = merge_fence.lease_id
                    record = _transition_journal(journal, record, "queued", "exact claim-ref fence recorded")
                    _checkpoint("claim-fence-recorded")
                    record = self._publish_target(record, journal, merge_fence.oid)
                    if record["status"] == "integrated":
                        result = self._receipt(record)
                    elif record["status"] == "stale":
                        raise RemoteDrift(str(record["detail"]))
                    elif record["status"] == "uncertain":
                        raise PublicationUncertain(str(record["detail"]))
                    else:
                        raise MergeQueueError(str(record["detail"]))
            except BaseException as error:
                pending_error = error
            finally:
                try:
                    if merge_fence is not None:
                        self._secure_claim_cleanup(record, merge_fence)
                        self._release_claim_fence(merge_fence.ref, merge_fence.oid)
                    elif merge_fence is None:
                        released = self.claim_board.release(claim_key)
                        if not released:
                            release_warning = "publication lease release was refused"
                except Exception as error:  # publication outcome and lease cleanup are distinct
                    release_warning = f"publication lease release failed: {error}"

            if result is not None and release_warning:
                record = self._read_journal(journal)
                record["detail"] = _append_detail(str(record.get("detail", "")), release_warning)
                _transition_journal(journal, record, str(record["status"]), record["detail"])
                result = self._receipt(record)
            if pending_error is not None:
                raise pending_error
            if result is None:  # pragma: no cover - defensive state invariant
                raise PublicationUncertain("publication ended without an outcome")
            return result

    def recover(
        self,
        queue_item_id: str,
        *,
        target_ref: str,
        queue_ref: str,
        expected_target_oid: str,
        candidate_oid: str,
        article_claim: ClaimFence,
        confirm_legacy_v2_article_binding: bool = False,
    ) -> PublicationReceipt:
        """Classify an interrupted publication from its journal and exact remote refs.

        Recovery binds the stable article-claim key and lease id. Its supplied OID may
        predate the final heartbeat renewal recorded by the publication journal.
        """
        self._validate_publication_identity(queue_item_id, target_ref, queue_ref, expected_target_oid, candidate_oid)
        self._validate_article_claim(article_claim)
        article_handoff_ref = claim_handoff_ref(article_claim.key)
        self._verify_state()
        claim_key = _merge_claim_key(target_ref)
        _, journal = self._publication_paths(queue_item_id)
        lock_digest = hashlib.sha256(f"{self.remote_id}\0{target_ref}".encode()).hexdigest()
        with CoordinatorLock(self.lock_root / f"publish-{lock_digest}.lock"):
            self._verify_state()
            stable_identity = {
                "queue_item_id": queue_item_id,
                "target_ref": target_ref,
                "queue_ref": queue_ref,
                "expected_target_oid": expected_target_oid,
                "candidate_oid": candidate_oid,
                "article_claim_key": article_claim.key,
                "article_claim_ref": article_claim.ref,
                "article_claim_lease_id": article_claim.lease_id,
                "article_handoff_ref": article_handoff_ref,
                "claim_key": claim_key,
                "claim_ref": CLAIM_REF_PREFIX + claim_key,
                "target_resolution_ref": target_resolution_ref(claim_key),
            }
            staging = self.publication_root / f".{queue_item_id}.preparing"
            if not journal.exists() and not journal.is_symlink() and (staging.exists() or staging.is_symlink()):
                self._import_original_objects(
                    expected_target_oid,
                    candidate_oid,
                    owner=queue_item_id,
                )
                record, journal = self._load_or_create(
                    **stable_identity,
                    article_claim_oid=article_claim.oid,
                )
            else:
                record = self._read_journal(journal)
            bound_v2 = record.get("schema") == _UNBOUND_PUBLICATION_SCHEMA
            if bound_v2:
                if confirm_legacy_v2_article_binding is not True:
                    raise PublicationUncertain(
                        "v2 publication recovery requires explicit confirmation of its article binding"
                    )
                record = self._bind_v2_journal(
                    record,
                    article_claim=article_claim,
                    article_handoff_ref=article_handoff_ref,
                    target_resolution_ref=target_resolution_ref(claim_key),
                )
            self._validate_journal(
                record,
                journal=journal,
                **stable_identity,
            )
            if bound_v2:
                _checkpoint("publication-v2-binding-confirmed")
            if record["status"] in {"integrated", "aborted"}:
                return self._receipt(record)
            if record.get("resolution_intents"):
                resolved = self._recover_resolution_intent(record, journal)
                if resolved is not None:
                    return self._receipt(resolved)
                raise PublicationUncertain("publication has a pending explicit resolution decision")
            self._ensure_migrated_resolution(record, journal)
            if record["status"] not in {"integrated", "aborted"}:
                self._ensure_original_objects(record)
                self._verify_candidate(expected_target_oid, candidate_oid, use_transport=True)
            return self._receipt(self._recover_current_record(record, journal))

    def replay_stale(
        self,
        queue_item_id: str,
        *,
        target_ref: str,
        queue_ref: str,
        expected_target_oid: str,
        candidate_oid: str,
        article_claim: ClaimFence,
        replay_target_oid: str,
        replay_candidate_oid: str,
    ) -> PublicationReceipt:
        """Publish an exact replay of a queued candidate over a drifted target.

        The original queue and author-handoff refs remain the ownership barrier
        until one atomic push advances the target and consumes both barriers and
        the exact merge claim. Every replay attempt is append-only in the original
        publication journal.
        """

        self._validate_publication_identity(
            queue_item_id,
            target_ref,
            queue_ref,
            expected_target_oid,
            candidate_oid,
        )
        self._validate_article_claim(article_claim)
        _validate_oid(replay_target_oid)
        _validate_oid(replay_candidate_oid)
        article_handoff_ref = claim_handoff_ref(article_claim.key)
        self._verify_state()
        claim_key = _merge_claim_key(target_ref)
        stable_identity = {
            "queue_item_id": queue_item_id,
            "target_ref": target_ref,
            "queue_ref": queue_ref,
            "expected_target_oid": expected_target_oid,
            "candidate_oid": candidate_oid,
            "article_claim_key": article_claim.key,
            "article_claim_ref": article_claim.ref,
            "article_claim_lease_id": article_claim.lease_id,
            "article_handoff_ref": article_handoff_ref,
            "claim_key": claim_key,
            "claim_ref": CLAIM_REF_PREFIX + claim_key,
            "target_resolution_ref": target_resolution_ref(claim_key),
        }
        lock_digest = hashlib.sha256(f"{self.remote_id}\0{target_ref}".encode()).hexdigest()
        merge_fence: ClaimFence | None = None
        with CoordinatorLock(self.lock_root / f"publish-{lock_digest}.lock"):
            self._verify_state()
            _, journal = self._publication_paths(queue_item_id)
            record = self._read_journal(journal)
            self._validate_journal(record, journal=journal, **stable_identity)
            if record["status"] == "integrated":
                self._assert_replay_oids_if_present(
                    record,
                    replay_target_oid=replay_target_oid,
                    replay_candidate_oid=replay_candidate_oid,
                )
                return self._receipt(record)
            if record["status"] == "aborted":
                raise MergeQueueError("publication was explicitly aborted")
            if record.get("resolution_intents"):
                resolved = self._recover_resolution_intent(record, journal)
                if resolved is not None:
                    return self._receipt(resolved)
                raise PublicationUncertain("publication has a pending explicit resolution decision")
            self._ensure_migrated_resolution(record, journal)
            self._ensure_original_objects(record)
            self._verify_candidate(expected_target_oid, candidate_oid, use_transport=True)
            if record["status"] == "replaying":
                active_before_recovery = self._active_replay_intent(record)
                if active_before_recovery is None:
                    raise PublicationUncertain("replaying publication has no durable replay intent")
                self._import_commits(
                    (("replay-target", replay_target_oid), ("replay-candidate", replay_candidate_oid)),
                    owner=queue_item_id,
                )
                rebuilt = self._make_resolution_commit(
                    record,
                    phase="replay",
                    replay={
                        key: value
                        for key, value in active_before_recovery.items()
                        if key not in {"replay_id", "prior_resolution_oid", "resolution_oid"}
                    },
                )
                if rebuilt != active_before_recovery["resolution_oid"]:
                    raise PublicationUncertain("durable replay resolution could not be reconstructed exactly")
            record = self._recover_current_record(record, journal)
            if record["status"] == "integrated":
                self._assert_replay_oids_if_present(
                    record,
                    replay_target_oid=replay_target_oid,
                    replay_candidate_oid=replay_candidate_oid,
                )
                return self._receipt(record)
            if record["status"] == "uncertain":
                raise PublicationUncertain(str(record["detail"]))

            active = self._active_replay_intent(record)
            if record["status"] == "replaying":
                if active is None:  # pragma: no cover - journal validation owns this invariant
                    raise PublicationUncertain("replaying publication has no durable replay intent")
                self._assert_replay_oids(
                    active,
                    replay_target_oid=replay_target_oid,
                    replay_candidate_oid=replay_candidate_oid,
                )
                self._ensure_resolution_objects(record)
                self._verify_transport_commit(replay_target_oid)
                self._verify_transport_commit(replay_candidate_oid)
                merge_fence = self._replay_claim_fence(active)
            else:
                if record["status"] != "stale":
                    raise MergeQueueError("publication is not in a stale state that can be replayed")
                intents = record.get("replay_intents")
                if not isinstance(intents, list):  # pragma: no cover - journal validation owns this invariant
                    raise PublicationUncertain("publication replay history is malformed")
                if len(intents) >= _MAX_REPLAY_ATTEMPTS:
                    detail = "stale replay attempt limit reached; manual reconciliation is required"
                    _transition_journal(journal, record, "uncertain", detail)
                    raise PublicationUncertain(detail)
                self._import_commits(
                    (("replay-target", replay_target_oid), ("replay-candidate", replay_candidate_oid)),
                    owner=queue_item_id,
                )
                delta_sha256, replay_tree_oid = self._verify_replay_candidate(
                    expected_target_oid=expected_target_oid,
                    candidate_oid=candidate_oid,
                    replay_target_oid=replay_target_oid,
                    replay_candidate_oid=replay_candidate_oid,
                )
                acquired = self.claim_board.acquire(
                    claim_key,
                    ttl=self.claim_ttl,
                    note=f"stale merge queue replay {queue_item_id}",
                    expected_resolution_oid=str(record["resolution_oid"]),
                )
                if not acquired:
                    raise MergeQueueBusy(f"another publisher owns {target_ref}")
                try:
                    merge_fence = self._held_claim_fence(claim_key)
                    if merge_fence is None:
                        raise PublicationUncertain("stale replay claim has no coherent ownership fence")
                    record = self._prepare_replay_intent(
                        record,
                        journal,
                        replay_target_oid=replay_target_oid,
                        replay_candidate_oid=replay_candidate_oid,
                        delta_sha256=delta_sha256,
                        replay_tree_oid=replay_tree_oid,
                        merge_fence=merge_fence,
                    )
                except BaseException:
                    if merge_fence is not None:
                        self._release_claim_fence(merge_fence.ref, merge_fence.oid)
                    else:
                        self.claim_board.release(claim_key)
                    raise
                active = self._active_replay_intent(record)
                if active is None:  # pragma: no cover - journal validation owns this invariant
                    raise PublicationUncertain("stale replay intent was not recorded")

            pending_error: BaseException | None = None
            result: PublicationReceipt | None = None
            try:
                record = self._execute_replay(record, journal, active)
                if record["status"] == "integrated":
                    result = self._receipt(record)
                elif record["status"] == "stale":
                    event = self._last_replay_event(record)
                    if event is not None and event.get("status") in {"pin-retry", "retry"}:
                        raise MergeQueueBusy(str(record["detail"]))
                    raise RemoteDrift(str(record["detail"]))
                elif record["status"] == "uncertain":
                    raise PublicationUncertain(str(record["detail"]))
                else:  # pragma: no cover - replay executor returns a classified state
                    raise PublicationUncertain("stale replay ended without a classified outcome")
            except BaseException as error:
                pending_error = error
            finally:
                if merge_fence is not None:
                    try:
                        self._secure_claim_cleanup(record, merge_fence)
                        self._release_claim_fence(merge_fence.ref, merge_fence.oid)
                    except Exception as error:
                        if pending_error is None:
                            pending_error = error
            if pending_error is not None:
                raise pending_error
            if result is None:  # pragma: no cover - defensive state invariant
                raise PublicationUncertain("stale replay ended without an outcome")
            return result

    def resolve(
        self,
        queue_item_id: str,
        *,
        target_ref: str,
        queue_ref: str,
        expected_target_oid: str,
        candidate_oid: str,
        article_claim: ClaimFence,
        decision: str,
    ) -> PublicationReceipt:
        """Resolve fenced work by exact-CAS abort or descendant adoption."""

        if decision not in {"abort", "adopt"}:
            raise MergeQueueError("publication resolution decision must be 'abort' or 'adopt'")
        self._validate_publication_identity(
            queue_item_id, target_ref, queue_ref, expected_target_oid, candidate_oid
        )
        self._validate_article_claim(article_claim)
        claim_key = _merge_claim_key(target_ref)
        identity = {
            "queue_item_id": queue_item_id,
            "target_ref": target_ref,
            "queue_ref": queue_ref,
            "expected_target_oid": expected_target_oid,
            "candidate_oid": candidate_oid,
            "article_claim_key": article_claim.key,
            "article_claim_ref": article_claim.ref,
            "article_claim_lease_id": article_claim.lease_id,
            "article_handoff_ref": claim_handoff_ref(article_claim.key),
            "claim_key": claim_key,
            "claim_ref": CLAIM_REF_PREFIX + claim_key,
            "target_resolution_ref": target_resolution_ref(claim_key),
        }
        self._verify_state()
        lock_digest = hashlib.sha256(f"{self.remote_id}\0{target_ref}".encode()).hexdigest()
        with CoordinatorLock(self.lock_root / f"publish-{lock_digest}.lock"):
            self._verify_state()
            _, journal = self._publication_paths(queue_item_id)
            record = self._read_journal(journal)
            self._validate_journal(record, journal=journal, **identity)
            if record["status"] in {"integrated", "aborted"}:
                return self._receipt(record)
            resolution_intents = record.get("resolution_intents")
            if not isinstance(resolution_intents, list):
                raise PublicationUncertain("publication resolution intent history is malformed")
            if resolution_intents:
                if any(
                    not isinstance(item, dict) or item.get("decision") != decision
                    for item in resolution_intents
                ):
                    raise PublicationUncertain(
                        "publication already has a durable resolution decision with a different action"
                    )
                recovered_resolution = self._recover_resolution_intent(record, journal)
                if recovered_resolution is not None:
                    return self._receipt(recovered_resolution)
            self._ensure_original_objects(record)
            resolution_oid = str(record["resolution_oid"])
            acquired = self.claim_board.acquire(
                claim_key,
                ttl=self.claim_ttl,
                note=f"resolve merge queue item {queue_item_id}",
                expected_resolution_oid=resolution_oid,
            )
            if not acquired:
                fenced = self._ensure_permanent_resolution_fence(record)
                if fenced == resolution_oid:
                    acquired = self.claim_board.acquire(
                        claim_key,
                        ttl=self.claim_ttl,
                        note=f"resolve merge queue item {queue_item_id}",
                        expected_resolution_oid=resolution_oid,
                    )
                    if not acquired:
                        acquired = self.claim_board.adopt_recovery_block(
                            claim_key,
                            queue_item_id=queue_item_id,
                            target_resolution_ref=str(record["target_resolution_ref"]),
                            resolution_oid=resolution_oid,
                            ttl=self.claim_ttl,
                            note=f"resolve merge queue item {queue_item_id}",
                        )
            if not acquired:
                raise MergeQueueBusy("publication resolution is fenced by another publisher")
            fence = self._held_claim_fence(claim_key)
            if fence is None:
                self.claim_board.release(claim_key)
                raise PublicationUncertain("publication resolution has no coherent merge fence")
            release = True
            try:
                observation = self._observe_replay_refs(record)
                self._record_replay_observation(record, observation)
                if (
                    observation["queue"] not in {None, candidate_oid}
                    or observation["handoff"] not in {None, candidate_oid}
                    or observation["resolution"] != resolution_oid
                    or observation["claim"] != fence.oid
                    or observation["article"] is not None
                ):
                    raise PublicationUncertain(
                        "publication resolution refused foreign or split queue ownership refs"
                    )
                target = observation["target"]
                latest_replay = self._effective_replay_intent(record)
                resolution_base = (
                    str(latest_replay["replay_target_oid"])
                    if latest_replay is not None
                    else _expected_oid(expected_target_oid)
                )
                resolution_candidate = (
                    str(latest_replay["replay_candidate_oid"])
                    if latest_replay is not None
                    else candidate_oid
                )
                if latest_replay is not None:
                    self._ensure_resolution_objects(record)
                    self._verify_transport_commit(resolution_base)
                    self._verify_transport_commit(resolution_candidate)
                candidate_integrated = False
                if target is not None:
                    self._fetch_exact_ref(
                        target_ref,
                        target,
                        owner=str(record["queue_item_id"]),
                    )
                    candidate_integrated = self._transport_is_ancestor(resolution_candidate, target)
                if decision == "abort":
                    if target != resolution_base:
                        if candidate_integrated:
                            raise MergeQueueError(
                                "candidate is already an ancestor of the target; adopt the integration instead"
                            )
                        raise PublicationUncertain(
                            "publication abort refused target drift from the exact original base"
                        )
                if decision == "adopt" and not candidate_integrated:
                    raise MergeQueueError("candidate is not an ancestor of the target and cannot be adopted")
                record = self._record_resolution_intent(
                    record,
                    journal,
                    decision=decision,
                    observation=observation,
                    resolution_base=resolution_base,
                    resolution_candidate=resolution_candidate,
                )
                _checkpoint("resolution-push-attempted")
                if not self._atomic_resolve_publication(
                    record,
                    observation=observation,
                    claim_oid=fence.oid,
                ):
                    fenced = self._ensure_permanent_resolution_fence(record)
                    if fenced is None:
                        blocker = self._install_recovery_block(record, expected_claim=fence.oid)
                        if blocker is None:
                            release = False
                            raise PublicationUncertain(
                                "resolution CAS failed and no permanent recovery fence could be installed"
                            )
                    _transition_journal(
                        journal,
                        record,
                        "uncertain",
                        "publication resolution exact CAS was rejected; ownership remains fenced",
                    )
                    raise PublicationUncertain(str(record["detail"]))
                _checkpoint("resolution-pushed")
                status = "aborted" if decision == "abort" else "integrated"
                detail = (
                    "operator abort consumed exact queue ownership refs without changing the target"
                    if decision == "abort"
                    else "operator adoption verified the candidate in target history and consumed ownership refs"
                )
                active_replay = self._active_replay_intent(record)
                last_replay = self._last_replay_event(record)
                if (
                    active_replay is not None
                    and last_replay is not None
                    and last_replay.get("status") not in _REPLAY_TERMINAL_STATES
                ):
                    record = self._append_replay_event(
                        record,
                        journal,
                        replay_id=str(active_replay["replay_id"]),
                        status="stale" if decision == "abort" else "integrated",
                        detail=detail,
                        top_status=status,
                    )
                else:
                    record = _transition_journal(journal, record, status, detail)
                return self._receipt(record)
            finally:
                if release:
                    self._secure_claim_cleanup(record, fence)
                    self._release_claim_fence(fence.ref, fence.oid)

    def _record_resolution_intent(
        self,
        record: dict[str, object],
        journal: Path,
        *,
        decision: str,
        observation: Mapping[str, str | None],
        resolution_base: str | None,
        resolution_candidate: str,
    ) -> dict[str, object]:
        history = record.get("history")
        if not isinstance(history, list) or len(history) + 2 > _MAX_PUBLICATION_HISTORY:
            raise PublicationUncertain("publication has no evidence capacity for explicit resolution")
        created_ns = time.time_ns()
        body: dict[str, object] = {
            "schema": _RESOLUTION_INTENT_SCHEMA,
            "decision": decision,
            "queue_item_id": record["queue_item_id"],
            "target_ref": record["target_ref"],
            "queue_ref": record["queue_ref"],
            "article_handoff_ref": record["article_handoff_ref"],
            "target_resolution_ref": record["target_resolution_ref"],
            "candidate_oid": record["candidate_oid"],
            "resolution_oid": record["resolution_oid"],
            "resolution_base_oid": resolution_base,
            "resolution_candidate_oid": resolution_candidate,
            "observed_target_oid": observation["target"],
            "observed_queue_oid": observation["queue"],
            "observed_article_handoff_oid": observation["handoff"],
            "observed_article_claim_oid": observation["article"],
            "observed_claim_oid": observation["claim"],
            "observed_resolution_oid": observation["resolution"],
            "created_ns": created_ns,
        }
        intent = {"intent_id": hashlib.sha256(_json_bytes(body)).hexdigest(), **body}
        proposed = copy.deepcopy(record)
        proposed_intents = proposed.get("resolution_intents")
        if not isinstance(proposed_intents, list):
            raise PublicationUncertain("publication resolution intent history is malformed")
        proposed_intents.append(intent)
        proposed_history = proposed.get("history")
        proposed_replay_events = proposed.get("replay_events")
        if not isinstance(proposed_history, list):
            raise PublicationUncertain("publication history is malformed")
        terminal_detail = (
            "operator abort consumed exact queue ownership refs without changing the target"
            if decision == "abort"
            else "operator adoption verified the candidate in target history and consumed ownership refs"
        )
        active_replay = self._active_replay_intent(record)
        last_replay = self._last_replay_event(record)
        if (
            active_replay is not None
            and last_replay is not None
            and last_replay.get("status") not in _REPLAY_TERMINAL_STATES
        ):
            if not isinstance(proposed_replay_events, list) or len(proposed_replay_events) >= _MAX_REPLAY_EVENTS:
                raise PublicationUncertain("publication has no replay-event capacity for resolution")
            proposed_replay_events.append(
                {
                    "replay_id": active_replay["replay_id"],
                    "status": "stale" if decision == "abort" else "integrated",
                    "detail": terminal_detail,
                    "created_ns": created_ns,
                    "observed_target_oid": observation["target"],
                    "observed_queue_oid": observation["queue"],
                    "observed_article_claim_oid": observation["article"],
                    "observed_article_handoff_oid": observation["handoff"],
                    "observed_claim_oid": observation["claim"],
                    "observed_resolution_oid": observation["resolution"],
                }
            )
        proposed_history.extend(
            (
                {
                    "status": str(record["status"]),
                    "detail": "durable explicit resolution decision recorded",
                    "created_ns": created_ns,
                },
                {
                    "status": "aborted" if decision == "abort" else "integrated",
                    "detail": terminal_detail,
                    "created_ns": created_ns,
                },
            )
        )
        if _publication_journal_size(proposed) > _MAX_PUBLICATION_BYTES:
            raise PublicationUncertain("publication has no byte capacity for explicit resolution")
        intents = record.get("resolution_intents")
        if not isinstance(intents, list):
            raise PublicationUncertain("publication resolution intent history is malformed")
        intents.append(intent)
        return _transition_journal(
            journal,
            record,
            str(record["status"]),
            "durable explicit resolution decision recorded",
        )

    def _recover_resolution_intent(
        self,
        record: dict[str, object],
        journal: Path,
    ) -> dict[str, object] | None:
        intents = record.get("resolution_intents")
        if not isinstance(intents, list) or not intents or not isinstance(intents[-1], dict):
            return None
        intent = intents[-1]
        observation = self._observe_replay_refs(record)
        self._record_replay_observation(record, observation)
        shared_consumed = all(
            intent.get(intent_key) is None or observation[current_key] != intent.get(intent_key)
            for intent_key, current_key in (
                ("observed_queue_oid", "queue"),
                ("observed_article_handoff_oid", "handoff"),
                ("observed_claim_oid", "claim"),
                ("observed_resolution_oid", "resolution"),
            )
        )
        if shared_consumed:
            target = observation["target"]
            candidate = str(intent["resolution_candidate_oid"])
            integrated = False
            if target is not None:
                self._fetch_exact_ref(
                    str(record["target_ref"]),
                    target,
                    owner=str(record["queue_item_id"]),
                )
                if not self._transport_has_commit(candidate):
                    raise PublicationUncertain(
                        "explicit resolution candidate is unavailable for recovery verification"
                    )
                integrated = self._transport_is_ancestor(candidate, target)
            decision = str(intent["decision"])
            if (decision == "abort" and not integrated) or (decision == "adopt" and integrated):
                status = "aborted" if decision == "abort" else "integrated"
                detail = f"recovery verified completed explicit {decision}"
                active_replay = self._active_replay_intent(record)
                last_replay = self._last_replay_event(record)
                if (
                    active_replay is not None
                    and last_replay is not None
                    and last_replay.get("status") not in _REPLAY_TERMINAL_STATES
                ):
                    return self._append_replay_event(
                        record,
                        journal,
                        replay_id=str(active_replay["replay_id"]),
                        status="stale" if decision == "abort" else "integrated",
                        detail=detail,
                        top_status=status,
                    )
                return _transition_journal(journal, record, status, detail)
        return None

    def _recover_current_record(
        self,
        record: dict[str, object],
        journal: Path,
    ) -> dict[str, object]:
        if record["status"] != "replaying":
            return self._recover_record(record, journal)
        active = self._active_replay_intent(record)
        if active is None:  # pragma: no cover - journal validation owns this invariant
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "replaying publication has no durable replay intent",
            )
        if self._remote_oid(str(record["target_resolution_ref"])) is None:
            return self._recover_replay_record(record, journal, active)
        record = self._ensure_replay_resolution_pinned(record, journal, active)
        if record["status"] != "replaying":
            return record
        return self._recover_replay_record(record, journal, active)

    def _prepare_replay_intent(
        self,
        record: dict[str, object],
        journal: Path,
        *,
        replay_target_oid: str,
        replay_candidate_oid: str,
        delta_sha256: str,
        replay_tree_oid: str,
        merge_fence: ClaimFence,
    ) -> dict[str, object]:
        observation = self._observe_replay_refs(record)
        self._record_replay_observation(record, observation)
        original_candidate = str(record["candidate_oid"])
        if observation["queue"] != original_candidate or observation["handoff"] != original_candidate:
            detail = self._replay_barrier_error(record, observation, phase="before replay intent")
            _transition_journal(journal, record, "uncertain", detail)
            raise PublicationUncertain(detail)
        if observation["resolution"] != record.get("resolution_oid"):
            detail = "target resolution fence changed before stale replay intent"
            _transition_journal(journal, record, "uncertain", detail)
            raise PublicationUncertain(detail)
        if observation["article"] is not None:
            detail = "an article claim reappeared before stale replay intent"
            _transition_journal(journal, record, "uncertain", detail)
            raise PublicationUncertain(detail)
        if observation["claim"] != merge_fence.oid:
            if observation["claim"] is None:
                detail = "stale replay merge claim disappeared before intent; retry with a new fence"
                _transition_journal(journal, record, "stale", detail)
                raise MergeQueueBusy(detail)
            detail = f"merge claim {merge_fence.ref} changed before stale replay intent"
            _transition_journal(journal, record, "uncertain", detail)
            raise PublicationUncertain(detail)
        if observation["target"] != replay_target_oid:
            detail = (
                f"target {record['target_ref']} drifted from replay base {replay_target_oid} "
                f"to {observation['target'] or 'absent'} before replay intent"
            )
            _transition_journal(journal, record, "stale", detail)
            raise RemoteDrift(detail)

        created_ns = time.time_ns()
        replay_payload: dict[str, object] = {
            "schema": _REPLAY_INTENT_SCHEMA,
            "object_format": self.repository.object_format,
            "original_expected_oid": str(record["expected_target_oid"]),
            "original_candidate_oid": original_candidate,
            "replay_target_oid": replay_target_oid,
            "replay_candidate_oid": replay_candidate_oid,
            "replay_tree_oid": replay_tree_oid,
            "source_delta_sha256": delta_sha256,
            "target_ref": str(record["target_ref"]),
            "queue_ref": str(record["queue_ref"]),
            "expected_queue_oid": original_candidate,
            "article_claim_ref": str(record["article_claim_ref"]),
            "expected_article_claim_oid": None,
            "article_handoff_ref": str(record["article_handoff_ref"]),
            "expected_article_handoff_oid": original_candidate,
            "claim_key": merge_fence.key,
            "claim_ref": merge_fence.ref,
            "claim_oid": merge_fence.oid,
            "claim_lease_id": merge_fence.lease_id,
            "target_resolution_ref": str(record["target_resolution_ref"]),
            "created_ns": created_ns,
        }
        prior_resolution_oid = str(record["resolution_oid"])
        replay_resolution_oid = self._make_resolution_commit(
            record,
            phase="replay",
            replay=replay_payload,
        )
        intent_body = {
            **replay_payload,
            "prior_resolution_oid": prior_resolution_oid,
            "resolution_oid": replay_resolution_oid,
        }
        replay_id = hashlib.sha256(_json_bytes(intent_body)).hexdigest()
        intent = {"replay_id": replay_id, **intent_body}
        intents = record.get("replay_intents")
        events = record.get("replay_events")
        if not isinstance(intents, list) or not isinstance(events, list):
            raise PublicationUncertain("publication replay history is malformed")
        if len(intents) >= _MAX_REPLAY_ATTEMPTS or len(events) + 3 > _MAX_REPLAY_EVENTS:
            detail = "stale replay evidence limit reached; manual reconciliation is required"
            _transition_journal(journal, record, "uncertain", detail)
            raise PublicationUncertain(detail)
        proposed = copy.deepcopy(record)
        proposed_intents = proposed.get("replay_intents")
        proposed_events = proposed.get("replay_events")
        proposed_history = proposed.get("history")
        if not all(isinstance(value, list) for value in (proposed_intents, proposed_events, proposed_history)):
            raise PublicationUncertain("publication replay evidence is malformed")
        proposed["resolution_oid"] = replay_resolution_oid
        proposed["observed_resolution_oid"] = prior_resolution_oid
        proposed_intents.append(intent)
        proposed_events.append(
            {
                "replay_id": replay_id,
                "status": "prepared",
                "detail": "durable exact-diff stale replay intent recorded",
                "created_ns": created_ns,
            }
        )
        proposed_history.append(
            {
                "status": "replaying",
                "detail": "durable exact-diff stale replay intent recorded",
                "created_ns": created_ns,
            }
        )
        proposed["status"] = "replaying"
        proposed["detail"] = "durable exact-diff stale replay intent recorded"
        proposed["updated_ns"] = created_ns
        if _publication_journal_size(proposed) > _MAX_PUBLICATION_BYTES - _REPLAY_EXECUTION_RESERVE_BYTES:
            detail = "stale replay evidence byte limit reached; manual reconciliation is required"
            _transition_journal(journal, record, "uncertain", detail)
            raise PublicationUncertain(detail)
        record["resolution_oid"] = replay_resolution_oid
        record["observed_resolution_oid"] = prior_resolution_oid
        intents.append(intent)
        events.append(
            {
                "replay_id": replay_id,
                "status": "prepared",
                "detail": "durable exact-diff stale replay intent recorded",
                "created_ns": created_ns,
            }
        )
        _checkpoint("replay-intent-recording")
        record = _transition_journal(
            journal,
            record,
            "replaying",
            "durable exact-diff stale replay intent recorded",
        )
        _checkpoint("replay-intent-recorded")
        self._pin_replay_resolution(record, intent)
        record["observed_resolution_oid"] = replay_resolution_oid
        _write_publication_journal(journal, record)
        _checkpoint("replay-resolution-pinned")
        return record

    def _ensure_replay_resolution_pinned(
        self,
        record: dict[str, object],
        journal: Path,
        intent: Mapping[str, object],
    ) -> dict[str, object]:
        for key in ("replay_target_oid", "replay_candidate_oid"):
            if not self._transport_has_commit(str(intent[key]), use_repository_alternate=False):
                raise PublicationUncertain(
                    f"durable replay object {key} is missing before resolution pin recovery"
                )
        observed = self._remote_oid(str(record["target_resolution_ref"]))
        current = str(intent["resolution_oid"])
        prior = str(intent["prior_resolution_oid"])
        record["observed_resolution_oid"] = observed
        if observed == current:
            self._fetch_exact_ref(
                str(record["target_resolution_ref"]),
                current,
                owner=str(record["queue_item_id"]),
            )
            return record
        if observed != prior:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "stale replay resolution ref changed outside its durable intent",
            )
        observed_claim = self._remote_oid(str(intent["claim_ref"]))
        record["observed_claim_oid"] = observed_claim
        if observed_claim is None:
            record["resolution_oid"] = prior
            return self._append_replay_event(
                record,
                journal,
                replay_id=str(intent["replay_id"]),
                status="pin-retry",
                detail="replay resolution pin was interrupted after intent recording; a new fence is required",
                top_status="stale",
            )
        if observed_claim != intent["claim_oid"]:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "stale replay merge claim changed before its durable resolution could be pinned",
            )
        self._pin_replay_resolution(record, intent)
        record["observed_resolution_oid"] = current
        _write_publication_journal(journal, record)
        return record

    def _execute_replay(
        self,
        record: dict[str, object],
        journal: Path,
        intent: Mapping[str, object],
    ) -> dict[str, object]:
        record = self._recover_replay_record(record, journal, intent)
        if record["status"] != "replaying":
            return record
        events = record.get("replay_events")
        if not isinstance(events, list):  # pragma: no cover - journal validation owns this invariant
            raise PublicationUncertain("publication replay event history is malformed")
        if len(events) + 2 > _MAX_REPLAY_EVENTS:
            return self._record_replay_classification(
                record,
                journal,
                intent,
                "uncertain",
                "stale replay event limit reached; manual reconciliation is required",
            )
        replay_id = str(intent["replay_id"])
        self._append_replay_event(
            record,
            journal,
            replay_id=replay_id,
            status="publishing",
            detail="atomic stale replay publication about to run",
            top_status="replaying",
        )
        _checkpoint("replay-push-attempted")
        pushed = self._atomic_replay_push(intent)
        _checkpoint("replay-pushed")
        _checkpoint("replay-observation-started")
        observation = self._observe_replay_refs(record)
        self._record_replay_observation(record, observation)
        if self._replay_barriers_split(record, observation):
            record = self._repair_or_fence_split(record, journal, phase="during stale replay")
            if record["status"] == "uncertain":
                return self._record_replay_classification(
                    record,
                    journal,
                    intent,
                    "uncertain",
                    str(record["detail"]),
                )
            observation = self._observe_replay_refs(record)
            self._record_replay_observation(record, observation)
        _checkpoint("replay-observed")
        status, detail = self._classify_replay_observation(
            record,
            intent,
            observation,
            reported_success=pushed,
        )
        return self._record_replay_classification(record, journal, intent, status, detail)

    def _recover_replay_record(
        self,
        record: dict[str, object],
        journal: Path,
        intent: Mapping[str, object],
    ) -> dict[str, object]:
        _checkpoint("replay-recovery-observation-started")
        observation = self._observe_replay_refs(record)
        self._record_replay_observation(record, observation)
        if self._replay_barriers_split(record, observation):
            record = self._repair_or_fence_split(record, journal, phase="during stale replay recovery")
            if record["status"] == "uncertain":
                return self._record_replay_classification(
                    record,
                    journal,
                    intent,
                    "uncertain",
                    str(record["detail"]),
                )
            observation = self._observe_replay_refs(record)
            self._record_replay_observation(record, observation)
        _checkpoint("replay-recovery-observed")
        status, detail = self._classify_replay_observation(
            record,
            intent,
            observation,
            reported_success=None,
        )
        if status == "replaying":
            record["detail"] = detail
            record["updated_ns"] = time.time_ns()
            _write_publication_journal(journal, record)
            return record
        return self._record_replay_classification(record, journal, intent, status, detail)

    def _classify_replay_observation(
        self,
        record: Mapping[str, object],
        intent: Mapping[str, object],
        observation: Mapping[str, str | None],
        *,
        reported_success: bool | None,
    ) -> tuple[str, str]:
        target = observation["target"]
        queue = observation["queue"]
        handoff = observation["handoff"]
        article = observation["article"]
        claim = observation["claim"]
        resolution = observation["resolution"]
        old_candidate = str(intent["original_candidate_oid"])
        replay_target = str(intent["replay_target_oid"])
        replay_candidate = str(intent["replay_candidate_oid"])
        replay_claim = str(intent["claim_oid"])
        replay_resolution = str(intent["resolution_oid"])

        if target == replay_candidate and queue is None and handoff is None and resolution is None:
            if claim == replay_claim:
                return "uncertain", "replay target advanced without consuming the exact merge claim"
            return "integrated", "recovery verified atomic stale replay publication"

        if queue != old_candidate or handoff != old_candidate:
            return "uncertain", self._replay_barrier_error(record, observation, phase="during stale replay")
        if article is not None:
            return "uncertain", "an article claim exists while stale replay barriers are active"
        if claim not in {None, replay_claim}:
            return "uncertain", f"merge claim {intent['claim_ref']} changed during stale replay"
        if resolution != replay_resolution:
            return "uncertain", "target resolution fence changed during stale replay"
        if target != replay_target:
            if target == replay_candidate:
                return "uncertain", "replay target advanced without atomically consuming its barriers"
            return (
                "stale",
                f"stale replay target drifted from {replay_target} to {target or 'absent'}",
            )
        if claim is None:
            return "retry", "stale replay merge claim disappeared before publication; a new fence is required"
        if reported_success is True:
            return "uncertain", "atomic stale replay reported success without the exact four-ref result"
        if reported_success is False:
            return "retry", "stale replay CAS was rejected without remote drift; a new fenced retry is required"
        return "replaying", "recovery verified the exact pre-push stale replay state"

    def _record_replay_classification(
        self,
        record: dict[str, object],
        journal: Path,
        intent: Mapping[str, object],
        status: str,
        detail: str,
    ) -> dict[str, object]:
        top_status = "stale" if status == "retry" else status
        _checkpoint("replay-terminal-recording")
        record = self._append_replay_event(
            record,
            journal,
            replay_id=str(intent["replay_id"]),
            status=status,
            detail=detail,
            top_status=top_status,
        )
        _checkpoint("replay-terminal-recorded")
        return record

    def _append_replay_event(
        self,
        record: dict[str, object],
        journal: Path,
        *,
        replay_id: str,
        status: str,
        detail: str,
        top_status: str,
    ) -> dict[str, object]:
        events = record.get("replay_events")
        if not isinstance(events, list):
            raise PublicationUncertain("publication replay event history is malformed")
        terminal = status in _REPLAY_TERMINAL_STATES
        limit = _MAX_REPLAY_EVENTS if terminal else _MAX_REPLAY_EVENTS - 1
        if len(events) >= limit:
            if terminal:
                raise PublicationUncertain("publication replay history has no reserved terminal event slot")
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "stale replay event limit reached; manual reconciliation is required",
            )
        event = {
            "replay_id": replay_id,
            "status": status,
            "detail": _bounded_publication_detail(detail),
            "created_ns": time.time_ns(),
            "observed_target_oid": record.get("observed_target_oid"),
            "observed_queue_oid": record.get("observed_queue_oid"),
            "observed_article_claim_oid": record.get("observed_article_claim_oid"),
            "observed_article_handoff_oid": record.get("observed_article_handoff_oid"),
            "observed_claim_oid": record.get("observed_claim_oid"),
            "observed_resolution_oid": record.get("observed_resolution_oid"),
        }
        proposed = copy.deepcopy(record)
        proposed_events = proposed.get("replay_events")
        proposed_history = proposed.get("history")
        if not isinstance(proposed_events, list) or not isinstance(proposed_history, list):
            raise PublicationUncertain("publication replay evidence is malformed")
        proposed_events.append(event)
        proposed_history.append(
            {"status": top_status, "detail": event["detail"], "created_ns": event["created_ns"]}
        )
        proposed["status"] = top_status
        proposed["detail"] = event["detail"]
        proposed["updated_ns"] = event["created_ns"]
        maximum = _MAX_PUBLICATION_BYTES
        if not terminal:
            maximum -= _PUBLICATION_TERMINAL_RESERVE_BYTES
        if _publication_journal_size(proposed) > maximum:
            if terminal:
                raise PublicationUncertain("publication replay terminal evidence exceeds its reserved bytes")
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "stale replay evidence byte limit reached; manual reconciliation is required",
            )
        events.append(event)
        return _transition_journal(journal, record, top_status, detail)

    def _observe_replay_refs(self, record: Mapping[str, object]) -> dict[str, str | None]:
        refs = {
            "target": str(record["target_ref"]),
            "queue": str(record["queue_ref"]),
            "handoff": str(record["article_handoff_ref"]),
            "article": str(record["article_claim_ref"]),
            "claim": str(record["claim_ref"]),
            "resolution": str(record["target_resolution_ref"]),
        }
        observation = self._remote_oids(refs.values())
        return {
            name: observation[ref]
            for name, ref in refs.items()
        }

    @staticmethod
    def _replay_barriers_split(
        record: Mapping[str, object], observation: Mapping[str, str | None]
    ) -> bool:
        expected = (
            str(record["candidate_oid"]),
            str(record["candidate_oid"]),
            str(record["resolution_oid"]),
        )
        observed = (observation["queue"], observation["handoff"], observation["resolution"])
        if observed == expected:
            return False
        active = RemoteMergeQueue._active_replay_intent(record)
        return not (
            active is not None
            and observation["target"] == active.get("replay_candidate_oid")
            and observed == (None, None, None)
        )

    def _repair_or_fence_split(
        self,
        record: dict[str, object],
        journal: Path,
        *,
        phase: str,
    ) -> dict[str, object]:
        observation = self._observe_replay_refs(record)
        self._record_replay_observation(record, observation)
        candidate = str(record["candidate_oid"])
        resolution_oid = str(record["resolution_oid"])
        claim_oid = observation["claim"]
        expected_claim = self._active_resolution_claim_oid(record)
        active_replay = self._effective_replay_intent(record)
        repair_target = (
            str(active_replay["replay_target_oid"])
            if active_replay is not None
            else _expected_oid(str(record["expected_target_oid"]))
        )
        if (
            observation["target"] == repair_target
            and observation["queue"] in {None, candidate}
            and observation["handoff"] in {None, candidate}
            and observation["resolution"] in {None, resolution_oid}
            and observation["article"] is None
            and expected_claim is not None
            and claim_oid == expected_claim
        ):
            repaired = self._atomic_restore_barriers(
                record,
                observation=observation,
                claim_oid=expected_claim,
            )
            if repaired:
                verified = self._observe_replay_refs(record)
                self._record_replay_observation(record, verified)
                if (
                    verified["queue"] == candidate
                    and verified["handoff"] == candidate
                    and verified["resolution"] == resolution_oid
                    and verified["article"] is None
                    and verified["claim"] == expected_claim
                ):
                    prior_status = str(record.get("status"))
                    restored_status = (
                        prior_status
                        if prior_status in {"replaying", "stale", "uncertain"}
                        else "queued"
                    )
                    return _transition_journal(
                        journal,
                        record,
                        restored_status,
                        f"restored exact queue, handoff, and resolution fences {phase}",
                    )
        collisions = []
        if observation["queue"] not in {None, candidate}:
            collisions.append("queue")
        if observation["handoff"] not in {None, candidate}:
            collisions.append("author-handoff")
        if observation["resolution"] not in {None, resolution_oid}:
            collisions.append("target-resolution")
        if observation["article"] is not None:
            collisions.append("article-claim")
        detail = (
            f"{', '.join(collisions)} ref collision {phase}"
            if collisions
            else f"queue, author-handoff, and target-resolution refs split {phase}"
        )
        try:
            fenced = self._ensure_permanent_resolution_fence(record)
        except (MergeQueueError, PublicationUncertain) as error:
            fenced = None
            detail = _append_detail(detail, str(error))
        if fenced is None:
            expected_claim = self._active_resolution_claim_oid(record)
            blocker = (
                self._install_recovery_block(record, expected_claim=expected_claim)
                if expected_claim is not None
                else None
            )
            if blocker is None:
                raise PublicationUncertain(
                    _append_detail(detail, "no permanent recovery fence could be installed")
                )
            record["observed_claim_oid"] = blocker
            detail = _append_detail(detail, "merge claim converted to a permanent recovery block")
        else:
            record["observed_resolution_oid"] = fenced
        return _transition_journal(journal, record, "uncertain", detail)

    @staticmethod
    def _active_resolution_claim_oid(record: Mapping[str, object]) -> str | None:
        if record.get("status") == "replaying":
            active = RemoteMergeQueue._active_replay_intent(record)
            if active is not None and isinstance(active.get("claim_oid"), str):
                return str(active["claim_oid"])
        value = record.get("claim_oid")
        return str(value) if isinstance(value, str) else None

    @staticmethod
    def _record_replay_observation(
        record: dict[str, object],
        observation: Mapping[str, str | None],
    ) -> None:
        record["observed_target_oid"] = observation["target"]
        record["observed_queue_oid"] = observation["queue"]
        record["observed_article_handoff_oid"] = observation["handoff"]
        record["observed_article_claim_oid"] = observation["article"]
        record["observed_claim_oid"] = observation["claim"]
        record["observed_resolution_oid"] = observation["resolution"]

    @staticmethod
    def _replay_barrier_error(
        record: Mapping[str, object],
        observation: Mapping[str, str | None],
        *,
        phase: str,
    ) -> str:
        candidate = str(record["candidate_oid"])
        queue = observation["queue"]
        handoff = observation["handoff"]
        if (queue is None) != (handoff is None):
            return f"queue and author-handoff refs split {phase}"
        if queue != candidate:
            return f"queue ref collision {phase}: {queue or 'absent'}"
        return f"author-handoff ref collision {phase}: {handoff or 'absent'}"

    @staticmethod
    def _active_replay_intent(record: Mapping[str, object]) -> Mapping[str, object] | None:
        intents = record.get("replay_intents")
        if not isinstance(intents, list) or not intents:
            return None
        intent = intents[-1]
        return intent if isinstance(intent, dict) else None

    @staticmethod
    def _effective_replay_intent(record: Mapping[str, object]) -> Mapping[str, object] | None:
        intents = record.get("replay_intents")
        events = record.get("replay_events")
        if not isinstance(intents, list) or not isinstance(events, list):
            return None
        abandoned = {
            str(event.get("replay_id"))
            for event in events
            if isinstance(event, dict) and event.get("status") == "pin-retry"
        }
        current = record.get("resolution_oid")
        for intent in reversed(intents):
            if (
                isinstance(intent, dict)
                and intent.get("replay_id") not in abandoned
                and intent.get("resolution_oid") == current
            ):
                return intent
        return None

    @staticmethod
    def _last_replay_event(record: Mapping[str, object]) -> Mapping[str, object] | None:
        events = record.get("replay_events")
        if not isinstance(events, list) or not events:
            return None
        event = events[-1]
        return event if isinstance(event, dict) else None

    def _assert_replay_oids_if_present(
        self,
        record: Mapping[str, object],
        *,
        replay_target_oid: str,
        replay_candidate_oid: str,
    ) -> None:
        active = self._active_replay_intent(record)
        if active is not None:
            self._assert_replay_oids(
                active,
                replay_target_oid=replay_target_oid,
                replay_candidate_oid=replay_candidate_oid,
            )

    @staticmethod
    def _assert_replay_oids(
        intent: Mapping[str, object],
        *,
        replay_target_oid: str,
        replay_candidate_oid: str,
    ) -> None:
        for key, value in (
            ("replay_target_oid", replay_target_oid),
            ("replay_candidate_oid", replay_candidate_oid),
        ):
            if intent.get(key) != value:
                raise PublicationUncertain(f"stale replay journal has a different {key}")

    @staticmethod
    def _replay_claim_fence(intent: Mapping[str, object]) -> ClaimFence:
        return ClaimFence(
            key=str(intent["claim_key"]),
            ref=str(intent["claim_ref"]),
            oid=str(intent["claim_oid"]),
            lease_id=str(intent["claim_lease_id"]),
        )

    def _ensure_queue_ref(
        self,
        record: dict[str, object],
        journal: Path,
        heartbeat: Any,
    ) -> dict[str, object]:
        queue_ref = str(record["queue_ref"])
        candidate = str(record["candidate_oid"])
        article_claim_ref = str(record["article_claim_ref"])
        article_claim_oid = str(record["article_claim_oid"])
        article_handoff_ref = str(record["article_handoff_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        resolution_oid = str(record["resolution_oid"])
        observation = self._remote_oids(
            (queue_ref, article_claim_ref, article_handoff_ref, resolution_ref)
        )
        observed_queue = observation[queue_ref]
        observed_article_claim = observation[article_claim_ref]
        observed_article_handoff = observation[article_handoff_ref]
        observed_resolution = observation[resolution_ref]
        record["observed_queue_oid"] = observed_queue
        record["observed_article_claim_oid"] = observed_article_claim
        record["observed_article_handoff_oid"] = observed_article_handoff
        record["observed_resolution_oid"] = observed_resolution
        if observed_queue == candidate:
            if observed_article_handoff != candidate or observed_resolution != resolution_oid:
                return self._repair_or_fence_split(record, journal, phase="queue verification")
            if observed_article_claim is not None:
                return _transition_journal(
                    journal,
                    record,
                    "uncertain",
                    "queue ref exists while an article claim is still present",
                )
            return _transition_journal(
                journal,
                record,
                "queued",
                "candidate queue ref and consumed article claim verified",
            )
        if observed_queue is not None:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} points to unexpected object {observed_queue}",
            )
        if observed_article_handoff is not None:
            return self._repair_or_fence_split(record, journal, phase="queue verification")
        if observed_resolution is not None:
            return self._repair_or_fence_split(record, journal, phase="queue verification")
        if observed_article_claim != article_claim_oid:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"article claim {article_claim_ref} changed before queue handoff",
            )
        expected_fence = ClaimFence(
            key=str(record["article_claim_key"]),
            ref=article_claim_ref,
            oid=article_claim_oid,
            lease_id=str(record["article_claim_lease_id"]),
        )
        if self._held_claim_fence(expected_fence.key) != expected_fence:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "article claim is not owned by this exact controller session",
            )
        record = _transition_journal(
            journal,
            record,
            "queueing",
            "atomic article-claim to queue-ref handoff about to run",
        )
        _checkpoint("queue-push-attempted")
        self._assert_lease(str(record["claim_key"]), heartbeat)
        pushed = self._atomic_queue_handoff(
            queue_ref=queue_ref,
            candidate=candidate,
            article_claim_ref=article_claim_ref,
            article_claim_oid=article_claim_oid,
            article_handoff_ref=article_handoff_ref,
            resolution_ref=resolution_ref,
            resolution_oid=resolution_oid,
        )
        _checkpoint("queue-pushed")
        observation = self._remote_oids(
            (queue_ref, article_claim_ref, article_handoff_ref, resolution_ref)
        )
        observed_queue = observation[queue_ref]
        observed_article_claim = observation[article_claim_ref]
        observed_article_handoff = observation[article_handoff_ref]
        observed_resolution = observation[resolution_ref]
        record["observed_queue_oid"] = observed_queue
        record["observed_article_claim_oid"] = observed_article_claim
        record["observed_article_handoff_oid"] = observed_article_handoff
        record["observed_resolution_oid"] = observed_resolution
        if (
            observed_queue == candidate
            and observed_article_handoff == candidate
            and observed_article_claim is None
            and observed_resolution == resolution_oid
        ):
            return _transition_journal(
                journal,
                record,
                "queued",
                "atomic article-claim to queue-ref handoff verified",
            )
        if observed_queue not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} changed during article-claim handoff",
            )
        if observed_article_handoff not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"author handoff ref {article_handoff_ref} changed during queue handoff",
            )
        if observed_resolution not in {None, resolution_oid}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"target resolution ref {resolution_ref} changed during queue handoff",
            )
        if len({observed_queue is None, observed_article_handoff is None, observed_resolution is None}) != 1:
            return self._repair_or_fence_split(record, journal, phase="queue handoff")
        if observed_article_claim != article_claim_oid:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"article claim {article_claim_ref} changed during queue handoff",
            )
        detail = (
            "article-claim handoff CAS was rejected without remote drift"
            if not pushed
            else "article-claim handoff reported success without the exact four-ref result"
        )
        return _transition_journal(
            journal,
            record,
            "prepared" if not pushed else "uncertain",
            detail,
        )

    def _publish_target(
        self,
        record: dict[str, object],
        journal: Path,
        claim_oid: str,
    ) -> dict[str, object]:
        target_ref = str(record["target_ref"])
        queue_ref = str(record["queue_ref"])
        claim_ref = str(record["claim_ref"])
        article_handoff_ref = str(record["article_handoff_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        resolution_oid = str(record["resolution_oid"])
        expected = str(record["expected_target_oid"])
        candidate = str(record["candidate_oid"])
        observation = self._remote_oids(
            (claim_ref, queue_ref, article_handoff_ref, resolution_ref, target_ref)
        )
        observed_claim = observation[claim_ref]
        record["observed_claim_oid"] = observed_claim
        if observed_claim != claim_oid:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"claim ref {claim_ref} changed before target publication",
            )
        observed_queue = observation[queue_ref]
        record["observed_queue_oid"] = observed_queue
        if observed_queue != candidate:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} changed before target publication",
            )
        observed_article_handoff = observation[article_handoff_ref]
        record["observed_article_handoff_oid"] = observed_article_handoff
        if observed_article_handoff != candidate:
            return self._repair_or_fence_split(record, journal, phase="before target publication")
        observed_resolution = observation[resolution_ref]
        record["observed_resolution_oid"] = observed_resolution
        if observed_resolution != resolution_oid:
            return self._repair_or_fence_split(record, journal, phase="before target publication")
        observed = observation[target_ref]
        record["observed_target_oid"] = observed
        if observed not in {_expected_oid(expected), candidate}:
            return _transition_journal(
                journal,
                record,
                "stale",
                f"target {target_ref} drifted from {expected} to {observed or 'absent'}",
            )
        record = _transition_journal(journal, record, "publishing", "target-ref CAS about to run")
        _checkpoint("target-push-attempted")
        pushed = self._atomic_target_push(
            target_ref=target_ref,
            queue_ref=queue_ref,
            article_handoff_ref=article_handoff_ref,
            resolution_ref=resolution_ref,
            resolution_oid=resolution_oid,
            claim_ref=claim_ref,
            claim_oid=claim_oid,
            expected_target=observed,
            candidate=candidate,
        )
        _checkpoint("target-pushed")
        observation = self._remote_oids(
            (target_ref, queue_ref, article_handoff_ref, claim_ref, resolution_ref)
        )
        observed = observation[target_ref]
        observed_queue = observation[queue_ref]
        observed_article_handoff = observation[article_handoff_ref]
        observed_claim = observation[claim_ref]
        observed_resolution = observation[resolution_ref]
        record["observed_target_oid"] = observed
        record["observed_queue_oid"] = observed_queue
        record["observed_article_handoff_oid"] = observed_article_handoff
        record["observed_claim_oid"] = observed_claim
        record["observed_resolution_oid"] = observed_resolution
        if pushed:
            if (
                observed == candidate
                and observed_queue is None
                and observed_article_handoff is None
                and observed_claim != claim_oid
                and observed_resolution is None
            ):
                _checkpoint("target-verified")
                return _transition_journal(
                    journal,
                    record,
                    "integrated",
                    "atomic target advance and queue/handoff/resolution/claim consumption verified",
                )
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "atomic publication reported success without the exact five-ref result",
            )
        if observed_claim != claim_oid:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"claim ref {claim_ref} changed during target publication",
            )
        if observed_queue not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} changed during target publication",
            )
        if observed_article_handoff not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"author handoff ref {article_handoff_ref} changed during target publication",
            )
        if observed_resolution not in {None, resolution_oid}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"target resolution ref {resolution_ref} changed during target publication",
            )
        if len({observed_queue is None, observed_article_handoff is None, observed_resolution is None}) != 1:
            return self._repair_or_fence_split(record, journal, phase="target publication")
        if (
            observed == candidate
            and observed_queue is None
            and observed_article_handoff is None
            and observed_resolution is None
        ):
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "target advanced and queue disappeared without consuming the exact merge claim",
            )
        if (
            observed in {_expected_oid(expected), candidate}
            and observed_queue == candidate
            and observed_article_handoff == candidate
            and observed_resolution == resolution_oid
        ):
            return _transition_journal(journal, record, "queued", "target CAS was rejected without drift")
        if observed != _expected_oid(expected):
            return _transition_journal(
                journal,
                record,
                "stale",
                f"target {target_ref} is {observed or 'absent'} after CAS",
            )
        return _transition_journal(journal, record, "uncertain", "target CAS outcome could not be classified")

    def _recover_record(self, record: dict[str, object], journal: Path) -> dict[str, object]:
        status = str(record["status"])
        if status in {"integrated", "aborted", "stale", "uncertain"}:
            return record
        queue_ref = str(record["queue_ref"])
        target_ref = str(record["target_ref"])
        claim_ref = str(record["claim_ref"])
        article_claim_ref = str(record["article_claim_ref"])
        article_handoff_ref = str(record["article_handoff_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        resolution_oid = str(record["resolution_oid"])
        article_claim_oid = str(record["article_claim_oid"])
        recorded_claim_oid = record.get("claim_oid")
        expected = _expected_oid(str(record["expected_target_oid"]))
        candidate = str(record["candidate_oid"])
        observation = self._remote_oids(
            (
                queue_ref,
                target_ref,
                claim_ref,
                article_claim_ref,
                article_handoff_ref,
                resolution_ref,
            )
        )
        queue_oid = observation[queue_ref]
        target_oid = observation[target_ref]
        claim_oid = observation[claim_ref]
        article_claim_oid_observed = observation[article_claim_ref]
        article_handoff_oid = observation[article_handoff_ref]
        observed_resolution_oid = observation[resolution_ref]
        record["observed_queue_oid"] = queue_oid
        record["observed_target_oid"] = target_oid
        record["observed_claim_oid"] = claim_oid
        record["observed_article_claim_oid"] = article_claim_oid_observed
        record["observed_article_handoff_oid"] = article_handoff_oid
        record["observed_resolution_oid"] = observed_resolution_oid
        if queue_oid not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"recovery found queue-ref collision: {queue_oid}",
            )
        if article_handoff_oid not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"recovery found author-handoff collision: {article_handoff_oid}",
            )
        if observed_resolution_oid not in {None, resolution_oid}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"recovery found target-resolution collision: {observed_resolution_oid}",
            )
        if len({queue_oid is None, article_handoff_oid is None, observed_resolution_oid is None}) != 1:
            return self._repair_or_fence_split(record, journal, phase="recovery")
        if target_oid == candidate:
            if queue_oid is None and article_handoff_oid is None and observed_resolution_oid is None:
                if article_claim_oid_observed == article_claim_oid:
                    return _transition_journal(
                        journal,
                        record,
                        "uncertain",
                        "target advanced without consuming the exact article claim",
                    )
                if (
                    (recorded_claim_oid is not None and claim_oid == recorded_claim_oid)
                    or (recorded_claim_oid is None and claim_oid is not None)
                ):
                    return _transition_journal(
                        journal,
                        record,
                        "uncertain",
                        "target advanced and queue disappeared without consuming the exact merge claim",
                    )
                return _transition_journal(
                    journal,
                    record,
                    "integrated",
                    "recovery verified target and consumed queue and author-handoff refs",
                )
            if article_claim_oid_observed is not None:
                return _transition_journal(
                    journal,
                    record,
                    "uncertain",
                    "target and queue point to the candidate while an article claim is present",
                )
            recovered_status = "queued"
        elif target_oid != expected:
            return _transition_journal(
                journal,
                record,
                "stale",
                f"recovery found target drift: {target_oid or 'absent'}",
            )
        elif (
            queue_oid == candidate
            and article_handoff_oid == candidate
            and observed_resolution_oid == resolution_oid
        ):
            if article_claim_oid_observed is not None:
                return _transition_journal(
                    journal,
                    record,
                    "uncertain",
                    "recovery found queued work while an article claim is present",
                )
            recovered_status = "queued"
        elif (
            queue_oid is None
            and article_handoff_oid is None
            and observed_resolution_oid is None
            and article_claim_oid_observed == article_claim_oid
        ):
            recovered_status = "prepared"
        else:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "recovery found neither the queued candidate nor the exact article claim",
            )
        if status == recovered_status:
            return record
        return _transition_journal(
            journal,
            record,
            recovered_status,
            f"recovery classified interrupted {status} as {recovered_status}",
        )

    def _load_or_create(self, **identity: str) -> tuple[dict[str, object], Path]:
        queue_item_id = identity["queue_item_id"]
        directory, journal = self._publication_paths(queue_item_id)
        staging = self.publication_root / f".{queue_item_id}.preparing"
        staging_journal = staging / "publication.json"
        if journal.exists() or journal.is_symlink():
            if staging.exists() or staging.is_symlink():
                raise PublicationUncertain("publication has both final and staging state")
            record = self._read_journal(journal)
            self._validate_journal(record, journal=journal, **identity)
            return record, journal
        if directory.exists() or directory.is_symlink():
            raise PublicationUncertain("publication path exists without a durable ownership journal")
        if staging.exists() or staging.is_symlink():
            if staging.is_symlink() or _canonical_existing_directory(staging) != staging:
                raise PublicationUncertain("publication staging path was replaced")
            if staging_journal.exists() or staging_journal.is_symlink():
                record = self._read_journal(staging_journal)
                self._validate_journal(record, journal=staging_journal, **identity)
            else:
                _remove_atomic_write_orphans(staging_journal)
                try:
                    next(staging.iterdir())
                except StopIteration:
                    record = self._new_publication_record(staging, identity)
                    _write_publication_journal(staging_journal, record)
                else:
                    raise PublicationUncertain("publication staging path has no durable ownership journal")
        else:
            staging.mkdir(mode=0o700)
            _checkpoint("publication-staging-created")
            record = self._new_publication_record(staging, identity)
            _write_publication_journal(staging_journal, record)
        _checkpoint("publication-staging-recorded")
        if directory.exists() or directory.is_symlink():
            raise PublicationUncertain("publication path appeared before its durable rename")
        os.rename(staging, directory)
        _fsync_directory(self.publication_root)
        self._validate_journal(record, journal=journal, **identity)
        _checkpoint("publication-intent-recorded")
        return record, journal

    def _new_publication_record(self, directory: Path, identity: Mapping[str, str]) -> dict[str, object]:
        publication_identity = _directory_identity(directory)
        now = time.time_ns()
        detail = "durable publication intent recorded"
        resolution_oid = self._make_resolution_commit(identity, phase="queued")
        return {
            "schema": _PUBLICATION_SCHEMA,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            **identity,
            "status": "prepared",
            "observed_target_oid": None,
            "observed_queue_oid": None,
            "observed_article_claim_oid": None,
            "observed_article_handoff_oid": None,
            "initial_resolution_oid": resolution_oid,
            "resolution_oid": resolution_oid,
            "observed_resolution_oid": None,
            "claim_oid": None,
            "observed_claim_oid": None,
            "claim_lease_id": None,
            "publication_device": publication_identity[0],
            "publication_inode": publication_identity[1],
            "detail": detail,
            "created_ns": now,
            "updated_ns": now,
            "history": [{"status": "prepared", "detail": detail, "created_ns": now}],
            "replay_intents": [],
            "replay_events": [],
            "resolution_intents": [],
        }

    def _publication_paths(self, queue_item_id: str) -> tuple[Path, Path]:
        _validate_name("queue item id", queue_item_id)
        directory = self.publication_root / queue_item_id
        return directory, directory / "publication.json"

    def _read_journal(self, journal: Path) -> dict[str, object]:
        return _read_json_file(journal, label="publication journal")

    def _validate_journal(
        self,
        record: Mapping[str, object],
        *,
        journal: Path,
        **identity: str,
    ) -> None:
        schema = record.get("schema")
        legacy_schema = schema in _LEGACY_PUBLICATION_SCHEMAS
        expected = {
            "schema": _PUBLICATION_SCHEMA,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            **identity,
        }
        for key, value in expected.items():
            if key == "schema" and legacy_schema:
                continue
            if legacy_schema and key in {"target_resolution_ref"}:
                continue
            if schema == "autoform-merge-publication/v3" and key == "article_handoff_ref":
                continue
            if key not in record or record[key] != value:
                raise PublicationUncertain(f"publication journal has a different {key}")
        if record["remote_kind"] == "local" and (
            not _is_integer(record["remote_device"]) or not _is_integer(record["remote_inode"])
        ):
            raise PublicationUncertain("publication journal has an invalid local remote identity")
        if record.get("status") not in _PUBLICATION_STATES:
            raise PublicationUncertain("publication journal has an unknown status")
        if not legacy_schema:
            terminal = record.get("status") in {"integrated", "aborted", "uncertain"}
            maximum = _MAX_PUBLICATION_BYTES
            if not terminal:
                maximum -= _PUBLICATION_TERMINAL_RESERVE_BYTES
            if _publication_journal_size(record) > maximum:
                raise PublicationUncertain("publication journal has consumed its reserved terminal bytes")
        if not isinstance(record.get("history"), list):
            raise PublicationUncertain("publication journal history is malformed")
        if len(record["history"]) > _MAX_PUBLICATION_HISTORY:
            raise PublicationUncertain("publication journal history exceeds its evidence bound")
        for key in ("publication_device", "publication_inode", "created_ns", "updated_ns"):
            if not _is_integer(record.get(key)):
                raise PublicationUncertain(f"publication journal has an invalid {key}")
        if not isinstance(record.get("detail"), str):
            raise PublicationUncertain("publication journal detail is malformed")
        oid_width = 64 if self.repository.object_format == "sha256" else 40
        lease_id = record.get("claim_lease_id")
        if lease_id is not None and (
            not isinstance(lease_id, str) or re.fullmatch(r"[0-9a-f]{64}", lease_id) is None
        ):
            raise PublicationUncertain("publication journal claim lease id is malformed")
        for key in (
            "article_claim_oid",
            "claim_oid",
            "observed_article_claim_oid",
            "observed_article_handoff_oid",
            "observed_claim_oid",
            "observed_target_oid",
            "observed_queue_oid",
            "initial_resolution_oid",
            "resolution_oid",
            "observed_resolution_oid",
        ):
            observed = record.get(key)
            if observed is not None and (
                not isinstance(observed, str)
                or not _OID.fullmatch(observed)
                or len(observed) != oid_width
                or set(observed) == {"0"}
            ):
                raise PublicationUncertain(f"publication journal has an invalid {key}")
        if not _journal_directory_matches(journal, record):
            raise PublicationUncertain("publication directory was replaced")
        for entry in record["history"]:
            if (
                not isinstance(entry, dict)
                or entry.get("status") not in _PUBLICATION_STATES
                or not isinstance(entry.get("detail"), str)
                or not _is_integer(entry.get("created_ns"))
            ):
                raise PublicationUncertain("publication journal history is malformed")
        if legacy_schema:
            if not isinstance(record, dict):
                raise PublicationUncertain("legacy publication journal is malformed")
            migrated = copy.deepcopy(record)
            if schema in {"autoform-merge-publication/v3", "autoform-merge-publication/v4"} and (
                "replay_intents" in migrated or "replay_events" in migrated
            ):
                raise PublicationUncertain("legacy publication journal has unexpected replay state")
            if schema in {"autoform-merge-publication/v3", "autoform-merge-publication/v4"} and (
                migrated.get("status") == "replaying"
                or any(
                    isinstance(entry, dict) and entry.get("status") == "replaying"
                    for entry in migrated["history"]
                )
            ):
                raise PublicationUncertain("legacy publication journal has an invalid replaying state")
            if schema == "autoform-merge-publication/v3":
                migrated["article_handoff_ref"] = identity["article_handoff_ref"]
            migrated["target_resolution_ref"] = identity["target_resolution_ref"]
            resolution_oid = self._legacy_resolution_oid(migrated)
            migrated["initial_resolution_oid"] = resolution_oid
            migrated["resolution_oid"] = resolution_oid
            migrated["observed_resolution_oid"] = None
            if schema == "autoform-merge-publication/v5":
                self._upgrade_v5_replay_history(migrated)
            migrated["schema"] = _PUBLICATION_SCHEMA
            migrated.setdefault("replay_intents", [])
            migrated.setdefault("replay_events", [])
            migrated.setdefault("resolution_intents", [])
            self._validate_replay_history(migrated)
            self._validate_resolution_chain(migrated)
            self._validate_resolution_intents(migrated)
            _checkpoint("publication-schema-upgrade-ready")
            _write_publication_journal(journal, migrated)
            record.clear()
            record.update(migrated)
            _checkpoint("publication-schema-upgraded")
        self._validate_replay_history(record)
        self._validate_resolution_chain(record)
        self._validate_resolution_intents(record)

    def _validate_resolution_intents(self, record: Mapping[str, object]) -> None:
        intents = record.get("resolution_intents")
        if not isinstance(intents, list) or len(intents) > _MAX_REPLAY_ATTEMPTS:
            raise PublicationUncertain("publication resolution intent history is malformed")
        keys = {
            "intent_id",
            "schema",
            "decision",
            "queue_item_id",
            "target_ref",
            "queue_ref",
            "article_handoff_ref",
            "target_resolution_ref",
            "candidate_oid",
            "resolution_oid",
            "resolution_base_oid",
            "resolution_candidate_oid",
            "observed_target_oid",
            "observed_queue_oid",
            "observed_article_handoff_oid",
            "observed_article_claim_oid",
            "observed_claim_oid",
            "observed_resolution_oid",
            "created_ns",
        }
        effective = self._effective_replay_intent(record)
        expected_base = (
            str(effective["replay_target_oid"])
            if effective is not None
            else _expected_oid(str(record["expected_target_oid"]))
        )
        expected_candidate = (
            str(effective["replay_candidate_oid"])
            if effective is not None
            else str(record["candidate_oid"])
        )
        oid_width = 64 if self.repository.object_format == "sha256" else 40
        decision: str | None = None
        for intent in intents:
            if not isinstance(intent, dict) or set(intent) != keys:
                raise PublicationUncertain("publication resolution intent is malformed")
            intent_id = intent.get("intent_id")
            body = {key: value for key, value in intent.items() if key != "intent_id"}
            if (
                not isinstance(intent_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", intent_id) is None
                or hashlib.sha256(_json_bytes(body)).hexdigest() != intent_id
            ):
                raise PublicationUncertain("publication resolution intent digest is invalid")
            expected = {
                "schema": _RESOLUTION_INTENT_SCHEMA,
                "queue_item_id": record.get("queue_item_id"),
                "target_ref": record.get("target_ref"),
                "queue_ref": record.get("queue_ref"),
                "article_handoff_ref": record.get("article_handoff_ref"),
                "target_resolution_ref": record.get("target_resolution_ref"),
                "candidate_oid": record.get("candidate_oid"),
                "resolution_oid": record.get("resolution_oid"),
                "resolution_base_oid": expected_base,
                "resolution_candidate_oid": expected_candidate,
                "observed_article_claim_oid": None,
                "observed_resolution_oid": record.get("resolution_oid"),
            }
            if any(intent.get(key) != value for key, value in expected.items()):
                raise PublicationUncertain("publication resolution intent does not match its journal")
            if intent.get("decision") not in {"abort", "adopt"} or not _is_integer(
                intent.get("created_ns")
            ):
                raise PublicationUncertain("publication resolution intent has invalid authorization fields")
            if decision is not None and intent["decision"] != decision:
                raise PublicationUncertain("publication resolution decisions conflict")
            decision = str(intent["decision"])
            if decision == "abort" and intent.get("observed_target_oid") != expected_base:
                raise PublicationUncertain("publication abort intent is not bound to its exact base")
            for key in (
                "resolution_candidate_oid",
                "observed_claim_oid",
                "observed_resolution_oid",
            ):
                value = intent.get(key)
                if (
                    not isinstance(value, str)
                    or not _OID.fullmatch(value)
                    or len(value) != oid_width
                    or set(value) == {"0"}
                ):
                    raise PublicationUncertain(f"publication resolution intent has an invalid {key}")
            for key in ("resolution_base_oid", "observed_target_oid"):
                value = intent.get(key)
                if value is not None and (
                    not isinstance(value, str)
                    or not _OID.fullmatch(value)
                    or len(value) != oid_width
                    or set(value) == {"0"}
                ):
                    raise PublicationUncertain(f"publication resolution intent has an invalid {key}")
            for key in ("observed_queue_oid", "observed_article_handoff_oid"):
                if intent.get(key) not in {None, record.get("candidate_oid")}:
                    raise PublicationUncertain(f"publication resolution intent has a foreign {key}")

    def _validate_resolution_chain(self, record: Mapping[str, object]) -> None:
        initial = self._resolution_commit_oid(record, phase="queued")
        if record.get("initial_resolution_oid") != initial:
            raise PublicationUncertain(
                "publication initial resolution OID does not match its canonical owner metadata"
            )
        current = initial
        intents = record.get("replay_intents")
        events = record.get("replay_events")
        if not isinstance(intents, list) or not isinstance(events, list):
            raise PublicationUncertain("publication replay history is malformed")
        unpinned = {
            str(event["replay_id"])
            for event in events
            if isinstance(event, dict) and event.get("status") == "pin-retry"
        }
        for intent in intents:
            if not isinstance(intent, dict):
                raise PublicationUncertain("publication replay intent is malformed")
            if intent.get("prior_resolution_oid") != current:
                raise PublicationUncertain("publication replay resolution chain is discontinuous")
            replay = {
                key: value
                for key, value in intent.items()
                if key not in {"replay_id", "prior_resolution_oid", "resolution_oid"}
            }
            expected = self._resolution_commit_oid(record, phase="replay", replay=replay)
            if intent.get("resolution_oid") != expected:
                raise PublicationUncertain(
                    "publication replay resolution OID does not match its canonical owner metadata"
                )
            if intent.get("replay_id") not in unpinned:
                current = expected
        if record.get("resolution_oid") != current:
            raise PublicationUncertain("publication current resolution OID does not match its replay chain")

    def _validate_replay_history(self, record: Mapping[str, object]) -> None:
        intents = record.get("replay_intents")
        events = record.get("replay_events")
        if not isinstance(intents, list) or not isinstance(events, list):
            raise PublicationUncertain("publication replay history is malformed")
        if len(intents) > _MAX_REPLAY_ATTEMPTS:
            raise PublicationUncertain("publication replay intent history exceeds its evidence bound")
        if len(events) > _MAX_REPLAY_EVENTS:
            raise PublicationUncertain("publication replay event history exceeds its evidence bound")
        intent_keys = {
            "replay_id",
            "schema",
            "object_format",
            "original_expected_oid",
            "original_candidate_oid",
            "replay_target_oid",
            "replay_candidate_oid",
            "replay_tree_oid",
            "source_delta_sha256",
            "target_ref",
            "queue_ref",
            "expected_queue_oid",
            "article_claim_ref",
            "expected_article_claim_oid",
            "article_handoff_ref",
            "expected_article_handoff_oid",
            "claim_key",
            "claim_ref",
            "claim_oid",
            "claim_lease_id",
            "created_ns",
            "prior_resolution_oid",
            "resolution_oid",
            "target_resolution_ref",
        }
        intent_ids: list[str] = []
        oid_width = 64 if self.repository.object_format == "sha256" else 40
        for intent in intents:
            if not isinstance(intent, dict) or set(intent) != intent_keys:
                raise PublicationUncertain("publication replay intent is malformed")
            replay_id = intent.get("replay_id")
            if not isinstance(replay_id, str) or re.fullmatch(r"[0-9a-f]{64}", replay_id) is None:
                raise PublicationUncertain("publication replay intent has an invalid id")
            if replay_id in intent_ids:
                raise PublicationUncertain("publication replay intent id is duplicated")
            body = {key: value for key, value in intent.items() if key != "replay_id"}
            if hashlib.sha256(_json_bytes(body)).hexdigest() != replay_id:
                raise PublicationUncertain("publication replay intent digest does not match its content")
            if intent.get("schema") != _REPLAY_INTENT_SCHEMA:
                raise PublicationUncertain("publication replay intent has an unknown schema")
            if intent.get("object_format") != self.repository.object_format:
                raise PublicationUncertain("publication replay intent has a different object format")
            identity = {
                "original_expected_oid": record.get("expected_target_oid"),
                "original_candidate_oid": record.get("candidate_oid"),
                "target_ref": record.get("target_ref"),
                "queue_ref": record.get("queue_ref"),
                "expected_queue_oid": record.get("candidate_oid"),
                "article_claim_ref": record.get("article_claim_ref"),
                "expected_article_claim_oid": None,
                "article_handoff_ref": record.get("article_handoff_ref"),
                "expected_article_handoff_oid": record.get("candidate_oid"),
                "claim_key": record.get("claim_key"),
                "claim_ref": record.get("claim_ref"),
                "target_resolution_ref": record.get("target_resolution_ref"),
            }
            if any(intent.get(key) != value for key, value in identity.items()):
                raise PublicationUncertain("publication replay intent does not match its publication")
            for key in (
                "original_expected_oid",
                "original_candidate_oid",
                "replay_target_oid",
                "replay_candidate_oid",
                "replay_tree_oid",
                "expected_queue_oid",
                "expected_article_handoff_oid",
                "claim_oid",
                "prior_resolution_oid",
                "resolution_oid",
            ):
                value = intent.get(key)
                if not isinstance(value, str) or not _OID.fullmatch(value) or len(value) != oid_width:
                    raise PublicationUncertain(f"publication replay intent has an invalid {key}")
            for key in ("source_delta_sha256", "claim_lease_id"):
                value = intent.get(key)
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise PublicationUncertain(f"publication replay intent has an invalid {key}")
            if not _is_integer(intent.get("created_ns")):
                raise PublicationUncertain("publication replay intent has an invalid created_ns")
            intent_ids.append(replay_id)

        event_counts = {replay_id: 0 for replay_id in intent_ids}
        terminal: set[str] = set()
        greatest_intent_index = -1
        for event in events:
            if not isinstance(event, dict):
                raise PublicationUncertain("publication replay event is malformed")
            replay_id = event.get("replay_id")
            if not isinstance(replay_id, str) or replay_id not in event_counts:
                raise PublicationUncertain("publication replay event references an unknown intent")
            intent_index = intent_ids.index(replay_id)
            if intent_index < greatest_intent_index or replay_id in terminal:
                raise PublicationUncertain("publication replay event ordering is malformed")
            greatest_intent_index = intent_index
            status = event.get("status")
            if status not in _REPLAY_EVENT_STATES or not isinstance(event.get("detail"), str):
                raise PublicationUncertain("publication replay event is malformed")
            if not _is_integer(event.get("created_ns")):
                raise PublicationUncertain("publication replay event has an invalid created_ns")
            event_counts[replay_id] += 1
            if event_counts[replay_id] == 1 and status != "prepared":
                raise PublicationUncertain("publication replay intent has no prepared event")
            if event_counts[replay_id] > 1 and status == "prepared":
                raise PublicationUncertain("publication replay intent has duplicate prepared events")
            if status in _REPLAY_TERMINAL_STATES:
                terminal.add(replay_id)
            for key in (
                "observed_target_oid",
                "observed_queue_oid",
                "observed_article_claim_oid",
                "observed_article_handoff_oid",
                "observed_claim_oid",
                "observed_resolution_oid",
            ):
                value = event.get(key)
                if value is not None and (
                    not isinstance(value, str) or not _OID.fullmatch(value) or len(value) != oid_width
                ):
                    raise PublicationUncertain(f"publication replay event has an invalid {key}")
        if any(count == 0 for count in event_counts.values()):
            raise PublicationUncertain("publication replay intent has no event history")
        if any(replay_id not in terminal for replay_id in intent_ids[:-1]):
            raise PublicationUncertain("publication replay intents overlap")
        if record.get("status") == "replaying":
            if not intent_ids or intent_ids[-1] in terminal:
                raise PublicationUncertain("publication replay status has no active intent")
        elif intent_ids and intent_ids[-1] not in terminal:
            raise PublicationUncertain("publication has an unterminated replay intent")
        elif intent_ids:
            last_status = next(
                str(event["status"])
                for event in reversed(events)
                if isinstance(event, dict) and event.get("replay_id") == intent_ids[-1]
            )
            expected_status = "stale" if last_status in {"pin-retry", "retry"} else last_status
            if record.get("status") not in {expected_status, "aborted", "uncertain"}:
                raise PublicationUncertain("publication status disagrees with its last replay event")

    def _bind_v2_journal(
        self,
        record: dict[str, object],
        *,
        article_claim: ClaimFence,
        article_handoff_ref: str,
        target_resolution_ref: str,
    ) -> dict[str, object]:
        bound = copy.deepcopy(record)
        bound.update(
            {
                "schema": "autoform-merge-publication/v3",
                "article_claim_key": article_claim.key,
                "article_claim_ref": article_claim.ref,
                "article_claim_oid": article_claim.oid,
                "article_claim_lease_id": article_claim.lease_id,
                "observed_article_claim_oid": None,
                "article_handoff_ref": article_handoff_ref,
                "observed_article_handoff_oid": None,
                "target_resolution_ref": target_resolution_ref,
            }
        )
        return bound

    def _upgrade_v5_replay_history(self, record: dict[str, object]) -> None:
        intents = record.get("replay_intents")
        events = record.get("replay_events")
        if not isinstance(intents, list) or not isinstance(events, list):
            raise PublicationUncertain("v5 publication replay history is malformed")
        prior_resolution = str(record["initial_resolution_oid"])
        id_map: dict[str, str] = {}
        upgraded: list[dict[str, object]] = []
        for old in intents:
            if not isinstance(old, dict) or not isinstance(old.get("replay_id"), str):
                raise PublicationUncertain("v5 publication replay intent is malformed")
            old_id = str(old["replay_id"])
            old_body = {key: value for key, value in old.items() if key != "replay_id"}
            if hashlib.sha256(_json_bytes(old_body)).hexdigest() != old_id:
                raise PublicationUncertain("v5 publication replay intent digest does not match its content")
            payload = dict(old_body)
            payload["target_resolution_ref"] = record["target_resolution_ref"]
            resolution_oid = self._make_resolution_commit(record, phase="replay", replay=payload)
            body = {
                **payload,
                "prior_resolution_oid": prior_resolution,
                "resolution_oid": resolution_oid,
            }
            new_id = hashlib.sha256(_json_bytes(body)).hexdigest()
            upgraded.append({"replay_id": new_id, **body})
            id_map[old_id] = new_id
            prior_resolution = resolution_oid
        for event in events:
            if isinstance(event, dict) and str(event.get("replay_id")) in id_map:
                event["replay_id"] = id_map[str(event["replay_id"])]
        record["replay_intents"] = upgraded
        record["resolution_oid"] = prior_resolution
        if record.get("status") == "replaying" and upgraded:
            active = upgraded[-1]
            now = time.time_ns()
            events.append(
                {
                    "replay_id": active["replay_id"],
                    "status": "pin-retry",
                    "detail": "v5 active replay requires a newly fenced v6 retry",
                    "created_ns": now,
                }
            )
            record["status"] = "stale"
            record["resolution_oid"] = active["prior_resolution_oid"]
            record["detail"] = "v5 active replay requires a newly fenced v6 retry"
            record["updated_ns"] = now
            record["claim_oid"] = active["claim_oid"]
            record["claim_lease_id"] = active["claim_lease_id"]
            history = record.get("history")
            if not isinstance(history, list) or len(history) >= _MAX_PUBLICATION_HISTORY:
                raise PublicationUncertain("v5 publication has no migration history capacity")
            history.append(
                {"status": "stale", "detail": record["detail"], "created_ns": now}
            )

    def _legacy_resolution_oid(self, record: dict[str, object]) -> str:
        if record.get("status") != "integrated":
            self._ensure_original_objects(record)
            self._verify_candidate(
                str(record["expected_target_oid"]),
                str(record["candidate_oid"]),
                use_transport=True,
            )
        return self._make_resolution_commit(record, phase="queued")

    def _ensure_migrated_resolution(self, record: dict[str, object], journal: Path) -> None:
        status = str(record["status"])
        if status in {"integrated", "aborted"}:
            return
        queue_ref = str(record["queue_ref"])
        handoff_ref = str(record["article_handoff_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        target_ref = str(record["target_ref"])
        claim_ref = str(record["claim_ref"])
        observation = self._remote_oids(
            (queue_ref, handoff_ref, resolution_ref, target_ref, claim_ref)
        )
        queue = observation[queue_ref]
        handoff = observation[handoff_ref]
        resolution = observation[resolution_ref]
        candidate = str(record["candidate_oid"])
        expected_resolution = str(record["resolution_oid"])
        target = observation[target_ref]
        claim = observation[claim_ref]
        record["observed_queue_oid"] = queue
        record["observed_article_handoff_oid"] = handoff
        record["observed_resolution_oid"] = resolution
        record["observed_target_oid"] = target
        record["observed_claim_oid"] = claim
        if target == candidate and claim is not None:
            _transition_journal(
                journal,
                record,
                "uncertain",
                "target advanced without consuming the exact merge claim",
            )
            return
        if queue is None and handoff is None and resolution is None:
            return
        active_replay = self._active_replay_intent(record) if status == "replaying" else None
        if (
            active_replay is not None
            and queue == candidate
            and handoff == candidate
            and resolution
            in {
                active_replay.get("prior_resolution_oid"),
                active_replay.get("resolution_oid"),
            }
        ):
            return
        if queue == candidate and handoff == candidate and resolution == expected_resolution:
            return
        claim_key = str(record["claim_key"])
        recorded_claim = record.get("claim_oid")
        if isinstance(recorded_claim, str) and claim == recorded_claim:
            self._repair_or_fence_split(record, journal, phase="during schema migration")
            return
        acquired = self.claim_board.acquire(
            claim_key,
            ttl=self.claim_ttl,
            note=f"migrate merge queue item {record['queue_item_id']}",
            expected_resolution_oid=resolution,
        )
        if not acquired:
            raise MergeQueueBusy("target resolution migration is fenced by another publisher")
        fence = self._held_claim_fence(claim_key)
        if fence is None:
            self.claim_board.release(claim_key)
            raise PublicationUncertain("resolution migration has no coherent merge fence")
        try:
            record["claim_oid"] = fence.oid
            record["claim_lease_id"] = fence.lease_id
            self._repair_or_fence_split(record, journal, phase="during schema migration")
        finally:
            self._secure_claim_cleanup(record, fence)
            self._release_claim_fence(fence.ref, fence.oid)

    def _validate_publication_identity(
        self,
        queue_item_id: str,
        target_ref: str,
        queue_ref: str,
        expected_target_oid: str,
        candidate_oid: str,
    ) -> None:
        _validate_name("queue item id", queue_item_id)
        _validate_full_ref(target_ref, prefix="refs/heads/")
        _validate_full_ref(queue_ref, prefix="refs/autoform/queue/")
        _validate_expected_oid(expected_target_oid)
        _validate_oid(candidate_oid)
        if target_ref == queue_ref:
            raise MergeQueueError("target ref and queue ref must differ")

    def _validate_article_claim(self, article_claim: ClaimFence) -> None:
        if not isinstance(article_claim, ClaimFence):
            raise MergeQueueError("article claim must be a coherent ClaimFence")
        if not article_claim.key.startswith("author/"):
            raise MergeQueueError("article claim must use the author claim namespace")
        expected_oid_length = 64 if self.repository.object_format == "sha256" else 40
        if len(article_claim.oid) != expected_oid_length:
            raise MergeQueueError("article claim object id does not match the repository object format")

    def _verify_candidate(self, expected: str, candidate: str, *, use_transport: bool = False) -> None:
        verify = self._verify_transport_commit if use_transport else self.repository._verify_commit
        verify(candidate)
        if expected not in _ZERO_OIDS:
            verify(expected)
            ancestor = (
                self._transport_is_ancestor(expected, candidate)
                if use_transport
                else self.repository._is_ancestor(expected, candidate)
            )
            if not ancestor:
                raise MergeQueueError("candidate is not descended from the expected target object")

    def _ensure_original_objects(self, record: Mapping[str, object]) -> None:
        expected = str(record["expected_target_oid"])
        candidate = str(record["candidate_oid"])
        if self._transport_has_commit(candidate) and (
            expected in _ZERO_OIDS or self._transport_has_commit(expected)
        ):
            return
        queue_ref = str(record["queue_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        if self._remote_oid(queue_ref) == candidate:
            self._fetch_exact_ref(
                queue_ref,
                candidate,
                owner=str(record["queue_item_id"]),
            )
        elif self._remote_oid(resolution_ref) == record.get("resolution_oid"):
            self._fetch_exact_ref(
                resolution_ref,
                str(record["resolution_oid"]),
                owner=str(record["queue_item_id"]),
            )
        self._verify_candidate(expected, candidate, use_transport=True)

    def _import_original_objects(self, expected: str, candidate: str, *, owner: str) -> None:
        """Copy the exact publication commits into the owned transport store."""

        commits: list[tuple[str, str]] = [("candidate", candidate)]
        if expected not in _ZERO_OIDS:
            commits.append(("expected", expected))
        self._import_commits(commits, owner=owner)
        self._verify_candidate(expected, candidate, use_transport=True)

    def _import_commits(self, commits: Iterable[tuple[str, str]], *, owner: str) -> None:
        self.repository._verify_repository()
        self._verify_state()
        self._verify_transport()
        source = os.fspath(self.repository.common_git_dir)
        for role, oid in commits:
            _validate_oid(oid)
            if self._transport_has_commit(oid, use_repository_alternate=False):
                continue
            cache_ref = self._cache_ref(owner, "source-" + hashlib.sha256(
                f"{role}\0{oid}".encode()
            ).hexdigest())
            self._run_local_git_bytes(
                ["fetch", "--quiet", "--no-tags", "--force", source, f"{oid}:{cache_ref}"],
                use_repository_alternate=False,
            )
            if not self._transport_has_commit(oid, use_repository_alternate=False):
                raise RepositoryError(f"failed to durably import exact {role} commit {oid}")
        self.repository._verify_repository()
        self._verify_state()
        self._verify_transport()

    def _ensure_resolution_objects(self, record: Mapping[str, object]) -> None:
        self._fetch_exact_ref(
            str(record["target_resolution_ref"]),
            str(record["resolution_oid"]),
            owner=str(record["queue_item_id"]),
        )

    def _cache_ref(self, owner: str, token: str) -> str:
        _validate_name("cache owner", owner)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", token):
            raise RepositoryError("transport cache token is invalid")
        scope = hashlib.sha256(f"{self.remote_id}\0{owner}".encode()).hexdigest()
        return f"refs/autoform-cache/{scope}/{token}"

    def _fetch_exact_ref(self, remote_ref: str, expected_oid: str, *, owner: str) -> None:
        observed = self._remote_oid(remote_ref)
        if observed != expected_oid:
            raise PublicationUncertain(
                f"remote ref {remote_ref} changed before its objects could be recovered"
            )
        cache_ref = self._cache_ref(
            owner,
            "remote-" + hashlib.sha256(f"{remote_ref}\0{expected_oid}".encode()).hexdigest(),
        )
        self._remote_git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--force",
                self.remote_url,
                f"{remote_ref}:{cache_ref}",
            ]
        )
        fetched = self._local_git_text(["rev-parse", "--verify", cache_ref]).strip()
        if fetched != expected_oid:
            raise PublicationUncertain(f"fetched ref {remote_ref} did not preserve its exact object")

    def _cleanup_transport_cache(self, owner: str) -> None:
        prefix = self._cache_ref(owner, "scope").rsplit("/", 1)[0] + "/"
        refs = self._local_git_text(
            ["for-each-ref", "--format=%(refname)", prefix]
        ).splitlines()
        for ref in refs:
            if not ref.startswith(prefix):  # pragma: no cover - Git output invariant
                raise RepositoryError("transport cache enumeration escaped its publication scope")
            self._run_local_git_bytes(["update-ref", "-d", ref])

    def _receipt(self, record: Mapping[str, object]) -> PublicationReceipt:
        receipt = _publication_receipt(record)
        if receipt.status in {"integrated", "aborted"}:
            self._cleanup_transport_cache(receipt.queue_item_id)
        return receipt

    def _verify_transport_commit(self, oid: str) -> None:
        proc = self._run_local_git_bytes(["cat-file", "-e", f"{oid}^{{commit}}"], check=False)
        if proc.returncode != 0:
            raise RepositoryError(f"base object does not resolve exactly to a commit: {oid}")

    def _transport_has_commit(self, oid: str, *, use_repository_alternate: bool = False) -> bool:
        proc = self._run_local_git_bytes(
            ["cat-file", "-e", f"{oid}^{{commit}}"],
            check=False,
            use_repository_alternate=use_repository_alternate,
        )
        return proc.returncode == 0

    def _transport_is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self._run_local_git_bytes(
            ["merge-base", "--is-ancestor", ancestor, descendant], check=False
        )
        if proc.returncode not in {0, 1}:
            raise RepositoryError("Git ancestry verification failed")
        return proc.returncode == 0

    def _local_git_text(self, args: list[str]) -> str:
        return self._run_local_git_bytes(args).stdout.decode("utf-8", errors="strict")

    def _make_resolution_commit(
        self,
        identity: Mapping[str, object],
        *,
        phase: str,
        replay: Mapping[str, object] | None = None,
    ) -> str:
        content = self._resolution_commit_content(identity, phase=phase, replay=replay)
        expected = _git_object_oid("commit", content, self.repository.object_format)
        empty_tree = self._run_local_git_bytes(
            ["hash-object", "-t", "tree", "-w", "--stdin"], input_bytes=b""
        ).stdout.decode("ascii").strip()
        if empty_tree != _git_object_oid("tree", b"", self.repository.object_format):
            raise RepositoryError("Git did not preserve the canonical empty resolution tree")
        result = self._run_local_git_bytes(
            ["hash-object", "-t", "commit", "-w", "--stdin"],
            input_bytes=content,
        ).stdout.decode("ascii").strip()
        if result != expected:
            raise RepositoryError("Git did not preserve the canonical target-resolution object")
        return result

    def _resolution_commit_oid(
        self,
        identity: Mapping[str, object],
        *,
        phase: str,
        replay: Mapping[str, object] | None = None,
    ) -> str:
        content = self._resolution_commit_content(identity, phase=phase, replay=replay)
        return _git_object_oid("commit", content, self.repository.object_format)

    def _resolution_commit_content(
        self,
        identity: Mapping[str, object],
        *,
        phase: str,
        replay: Mapping[str, object] | None = None,
    ) -> bytes:
        body: dict[str, object] = {
            "schema": _RESOLUTION_SCHEMA,
            "phase": phase,
            "remote_id": self.remote_id,
            "queue_item_id": identity["queue_item_id"],
            "target_ref": identity["target_ref"],
            "queue_ref": identity["queue_ref"],
            "expected_target_oid": identity["expected_target_oid"],
            "candidate_oid": identity["candidate_oid"],
            "article_claim_key": identity["article_claim_key"],
            "article_claim_ref": identity["article_claim_ref"],
            "article_handoff_ref": identity["article_handoff_ref"],
            "claim_key": identity["claim_key"],
            "claim_ref": identity["claim_ref"],
            "target_resolution_ref": identity["target_resolution_ref"],
        }
        parents = [str(identity["candidate_oid"])]
        if replay is not None:
            body["replay"] = dict(replay)
            parents.append(str(replay["replay_candidate_oid"]))
        tree = _git_object_oid("tree", b"", self.repository.object_format)
        headers = [f"tree {tree}", *(f"parent {parent}" for parent in dict.fromkeys(parents))]
        headers.extend(
            (
                "author autoform <autoform@localhost> 0 +0000",
                "committer autoform <autoform@localhost> 0 +0000",
            )
        )
        return ("\n".join(headers) + "\n\n").encode("ascii") + _json_bytes(body) + b"\n"

    def _verify_replay_candidate(
        self,
        *,
        expected_target_oid: str,
        candidate_oid: str,
        replay_target_oid: str,
        replay_candidate_oid: str,
    ) -> tuple[str, str]:
        """Verify one direct replay commit by reproducing its tree in a private index."""

        self._verify_candidate(expected_target_oid, candidate_oid, use_transport=True)
        self._verify_transport_commit(replay_target_oid)
        self._verify_transport_commit(replay_candidate_oid)
        parents = self._local_git_text(
            ["rev-list", "--parents", "--max-count=1", replay_candidate_oid]
        ).split()
        if parents != [replay_candidate_oid, replay_target_oid]:
            raise MergeQueueError("replay candidate must have exactly the replay target as its single parent")

        if expected_target_oid in _ZERO_OIDS:
            source_tree = self._run_local_git_bytes(["mktree"], input_bytes=b"").stdout.decode("ascii").strip()
        else:
            source_tree = self._local_git_text(
                ["rev-parse", "--verify", f"{expected_target_oid}^{{tree}}"]
            ).strip()
        candidate_tree = self._local_git_text(
            ["rev-parse", "--verify", f"{candidate_oid}^{{tree}}"]
        ).strip()
        replay_tree = self._local_git_text(
            ["rev-parse", "--verify", f"{replay_candidate_oid}^{{tree}}"]
        ).strip()
        patch = self._run_local_git_bytes(
            [
                "-c",
                "core.quotePath=true",
                "-c",
                "diff.noprefix=false",
                "-c",
                "diff.mnemonicPrefix=false",
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--ignore-submodules=none",
                "--submodule=short",
                "--no-color",
                "--no-indent-heuristic",
                "--diff-algorithm=myers",
                "--unified=3",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                source_tree,
                candidate_tree,
                "--",
            ]
        ).stdout
        delta_sha256 = hashlib.sha256(patch).hexdigest()

        with tempfile.TemporaryDirectory(prefix=".replay-verify-", dir=self.state_root) as temporary:
            index_path = Path(temporary) / "index"
            environment = {"GIT_INDEX_FILE": str(index_path)}
            index_config = [
                "-c",
                "core.sparseCheckout=false",
                "-c",
                "core.sparseCheckoutCone=false",
                "-c",
                "index.sparse=false",
                "-c",
                "rerere.enabled=false",
                "-c",
                "rerere.autoupdate=false",
            ]
            self._run_local_git_bytes(
                [*index_config, "read-tree", replay_target_oid],
                environment=environment,
            )
            if patch:
                applied = self._run_local_git_bytes(
                    [
                        *index_config,
                        "-c",
                        "apply.ignoreWhitespace=no",
                        "apply",
                        "--cached",
                        "--3way",
                        "--binary",
                        "--whitespace=nowarn",
                        "-",
                    ],
                    input_bytes=patch,
                    environment=environment,
                    check=False,
                )
                if applied.returncode != 0:
                    raise MergeQueueError("replay candidate does not carry the exact original tree delta")
            reproduced_tree = self._run_local_git_bytes(
                [*index_config, "write-tree"],
                environment=environment,
            ).stdout.decode("ascii").strip()
        if reproduced_tree != replay_tree:
            raise MergeQueueError("replay candidate contains changes outside the exact original tree delta")
        return delta_sha256, replay_tree

    def _run_local_git_bytes(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        environment: Mapping[str, str] | None = None,
        check: bool = True,
        use_repository_alternate: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        self._verify_state()
        git_environment = _git_environment()
        if environment is not None:
            git_environment.update(environment)
        if use_repository_alternate:
            git_environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(
                self.repository.common_git_dir / "objects"
            )
        else:
            git_environment.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
        try:
            proc = subprocess.run(
                _git_command(args),
                cwd=self.transport_root,
                input=input_bytes,
                capture_output=True,
                timeout=120,
                env=git_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RepositoryError(f"Git replay verification failed: {error}") from error
        self._verify_state()
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()[:500]
            raise RepositoryError(f"Git replay verification failed: {detail}")
        return proc

    def _held_claim_fence(self, key: str) -> ClaimFence | None:
        method = getattr(self.claim_board, "held_claim_fence", None)
        if method is None:
            raise PublicationUncertain("claim board does not expose coherent ownership fences")
        value = method(key)
        if value is None:
            return None
        if not isinstance(value, ClaimFence) or value.key != key:
            raise PublicationUncertain("claim board returned an invalid ownership fence")
        expected_oid_length = 64 if self.repository.object_format == "sha256" else 40
        if len(value.oid) != expected_oid_length:
            raise PublicationUncertain("claim board returned an ownership fence for a different object format")
        return value

    def _assert_lease(self, key: str, heartbeat: Any) -> None:
        if heartbeat.lost.is_set() or not self.claim_board.holds(key):
            raise PublicationUncertain("publication lease ownership was lost")

    def _remote_oid(self, ref: str) -> str | None:
        return self._remote_oids((ref,))[ref]

    def _remote_oids(self, refs: Iterable[str]) -> dict[str, str | None]:
        requested = tuple(refs)
        if not requested:
            return {}
        if len(set(requested)) != len(requested):
            raise MergeQueueError("remote ref observation contains duplicate requests")
        observed: dict[str, str | None] = dict.fromkeys(requested)
        proc = self._remote_git(["ls-remote", self.remote_url, *requested])
        lines = [line for line in proc.stdout.splitlines() if line]
        returned: set[str] = set()
        for line in lines:
            oid, separator, observed_ref = line.partition("\t")
            if (
                not separator
                or observed_ref not in observed
                or observed_ref in returned
                or not _OID.fullmatch(oid)
            ):
                raise PublicationUncertain("remote returned invalid results for exact refs")
            observed[observed_ref] = oid
            returned.add(observed_ref)
        return observed

    def _atomic_queue_handoff(
        self,
        *,
        queue_ref: str,
        candidate: str,
        article_claim_ref: str,
        article_claim_oid: str,
        article_handoff_ref: str,
        resolution_ref: str,
        resolution_oid: str,
    ) -> bool:
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                "--atomic",
                f"--force-with-lease={queue_ref}:",
                f"--force-with-lease={article_handoff_ref}:",
                f"--force-with-lease={resolution_ref}:",
                f"--force-with-lease={article_claim_ref}:{article_claim_oid}",
                self.remote_url,
                f"{candidate}:{queue_ref}",
                f"{candidate}:{article_handoff_ref}",
                f"{resolution_oid}:{resolution_ref}",
                f":{article_claim_ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            return False
        raise PublicationUncertain(
            "remote article-claim handoff outcome is uncertain: "
            + _redact_remote(detail[:500], self.remote_url)
        )

    def _release_claim_fence(self, claim_ref: str, claim_oid: str) -> None:
        observed = self._remote_oid(claim_ref)
        if observed is None or observed != claim_oid:
            return
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                f"--force-with-lease={claim_ref}:{claim_oid}",
                self.remote_url,
                f":{claim_ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if any(marker in detail.casefold() for marker in _CAS_REJECTIONS):
            return
        raise MergeQueueError(f"exact claim cleanup failed: {_redact_remote(detail[:500], self.remote_url)}")

    def _secure_claim_cleanup(self, record: dict[str, object], fence: ClaimFence) -> None:
        """Prevent an expiring merge lease from becoming the last ownership barrier."""

        if record.get("status") in {"integrated", "aborted"}:
            return
        article = self._remote_oid(str(record["article_claim_ref"]))
        record["observed_article_claim_oid"] = article
        resolution = self._remote_oid(str(record["target_resolution_ref"]))
        record["observed_resolution_oid"] = resolution
        if resolution is not None:
            return
        queue = self._remote_oid(str(record["queue_ref"]))
        handoff = self._remote_oid(str(record["article_handoff_ref"]))
        target = self._remote_oid(str(record["target_ref"]))
        record["observed_queue_oid"] = queue
        record["observed_article_handoff_oid"] = handoff
        record["observed_target_oid"] = target
        intents = record.get("resolution_intents")
        intent = intents[-1] if isinstance(intents, list) and intents else None
        if isinstance(intent, dict):
            claim = self._remote_oid(str(record["claim_ref"]))
            shared_consumed = all(
                old is None or current != old
                for old, current in (
                    (intent.get("observed_queue_oid"), queue),
                    (intent.get("observed_article_handoff_oid"), handoff),
                    (intent.get("observed_claim_oid"), claim),
                    (intent.get("observed_resolution_oid"), resolution),
                )
            )
            integrated = False
            if target is not None:
                self._fetch_exact_ref(
                    str(record["target_ref"]),
                    target,
                    owner=str(record["queue_item_id"]),
                )
                candidate = str(intent["resolution_candidate_oid"])
                integrated = self._transport_has_commit(candidate) and self._transport_is_ancestor(
                    candidate, target
                )
            decision = intent.get("decision")
            if shared_consumed and (
                (decision == "abort" and not integrated) or (decision == "adopt" and integrated)
            ):
                return
        if (
            article == record.get("article_claim_oid")
            and queue is None
            and handoff is None
            and target == _expected_oid(str(record["expected_target_oid"]))
        ):
            return
        blocker = self._install_recovery_block(record, expected_claim=fence.oid)
        if blocker is not None:
            record["observed_claim_oid"] = blocker
            return
        resolution = self._remote_oid(str(record["target_resolution_ref"]))
        record["observed_resolution_oid"] = resolution
        if resolution is None:
            raise PublicationUncertain(
                "merge claim cleanup refused because no permanent ownership fence could be proven"
            )

    def _install_recovery_block(
        self,
        record: Mapping[str, object],
        *,
        expected_claim: str,
    ) -> str | None:
        resolution_oid = str(record["resolution_oid"])
        body = {
            "blocked_at": time.time(),
            "queue_item_id": str(record["queue_item_id"]),
            "resolution_oid": resolution_oid,
            "resource": str(record["claim_key"]),
            "schema": RECOVERY_BLOCK_SCHEMA,
            "target_resolution_ref": str(record["target_resolution_ref"]),
        }
        tree = _git_object_oid("tree", b"", self.repository.object_format)
        content = (
            f"tree {tree}\n"
            f"parent {resolution_oid}\n"
            "author autoform <autoform@localhost> 0 +0000\n"
            "committer autoform <autoform@localhost> 0 +0000\n\n"
        ).encode("ascii") + _json_bytes(body) + b"\n"
        block_oid = self._run_local_git_bytes(
            ["hash-object", "-t", "commit", "-w", "--stdin"], input_bytes=content
        ).stdout.decode("ascii").strip()
        claim_ref = str(record["claim_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                "--atomic",
                f"--force-with-lease={claim_ref}:{expected_claim}",
                f"--force-with-lease={resolution_ref}:",
                self.remote_url,
                f"{block_oid}:{claim_ref}",
                f"{resolution_oid}:{resolution_ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return block_oid
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if any(marker in detail.casefold() for marker in _CAS_REJECTIONS):
            return None
        raise PublicationUncertain(
            f"remote recovery-block outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _atomic_restore_barriers(
        self,
        record: Mapping[str, object],
        *,
        observation: Mapping[str, str | None],
        claim_oid: str,
    ) -> bool:
        queue_ref = str(record["queue_ref"])
        handoff_ref = str(record["article_handoff_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        article_ref = str(record["article_claim_ref"])
        claim_ref = str(record["claim_ref"])
        target_ref = str(record["target_ref"])
        candidate = str(record["candidate_oid"])
        resolution_oid = str(record["resolution_oid"])
        target = observation["target"]
        owner = str(record["queue_item_id"])
        self._fetch_exact_ref(claim_ref, claim_oid, owner=owner)
        if target is not None:
            self._fetch_exact_ref(target_ref, target, owner=owner)
        updates = [
            f"{candidate}:{queue_ref}",
            f"{candidate}:{handoff_ref}",
            f"{resolution_oid}:{resolution_ref}",
            f":{article_ref}",
            f"{claim_oid}:{claim_ref}",
        ]
        leases = [
            f"--force-with-lease={queue_ref}:{observation['queue'] or ''}",
            f"--force-with-lease={handoff_ref}:{observation['handoff'] or ''}",
            f"--force-with-lease={resolution_ref}:{observation['resolution'] or ''}",
            f"--force-with-lease={article_ref}:",
            f"--force-with-lease={claim_ref}:{claim_oid}",
            f"--force-with-lease={target_ref}:{target or ''}",
        ]
        updates.append(f"{target}:{target_ref}" if target is not None else f":{target_ref}")
        proc = self._remote_git(
            ["push", "--quiet", "--porcelain", "--atomic", *leases, self.remote_url, *updates],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            return False
        raise PublicationUncertain(
            f"remote barrier repair outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _ensure_permanent_resolution_fence(self, record: Mapping[str, object]) -> str | None:
        """Install or observe a non-expiring owner marker before a merge claim is released."""

        resolution_ref = str(record["target_resolution_ref"])
        expected = str(record["resolution_oid"])
        for _ in range(4):
            observation = self._observe_replay_refs(record)
            observed = observation["resolution"]
            if observed is not None:
                return observed
            if self._atomic_install_resolution_fence(record, observation=observation):
                verified = self._remote_oid(resolution_ref)
                if verified == expected:
                    return verified
        return self._remote_oid(resolution_ref)

    def _atomic_install_resolution_fence(
        self,
        record: Mapping[str, object],
        *,
        observation: Mapping[str, str | None],
    ) -> bool:
        resolution_ref = str(record["target_resolution_ref"])
        resolution_oid = str(record["resolution_oid"])
        refs = (
            (str(record["queue_ref"]), observation["queue"]),
            (str(record["article_handoff_ref"]), observation["handoff"]),
            (str(record["article_claim_ref"]), observation["article"]),
            (str(record["claim_ref"]), observation["claim"]),
            (str(record["target_ref"]), observation["target"]),
        )
        for ref, oid in refs:
            if oid is not None:
                self._fetch_exact_ref(
                    ref,
                    oid,
                    owner=str(record["queue_item_id"]),
                )
        leases = [f"--force-with-lease={resolution_ref}:"]
        updates = [f"{resolution_oid}:{resolution_ref}"]
        for ref, oid in refs:
            leases.append(f"--force-with-lease={ref}:{oid or ''}")
            updates.append(f"{oid}:{ref}" if oid is not None else f":{ref}")
        proc = self._remote_git(
            ["push", "--quiet", "--porcelain", "--atomic", *leases, self.remote_url, *updates],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            return False
        raise PublicationUncertain(
            f"remote resolution fencing outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _atomic_resolve_publication(
        self,
        record: Mapping[str, object],
        *,
        observation: Mapping[str, str | None],
        claim_oid: str,
    ) -> bool:
        queue_ref = str(record["queue_ref"])
        handoff_ref = str(record["article_handoff_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        article_ref = str(record["article_claim_ref"])
        claim_ref = str(record["claim_ref"])
        target_ref = str(record["target_ref"])
        resolution_oid = str(record["resolution_oid"])
        target = observation["target"]
        article = observation["article"]
        self._fetch_exact_ref(
            claim_ref,
            claim_oid,
            owner=str(record["queue_item_id"]),
        )
        updates = [
            f":{queue_ref}",
            f":{handoff_ref}",
            f":{resolution_ref}",
            f":{claim_ref}",
            f"{target}:{target_ref}" if target is not None else f":{target_ref}",
            f"{article}:{article_ref}" if article is not None else f":{article_ref}",
        ]
        leases = [
            f"--force-with-lease={queue_ref}:{observation['queue'] or ''}",
            f"--force-with-lease={handoff_ref}:{observation['handoff'] or ''}",
            f"--force-with-lease={resolution_ref}:{resolution_oid}",
            f"--force-with-lease={claim_ref}:{claim_oid}",
            f"--force-with-lease={target_ref}:{target or ''}",
            f"--force-with-lease={article_ref}:{article or ''}",
        ]
        proc = self._remote_git(
            ["push", "--quiet", "--porcelain", "--atomic", *leases, self.remote_url, *updates],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            return False
        raise PublicationUncertain(
            f"remote explicit resolution outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _atomic_target_push(
        self,
        *,
        target_ref: str,
        queue_ref: str,
        article_handoff_ref: str,
        resolution_ref: str,
        resolution_oid: str,
        claim_ref: str,
        claim_oid: str,
        expected_target: str | None,
        candidate: str,
    ) -> bool:
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                "--atomic",
                f"--force-with-lease={queue_ref}:{candidate}",
                f"--force-with-lease={article_handoff_ref}:{candidate}",
                f"--force-with-lease={resolution_ref}:{resolution_oid}",
                f"--force-with-lease={target_ref}:{expected_target or ''}",
                f"--force-with-lease={claim_ref}:{claim_oid}",
                self.remote_url,
                f":{queue_ref}",
                f":{article_handoff_ref}",
                f":{resolution_ref}",
                f"{candidate}:{target_ref}",
                f":{claim_ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            return False
        raise PublicationUncertain(
            f"remote atomic CAS push outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _pin_replay_resolution(
        self,
        record: Mapping[str, object],
        intent: Mapping[str, object],
    ) -> None:
        target_ref = str(record["target_ref"])
        queue_ref = str(record["queue_ref"])
        handoff_ref = str(record["article_handoff_ref"])
        article_ref = str(record["article_claim_ref"])
        claim_ref = str(intent["claim_ref"])
        resolution_ref = str(record["target_resolution_ref"])
        candidate = str(record["candidate_oid"])
        replay_target = str(intent["replay_target_oid"])
        claim_oid = str(intent["claim_oid"])
        prior_resolution = str(intent["prior_resolution_oid"])
        replay_resolution = str(intent["resolution_oid"])
        self._fetch_exact_ref(
            claim_ref,
            claim_oid,
            owner=str(record["queue_item_id"]),
        )
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                "--atomic",
                f"--force-with-lease={queue_ref}:{candidate}",
                f"--force-with-lease={handoff_ref}:{candidate}",
                f"--force-with-lease={article_ref}:",
                f"--force-with-lease={claim_ref}:{claim_oid}",
                f"--force-with-lease={target_ref}:{replay_target}",
                f"--force-with-lease={resolution_ref}:{prior_resolution}",
                self.remote_url,
                f"{candidate}:{queue_ref}",
                f"{candidate}:{handoff_ref}",
                f":{article_ref}",
                f"{claim_oid}:{claim_ref}",
                f"{replay_target}:{target_ref}",
                f"{replay_resolution}:{resolution_ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            raise MergeQueueBusy("stale replay resolution fence changed before it could be pinned")
        raise PublicationUncertain(
            f"remote replay pin outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _atomic_replay_push(self, intent: Mapping[str, object]) -> bool:
        target_ref = str(intent["target_ref"])
        queue_ref = str(intent["queue_ref"])
        handoff_ref = str(intent["article_handoff_ref"])
        claim_ref = str(intent["claim_ref"])
        old_candidate = str(intent["original_candidate_oid"])
        replay_target = str(intent["replay_target_oid"])
        replay_candidate = str(intent["replay_candidate_oid"])
        claim_oid = str(intent["claim_oid"])
        resolution_ref = str(intent["target_resolution_ref"])
        resolution_oid = str(intent["resolution_oid"])
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                "--atomic",
                f"--force-with-lease={queue_ref}:{old_candidate}",
                f"--force-with-lease={handoff_ref}:{old_candidate}",
                f"--force-with-lease={target_ref}:{replay_target}",
                f"--force-with-lease={claim_ref}:{claim_oid}",
                f"--force-with-lease={resolution_ref}:{resolution_oid}",
                self.remote_url,
                f":{queue_ref}",
                f":{handoff_ref}",
                f"{replay_candidate}:{target_ref}",
                f":{claim_ref}",
                f":{resolution_ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        folded = detail.casefold()
        if "does not support --atomic" in folded:
            raise MergeQueueError("publication remote does not support atomic pushes")
        if any(marker in folded for marker in _CAS_REJECTIONS):
            return False
        raise PublicationUncertain(
            f"remote atomic stale replay outcome is uncertain: {_redact_remote(detail[:500], self.remote_url)}"
        )

    def _remote_git(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._verify_state()
        command_args = args
        pass_fds: tuple[int, ...] = ()
        if self._remote_descriptor is not None:
            if not args or args[0] not in {"fetch", "ls-remote", "push"}:
                raise MergeQueueError("unsupported local publication transport operation")
            mode = "upload" if args[0] in {"fetch", "ls-remote"} else "receive"
            option = "--upload-pack" if mode == "upload" else "--receive-pack"
            helper = shlex.join(
                (
                    os.fspath(self._transport_python),
                    os.fspath(self._transport_helper),
                    mode,
                    str(self._remote_descriptor),
                )
            )
            command_args = ["." if item == self.remote_url else item for item in args]
            if command_args == args:
                raise MergeQueueError("local publication remote was not explicit")
            command_args.insert(1, f"{option}={helper}")
            pass_fds = (self._remote_descriptor,)
        environment = _git_environment()
        environment.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
        try:
            proc = subprocess.run(
                _git_command(command_args),
                cwd=self.transport_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            if args and args[0] == "push":
                raise PublicationUncertain(f"remote Git push outcome is uncertain: {error}") from error
            raise MergeQueueError(f"remote Git operation failed: {error}") from error
        try:
            self._verify_state()
        except RepositoryError as error:
            if args and args[0] == "push":
                raise PublicationUncertain(
                    f"remote Git push completed but local verification failed: {error}"
                ) from error
            raise
        if check and proc.returncode != 0:
            detail = _git_failure(f"git {args[0] if args else ''}", proc)
            raise MergeQueueError(_redact_remote(detail, self.remote_url))
        return proc

    def _initialize_transport(self) -> None:
        root_exists = self.transport_root.exists() or self.transport_root.is_symlink()
        marker_exists = self.transport_marker.exists() or self.transport_marker.is_symlink()
        staging_exists = self.transport_staging.exists() or self.transport_staging.is_symlink()
        intent_exists = self.transport_intent.exists() or self.transport_intent.is_symlink()
        if root_exists and marker_exists:
            if staging_exists:
                raise PublicationUncertain("Git transport has unexpected staging state")
            record = _read_json_file(self.transport_marker, label="Git transport marker")
            self._adopt_transport_record(record, path=self.transport_root)
            if intent_exists:
                intent = self._read_transport_intent()
                if intent.get("phase") != "ready" or intent.get("record") != record:
                    raise PublicationUncertain("Git transport completion intent is inconsistent")
                self.transport_intent.unlink()
                _fsync_directory(self.state_root)
            self._verify_transport()
            return
        if marker_exists:
            raise PublicationUncertain("Git transport marker exists without its repository")
        if root_exists and not intent_exists:
            raise PublicationUncertain("Git transport repository exists without a durable creation intent")
        if staging_exists and not intent_exists:
            raise PublicationUncertain("Git transport staging exists without a durable creation intent")

        if not intent_exists:
            intent: dict[str, object] = {**self._transport_intent_identity(), "phase": "planned"}
            _write_json_file(self.transport_intent, intent)
            _checkpoint("transport-intent-recorded")
        else:
            intent = self._read_transport_intent()

        phase = intent.get("phase")
        if phase == "ready":
            record = intent.get("record")
            if not isinstance(record, dict):
                raise PublicationUncertain("Git transport ready intent has no valid record")
            if root_exists:
                self._adopt_transport_record(record, path=self.transport_root)
            elif staging_exists:
                self._adopt_transport_record(record, path=self.transport_staging)
                os.rename(self.transport_staging, self.transport_root)
                _fsync_directory(self.state_root)
            else:
                raise PublicationUncertain("Git transport ready intent has no repository")
            _write_json_file(self.transport_marker, record)
            self.transport_intent.unlink()
            _fsync_directory(self.state_root)
            self._verify_transport()
            return

        if root_exists or phase not in {"planned", "initializing"}:
            raise PublicationUncertain("Git transport creation intent has an invalid phase")
        if not staging_exists:
            if phase != "planned":
                raise PublicationUncertain("Git transport staging directory disappeared during initialization")
            self.transport_staging.mkdir(mode=0o700)
            _checkpoint("transport-staging-created")
            staging_identity = _directory_identity(self.transport_staging)
            intent.update(
                {
                    "phase": "initializing",
                    "staging_device": staging_identity[0],
                    "staging_inode": staging_identity[1],
                }
            )
            _write_json_file(self.transport_intent, intent)
        else:
            if (
                self.transport_staging.is_symlink()
                or _canonical_existing_directory(self.transport_staging) != self.transport_staging
            ):
                raise PublicationUncertain("Git transport staging path was replaced")
            staging_identity = _directory_identity(self.transport_staging)
            if phase == "planned":
                try:
                    next(self.transport_staging.iterdir())
                except StopIteration:
                    intent.update(
                        {
                            "phase": "initializing",
                            "staging_device": staging_identity[0],
                            "staging_inode": staging_identity[1],
                        }
                    )
                    _write_json_file(self.transport_intent, intent)
                else:
                    raise PublicationUncertain("unowned Git transport staging is not empty")
            elif staging_identity != (intent.get("staging_device"), intent.get("staging_inode")):
                raise PublicationUncertain("Git transport staging directory was replaced")

        arguments = ["init", "--bare", "--quiet", "--template="]
        if self.repository.object_format == "sha256":
            arguments.append("--object-format=sha256")
        arguments.append(str(self.transport_staging))
        try:
            proc = subprocess.run(
                _git_command(arguments),
                cwd=self.state_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=_git_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublicationUncertain(f"Git transport initialization failed: {error}") from error
        if proc.returncode != 0:
            raise PublicationUncertain(_git_failure("git init --bare", proc))
        _write_bytes_file(
            self.transport_staging / "config",
            _transport_config(self.repository.object_format),
        )
        self._transport_identity = _directory_identity(self.transport_staging)
        self._transport_config_identity = _regular_file_identity(
            self.transport_staging / "config",
            label="Git transport config",
        )
        self._transport_head_identity = _regular_file_identity(
            self.transport_staging / "HEAD",
            label="Git transport HEAD",
        )
        record = {
            "schema": _TRANSPORT_SCHEMA,
            "repository_id": self.repository.repository_id,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            "object_format": self.repository.object_format,
            "path": str(self.transport_root),
            "device": self._transport_identity[0],
            "inode": self._transport_identity[1],
            "config_device": self._transport_config_identity[0],
            "config_inode": self._transport_config_identity[1],
            "config_sha256": self._transport_config_identity[2],
            "head_device": self._transport_head_identity[0],
            "head_inode": self._transport_head_identity[1],
            "head_sha256": self._transport_head_identity[2],
        }
        intent["phase"] = "ready"
        intent["record"] = record
        _write_json_file(self.transport_intent, intent)
        _checkpoint("transport-staging-recorded")
        os.rename(self.transport_staging, self.transport_root)
        _fsync_directory(self.state_root)
        _write_json_file(self.transport_marker, record)
        _checkpoint("transport-marker-recorded")
        self.transport_intent.unlink()
        _fsync_directory(self.state_root)
        self._verify_transport()

    def _transport_intent_identity(self) -> dict[str, object]:
        return {
            "schema": _TRANSPORT_INTENT_SCHEMA,
            "repository_id": self.repository.repository_id,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            "object_format": self.repository.object_format,
            "path": str(self.transport_root),
            "staging_path": str(self.transport_staging),
        }

    def _read_transport_intent(self) -> dict[str, object]:
        intent = _read_json_file(self.transport_intent, label="Git transport creation intent")
        for key, value in self._transport_intent_identity().items():
            if key not in intent or intent[key] != value:
                raise PublicationUncertain(f"Git transport creation intent has a different {key}")
        return intent

    def _adopt_transport_record(self, record: Mapping[str, object], *, path: Path) -> None:
        expected = {
            "schema": _TRANSPORT_SCHEMA,
            "repository_id": self.repository.repository_id,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            "object_format": self.repository.object_format,
            "path": str(self.transport_root),
        }
        for key, value in expected.items():
            if key not in record or record[key] != value:
                if key in {"remote_kind", "remote_device", "remote_inode"}:
                    raise PublicationUncertain("local publication remote was replaced across restart")
                raise PublicationUncertain(f"Git transport marker has a different {key}")
        if record["remote_kind"] == "local" and (
            not _is_integer(record["remote_device"]) or not _is_integer(record["remote_inode"])
        ):
            raise PublicationUncertain("Git transport marker has an invalid local remote identity")
        for key in ("device", "inode", "config_device", "config_inode", "head_device", "head_inode"):
            if not _is_integer(record.get(key)):
                raise PublicationUncertain(f"Git transport marker has an invalid {key}")
        for key in ("config_sha256", "head_sha256"):
            value = record.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PublicationUncertain(f"Git transport marker has an invalid {key}")
        if _canonical_existing_directory(path) != path or _directory_identity(path) != (
            record["device"],
            record["inode"],
        ):
            raise PublicationUncertain("Git transport directory was replaced")
        self._transport_identity = (int(record["device"]), int(record["inode"]))
        self._transport_config_identity = (
            int(record["config_device"]),
            int(record["config_inode"]),
            str(record["config_sha256"]),
        )
        self._transport_head_identity = (
            int(record["head_device"]),
            int(record["head_inode"]),
            str(record["head_sha256"]),
        )
        if _regular_file_identity(path / "config", label="Git transport config") != self._transport_config_identity:
            raise PublicationUncertain("Git transport config was replaced")
        if _regular_file_identity(path / "HEAD", label="Git transport HEAD") != self._transport_head_identity:
            raise PublicationUncertain("Git transport HEAD was replaced")

    def _verify_transport(self) -> None:
        if _canonical_existing_directory(self.transport_root) != self.transport_root:
            raise PublicationUncertain("Git transport directory path changed")
        if _directory_identity(self.transport_root) != self._transport_identity:
            raise PublicationUncertain("Git transport directory was replaced")
        if (
            _regular_file_identity(
                self.transport_root / "config",
                label="Git transport config",
            )
            != self._transport_config_identity
        ):
            raise PublicationUncertain("Git transport config was replaced")
        if (
            _regular_file_identity(
                self.transport_root / "HEAD",
                label="Git transport HEAD",
            )
            != self._transport_head_identity
        ):
            raise PublicationUncertain("Git transport HEAD was replaced")

    def _verify_remote(self) -> None:
        if self._remote_path is None:
            return
        if _canonical_existing_directory(self._remote_path) != self._remote_path:
            raise PublicationUncertain("local publication remote path changed")
        if _directory_identity(self._remote_path) != self._remote_identity:
            raise PublicationUncertain("local publication remote was replaced")
        if self._remote_descriptor is None:
            raise PublicationUncertain("local publication remote is not pinned")
        info = os.fstat(self._remote_descriptor)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._remote_identity:
            raise PublicationUncertain("local publication remote descriptor changed")
        if _executable_identity(self._transport_python) != self._transport_python_identity:
            raise PublicationUncertain("local transport Python executable was replaced")
        if (
            _regular_file_identity(self._transport_helper, label="local transport helper")
            != self._transport_helper_identity
        ):
            raise PublicationUncertain("local transport helper was replaced")

    def _verify_state(self) -> None:
        for path, expected, label in (
            (self.state_root, self._state_identity, "merge-queue state root"),
            (self.publication_root, self._publication_root_identity, "publication state root"),
            (self.lock_root, self._lock_root_identity, "publication lock root"),
        ):
            if _canonical_existing_directory(path) != path or _directory_identity(path) != expected:
                raise PublicationUncertain(f"{label} was replaced")
        self.repository._verify_repository()
        self._verify_remote()
        self._verify_transport()


def _publication_receipt(record: Mapping[str, object]) -> PublicationReceipt:
    history = record.get("history")
    replay_intents = record.get("replay_intents")
    replay_events = record.get("replay_events")
    resolution_intents = record.get("resolution_intents")
    if (
        not isinstance(history, list)
        or not isinstance(replay_intents, list)
        or not isinstance(replay_events, list)
        or not isinstance(resolution_intents, list)
    ):
        raise PublicationUncertain("publication journal history is malformed")
    return PublicationReceipt(
        queue_item_id=str(record["queue_item_id"]),
        remote_id=str(record["remote_id"]),
        remote_kind=str(record["remote_kind"]),
        remote_device=(int(record["remote_device"]) if record.get("remote_device") is not None else None),
        remote_inode=(int(record["remote_inode"]) if record.get("remote_inode") is not None else None),
        target_ref=str(record["target_ref"]),
        queue_ref=str(record["queue_ref"]),
        expected_target_oid=str(record["expected_target_oid"]),
        candidate_oid=str(record["candidate_oid"]),
        status=str(record["status"]),
        observed_target_oid=(
            str(record["observed_target_oid"]) if record.get("observed_target_oid") is not None else None
        ),
        observed_queue_oid=(
            str(record["observed_queue_oid"]) if record.get("observed_queue_oid") is not None else None
        ),
        article_claim_key=str(record["article_claim_key"]),
        article_claim_ref=str(record["article_claim_ref"]),
        article_claim_oid=str(record["article_claim_oid"]),
        article_claim_lease_id=str(record["article_claim_lease_id"]),
        observed_article_claim_oid=(
            str(record["observed_article_claim_oid"])
            if record.get("observed_article_claim_oid") is not None
            else None
        ),
        article_handoff_ref=str(record["article_handoff_ref"]),
        observed_article_handoff_oid=(
            str(record["observed_article_handoff_oid"])
            if record.get("observed_article_handoff_oid") is not None
            else None
        ),
        target_resolution_ref=str(record["target_resolution_ref"]),
        initial_resolution_oid=str(record["initial_resolution_oid"]),
        resolution_oid=str(record["resolution_oid"]),
        observed_resolution_oid=(
            str(record["observed_resolution_oid"])
            if record.get("observed_resolution_oid") is not None
            else None
        ),
        claim_key=str(record["claim_key"]),
        claim_ref=str(record["claim_ref"]),
        claim_oid=(str(record["claim_oid"]) if record.get("claim_oid") is not None else None),
        observed_claim_oid=(
            str(record["observed_claim_oid"]) if record.get("observed_claim_oid") is not None else None
        ),
        claim_lease_id=(str(record["claim_lease_id"]) if record.get("claim_lease_id") is not None else None),
        detail=str(record.get("detail", "")),
        history=tuple(dict(item) for item in history if isinstance(item, dict)),
        replay_intents=tuple(dict(item) for item in replay_intents if isinstance(item, dict)),
        replay_events=tuple(dict(item) for item in replay_events if isinstance(item, dict)),
        resolution_intents=tuple(
            dict(item) for item in resolution_intents if isinstance(item, dict)
        ),
    )


def _transition_journal(
    journal: Path,
    record: dict[str, object],
    status: str,
    detail: object,
) -> dict[str, object]:
    if not _journal_directory_matches(journal, record):
        raise PublicationUncertain("publication directory changed before its journal update")
    now = time.time_ns()
    history = record.setdefault("history", [])
    if not isinstance(history, list):
        raise PublicationUncertain("publication journal history is malformed")
    detail_text = _bounded_publication_detail(detail)
    entry = {"status": status, "detail": detail_text, "created_ns": now}
    terminal = status in {"integrated", "aborted", "uncertain"}
    if not terminal and len(history) >= _MAX_PUBLICATION_HISTORY - 1:
        status = "uncertain"
        detail_text = "publication history limit reached; manual reconciliation is required"
        entry = {"status": status, "detail": detail_text, "created_ns": now}
        terminal = True
    if len(history) >= _MAX_PUBLICATION_HISTORY:
        raise PublicationUncertain("publication journal has no reserved terminal history slot")
    if not history or history[-1] != entry:
        history.append(entry)
    record["status"] = status
    record["detail"] = detail_text
    record["updated_ns"] = now
    if not terminal and _publication_journal_size(record) > (
        _MAX_PUBLICATION_BYTES - _PUBLICATION_TERMINAL_RESERVE_BYTES
    ):
        history.pop()
        status = "uncertain"
        detail_text = "publication evidence byte limit reached; manual reconciliation is required"
        history.append({"status": status, "detail": detail_text, "created_ns": now})
        record["status"] = status
        record["detail"] = detail_text
        terminal = True
    _write_publication_journal(journal, record, reserve_terminal=not terminal)
    _checkpoint(f"publication-recorded:{status}")
    return record


def _bounded_publication_detail(detail: object) -> str:
    value = str(detail)
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_PUBLICATION_DETAIL_BYTES:
        return value
    suffix = " [truncated]"
    budget = _MAX_PUBLICATION_DETAIL_BYTES - len(suffix.encode())
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def _publication_journal_size(value: Mapping[str, object]) -> int:
    return len(_json_bytes(value)) + 1


def _write_publication_journal(
    path: Path,
    value: Mapping[str, object],
    *,
    reserve_terminal: bool | None = None,
) -> None:
    if reserve_terminal is None:
        reserve_terminal = value.get("status") not in {"integrated", "aborted", "uncertain"}
    maximum = _MAX_PUBLICATION_BYTES
    if reserve_terminal:
        maximum -= _PUBLICATION_TERMINAL_RESERVE_BYTES
    content = _json_bytes(value) + b"\n"
    if len(content) > maximum:
        raise PublicationUncertain("publication journal exceeds its serialized evidence bound")
    _write_bytes_file(path, content)


def _merge_claim_key(target_ref: str) -> str:
    digest = hashlib.sha256(target_ref.encode()).hexdigest()[:32]
    return f"merge/{digest}"


def _journal_directory_matches(journal: Path, record: Mapping[str, object]) -> bool:
    try:
        canonical = _canonical_existing_directory(journal.parent)
        identity = _directory_identity(journal.parent)
    except RepositoryError:
        return False
    return canonical == journal.parent and identity == (
        record.get("publication_device"),
        record.get("publication_inode"),
    )


def _expected_oid(value: str) -> str | None:
    return None if value in _ZERO_OIDS else value


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_name(label: str, value: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise RepositoryError(f"{label} is not a safe portable identifier: {value!r}")
    if value.startswith(".") or value.endswith(".") or value.endswith(".lock"):
        raise RepositoryError(f"{label} is not a safe portable identifier: {value!r}")


def _validate_oid(value: str) -> None:
    if not isinstance(value, str) or not _OID.fullmatch(value) or value in _ZERO_OIDS:
        raise RepositoryError(f"invalid Git commit object id: {value!r}")


def _validate_expected_oid(value: str) -> None:
    if not isinstance(value, str) or (not _OID.fullmatch(value) and value not in _ZERO_OIDS):
        raise RepositoryError(f"invalid expected Git object id: {value!r}")


def _validate_full_ref(value: str, *, prefix: str) -> None:
    if not isinstance(value, str) or not value.startswith(prefix) or ".." in value or "@{" in value:
        raise RepositoryError(f"invalid Git ref: {value!r}")
    if any(part in {"", ".", ".."} or part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        raise RepositoryError(f"invalid Git ref: {value!r}")
    try:
        proc = subprocess.run(
            ["git", "check-ref-format", value],
            capture_output=True,
            text=True,
            timeout=10,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RepositoryError(f"Git ref validation failed: {error}") from error
    if proc.returncode != 0:
        raise RepositoryError(f"invalid Git ref: {value!r}")


def _normalize_remote(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    if not raw or any(ord(character) < 32 or ord(character) == 127 for character in raw) or raw.startswith("-"):
        raise RepositoryError("remote URL is invalid")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", raw):
        raise RepositoryError("Git external remote helpers are not supported")
    initial = urlsplit(raw)
    if "://" in raw and initial.scheme.casefold() not in {"file", "git", "http", "https", "ssh"}:
        raise RepositoryError(f"unsupported Git remote URL scheme: {initial.scheme}")
    normalizer = getattr(claims_module, "normalize_claim_repository", None)
    if normalizer is not None:
        try:
            raw = os.fspath(normalizer(raw))
        except (OSError, TypeError, ValueError) as error:
            raise RepositoryError(f"remote URL is invalid: {error}") from error
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() == "file":
        if parsed.query or parsed.fragment or parsed.netloc.casefold() not in {"", "localhost"}:
            raise RepositoryError("file remote must identify an absolute local repository")
        local = Path(unquote(parsed.path))
        if not local.is_absolute():
            raise RepositoryError("file remote must identify an absolute local repository")
        return str(_existing_real_directory(local, label="local publication remote"))
    if not _claim_repository_is_remote(raw):
        path = _existing_real_directory(raw, label="local publication remote")
        return str(path)
    return raw


def _remote_is_local(value: str) -> bool:
    return not _claim_repository_is_remote(value)


def _claim_repository_is_remote(value: str) -> bool:
    detector = getattr(claims_module, "claim_repository_is_remote", None)
    if detector is not None:
        try:
            result = detector(value)
        except (OSError, TypeError, ValueError) as error:
            raise RepositoryError(f"remote URL is invalid: {error}") from error
        if not isinstance(result, bool):
            raise RepositoryError("claim repository remote detector returned a non-boolean result")
        return result
    if _WINDOWS_DRIVE.match(value):
        return False
    return "://" in value or bool(_SCP_REMOTE.fullmatch(value))


def _open_directory(path: Path, identity: tuple[int, int], *, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise RepositoryError(f"{label} cannot be pinned safely") from error
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        os.close(descriptor)
        raise RepositoryError(f"{label} changed while it was being pinned")
    return descriptor


def _prepare_private_root(value: str | Path) -> Path:
    path = _absolute_path(value)
    missing: list[Path] = []
    cursor = path
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    if cursor.is_symlink() or _canonical_existing_directory(cursor) != cursor:
        raise RepositoryError(f"state path traverses a symbolic link: {path}")
    for component in reversed(missing):
        try:
            component.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if component.is_symlink() or _canonical_existing_directory(component) != component:
            raise RepositoryError(f"state path was substituted while being created: {path}")
    if path.is_symlink() or _canonical_existing_directory(path) != path:
        raise RepositoryError(f"state path traverses a symbolic link: {path}")
    return path


def _absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _existing_real_directory(value: str | Path, *, label: str) -> Path:
    path = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    try:
        canonical = path.resolve(strict=True)
        info = path.lstat()
    except (OSError, RuntimeError) as error:
        raise RepositoryError(f"{label} cannot be resolved safely") from error
    if canonical != path or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RepositoryError(f"{label} must be a real canonical directory")
    return path


def _existing_executable(value: str | Path) -> Path:
    try:
        path = Path(value).resolve(strict=True)
        info = path.lstat()
    except (OSError, RuntimeError) as error:
        raise RepositoryError("Python executable cannot be resolved safely") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or not os.access(path, os.X_OK):
        raise RepositoryError("Python executable must resolve to an executable regular file")
    return path


def _executable_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RepositoryError("Python executable cannot be inspected safely") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or not os.access(path, os.X_OK):
        raise RepositoryError("Python executable is no longer an executable regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _canonical_existing_directory(value: str | Path) -> Path:
    return _existing_real_directory(value, label="directory")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise RepositoryError(f"state directory cannot be inspected: {path}") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RepositoryError(f"state path is not a real directory: {path}")


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RepositoryError(f"directory cannot be inspected: {path}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise RepositoryError(f"path is not a real directory: {path}")
    return info.st_dev, info.st_ino


def _coordinator_git_entry_identity(tree: Path) -> tuple[str, int, int, str | None]:
    path = tree / ".git"
    try:
        info = path.lstat()
    except OSError as error:
        raise RepositoryError("coordinator checkout .git entry cannot be inspected safely") from error
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        return "directory", info.st_dev, info.st_ino, None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024:
        raise RepositoryError("coordinator checkout .git entry must be a private file or real directory")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RepositoryError("coordinator checkout .git entry cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
            raise RepositoryError("coordinator checkout .git entry changed while being opened")
        content = os.read(descriptor, opened.st_size + 1)
    finally:
        os.close(descriptor)
    if len(content) != opened.st_size:
        raise RepositoryError("coordinator checkout .git entry changed while being inspected")
    return "file", opened.st_dev, opened.st_ino, hashlib.sha256(content).hexdigest()


def _safe_git_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and all(part not in {"", ".", ".."} for part in path.split("/"))


def _safe_candidate_path(path: str) -> bool:
    if not isinstance(path, str) or not _safe_git_path(path) or "\\" in path or "\0" in path:
        return False
    if any(part.casefold() == ".git" for part in path.split("/")):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validate_candidate_paths(paths: Set[str]) -> tuple[str, ...]:
    if not isinstance(paths, Set) or isinstance(paths, (str, bytes)):
        raise RepositoryError("allowed_paths must be an explicit finite set of repository-relative strings")
    values: list[str] = []
    for path in paths:
        if not _safe_candidate_path(path):
            raise RepositoryError(f"candidate path is unsafe, reserved, or not repository-relative: {path!r}")
        values.append(path)
    values.sort(key=lambda value: value.encode("utf-8"))
    selected = frozenset(values)
    for path in values:
        parts = path.split("/")
        if any("/".join(parts[:index]) in selected for index in range(1, len(parts))):
            raise RepositoryError(f"candidate allowed paths overlap as file and directory: {path}")
    return tuple(values)


def _validate_candidate_message(message: str) -> bytes:
    if not isinstance(message, str) or not message or "\0" in message:
        raise RepositoryError("candidate message must be a nonempty NUL-free string")
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RepositoryError("candidate message must be valid UTF-8") from error
    if len(encoded) > 1024 * 1024:
        raise RepositoryError("candidate message exceeds the 1 MiB safety limit")
    return encoded


def _validate_candidate_identity(author_name: str, author_email: str) -> None:
    if not isinstance(author_name, str) or not author_name or author_name != author_name.strip():
        raise RepositoryError("candidate author name must be a nonempty trimmed string")
    if not isinstance(author_email, str) or not author_email or author_email != author_email.strip():
        raise RepositoryError("candidate author email must be a nonempty trimmed string")
    if len(author_name.encode("utf-8", errors="ignore")) > 256 or any(
        character in "<>" or ord(character) < 32 or ord(character) == 127 for character in author_name
    ):
        raise RepositoryError("candidate author name contains a forbidden commit-header character")
    if (
        len(author_email.encode("utf-8", errors="ignore")) > 256
        or any(
            character in "<>" or character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in author_email
        )
        or "@" not in author_email
    ):
        raise RepositoryError("candidate author email is not safe for a commit header")
    try:
        author_name.encode("utf-8")
        author_email.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RepositoryError("candidate author identity must be valid UTF-8") from error


def _git_blob_oid(content: bytes, object_format: str) -> str:
    return _git_object_oid("blob", content, object_format)


def _verify_candidate_blob_batch_output(
    stream: BinaryIO,
    oids: tuple[str, ...],
    object_format: str,
    *,
    progress: Callable[[], None] | None = None,
) -> None:
    _verify_candidate_object_batch_output(
        stream,
        tuple((oid, "blob") for oid in oids),
        object_format,
        label="candidate tree closure",
        progress=progress,
    )


def _verify_candidate_object_batch_output(
    stream: BinaryIO,
    objects: tuple[tuple[str, str], ...],
    object_format: str,
    *,
    label: str,
    progress: Callable[[], None] | None = None,
    expected_contents: tuple[bytes, ...] | None = None,
    require_eof: bool = True,
) -> tuple[str | None, ...]:
    if expected_contents is not None and len(expected_contents) != len(objects):
        raise CandidateUncertain(f"{label} has an invalid expected object inventory")
    tree_oids: list[str | None] = []
    for object_index, (oid, object_type) in enumerate(objects):
        if object_type not in {"blob", "tree", "commit"}:
            raise CandidateUncertain(f"{label} requested an unsupported object type")
        header = stream.readline(_CANDIDATE_BATCH_HEADER_BYTES + 1)
        if header and progress is not None:
            progress()
        if len(header) > _CANDIDATE_BATCH_HEADER_BYTES or not header.endswith(b"\n"):
            raise CandidateUncertain(f"{label} is incomplete: truncated batch header")
        fields = header[:-1].split(b" ")
        if len(fields) != 3 or fields[0] != oid.encode("ascii") or fields[1] != object_type.encode("ascii"):
            raise CandidateUncertain(f"{label} is incomplete: invalid batch header")
        try:
            size = int(fields[2])
        except ValueError as error:
            raise CandidateUncertain(f"{label} is incomplete: invalid {object_type} size") from error
        if size < 0 or fields[2] != str(size).encode("ascii"):
            raise CandidateUncertain(f"{label} is incomplete: invalid {object_type} size")

        digest = hashlib.new(object_format)
        digest.update(f"{object_type} {size}\0".encode("ascii"))
        expected_content = None if expected_contents is None else expected_contents[object_index]
        if expected_content is not None and len(expected_content) != size:
            raise CandidateUncertain(f"{label} has unexpected {object_type} content")
        expected_offset = 0
        first_line = bytearray() if object_type == "commit" else None
        first_line_complete = False
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, _CANDIDATE_BLOB_CHUNK_BYTES))
            if not chunk:
                raise CandidateUncertain(f"{label} is incomplete: truncated {object_type} content")
            if progress is not None:
                progress()
            digest.update(chunk)
            if expected_content is not None:
                if chunk != expected_content[expected_offset : expected_offset + len(chunk)]:
                    raise CandidateUncertain(f"{label} has unexpected {object_type} content")
                expected_offset += len(chunk)
            if first_line is not None and not first_line_complete:
                prefix, separator, _ = chunk.partition(b"\n")
                first_line.extend(prefix)
                if len(first_line) > _CANDIDATE_BATCH_HEADER_BYTES:
                    raise CandidateUncertain(f"{label} has an invalid commit tree header")
                first_line_complete = bool(separator)
            remaining -= len(chunk)
        delimiter = stream.read(1)
        if not delimiter:
            raise CandidateUncertain(f"{label} is incomplete: truncated {object_type} content")
        if progress is not None:
            progress()
        if delimiter != b"\n":
            raise CandidateUncertain(f"{label} is incomplete: invalid {object_type} delimiter")
        if digest.hexdigest() != oid:
            raise CandidateUncertain(f"{label} is incomplete: {object_type} identity mismatch")
        tree_oid: str | None = None
        if first_line is not None:
            prefix, separator, encoded_oid = bytes(first_line).partition(b" ")
            try:
                tree_oid = encoded_oid.decode("ascii")
            except UnicodeDecodeError as error:
                raise CandidateUncertain(f"{label} has an invalid commit tree header") from error
            if not first_line_complete or prefix != b"tree" or not separator:
                raise CandidateUncertain(f"{label} has an invalid commit tree header")
            try:
                _validate_oid(tree_oid)
            except RepositoryError as error:
                raise CandidateUncertain(f"{label} has an invalid commit tree header") from error
            if len(tree_oid) != hashlib.new(object_format).digest_size * 2:
                raise CandidateUncertain(f"{label} has an invalid commit tree header")
        tree_oids.append(tree_oid)
    if require_eof and stream.read(1):
        raise CandidateUncertain(f"{label} is incomplete: trailing batch output")
    return tuple(tree_oids)


def _git_object_oid(object_type: str, content: bytes, object_format: str) -> str:
    if object_type not in {"blob", "tree", "commit"}:
        raise RepositoryError(f"unsupported Git object type: {object_type}")
    if object_format not in {"sha1", "sha256"}:
        raise RepositoryError(f"unsupported Git object format: {object_format}")
    digest = hashlib.new(object_format)
    digest.update(f"{object_type} {len(content)}\0".encode())
    digest.update(content)
    return digest.hexdigest()


def _candidate_commit_prefix(tree_oid: str, base_oid: str, author_name: str, author_email: str) -> bytes:
    _validate_oid(tree_oid)
    _validate_oid(base_oid)
    _validate_candidate_identity(author_name, author_email)
    identity = f"{author_name} <{author_email}> 0 +0000"
    return f"tree {tree_oid}\nparent {base_oid}\nauthor {identity}\ncommitter {identity}\n\n".encode("utf-8")


def _candidate_commit_content(
    tree_oid: str,
    base_oid: str,
    author_name: str,
    author_email: str,
    message: bytes,
) -> bytes:
    return _candidate_commit_prefix(tree_oid, base_oid, author_name, author_email) + message


def _snapshot_regular_tree(
    tree: Path,
    object_format: str,
    *,
    retained_paths: frozenset[str],
    required_paths: frozenset[str],
    hash_files: bool = True,
) -> tuple[
    dict[str, tuple[str, str]],
    dict[str, bytes],
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
]:
    files: dict[str, tuple[str, str]] = {}
    contents: dict[str, bytes] = {}
    identities: dict[str, tuple[int, ...]] = {}
    directories: dict[str, tuple[int, ...]] = {}
    retained_bytes = 0
    required_directories = {
        "/".join(parts[:index])
        for path in required_paths
        for parts in (path.split("/"),)
        for index in range(1, len(parts))
    }
    directory_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW

    def visit(directory_fd: int, relative_directory: str) -> None:
        nonlocal retained_bytes
        try:
            before = os.fstat(directory_fd)
            with os.scandir(directory_fd) as iterator:
                entries = list(iterator)
            listed = os.fstat(directory_fd)
        except OSError as error:
            label = relative_directory or "."
            raise CandidateUncertain(f"candidate directory cannot be inspected safely: {label}") from error
        before_identity = _candidate_stat_identity(before)
        if not stat.S_ISDIR(before.st_mode) or before_identity != _candidate_stat_identity(listed):
            label = relative_directory or "."
            raise CandidateUncertain(f"candidate directory changed while being inspected: {label}")
        if relative_directory:
            directories[relative_directory] = before_identity[:3]
        _checkpoint(f"candidate-directory-listed:{relative_directory or '.'}")
        for entry in sorted(entries, key=lambda item: item.name.encode("utf-8", errors="surrogateescape")):
            relative = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
            if relative == ".git":
                continue
            if not _safe_candidate_path(relative):
                raise CandidateUncertain(f"candidate contains an unsafe or reserved path: {relative!r}")
            try:
                info = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise CandidateUncertain(f"candidate path cannot be inspected safely: {relative}") from error
            generated = bool(GENERATED_DIRECTORY_NAMES.intersection(relative.split("/")))
            required = relative in required_paths or relative in required_directories
            if generated and not required:
                ordinary_directory = stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                ordinary_file = stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_nlink == 1
                if not ordinary_directory and not ordinary_file:
                    kind = "symbolic link" if stat.S_ISLNK(info.st_mode) else "special or shared file"
                    raise CandidateUncertain(f"candidate generated output is not ordinary: {relative} ({kind})")
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                try:
                    child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                except OSError as error:
                    raise CandidateUncertain(f"candidate directory cannot be opened safely: {relative}") from error
                try:
                    if _candidate_stat_identity(os.fstat(child_fd)) != _candidate_stat_identity(info):
                        raise CandidateUncertain(f"candidate directory changed while being opened: {relative}")
                    visit(child_fd, relative)
                    current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                    if _candidate_stat_identity(current) != _candidate_stat_identity(info):
                        raise CandidateUncertain(f"candidate directory was replaced after being read: {relative}")
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                kind = "symbolic link" if stat.S_ISLNK(info.st_mode) else "special file"
                raise CandidateUncertain(f"candidate commits reject {kind}: {relative}")
            retain = relative in retained_paths
            if retain and info.st_size > _MAX_CANDIDATE_BLOB_BYTES:
                limit_mib = _MAX_CANDIDATE_BLOB_BYTES // (1024 * 1024)
                raise CandidateUncertain(f"candidate allowed file exceeds the {limit_mib} MiB safety limit: {relative}")
            if retain and retained_bytes + info.st_size > _MAX_CANDIDATE_TOTAL_BLOB_BYTES:
                limit_mib = _MAX_CANDIDATE_TOTAL_BLOB_BYTES // (1024 * 1024)
                raise CandidateUncertain(f"candidate allowed files exceed the {limit_mib} MiB aggregate safety limit")
            if hash_files:
                oid, content, identity = _hash_candidate_regular_file_at(
                    directory_fd,
                    entry.name,
                    info,
                    relative,
                    object_format=object_format,
                    retain=retain,
                )
            else:
                if info.st_nlink != 1:
                    raise CandidateUncertain(f"candidate path is not a private regular file: {relative}")
                identity = _candidate_stat_identity(info)
                try:
                    current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise CandidateUncertain(f"candidate path cannot be rechecked safely: {relative}") from error
                if _candidate_stat_identity(current) != identity:
                    raise CandidateUncertain(f"candidate regular file changed while being inspected: {relative}")
                oid = ""
                content = None
            mode = "100755" if info.st_mode & 0o111 else "100644"
            files[relative] = (mode, oid)
            if content is not None:
                contents[relative] = content
                retained_bytes += len(content)
            identities[relative] = identity
        try:
            finished = os.fstat(directory_fd)
        except OSError as error:
            label = relative_directory or "."
            raise CandidateUncertain(f"candidate directory cannot be rechecked safely: {label}") from error
        if _candidate_stat_identity(finished) != before_identity:
            label = relative_directory or "."
            raise CandidateUncertain(f"candidate directory changed while being read: {label}")

    try:
        root_info = tree.stat(follow_symlinks=False)
        root_fd = os.open(tree, directory_flags)
    except OSError as error:
        raise CandidateUncertain("candidate worktree root cannot be opened safely") from error
    try:
        root_identity = _candidate_stat_identity(root_info)
        if not stat.S_ISDIR(root_info.st_mode) or _candidate_stat_identity(os.fstat(root_fd)) != root_identity:
            raise CandidateUncertain("candidate worktree root changed while being opened")
        visit(root_fd, "")
    finally:
        os.close(root_fd)
    try:
        current_root = tree.stat(follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain("candidate worktree root disappeared after inspection") from error
    if _candidate_stat_identity(current_root) != root_identity:
        raise CandidateUncertain("candidate worktree root was replaced after inspection")

    expected_directories: set[str] = set()
    for path in files:
        parts = path.split("/")
        expected_directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    foreign_directories = sorted(set(directories) - expected_directories)
    if foreign_directories:
        raise CandidateUncertain(f"candidate contains foreign directory output: {foreign_directories[0]}")
    return files, contents, identities, directories


def _hash_candidate_regular_file_at(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    relative: str,
    *,
    object_format: str,
    retain: bool,
) -> tuple[str, bytes | None, tuple[int, ...]]:
    if not stat.S_ISREG(expected.st_mode) or stat.S_ISLNK(expected.st_mode) or expected.st_nlink != 1:
        raise CandidateUncertain(f"candidate path is not a private regular file: {relative}")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise CandidateUncertain(f"candidate regular file cannot be opened safely: {relative}") from error
    try:
        opened = os.fstat(descriptor)
        expected_identity = _candidate_stat_identity(expected)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _candidate_stat_identity(opened) != expected_identity
        ):
            raise CandidateUncertain(f"candidate regular file changed while being opened: {relative}")
        digest = hashlib.new(object_format)
        digest.update(f"blob {opened.st_size}\0".encode("ascii"))
        content = bytearray() if retain else None
        observed_size = 0
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, _CANDIDATE_BLOB_CHUNK_BYTES))
            if not chunk:
                break
            digest.update(chunk)
            observed_size += len(chunk)
            remaining -= len(chunk)
            if content is not None:
                content.extend(chunk)
            _checkpoint(f"candidate-file-chunk:{relative}")
        extra = os.read(descriptor, 1) if not remaining else b""
        _checkpoint(f"candidate-file-read:{relative}")
        if (
            remaining
            or extra
            or _candidate_stat_identity(os.fstat(descriptor)) != expected_identity
            or observed_size != opened.st_size
        ):
            raise CandidateUncertain(f"candidate regular file changed while being read: {relative}")
    finally:
        os.close(descriptor)
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain(f"candidate regular file disappeared after being read: {relative}") from error
    if _candidate_stat_identity(current) != expected_identity:
        raise CandidateUncertain(f"candidate regular file was replaced after being read: {relative}")
    return digest.hexdigest(), None if content is None else bytes(content), expected_identity


def _read_candidate_regular_file(path: Path, expected: os.stat_result, relative: str) -> tuple[bytes, tuple[int, ...]]:
    if not stat.S_ISREG(expected.st_mode) or stat.S_ISLNK(expected.st_mode) or expected.st_nlink != 1:
        raise CandidateUncertain(f"candidate path is not a private regular file: {relative}")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateUncertain(f"candidate regular file cannot be opened safely: {relative}") from error
    try:
        opened = os.fstat(descriptor)
        expected_identity = _candidate_stat_identity(expected)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _candidate_stat_identity(opened) != expected_identity
        ):
            raise CandidateUncertain(f"candidate regular file changed while being opened: {relative}")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
        finished = os.fstat(descriptor)
        if _candidate_stat_identity(finished) != expected_identity or len(content) != opened.st_size:
            raise CandidateUncertain(f"candidate regular file changed while being read: {relative}")
    finally:
        os.close(descriptor)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain(f"candidate regular file disappeared after being read: {relative}") from error
    if _candidate_stat_identity(current) != expected_identity:
        raise CandidateUncertain(f"candidate regular file was replaced after being read: {relative}")
    return bytes(content), expected_identity


def _candidate_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _candidate_tree_objects(
    entries: tuple[tuple[str, tuple[str, str]], ...],
    blobs: tuple[tuple[str, bytes], ...],
    object_format: str,
) -> tuple[str, tuple[tuple[str, str, bytes], ...]]:
    tree_objects: list[tuple[str, str, bytes]] = []
    root_oid = _candidate_tree_oid(entries, object_format, tree_objects=tree_objects)
    blob_objects = [("blob", oid, content) for oid, content in blobs]
    objects = tuple(sorted((*blob_objects, *tree_objects), key=lambda item: (item[0], item[1])))
    return root_oid, objects


def _candidate_tree_oid(
    entries: tuple[tuple[str, tuple[str, str]], ...],
    object_format: str,
    *,
    tree_objects: list[tuple[str, str, bytes]] | None = None,
) -> str:
    root: dict[str, object] = {}
    for path, leaf in entries:
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise CandidateUncertain(f"candidate tree has a file/directory collision: {path}")
            node = child
        if parts[-1] in node:
            raise CandidateUncertain(f"candidate tree has a duplicate or colliding path: {path}")
        node[parts[-1]] = leaf

    def build(node: dict[str, object]) -> str:
        encoded: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            name_bytes = name.encode("utf-8")
            if isinstance(value, dict):
                oid = build(value)
                mode = "40000"
                sort_name = name_bytes + b"/"
            else:
                mode, oid = value
                sort_name = name_bytes
            raw = mode.encode("ascii") + b" " + name_bytes + b"\0" + bytes.fromhex(oid)
            encoded.append((sort_name, raw))
        content = b"".join(raw for _, raw in sorted(encoded, key=lambda item: item[0]))
        oid = _git_object_oid("tree", content, object_format)
        if tree_objects is not None:
            tree_objects.append(("tree", oid, content))
        return oid

    return build(root)


def _candidate_private_file_snapshot(path: Path, *, label: str) -> _CandidateFileSnapshot:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be inspected safely: {path}") from error
    content, identity = _read_candidate_regular_file(path, info, label)
    return _CandidateFileSnapshot(
        identity=identity,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _candidate_snapshot_matches_recorded_base(
    record: Mapping[str, object],
    snapshot: _CandidateFileSnapshot,
) -> bool:
    identity = record.get("base_index_identity")
    return (
        isinstance(identity, list)
        and len(identity) == 7
        and list(snapshot.identity[:5]) == identity[:5]
        and snapshot.sha256 == record.get("base_index_sha256")
    )


def _exchange_paths(left: Path, right: Path) -> None:
    """Atomically exchange two directory entries without discarding either one."""
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    left_bytes = os.fsencode(left)
    right_bytes = os.fsencode(right)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(left_bytes, right_bytes, 0x00000002)
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(-100, left_bytes, -100, right_bytes, 0x00000002)
    else:  # pragma: no cover - no portable atomic exchange primitive
        raise OSError("atomic path exchange is unsupported on this platform")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(left), str(right))


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename one entry only when the destination is absent."""
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 0x00000001)
    else:  # pragma: no cover - no portable no-replace rename primitive
        raise OSError("atomic no-replace rename is unsupported on this platform")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(source), str(destination))


def _quarantine_bound_path(
    path: Path,
    expected: tuple[int, int],
    *,
    label: str,
    checkpoint: str,
    kind: str,
) -> Path:
    name_digest = hashlib.sha256(os.fsencode(path.name)).hexdigest()[:16]
    quarantine = path.with_name(
        f".autoform-removing-{name_digest}-{expected[0]:x}-{expected[1]:x}-{secrets.token_hex(16)}"
    )
    if quarantine.exists() or quarantine.is_symlink():  # pragma: no cover - random collision
        raise WorktreeConflict(f"{label} quarantine already exists")
    _checkpoint(checkpoint)
    try:
        _rename_noreplace(path, quarantine)
        _fsync_directory(path.parent)
        moved = quarantine.stat(follow_symlinks=False)
    except OSError as error:
        raise WorktreeConflict(f"{label} could not be quarantined safely") from error
    expected_type = {
        "directory": stat.S_ISDIR,
        "regular": stat.S_ISREG,
        "symlink": stat.S_ISLNK,
    }.get(kind)
    if expected_type is None:  # pragma: no cover - internal invariant
        raise WorktreeConflict(f"{label} has an unsupported quarantine type")
    if (
        not expected_type(moved.st_mode)
        or (kind != "symlink" and stat.S_ISLNK(moved.st_mode))
        or (moved.st_dev, moved.st_ino) != expected
    ):
        raise WorktreeConflict(f"{label} was replaced; its quarantine is preserved")
    return quarantine


def _final_removal_quarantine(
    path: Path,
    expected: tuple[int, int],
    *,
    label: str,
    checkpoint: str,
    kind: str,
) -> Path:
    """Re-isolate a verified entry immediately before its final removal.

    The second unpredictable no-replace rename preserves a bounded concurrent
    replacement made after the first validation. A continuously hostile
    same-UID process that discovers and races every random name is outside the
    repository lifecycle-containment contract.
    """
    return _quarantine_bound_path(
        path,
        expected,
        label=label,
        checkpoint=checkpoint,
        kind=kind,
    )


def _remove_bound_private_file(
    path: Path,
    expected: _CandidateFileSnapshot,
    *,
    label: str,
    checkpoint: str,
) -> None:
    quarantine = _quarantine_bound_path(
        path,
        (int(expected.identity[0]), int(expected.identity[1])),
        label=label,
        checkpoint=checkpoint,
        kind="regular",
    )
    moved = _candidate_bound_file_snapshot(
        quarantine,
        expected=(int(expected.identity[0]), int(expected.identity[1])),
        label=f"quarantined {label}",
    )
    if (
        moved.identity[:6] != expected.identity[:6]
        or moved.sha256 != expected.sha256
        or moved.content != expected.content
    ):
        raise WorktreeConflict(f"{label} changed while being quarantined; its quarantine is preserved")
    quarantine = _final_removal_quarantine(
        quarantine,
        (int(expected.identity[0]), int(expected.identity[1])),
        label=label,
        checkpoint=f"{checkpoint}-final-isolation",
        kind="regular",
    )
    moved = _candidate_bound_file_snapshot(
        quarantine,
        expected=(int(expected.identity[0]), int(expected.identity[1])),
        label=f"final quarantined {label}",
    )
    if (
        moved.identity[:6] != expected.identity[:6]
        or moved.sha256 != expected.sha256
        or moved.content != expected.content
    ):
        raise WorktreeConflict(f"{label} changed during final isolation; its quarantine is preserved")
    try:
        quarantine.unlink()
        _fsync_directory(path.parent)
    except OSError as error:
        raise WorktreeConflict(f"quarantined {label} could not be removed") from error


def _remove_bound_empty_directory(
    path: Path,
    expected: tuple[int, int],
    *,
    label: str,
    checkpoint: str,
) -> None:
    quarantine = _quarantine_bound_path(
        path,
        expected,
        label=label,
        checkpoint=checkpoint,
        kind="directory",
    )
    try:
        with os.scandir(quarantine) as iterator:
            if next(iterator, None) is not None:
                raise WorktreeConflict(f"{label} quarantine contains foreign state")
        if _directory_identity(quarantine) != expected:
            raise WorktreeConflict(f"{label} quarantine was replaced")
        quarantine = _final_removal_quarantine(
            quarantine,
            expected,
            label=label,
            checkpoint=f"{checkpoint}-final-isolation",
            kind="directory",
        )
        with os.scandir(quarantine) as iterator:
            if next(iterator, None) is not None:
                raise WorktreeConflict(f"{label} final quarantine contains foreign state")
        if _directory_identity(quarantine) != expected:
            raise WorktreeConflict(f"{label} final quarantine was replaced")
        quarantine.rmdir()
        _fsync_directory(path.parent)
    except OSError as error:
        raise WorktreeConflict(f"{label} quarantine could not be removed") from error


def _candidate_bound_file_snapshot(
    path: Path,
    *,
    expected: tuple[int, int],
    label: str,
) -> _CandidateFileSnapshot:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be inspected safely: {path}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != expected:
        raise CandidateUncertain(f"{label} does not match its durable file identity")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be opened safely: {path}") from error
    try:
        opened = os.fstat(descriptor)
        opened_identity = _candidate_stat_identity(opened)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != expected:
            raise CandidateUncertain(f"{label} changed while being opened")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
        if _candidate_stat_identity(os.fstat(descriptor)) != opened_identity or len(content) != opened.st_size:
            raise CandidateUncertain(f"{label} changed while being read")
    finally:
        os.close(descriptor)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain(f"{label} disappeared after being read") from error
    if _candidate_stat_identity(current) != opened_identity:
        raise CandidateUncertain(f"{label} changed after being read")
    return _CandidateFileSnapshot(
        identity=opened_identity,
        sha256=hashlib.sha256(content).hexdigest(),
        content=bytes(content),
    )


def _optional_candidate_private_file_snapshot(
    path: Path,
    *,
    label: str,
) -> _CandidateFileSnapshot | None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if path.is_symlink():
            raise CandidateUncertain(f"{label} is a dangling symbolic link: {path}")
        return None
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be inspected: {path}") from error
    content, identity = _read_candidate_regular_file(path, info, label)
    return _CandidateFileSnapshot(
        identity=identity,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _candidate_head_oid(snapshot: _CandidateFileSnapshot) -> str:
    try:
        content = snapshot.content.decode("ascii")
    except UnicodeDecodeError as error:
        raise CandidateUncertain("attempt Git HEAD is not ASCII") from error
    oid = content[:-1] if content.endswith("\n") else content
    if not oid or "\n" in oid or "\r" in oid or content not in {oid, f"{oid}\n"}:
        raise CandidateUncertain("attempt Git HEAD is not a detached object id")
    try:
        _validate_oid(oid)
    except RepositoryError as error:
        raise CandidateUncertain("attempt Git HEAD is not a detached object id") from error
    return oid


def _candidate_control_path(content: bytes, *, base: Path, label: str) -> Path:
    try:
        line = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateUncertain(f"{label} is not UTF-8") from error
    value = line[:-1] if line.endswith("\n") else line
    if not value or "\n" in value or "\r" in value or "\0" in value or line not in {value, f"{value}\n"}:
        raise CandidateUncertain(f"{label} is malformed")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    path = _absolute_path(path)
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CandidateUncertain(f"{label} cannot be resolved safely") from error
    if canonical != path:
        raise CandidateUncertain(f"{label} traverses a symbolic link")
    return path


def _assert_candidate_config_is_self_contained(repository: AttemptWorktrees, content: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="autoform-candidate-config-") as scratch:
        config = Path(scratch) / "config"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        proc = repository._run_git_bytes(  # noqa: SLF001 - isolated parser for repository-owned config
            ["config", "--file", str(config), "--no-includes", "--null", "--name-only", "--list"],
            check=False,
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()[:500]
        raise CandidateUncertain(f"repository configuration cannot be parsed safely: {detail}")
    for raw_name in proc.stdout.split(b"\0"):
        name = raw_name.decode("utf-8", errors="surrogateescape").casefold()
        if name == "include.path" or (name.startswith("includeif.") and name.endswith(".path")):
            raise CandidateUncertain("repository configuration includes external configuration")
        if name == "core.excludesfile":
            raise CandidateUncertain("repository configuration uses an external excludes file")


def _assert_missing_worktree_path(tree: Path, path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise WorktreeUncertain(f"interrupted checkout path reappeared before repair: {path}")
    cursor = tree
    relative_parts = path.relative_to(tree).parts
    for part in relative_parts[:-1]:
        cursor /= part
        if not cursor.exists() and not cursor.is_symlink():
            break
        try:
            info = cursor.lstat()
        except OSError as error:
            raise WorktreeUncertain(f"interrupted checkout parent cannot be inspected: {cursor}") from error
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise WorktreeUncertain(f"interrupted checkout parent is not a real directory: {cursor}")


def _git_entry_identity(tree: Path) -> tuple[int, int, str]:
    path = tree / ".git"
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorktreeConflict("attempt worktree .git entry cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024:
            raise WorktreeConflict("attempt worktree .git entry is not a private regular file")
        content = os.read(descriptor, info.st_size + 1)
    finally:
        os.close(descriptor)
    if len(content) != info.st_size:
        raise WorktreeConflict("attempt worktree .git entry changed while being inspected")
    return info.st_dev, info.st_ino, hashlib.sha256(content).hexdigest()


def _regular_file_identity(path: Path, *, label: str) -> tuple[int, int, str]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PublicationUncertain(f"{label} cannot be opened safely") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise PublicationUncertain(f"{label} is not a private regular file")
        content = os.read(descriptor, info.st_size + 1)
    finally:
        os.close(descriptor)
    if len(content) != info.st_size:
        raise PublicationUncertain(f"{label} changed while being inspected")
    return info.st_dev, info.st_ino, hashlib.sha256(content).hexdigest()


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _read_json_file(path: Path, *, label: str) -> dict[str, object]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RepositoryError(f"{label} cannot be opened safely: {path}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise RepositoryError(f"{label} is not a private regular file: {path}")
        content = b""
        while len(content) <= info.st_size:
            chunk = os.read(descriptor, min(64 * 1024, info.st_size + 1 - len(content)))
            if not chunk:
                break
            content += chunk
    finally:
        os.close(descriptor)
    if len(content) != info.st_size:
        raise RepositoryError(f"{label} changed while being read: {path}")
    try:
        value = json.loads(content, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, ValueError) as error:
        raise RepositoryError(f"{label} is not valid canonical JSON: {path}") from error
    if not isinstance(value, dict):
        raise RepositoryError(f"{label} must contain a JSON object: {path}")
    return value


def _write_json_file(path: Path, value: Mapping[str, object]) -> None:
    _write_bytes_file(path, _json_bytes(value) + b"\n")


def _write_bytes_file(path: Path, content: bytes) -> None:
    _ensure_private_directory(path.parent)
    _remove_atomic_write_orphans(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=_atomic_write_prefix(path),
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    created_identity: tuple[int, int] | None = None
    written: _CandidateFileSnapshot | None = None
    try:
        created = os.fstat(descriptor)
        created_identity = (created.st_dev, created.st_ino)
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        written = _atomic_state_file_snapshot(
            temporary,
            expected=created_identity,
            label="atomic state temporary file",
        )
        if written.content != content:
            raise RepositoryError(f"atomic state temporary file changed before installation: {temporary}")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created_identity is not None:
            _remove_exact_atomic_state_file(
                temporary,
                expected_identity=created_identity,
                expected_snapshot=written,
                prefix=_atomic_write_prefix(path),
                label="atomic state temporary file",
                missing_ok=True,
            )


def _atomic_write_prefix(path: Path) -> str:
    target = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    return f".autoform-state-{target}-"


def _atomic_state_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (*_candidate_stat_identity(info), info.st_uid)


def _atomic_state_file_snapshot(
    path: Path,
    *,
    expected: tuple[int, int] | None,
    label: str,
) -> _CandidateFileSnapshot:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RepositoryError(f"{label} cannot be inspected: {path}") from error
    owner_matches = not hasattr(os, "geteuid") or info.st_uid == os.geteuid()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not owner_matches
        or info.st_size > 1024 * 1024
    ):
        raise RepositoryError(f"{label} is not a safe orphan: {path}")
    identity = _atomic_state_stat_identity(info)
    if expected is not None and identity[:2] != expected:
        raise RepositoryError(f"{label} contains a replacement: {path}")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RepositoryError(f"{label} cannot be opened safely: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if _atomic_state_stat_identity(opened) != identity:
            raise RepositoryError(f"{label} changed while being opened: {path}")
        content = bytearray()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            content.extend(chunk)
            remaining -= len(chunk)
        extra = os.read(descriptor, 1) if not remaining else b""
        if remaining or extra or _atomic_state_stat_identity(os.fstat(descriptor)) != identity:
            raise RepositoryError(f"{label} changed while being read: {path}")
    finally:
        os.close(descriptor)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RepositoryError(f"{label} disappeared after being read: {path}") from error
    if _atomic_state_stat_identity(current) != identity:
        raise RepositoryError(f"{label} changed after being read: {path}")
    data = bytes(content)
    return _CandidateFileSnapshot(identity=identity, sha256=hashlib.sha256(data).hexdigest(), content=data)


def _atomic_state_snapshots_match(left: _CandidateFileSnapshot, right: _CandidateFileSnapshot) -> bool:
    return (
        left.identity[:6] == right.identity[:6]
        and left.identity[7:] == right.identity[7:]
        and left.sha256 == right.sha256
        and left.content == right.content
    )


def _quarantine_atomic_state_file(
    path: Path,
    expected: _CandidateFileSnapshot,
    *,
    prefix: str,
    label: str,
) -> tuple[Path, _CandidateFileSnapshot]:
    quarantine = path.with_name(
        f"{prefix}quarantine-{expected.identity[0]:x}-{expected.identity[1]:x}-{os.urandom(16).hex()}.tmp"
    )
    try:
        _rename_noreplace(path, quarantine)
        _fsync_directory(path.parent)
    except OSError as error:
        raise RepositoryError(f"{label} cannot be quarantined: {path}") from error
    moved = _atomic_state_file_snapshot(
        quarantine,
        expected=None,
        label=label,
    )
    if not _atomic_state_snapshots_match(moved, expected):
        raise RepositoryError(f"{label} changed while being quarantined: {quarantine}")
    return quarantine, moved


def _remove_exact_atomic_state_file(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    expected_snapshot: _CandidateFileSnapshot | None,
    prefix: str,
    label: str,
    missing_ok: bool,
) -> None:
    """Remove one bound temp file after two verified no-replace isolations.

    The second unpredictable rename preserves a bounded replacement made after
    the first validation. A continuously hostile same-UID process that races
    every random name is outside the repository lifecycle-containment contract.
    """
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok and not path.is_symlink():
            return
        raise RepositoryError(f"{label} disappeared before cleanup: {path}") from None
    except OSError as error:
        raise RepositoryError(f"{label} cannot be inspected before cleanup: {path}") from error
    current = _atomic_state_file_snapshot(path, expected=expected_identity, label=label)
    if expected_snapshot is not None and not _atomic_state_snapshots_match(current, expected_snapshot):
        raise RepositoryError(f"{label} changed before cleanup: {path}")
    quarantine, current = _quarantine_atomic_state_file(
        path,
        current,
        prefix=prefix,
        label=label,
    )
    quarantine, current = _quarantine_atomic_state_file(
        quarantine,
        current,
        prefix=prefix,
        label=f"final quarantined {label}",
    )
    try:
        quarantine.unlink()
        _fsync_directory(path.parent)
    except OSError as error:
        raise RepositoryError(f"{label} cannot be removed safely: {quarantine}") from error


def _remove_atomic_write_orphans(path: Path) -> None:
    """Remove only private regular files reserved for atomic writes to ``path``."""
    prefix = _atomic_write_prefix(path)
    quarantine_pattern = re.compile(rf"^{re.escape(prefix)}quarantine-([0-9a-f]+)-([0-9a-f]+)-[0-9a-f]{{32}}\.tmp$")
    try:
        entries = list(os.scandir(path.parent))
    except OSError as error:
        raise RepositoryError(f"atomic state directory cannot be inspected: {path.parent}") from error
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
            continue
        candidate = Path(entry.path)
        snapshot = _atomic_state_file_snapshot(candidate, expected=None, label="reserved atomic state path")
        quarantined = quarantine_pattern.fullmatch(candidate.name)
        if quarantined is not None:
            expected_identity = (int(quarantined.group(1), 16), int(quarantined.group(2), 16))
            if expected_identity != snapshot.identity[:2]:
                raise RepositoryError(f"atomic state quarantine contains a replacement: {candidate}")
        _remove_exact_atomic_state_file(
            candidate,
            expected_identity=(int(snapshot.identity[0]), int(snapshot.identity[1])),
            expected_snapshot=snapshot,
            prefix=prefix,
            label="atomic state temporary file",
            missing_ok=False,
        )


def _transport_config(object_format: str) -> bytes:
    if object_format == "sha1":
        return (f"[core]\n\trepositoryformatversion = 0\n\tbare = true\n\thooksPath = {os.devnull}\n").encode()
    if object_format == "sha256":
        return (
            "[core]\n"
            "\trepositoryformatversion = 1\n"
            "\tbare = true\n"
            f"\thooksPath = {os.devnull}\n"
            "[extensions]\n"
            "\tobjectFormat = sha256\n"
        ).encode()
    raise RepositoryError(f"unsupported Git object format: {object_format}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise RepositoryError(f"value is not canonical JSON: {error}") from error


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key in _GIT_ENV_ALLOWLIST or key.startswith("LC_")
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git_command(args: list[str]) -> list[str]:
    return [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "credential.helper=",
        *args,
    ]


def _git_failure(operation: str, proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout).strip()[:500]
    return f"{operation} failed: {detail}"


def _redact_remote(detail: str, remote: str) -> str:
    return detail.replace(remote, "<remote>")


def _append_detail(current: str, extra: str) -> str:
    return f"{current}; {extra}" if current else extra


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _primary_pack_file_identity(path: Path, *, label: str) -> tuple[int, ...]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be inspected safely") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink < 1:
        raise CandidateUncertain(f"{label} is not regular primary storage")
    return _candidate_stat_identity(info)


def _primary_pack_trace_paths(content: bytes, *, repository_root: Path) -> tuple[Path, ...]:
    if not content:
        return ()
    if not content.endswith(b"\n"):
        raise CandidateUncertain("primary Git pack trace returned a truncated record")
    result: list[Path] = []
    for line in content.splitlines():
        match = re.fullmatch(rb".*packfile\.c:[0-9]+ +(.+) +[0-9]+", line)
        if match is None or b"\0" in match.group(1):
            raise CandidateUncertain("primary Git pack trace returned a malformed record")
        value = Path(os.fsdecode(match.group(1)))
        if not value.is_absolute():
            value = repository_root / value
        result.append(Path(os.path.abspath(value)))
    return tuple(result)


def _inherited_descriptor_path(descriptor: int) -> str:
    if sys.platform == "darwin" and os.path.isdir("/dev/fd"):
        return f"/dev/fd/{descriptor}"
    if sys.platform.startswith("linux") and os.path.isdir("/proc/self/fd"):
        return f"/proc/self/fd/{descriptor}"
    raise CandidateUncertain("primary Git pack tracing requires Darwin or Linux descriptor paths")


def _primary_pack_file_digest(path: Path, *, label: str) -> tuple[tuple[int, ...], str]:
    before_identity = _primary_pack_file_identity(path, label=label)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be opened safely") from error
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if _candidate_stat_identity(opened) != before_identity:
            raise CandidateUncertain(f"{label} changed while being opened")
        while True:
            chunk = os.read(descriptor, _CANDIDATE_BLOB_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if _candidate_stat_identity(finished) != before_identity:
            raise CandidateUncertain(f"{label} changed while being read")
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be read safely") from error
    finally:
        os.close(descriptor)
    if _primary_pack_file_identity(path, label=label) != before_identity:
        raise CandidateUncertain(f"{label} was replaced after being read")
    return before_identity, digest.hexdigest()


def _fsync_primary_pack_file(path: Path, *, label: str) -> None:
    before_identity = _primary_pack_file_identity(path, label=label)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be opened safely") from error
    try:
        if _candidate_stat_identity(os.fstat(descriptor)) != before_identity:
            raise CandidateUncertain(f"{label} changed while being opened")
        os.fsync(descriptor)
        if _candidate_stat_identity(os.fstat(descriptor)) != before_identity:
            raise CandidateUncertain(f"{label} changed while being synchronized")
    except OSError as error:
        raise CandidateUncertain(f"{label} cannot be synchronized") from error
    finally:
        os.close(descriptor)
    if _primary_pack_file_identity(path, label=label) != before_identity:
        raise CandidateUncertain(f"{label} was replaced after being synchronized")


def _candidate_abandoned_index_stages(
    record: Mapping[str, object],
) -> tuple[tuple[str, tuple[int, int]], ...]:
    value = record.get("candidate_index_abandoned_stages")
    if not isinstance(value, list) or len(value) > _MAX_CANDIDATE_ABANDONED_INDEX_STAGES:
        raise CandidateUncertain("candidate journal has an invalid abandoned index stage inventory")
    active = record.get("candidate_index_stage_name")
    seen: set[str] = set()
    result: list[tuple[str, tuple[int, int]]] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"name", "device", "inode"}:
            raise CandidateUncertain("candidate journal has an invalid abandoned index stage")
        name = entry.get("name")
        device = entry.get("device")
        inode = entry.get("inode")
        if (
            not isinstance(name, str)
            or not _CANDIDATE_INDEX_STAGE_NAME.fullmatch(name)
            or name == active
            or name in seen
            or not _is_integer(device)
            or not _is_integer(inode)
            or int(device) < 0
            or int(inode) < 0
        ):
            raise CandidateUncertain("candidate journal has an invalid abandoned index stage")
        seen.add(name)
        result.append((name, (int(device), int(inode))))
    return tuple(result)


def _checkpoint(_name: str) -> None:
    """Test hook for interruption at durable operation boundaries."""


__all__ = [
    "AttemptWorktrees",
    "CandidateError",
    "CandidateNotFound",
    "CandidateReceipt",
    "CandidateUncertain",
    "MergeQueueBusy",
    "MergeQueueError",
    "PublicationReceipt",
    "PublicationUncertain",
    "RemoteDrift",
    "RemoteMergeQueue",
    "RepositoryError",
    "WorktreeConflict",
    "WorktreeReceipt",
    "WorktreeUncertain",
]
