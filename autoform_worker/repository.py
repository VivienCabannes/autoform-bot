"""Isolated Git worktrees and a durable compare-and-swap merge queue."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import weakref
from collections.abc import Mapping, Set
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import autoform_cli.claims as claims_module
from autoform_cli.claims import CLAIM_HEARTBEAT_S, CLAIM_REF_PREFIX, CLAIM_TTL_S, ClaimBoard

from .ledger import CoordinatorLock


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WORKTREE_SCHEMA = "autoform-worktree/v2"
_CANDIDATE_SCHEMA = "autoform-candidate/v1"
_PUBLICATION_SCHEMA = "autoform-merge-publication/v2"
_TRANSPORT_SCHEMA = "autoform-git-transport/v2"
_TRANSPORT_INTENT_SCHEMA = "autoform-git-transport-intent/v1"
_PUBLICATION_STATES = frozenset({"prepared", "queueing", "queued", "publishing", "integrated", "stale", "uncertain"})
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
    ) -> bool: ...

    def holds(self, key: str) -> bool: ...

    def held_claim_oid(self, key: str) -> str | None: ...

    def release(self, key: str) -> bool: ...

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
    claim_key: str
    claim_ref: str
    claim_oid: str | None
    observed_claim_oid: str | None
    claim_lease_id: str | None
    detail: str
    history: tuple[Mapping[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["history"] = [dict(item) for item in self.history]
        return value

    def evidence_bytes(self) -> bytes:
        """Return canonical bytes suitable for :meth:`RunLedger.put_artifact`."""
        return _json_bytes({"schema": _PUBLICATION_SCHEMA, **self.as_dict()})

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
            self._assert_candidate_index(tree, base_entries, label="recorded base")
            config_snapshot = self._candidate_config_snapshot(tree)
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
            root_device, root_inode = _directory_identity(attempt_root)
            record: dict[str, object] = {
                "schema": _CANDIDATE_SCHEMA,
                **expected_identity,
                "state": "prepared",
                "tree_oid": tree_oid,
                "candidate_oid": candidate_oid,
                "root_device": root_device,
                "root_inode": root_inode,
                "created_ns": time.time_ns(),
                "ready_ns": None,
            }
            self._write_candidate_journal(journal, record)
            _checkpoint("candidate-intent-recorded")
            self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
            self._assert_candidate_config_snapshot(tree, config_snapshot)
            return self._finish_candidate(
                record,
                journal,
                tree,
                message_bytes=message_bytes,
                snapshot=snapshot,
                objects=objects,
                commit_content=commit_content,
                config_snapshot=config_snapshot,
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
        paths = tuple(str(path) for path in record["allowed_paths"])
        base_entries = self._candidate_base_entries(worktree.base_oid)
        snapshot = self._candidate_snapshot(tree, base_entries, paths)
        tree_oid, objects = _candidate_tree_objects(snapshot.entries, snapshot.blobs, self.object_format)
        if tree_oid != record["tree_oid"]:
            raise CandidateUncertain("candidate worktree no longer matches the recorded tree")
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(record)

        candidate_oid = str(record["candidate_oid"])
        head_oid = self._tree_head(tree)
        if head_oid != candidate_oid:
            if record["state"] == "prepared" and head_oid == worktree.base_oid:
                raise CandidateUncertain("candidate intent exists but its commit is not current")
            raise CandidateUncertain("attempt HEAD conflicts with the candidate journal")
        candidate_entries = dict(snapshot.entries)
        if record["state"] == "ready":
            self._assert_candidate_index(tree, candidate_entries, label="candidate")
            return self._candidate_receipt(record, state="ready")
        index_entries = self._candidate_index_entries(tree)
        if index_entries not in (base_entries, candidate_entries):
            raise CandidateUncertain("candidate index conflicts with its recorded base and tree")
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
        config_snapshot: tuple[tuple[str, object], ...] | None = None,
    ) -> CandidateReceipt:
        base_oid = str(record["base_oid"])
        paths = tuple(str(path) for path in record["allowed_paths"])
        base_entries = self._candidate_base_entries(base_oid)
        if snapshot is None:
            snapshot = self._candidate_snapshot(tree, base_entries, paths)
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
        if config_snapshot is None:
            config_snapshot = self._candidate_config_snapshot(tree)

        head_oid = self._tree_head(tree)
        candidate_entries = dict(snapshot.entries)
        if record["state"] == "ready":
            if head_oid != candidate_oid:
                raise CandidateUncertain("ready candidate HEAD no longer matches its journal")
            self._verify_candidate_objects(objects)
            self._verify_candidate_object(record, expected_content=commit_content)
            self._assert_candidate_index(tree, candidate_entries, label="candidate")
            self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
            self._assert_candidate_config_snapshot(tree, config_snapshot)
            return self._candidate_receipt(record, state="ready")
        if record["state"] != "prepared":  # pragma: no cover - validated before dispatch
            raise CandidateUncertain("candidate journal has an unknown state")
        if head_oid not in {base_oid, candidate_oid}:
            raise CandidateUncertain("attempt HEAD conflicts with the prepared candidate")
        index_entries = self._candidate_index_entries(tree)
        if head_oid == base_oid and index_entries != base_entries:
            raise CandidateUncertain("candidate index does not match the recorded base")
        if head_oid == candidate_oid and index_entries not in (base_entries, candidate_entries):
            raise CandidateUncertain("candidate index conflicts with its recorded base and tree")
        self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(tree, config_snapshot)

        for object_type, expected_oid, content in (*objects, ("commit", candidate_oid, commit_content)):
            self._write_candidate_object(object_type, expected_oid, content)
        _checkpoint("candidate-objects-written")
        self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(tree, config_snapshot)
        self._verify_candidate_object(record, expected_content=commit_content)

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
        self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(tree, config_snapshot)

        index_entries = self._candidate_index_entries(tree)
        if index_entries == base_entries:
            self._run_tree_git(tree, ["read-tree", candidate_oid])
        elif index_entries != candidate_entries:
            raise CandidateUncertain("candidate index changed during commit recovery")
        _checkpoint("candidate-index-updated")
        self._assert_candidate_index(tree, candidate_entries, label="candidate")
        self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(tree, config_snapshot)

        ready = dict(record)
        ready.update({"state": "ready", "ready_ns": time.time_ns()})
        self._write_candidate_journal(journal, ready)
        _checkpoint("candidate-result-recorded")
        if self._tree_head(tree) != candidate_oid:
            raise CandidateUncertain("candidate HEAD changed after its result was recorded")
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(ready, expected_content=commit_content)
        self._assert_candidate_index(tree, candidate_entries, label="candidate")
        self._assert_candidate_snapshot(tree, base_entries, paths, snapshot)
        self._assert_candidate_config_snapshot(tree, config_snapshot)
        return self._candidate_receipt(ready, state="ready")

    def _candidate_base_entries(self, base_oid: str) -> dict[str, tuple[str, str]]:
        proc = self._run_git(["ls-tree", "-r", "-z", "--full-tree", base_oid])
        entries: dict[str, tuple[str, str]] = {}
        for entry in proc.stdout.split("\0"):
            if not entry:
                continue
            metadata, separator, path = entry.partition("\t")
            fields = metadata.split()
            if not separator or not _safe_candidate_path(path) or len(fields) != 3:
                raise CandidateUncertain("base commit tree output is malformed")
            mode, object_type, oid = fields
            if object_type == "commit" or mode == "160000":
                raise CandidateUncertain(f"candidate commits reject submodules: {path}")
            if object_type != "blob" or mode not in {"100644", "100755"}:
                kind = "symbolic links" if mode == "120000" else "unsupported entries"
                raise CandidateUncertain(f"candidate commits reject {kind}: {path}")
            _validate_oid(oid)
            if path in entries:
                raise CandidateUncertain("base commit tree contains duplicate paths")
            entries[path] = (mode, oid)
        return entries

    def _candidate_snapshot(
        self,
        tree: Path,
        base_entries: Mapping[str, tuple[str, str]],
        allowed_paths: tuple[str, ...],
    ) -> _CandidateSnapshot:
        allowed = frozenset(allowed_paths)
        git_identity = _git_entry_identity(tree)
        files, contents, identities, directories = _snapshot_regular_tree(tree, self.object_format)
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

    def _candidate_index_entries(self, tree: Path) -> dict[str, tuple[str, str]]:
        self._assert_canonical_index_flags(tree)
        proc = self._run_tree_git(tree, ["ls-files", "--stage", "-z", "--cached"])
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
        return entries

    def _assert_candidate_index(
        self,
        tree: Path,
        expected: Mapping[str, tuple[str, str]],
        *,
        label: str,
    ) -> None:
        if self._candidate_index_entries(tree) != dict(expected):
            raise CandidateUncertain(f"candidate index does not match the {label}")

    def _candidate_config_snapshot(self, tree: Path) -> tuple[tuple[str, object], ...]:
        git_dir = self._run_tree_git(tree, ["rev-parse", "--path-format=absolute", "--absolute-git-dir"])
        worktree_git_dir = _existing_real_directory(git_dir.stdout.strip(), label="attempt Git directory")
        paths = (self.common_git_dir / "config", worktree_git_dir / "config.worktree")
        return tuple((str(path), _optional_regular_file_snapshot(path)) for path in paths)

    def _assert_candidate_config_snapshot(
        self,
        tree: Path,
        expected: tuple[tuple[str, object], ...],
    ) -> None:
        if self._candidate_config_snapshot(tree) != expected:
            raise CandidateUncertain("repository configuration changed during candidate creation")

    def _write_candidate_object(self, object_type: str, expected_oid: str, content: bytes) -> None:
        proc = self._run_git_bytes(
            ["hash-object", "-t", object_type, "-w", "--stdin", "--no-filters"],
            input_bytes=content,
        )
        try:
            observed = proc.stdout.decode("ascii").strip()
        except UnicodeDecodeError as error:  # pragma: no cover - Git object IDs are ASCII
            raise CandidateUncertain("git hash-object returned a malformed object id") from error
        if observed != expected_oid:
            raise CandidateUncertain(f"git wrote an unexpected {object_type} object")
        stored = self._run_git_bytes(["cat-file", object_type, expected_oid]).stdout
        if stored != content:
            raise CandidateUncertain(f"stored {object_type} object does not match its expected bytes")

    def _verify_candidate_objects(self, objects: tuple[tuple[str, str, bytes], ...]) -> None:
        for object_type, expected_oid, content in objects:
            stored = self._run_git_bytes(["cat-file", object_type, expected_oid], check=False)
            if stored.returncode != 0 or stored.stdout != content:
                raise CandidateUncertain(f"candidate {object_type} object is missing or invalid")

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

    def _assert_ready_candidate_tree(self, record: Mapping[str, object], tree: Path) -> None:
        if record["state"] != "ready":
            raise CandidateUncertain("candidate recovery must finish before cleanup")
        base_entries = self._candidate_base_entries(str(record["base_oid"]))
        paths = tuple(str(path) for path in record["allowed_paths"])
        snapshot = self._candidate_snapshot(tree, base_entries, paths)
        tree_oid, objects = _candidate_tree_objects(snapshot.entries, snapshot.blobs, self.object_format)
        if tree_oid != record["tree_oid"]:
            raise CandidateUncertain("candidate worktree no longer matches the recorded tree")
        self._verify_candidate_objects(objects)
        self._verify_candidate_object(record)
        if self._tree_head(tree) != record["candidate_oid"]:
            raise CandidateUncertain("ready candidate HEAD no longer matches its journal")
        self._assert_candidate_index(tree, dict(snapshot.entries), label="candidate")

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
        for key in ("message_sha256",):
            if not isinstance(record.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", str(record.get(key))):
                raise CandidateUncertain(f"candidate journal has an invalid {key}")
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
                        try:
                            tree.rmdir()
                        except OSError as error:
                            raise WorktreeConflict(
                                "unregistered attempt path is not an empty owned directory"
                            ) from error
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
                            raise WorktreeConflict(
                                f"attempt worktree contains unowned path {foreign}; cleanup stopped"
                            )
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
                try:
                    tree.rmdir()
                except OSError as error:
                    raise WorktreeConflict("unregistered attempt path is not an empty owned directory") from error
            if candidate_record is not None:
                if not candidate_journal.exists() and not candidate_journal.is_symlink():
                    raise CandidateUncertain("candidate journal disappeared during cleanup")
                final_candidate_record = self._read_candidate_journal(candidate_journal)
                self._validate_candidate_record(final_candidate_record, record)
                if final_candidate_record["state"] != "ready" or final_candidate_record["candidate_oid"] != record.get(
                    "cleanup_head_oid"
                ):
                    raise CandidateUncertain("candidate journal changed during cleanup")
                candidate_journal.unlink()
                _fsync_directory(attempt_root)
                _checkpoint("candidate-journal-removed")
            if marker.exists() or marker.is_symlink():
                marker.unlink()
                _fsync_directory(attempt_root)
                _checkpoint("worktree-marker-removed")
            try:
                attempt_root.rmdir()
            except OSError as error:
                raise WorktreeConflict("attempt directory contains foreign state; cleanup stopped") from error
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
            os.rename(tree, cleanup_tree)
            _fsync_directory(cleanup_tree.parent)
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
            try:
                cleanup_tree.rmdir()
            except OSError as error:
                raise WorktreeConflict("cleanup quarantine contains foreign state") from error
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
            before_snapshot = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_snapshot = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if before_snapshot != after_snapshot or hashed != oid:
                raise WorktreeConflict(f"tracked cleanup path changed or no longer matches HEAD: {relative}")
            path.unlink()
        for directory in sorted(tracked_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise WorktreeConflict(f"cleanup quarantine preserves foreign state at {directory}") from error
        remaining = [entry.name for entry in os.scandir(cleanup_tree) if entry.name != ".git"]
        if remaining:
            raise WorktreeConflict(f"cleanup quarantine preserves foreign state at {remaining[0]}")
        if _git_entry_identity(cleanup_tree) != (
            record["git_entry_device"],
            record["git_entry_inode"],
            record["git_entry_sha256"],
        ):
            raise WorktreeConflict("attempt cleanup Git identity changed")
        dot_git.unlink()
        try:
            cleanup_tree.rmdir()
        except OSError as error:
            raise WorktreeConflict("cleanup quarantine changed before removal") from error

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
            try:
                tree.rmdir()
            except OSError as error:
                raise WorktreeConflict("attempt path exists without a durable ownership marker") from error
            entries = []
        if entries:
            raise WorktreeConflict("attempt path exists without a durable ownership marker")
        attempt_root.rmdir()

    @staticmethod
    def _remove_empty_run_root(run_root: Path) -> None:
        try:
            run_root.rmdir()
        except OSError:
            return
        _fsync_directory(run_root.parent)

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
        proc = self._run_git(["rev-parse", "--verify", f"{oid}^{{commit}}"], check=False)
        if proc.returncode != 0 or proc.stdout.strip() != oid:
            raise RepositoryError(f"base object does not resolve exactly to a commit: {oid}")

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
        if not callable(getattr(claim_provider, "held_claim_oid", None)):
            raise RepositoryError("merge claim board must expose an exact held_claim_oid ownership fence")
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
    ) -> PublicationReceipt:
        """Publish one descendant candidate, or fail without overwriting drift."""
        self._validate_publication_identity(queue_item_id, target_ref, queue_ref, expected_target_oid, candidate_oid)
        self._verify_state()
        claim_key = _merge_claim_key(target_ref)
        lock_digest = hashlib.sha256(f"{self.remote_id}\0{target_ref}".encode()).hexdigest()
        with CoordinatorLock(self.lock_root / f"publish-{lock_digest}.lock"):
            self._verify_state()
            record, journal = self._load_or_create(
                queue_item_id=queue_item_id,
                target_ref=target_ref,
                queue_ref=queue_ref,
                expected_target_oid=expected_target_oid,
                candidate_oid=candidate_oid,
                claim_key=claim_key,
                claim_ref=CLAIM_REF_PREFIX + claim_key,
            )
            if record["status"] != "integrated":
                self._verify_candidate(expected_target_oid, candidate_oid)
            recovered = self._recover_record(record, journal)
            if recovered["status"] == "integrated":
                return _publication_receipt(recovered)
            if recovered["status"] == "stale":
                raise RemoteDrift(str(recovered["detail"]))
            if recovered["status"] == "uncertain":
                raise PublicationUncertain(str(recovered["detail"]))
            acquired = self.claim_board.acquire(
                claim_key,
                ttl=self.claim_ttl,
                note=f"merge queue item {queue_item_id}",
            )
            if not acquired:
                raise MergeQueueBusy(f"another publisher owns {target_ref}")
            lease_id: str | None = None
            release_warning = ""
            result: PublicationReceipt | None = None
            pending_error: BaseException | None = None
            fenced_claim_oid: str | None = None
            try:
                lease_id = self._held_lease_id(claim_key)
                ready_to_publish = False
                with self.claim_board.heartbeat(
                    claim_key,
                    interval=self.heartbeat_interval,
                    ttl=self.claim_ttl,
                ) as heartbeat:
                    record = self._read_journal(journal)
                    record["claim_lease_id"] = lease_id
                    self._assert_lease(claim_key, heartbeat)
                    record = self._recover_record(record, journal)
                    if record["status"] == "integrated":
                        result = _publication_receipt(record)
                    elif record["status"] == "stale":
                        raise RemoteDrift(str(record["detail"]))
                    elif record["status"] == "uncertain":
                        raise PublicationUncertain(str(record["detail"]))
                    else:
                        record = self._ensure_queue_ref(record, journal, heartbeat)
                        if record["status"] == "uncertain":
                            raise PublicationUncertain(str(record["detail"]))
                        self._assert_lease(claim_key, heartbeat)
                        ready_to_publish = True
                if ready_to_publish or result is not None:
                    fenced_claim_oid = self._held_claim_oid(claim_key)
                    if fenced_claim_oid is None:
                        raise PublicationUncertain("publication claim has no exact owned ref fence")
                if ready_to_publish:
                    record["claim_oid"] = fenced_claim_oid
                    record = _transition_journal(journal, record, "queued", "exact claim-ref fence recorded")
                    _checkpoint("claim-fence-recorded")
                    record = self._publish_target(record, journal, str(fenced_claim_oid))
                    if record["status"] == "integrated":
                        result = _publication_receipt(record)
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
                    if fenced_claim_oid is not None:
                        self._release_claim_fence(CLAIM_REF_PREFIX + claim_key, fenced_claim_oid)
                    else:
                        released = self.claim_board.release(claim_key)
                        if not released:
                            release_warning = "publication lease release was refused"
                except Exception as error:  # publication outcome and lease cleanup are distinct
                    release_warning = f"publication lease release failed: {error}"

            if result is not None and release_warning:
                record = self._read_journal(journal)
                record["detail"] = _append_detail(str(record.get("detail", "")), release_warning)
                _transition_journal(journal, record, str(record["status"]), record["detail"])
                result = _publication_receipt(record)
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
    ) -> PublicationReceipt:
        """Classify an interrupted publication from its journal and exact remote refs."""
        self._validate_publication_identity(queue_item_id, target_ref, queue_ref, expected_target_oid, candidate_oid)
        self._verify_state()
        _, journal = self._publication_paths(queue_item_id)
        lock_digest = hashlib.sha256(f"{self.remote_id}\0{target_ref}".encode()).hexdigest()
        with CoordinatorLock(self.lock_root / f"publish-{lock_digest}.lock"):
            self._verify_state()
            identity = {
                "queue_item_id": queue_item_id,
                "target_ref": target_ref,
                "queue_ref": queue_ref,
                "expected_target_oid": expected_target_oid,
                "candidate_oid": candidate_oid,
                "claim_key": _merge_claim_key(target_ref),
                "claim_ref": CLAIM_REF_PREFIX + _merge_claim_key(target_ref),
            }
            staging = self.publication_root / f".{queue_item_id}.preparing"
            if not journal.exists() and not journal.is_symlink() and (staging.exists() or staging.is_symlink()):
                record, journal = self._load_or_create(**identity)
            else:
                record = self._read_journal(journal)
            self._validate_journal(
                record,
                journal=journal,
                **identity,
            )
            if record["status"] != "integrated":
                self._verify_candidate(expected_target_oid, candidate_oid)
            return _publication_receipt(self._recover_record(record, journal))

    def _ensure_queue_ref(
        self,
        record: dict[str, object],
        journal: Path,
        heartbeat: Any,
    ) -> dict[str, object]:
        queue_ref = str(record["queue_ref"])
        candidate = str(record["candidate_oid"])
        observed = self._remote_oid(queue_ref)
        record["observed_queue_oid"] = observed
        if observed == candidate:
            return _transition_journal(journal, record, "queued", "candidate queue ref verified")
        if observed is not None:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} points to unexpected object {observed}",
            )
        record = _transition_journal(journal, record, "queueing", "queue-ref CAS about to run")
        _checkpoint("queue-push-attempted")
        self._assert_lease(str(record["claim_key"]), heartbeat)
        pushed = self._cas_push(queue_ref, None, candidate)
        _checkpoint("queue-pushed")
        observed = self._remote_oid(queue_ref)
        record["observed_queue_oid"] = observed
        if observed != candidate:
            detail = (
                f"queue ref {queue_ref} is {observed or 'absent'} after "
                + ("a rejected" if not pushed else "a successful")
                + " CAS push"
            )
            return _transition_journal(journal, record, "uncertain", detail)
        return _transition_journal(journal, record, "queued", "candidate queue ref verified")

    def _publish_target(
        self,
        record: dict[str, object],
        journal: Path,
        claim_oid: str,
    ) -> dict[str, object]:
        target_ref = str(record["target_ref"])
        queue_ref = str(record["queue_ref"])
        claim_ref = str(record["claim_ref"])
        expected = str(record["expected_target_oid"])
        candidate = str(record["candidate_oid"])
        observed_claim = self._remote_oid(claim_ref)
        record["observed_claim_oid"] = observed_claim
        if observed_claim != claim_oid:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"claim ref {claim_ref} changed before target publication",
            )
        observed_queue = self._remote_oid(queue_ref)
        record["observed_queue_oid"] = observed_queue
        if observed_queue != candidate:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} changed before target publication",
            )
        observed = self._remote_oid(target_ref)
        record["observed_target_oid"] = observed
        if observed == candidate:
            return _transition_journal(journal, record, "integrated", "target already equals candidate")
        if observed != _expected_oid(expected):
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
            claim_ref=claim_ref,
            claim_oid=claim_oid,
            expected_target=_expected_oid(expected),
            candidate=candidate,
        )
        _checkpoint("target-pushed")
        observed = self._remote_oid(target_ref)
        observed_queue = self._remote_oid(queue_ref)
        observed_claim = self._remote_oid(claim_ref)
        record["observed_target_oid"] = observed
        record["observed_queue_oid"] = observed_queue
        record["observed_claim_oid"] = observed_claim
        if pushed:
            if observed == candidate and observed_queue == candidate and observed_claim != claim_oid:
                _checkpoint("target-verified")
                return _transition_journal(journal, record, "integrated", "atomic claim/queue/target CAS verified")
            return _transition_journal(
                journal,
                record,
                "uncertain",
                "atomic publication reported success without the exact three-ref result",
            )
        if observed_claim != claim_oid:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"claim ref {claim_ref} changed during target publication",
            )
        if observed_queue != candidate:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"queue ref {queue_ref} changed during target publication",
            )
        if observed == candidate:
            return _transition_journal(journal, record, "integrated", "target already equals candidate")
        if observed == _expected_oid(expected):
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
        if status in {"integrated", "stale", "uncertain"}:
            return record
        queue_ref = str(record["queue_ref"])
        target_ref = str(record["target_ref"])
        claim_ref = str(record["claim_ref"])
        expected = _expected_oid(str(record["expected_target_oid"]))
        candidate = str(record["candidate_oid"])
        queue_oid = self._remote_oid(queue_ref)
        target_oid = self._remote_oid(target_ref)
        claim_oid = self._remote_oid(claim_ref)
        record["observed_queue_oid"] = queue_oid
        record["observed_target_oid"] = target_oid
        record["observed_claim_oid"] = claim_oid
        if queue_oid not in {None, candidate}:
            return _transition_journal(
                journal,
                record,
                "uncertain",
                f"recovery found queue-ref collision: {queue_oid}",
            )
        if target_oid == candidate:
            if queue_oid == candidate:
                return _transition_journal(
                    journal,
                    record,
                    "integrated",
                    "recovery verified target and queue candidate",
                )
            return _transition_journal(
                journal,
                record,
                "prepared",
                "target equals candidate but queue evidence is absent; publication must reconcile",
            )
        if target_oid != expected:
            return _transition_journal(
                journal,
                record,
                "stale",
                f"recovery found target drift: {target_oid or 'absent'}",
            )
        recovered_status = "queued" if queue_oid == candidate else "prepared"
        if status == recovered_status:
            record["detail"] = f"recovery verified {recovered_status} remote state"
            _write_json_file(journal, record)
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
                    _write_json_file(staging_journal, record)
                else:
                    raise PublicationUncertain("publication staging path has no durable ownership journal")
        else:
            staging.mkdir(mode=0o700)
            _checkpoint("publication-staging-created")
            record = self._new_publication_record(staging, identity)
            _write_json_file(staging_journal, record)
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
        return {
            "schema": _PUBLICATION_SCHEMA,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            **identity,
            "status": "prepared",
            "observed_target_oid": None,
            "observed_queue_oid": None,
            "claim_oid": None,
            "observed_claim_oid": None,
            "claim_lease_id": None,
            "publication_device": publication_identity[0],
            "publication_inode": publication_identity[1],
            "detail": detail,
            "created_ns": now,
            "updated_ns": now,
            "history": [{"status": "prepared", "detail": detail, "created_ns": now}],
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
        expected = {
            "schema": _PUBLICATION_SCHEMA,
            "remote_id": self.remote_id,
            **self._remote_record_identity(),
            **identity,
        }
        for key, value in expected.items():
            if key not in record or record[key] != value:
                raise PublicationUncertain(f"publication journal has a different {key}")
        if record["remote_kind"] == "local" and (
            not _is_integer(record["remote_device"]) or not _is_integer(record["remote_inode"])
        ):
            raise PublicationUncertain("publication journal has an invalid local remote identity")
        if record.get("status") not in _PUBLICATION_STATES:
            raise PublicationUncertain("publication journal has an unknown status")
        if not isinstance(record.get("history"), list):
            raise PublicationUncertain("publication journal history is malformed")
        for key in ("publication_device", "publication_inode", "created_ns", "updated_ns"):
            if not _is_integer(record.get(key)):
                raise PublicationUncertain(f"publication journal has an invalid {key}")
        if not isinstance(record.get("detail"), str):
            raise PublicationUncertain("publication journal detail is malformed")
        lease_id = record.get("claim_lease_id")
        if lease_id is not None and (not isinstance(lease_id, str) or not lease_id):
            raise PublicationUncertain("publication journal claim lease id is malformed")
        for key in ("claim_oid", "observed_claim_oid", "observed_target_oid", "observed_queue_oid"):
            observed = record.get(key)
            if observed is not None and (not isinstance(observed, str) or not _OID.fullmatch(observed)):
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

    def _verify_candidate(self, expected: str, candidate: str) -> None:
        self.repository._verify_commit(candidate)
        if expected not in _ZERO_OIDS:
            self.repository._verify_commit(expected)
            if not self.repository._is_ancestor(expected, candidate):
                raise MergeQueueError("candidate is not descended from the expected target object")

    def _held_lease_id(self, key: str) -> str | None:
        method = getattr(self.claim_board, "held_lease_id", None)
        if method is None:
            return None
        value = method(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise PublicationUncertain("publication claim returned an invalid lease id")
        return value

    def _held_claim_oid(self, key: str) -> str | None:
        method = getattr(self.claim_board, "held_claim_oid", None)
        if method is None:
            raise PublicationUncertain("claim board does not expose an exact claim-ref ownership fence")
        value = method(key)
        if value is None:
            return None
        if not isinstance(value, str) or not _OID.fullmatch(value) or value in _ZERO_OIDS:
            raise PublicationUncertain("publication claim returned an invalid claim-ref object id")
        return value

    def _assert_lease(self, key: str, heartbeat: Any) -> None:
        if heartbeat.lost.is_set() or not self.claim_board.holds(key):
            raise PublicationUncertain("publication lease ownership was lost")

    def _remote_oid(self, ref: str) -> str | None:
        proc = self._remote_git(["ls-remote", self.remote_url, ref])
        lines = [line for line in proc.stdout.splitlines() if line]
        if not lines:
            return None
        if len(lines) != 1:
            raise PublicationUncertain(f"remote returned multiple results for exact ref {ref}")
        oid, separator, observed_ref = lines[0].partition("\t")
        if not separator or observed_ref != ref or not _OID.fullmatch(oid):
            raise PublicationUncertain(f"remote returned an invalid result for exact ref {ref}")
        return oid

    def _cas_push(self, ref: str, expected: str | None, candidate: str) -> bool:
        lease = expected or ""
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                f"--force-with-lease={ref}:{lease}",
                self.remote_url,
                f"{candidate}:{ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if any(marker in detail.casefold() for marker in _CAS_REJECTIONS):
            return False
        raise MergeQueueError(f"remote CAS push failed: {_redact_remote(detail[:500], self.remote_url)}")

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

    def _atomic_target_push(
        self,
        *,
        target_ref: str,
        queue_ref: str,
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
                f"--force-with-lease={target_ref}:{expected_target or ''}",
                f"--force-with-lease={claim_ref}:{claim_oid}",
                self.remote_url,
                f"{candidate}:{queue_ref}",
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

    def _remote_git(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.repository._verify_repository()
        self._verify_state()
        self._verify_remote()
        self._verify_transport()
        command_args = args
        pass_fds: tuple[int, ...] = ()
        if self._remote_descriptor is not None:
            if not args or args[0] not in {"ls-remote", "push"}:
                raise MergeQueueError("unsupported local publication transport operation")
            mode = "upload" if args[0] == "ls-remote" else "receive"
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
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(self.repository.common_git_dir / "objects")
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
            self.repository._verify_repository()
            self._verify_state()
            self._verify_remote()
            self._verify_transport()
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


def _publication_receipt(record: Mapping[str, object]) -> PublicationReceipt:
    history = record.get("history")
    if not isinstance(history, list):
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
        claim_key=str(record["claim_key"]),
        claim_ref=str(record["claim_ref"]),
        claim_oid=(str(record["claim_oid"]) if record.get("claim_oid") is not None else None),
        observed_claim_oid=(
            str(record["observed_claim_oid"]) if record.get("observed_claim_oid") is not None else None
        ),
        claim_lease_id=(str(record["claim_lease_id"]) if record.get("claim_lease_id") is not None else None),
        detail=str(record.get("detail", "")),
        history=tuple(dict(item) for item in history if isinstance(item, dict)),
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
    entry = {"status": status, "detail": str(detail), "created_ns": now}
    if not history or history[-1] != entry:
        history.append(entry)
    record["status"] = status
    record["detail"] = str(detail)
    record["updated_ns"] = now
    _write_json_file(journal, record)
    _checkpoint(f"publication-recorded:{status}")
    return record


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
    if len(author_email.encode("utf-8", errors="ignore")) > 256 or any(
        character in "<>" or character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in author_email
    ) or "@" not in author_email:
        raise RepositoryError("candidate author email is not safe for a commit header")
    try:
        author_name.encode("utf-8")
        author_email.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RepositoryError("candidate author identity must be valid UTF-8") from error


def _git_blob_oid(content: bytes, object_format: str) -> str:
    return _git_object_oid("blob", content, object_format)


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
    pending: list[tuple[Path, str]] = [(tree, "")]
    while pending:
        directory, relative_directory = pending.pop()
        try:
            before = directory.stat(follow_symlinks=False)
            entries = list(os.scandir(directory))
            after = directory.stat(follow_symlinks=False)
        except OSError as error:
            label = relative_directory or "."
            raise CandidateUncertain(f"candidate directory cannot be inspected safely: {label}") from error
        before_identity = _candidate_stat_identity(before)
        if not stat.S_ISDIR(before.st_mode) or before_identity != _candidate_stat_identity(after):
            label = relative_directory or "."
            raise CandidateUncertain(f"candidate directory changed while being inspected: {label}")
        if relative_directory:
            directories[relative_directory] = before_identity
        for entry in entries:
            relative = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
            if relative == ".git":
                continue
            if not _safe_candidate_path(relative):
                raise CandidateUncertain(f"candidate contains an unsafe or reserved path: {relative!r}")
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise CandidateUncertain(f"candidate path cannot be inspected safely: {relative}") from error
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                pending.append((path, relative))
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                kind = "symbolic link" if stat.S_ISLNK(info.st_mode) else "special file"
                raise CandidateUncertain(f"candidate commits reject {kind}: {relative}")
            content, identity = _read_candidate_regular_file(path, info, relative)
            mode = "100755" if info.st_mode & 0o111 else "100644"
            files[relative] = (mode, _git_blob_oid(content, object_format))
            contents[relative] = content
            identities[relative] = identity

    expected_directories: set[str] = set()
    for path in files:
        parts = path.split("/")
        expected_directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    foreign_directories = sorted(set(directories) - expected_directories)
    if foreign_directories:
        raise CandidateUncertain(f"candidate contains foreign directory output: {foreign_directories[0]}")
    return files, contents, identities, directories


def _read_candidate_regular_file(path: Path, expected: os.stat_result, relative: str) -> tuple[bytes, tuple[int, ...]]:
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
        if not stat.S_ISREG(opened.st_mode) or _candidate_stat_identity(opened) != expected_identity:
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

    tree_objects: list[tuple[str, str, bytes]] = []

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
        tree_objects.append(("tree", oid, content))
        return oid

    root_oid = build(root)
    blob_objects = [("blob", oid, content) for oid, content in blobs]
    objects = tuple(sorted((*blob_objects, *tree_objects), key=lambda item: (item[0], item[1])))
    return root_oid, objects


def _optional_regular_file_snapshot(path: Path) -> object:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if path.is_symlink():
            raise CandidateUncertain(f"repository config path is a dangling symbolic link: {path}")
        return None
    except OSError as error:
        raise CandidateUncertain(f"repository config cannot be inspected: {path}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CandidateUncertain(f"repository config is not a regular file: {path}")
    content, identity = _read_candidate_regular_file(path, info, str(path))
    return (*identity, hashlib.sha256(content).hexdigest())


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
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_prefix(path: Path) -> str:
    target = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    return f".autoform-state-{target}-"


def _remove_atomic_write_orphans(path: Path) -> None:
    """Remove only private regular files reserved for atomic writes to ``path``."""
    prefix = _atomic_write_prefix(path)
    quarantine_pattern = re.compile(rf"^{re.escape(prefix)}quarantine-([0-9a-f]+)-([0-9a-f]+)-[0-9a-f]{{32}}\.tmp$")
    removed = False
    try:
        entries = list(os.scandir(path.parent))
    except OSError as error:
        raise RepositoryError(f"atomic state directory cannot be inspected: {path.parent}") from error
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
            continue
        candidate = Path(entry.path)
        try:
            info = candidate.lstat()
        except OSError as error:
            raise RepositoryError(f"atomic state temporary file cannot be inspected: {candidate}") from error
        owner_matches = not hasattr(os, "geteuid") or info.st_uid == os.geteuid()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or not owner_matches
            or info.st_size > 1024 * 1024
        ):
            raise RepositoryError(f"reserved atomic state path is not a safe orphan: {candidate}")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(candidate, flags)
        except OSError as error:
            raise RepositoryError(f"atomic state temporary file cannot be opened safely: {candidate}") from error
        try:
            opened = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_uid)
            opened_identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_nlink,
                opened.st_size,
                opened.st_uid,
            )
            if opened_identity != identity:
                raise RepositoryError(f"atomic state temporary file changed: {candidate}")
        finally:
            os.close(descriptor)

        quarantined = quarantine_pattern.fullmatch(candidate.name)
        if quarantined is not None:
            expected_identity = (int(quarantined.group(1), 16), int(quarantined.group(2), 16))
            if expected_identity != identity[:2]:
                raise RepositoryError(f"atomic state quarantine contains a replacement: {candidate}")
            quarantine = candidate
        else:
            quarantine = candidate.with_name(
                f"{prefix}quarantine-{info.st_dev:x}-{info.st_ino:x}-{os.urandom(16).hex()}.tmp"
            )
            if quarantine.exists() or quarantine.is_symlink():  # pragma: no cover - random collision
                raise RepositoryError(f"atomic state quarantine path already exists: {quarantine}")
            try:
                os.rename(candidate, quarantine)
                _fsync_directory(path.parent)
                moved = quarantine.lstat()
            except OSError as error:
                raise RepositoryError(f"atomic state temporary file cannot be quarantined: {candidate}") from error
            moved_identity = (
                moved.st_dev,
                moved.st_ino,
                moved.st_mode,
                moved.st_nlink,
                moved.st_size,
                moved.st_uid,
            )
            if moved_identity != identity:
                raise RepositoryError(f"atomic state temporary file changed while being quarantined: {quarantine}")
        quarantine.unlink()
        removed = True
    if removed:
        _fsync_directory(path.parent)


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
