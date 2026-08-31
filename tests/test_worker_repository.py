from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

import autoform_worker
import autoform_worker.repository as repository_module
from autoform_cli.claims import ClaimFence, claim_handoff_ref
from autoform_worker.ledger import RunLedger
from autoform_worker.repository import (
    AttemptWorktrees,
    CandidateNotFound,
    CandidateUncertain,
    MergeQueueError,
    MergeQueueBusy,
    PublicationUncertain,
    RemoteDrift,
    RemoteMergeQueue,
    RepositoryError,
    WorktreeConflict,
    WorktreeUncertain,
)


class _FencedTestClaimBoard:
    """Minimal exact-ref claim board for isolated merge-queue tests."""

    def __init__(self, remote: Path, worker: str, scratch: Path) -> None:
        self.remote = remote
        self.worker = worker
        self.scratch = scratch
        self.repo_url = str(remote.resolve())
        self._owned_oids: dict[str, str] = {}
        self._lease_ids: dict[str, str] = {}

    def _ref(self, key: str) -> str:
        return f"refs/autoform-claims/{key}"

    def _remote_oid(self, key: str) -> str | None:
        return _git("for-each-ref", "--format=%(objectname)", self._ref(key), cwd=self.remote) or None

    def acquire(self, key: str, ttl: int | float = 60, steal: bool = False, note: str = "") -> bool:
        handoff_ref = claim_handoff_ref(key) if key.startswith("author/") else None
        if handoff_ref is not None and _git(
            "for-each-ref", "--format=%(objectname)", handoff_ref, cwd=self.remote
        ):
            return False
        old = self._remote_oid(key)
        if old is not None and old != self._owned_oids.get(key) and not steal:
            return False
        self.scratch.mkdir(parents=True, exist_ok=True)
        token = self.scratch / "claim-token"
        token.write_bytes(os.urandom(32))
        new = _git("hash-object", "-w", str(token), cwd=self.remote)
        try:
            _git("update-ref", self._ref(key), new, old or ("0" * len(new)), cwd=self.remote)
        except subprocess.CalledProcessError:
            return False
        if handoff_ref is not None and _git(
            "for-each-ref", "--format=%(objectname)", handoff_ref, cwd=self.remote
        ):
            if old is None:
                _git("update-ref", "-d", self._ref(key), new, cwd=self.remote)
            else:
                _git("update-ref", self._ref(key), old, new, cwd=self.remote)
            return False
        self._owned_oids[key] = new
        self._lease_ids[key] = os.urandom(32).hex()
        return True

    def holds(self, key: str) -> bool:
        return key in self._owned_oids and self._remote_oid(key) == self._owned_oids[key]

    def held_claim_oid(self, key: str) -> str | None:
        return self._owned_oids.get(key) if self.holds(key) else None

    def held_lease_id(self, key: str) -> str | None:
        return self._lease_ids.get(key) if self.holds(key) else None

    def held_claim_fence(self, key: str) -> ClaimFence | None:
        if not self.holds(key):
            return None
        return ClaimFence(
            key=key,
            ref=self._ref(key),
            oid=self._owned_oids[key],
            lease_id=self._lease_ids[key],
        )

    def renew(self, key: str, ttl: int | float = 60, *, lease_id: str | None = None) -> bool:
        old = self._owned_oids.get(key)
        if old is None or self._remote_oid(key) != old:
            return False
        if lease_id is not None and lease_id != self._lease_ids[key]:
            return False
        token = self.scratch / "claim-token"
        token.write_bytes(os.urandom(32))
        new = _git("hash-object", "-w", str(token), cwd=self.remote)
        try:
            _git("update-ref", self._ref(key), new, old, cwd=self.remote)
        except subprocess.CalledProcessError:
            return False
        self._owned_oids[key] = new
        return True

    def release(self, key: str) -> bool:
        owned_oid = self._owned_oids.get(key)
        if owned_oid is None:
            return True
        if self._remote_oid(key) != owned_oid:
            return False
        try:
            _git("update-ref", "-d", self._ref(key), owned_oid, cwd=self.remote)
        except subprocess.CalledProcessError:
            return False
        self._owned_oids.pop(key, None)
        self._lease_ids.pop(key, None)
        return True

    def heartbeat(self, key: str, *, interval: float = 300, ttl: int | float = 600) -> _StaticHeartbeat:
        return _StaticHeartbeat(self, key)


class _StaticHeartbeat:
    def __init__(self, board: _FencedTestClaimBoard, key: str) -> None:
        self.board = board
        self.key = key
        self.lost = threading.Event()

    def __enter__(self) -> _StaticHeartbeat:
        if not self.board.holds(self.key):
            self.lost.set()
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Autoform test",
            "GIT_AUTHOR_EMAIL": "autoform@example.test",
            "GIT_COMMITTER_NAME": "Autoform test",
            "GIT_COMMITTER_EMAIL": "autoform@example.test",
        },
    )
    return proc.stdout.strip()


def _git_bytes(*args: str, cwd: Path | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Autoform test",
            "GIT_AUTHOR_EMAIL": "autoform@example.test",
            "GIT_COMMITTER_NAME": "Autoform test",
            "GIT_COMMITTER_EMAIL": "autoform@example.test",
        },
    )
    return proc.stdout


def _replace_loose_object(manager: AttemptWorktrees, oid: str, object_type: str, content: bytes) -> None:
    object_path = manager.common_git_dir / "objects" / oid[:2] / oid[2:]
    assert object_path.is_file()
    object_path.chmod(0o600)
    object_path.write_bytes(zlib.compress(f"{object_type} {len(content)}\0".encode() + content))


@pytest.fixture
def git_repository(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    coordinator = tmp_path / "coordinator"
    _git("init", "--bare", "--quiet", str(remote))
    _git("init", "--quiet", "--initial-branch=main", str(seed))
    (seed / "book.txt").write_text("base\n", encoding="utf-8")
    (seed / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git("add", "book.txt", ".gitignore", cwd=seed)
    _git("commit", "--quiet", "-m", "base", cwd=seed)
    base = _git("rev-parse", "HEAD", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    _git("clone", "--quiet", str(remote), str(coordinator))
    return remote, seed, coordinator, base


def _candidate(
    manager: AttemptWorktrees,
    *,
    run_id: str,
    attempt_id: str,
    base: str,
    content: str,
) -> str:
    receipt = manager.prepare(run_id, attempt_id, base_oid=base)
    tree = Path(receipt.path)
    (tree / "book.txt").write_text(content, encoding="utf-8")
    _git("add", "book.txt", cwd=tree)
    _git("commit", "--quiet", "-m", attempt_id, cwd=tree)
    return manager.candidate_oid(run_id, attempt_id)


def _queue(
    manager: AttemptWorktrees,
    remote: Path,
    state: Path,
    worker: str,
) -> RemoteMergeQueue:
    board = _FencedTestClaimBoard(remote, worker, state / "test-claims")
    return RemoteMergeQueue(
        manager,
        remote_url=remote,
        state_root=state,
        worker_id=worker,
        claim_board=board,
        claim_ttl=600,
        heartbeat_interval=300,
    )


def _article_claim(queue: RemoteMergeQueue) -> ClaimFence:
    key = f"author/{queue.worker_id}"
    cached = getattr(queue, "_test_article_claim", None)
    if isinstance(cached, ClaimFence):
        return cached
    assert queue.claim_board.acquire(key, ttl=600)
    fence = queue.claim_board.held_claim_fence(key)
    assert fence is not None
    queue._test_article_claim = fence
    return fence


def test_scp_style_remote_without_user_is_not_treated_as_local() -> None:
    remote = "github.com:organization/repository.git"

    assert repository_module._normalize_remote(remote) == remote
    assert not repository_module._remote_is_local(remote)


@pytest.mark.parametrize("remote", ("ext::sh -c touch-owned", "evil://host/repository.git"))
def test_external_remote_helpers_are_rejected_before_state_creation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    remote: str,
) -> None:
    _, _, coordinator, _ = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    state = tmp_path / "queue-state"

    with pytest.raises(RepositoryError, match="remote helpers|URL scheme"):
        RemoteMergeQueue(manager, remote_url=remote, state_root=state, worker_id="worker-a")
    assert not state.exists()


def test_attempt_worktree_is_isolated_resumable_and_cleanup_is_owned(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    before = (coordinator / "book.txt").read_bytes()
    manager = AttemptWorktrees(coordinator, state)

    first = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert first.state == "ready"
    assert first.head_oid == base
    assert Path(first.path).parent.parent.parent == state / "worktrees"
    assert Path(first.path) != coordinator
    assert (coordinator / "book.txt").read_bytes() == before
    assert _git("status", "--porcelain", cwd=coordinator) == ""

    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    reopened = AttemptWorktrees(coordinator, state)
    resumed = reopened.prepare("run-1", "attempt-1", base_oid=base)
    assert resumed.identity_sha256 == first.identity_sha256
    assert resumed.head_oid == candidate
    with pytest.raises(WorktreeConflict, match="different base_oid"):
        reopened.prepare("run-1", "attempt-1", base_oid=candidate)
    assert (coordinator / "book.txt").read_bytes() == before

    dot_git = Path(first.path) / ".git"
    original_dot_git = dot_git.with_name(".git-original")
    dot_git.rename(original_dot_git)
    dot_git.write_bytes(original_dot_git.read_bytes())
    with pytest.raises(WorktreeConflict, match="Git identity was replaced"):
        reopened.inspect("run-1", "attempt-1")
    dot_git.unlink()
    original_dot_git.rename(dot_git)

    reopened.cleanup("run-1", "attempt-1")
    assert not Path(first.path).exists()
    assert (coordinator / "book.txt").read_bytes() == before


def test_attempt_worktree_must_remain_detached(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(receipt.path)

    _git("checkout", "--quiet", "--ignore-other-worktrees", "main", cwd=tree)
    with pytest.raises(WorktreeConflict, match="attached to a shared branch"):
        manager.inspect("run-1", "attempt-1")
    with pytest.raises(WorktreeConflict, match="attached to a shared branch"):
        manager.candidate_oid("run-1", "attempt-1")
    with pytest.raises(WorktreeConflict, match="attached to a shared branch"):
        manager.cleanup("run-1", "attempt-1")

    _git("checkout", "--quiet", "--detach", base, cwd=tree)
    manager.cleanup("run-1", "attempt-1")


def test_attempt_names_and_state_paths_cannot_escape_or_follow_symlinks(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    for run_id, attempt_id in (("../escape", "attempt"), ("run", "../../escape"), (".run", "attempt")):
        with pytest.raises(RepositoryError, match="safe portable"):
            manager.prepare(run_id, attempt_id, base_oid=base)

    real = tmp_path / "real-state"
    real.mkdir()
    linked = tmp_path / "linked-state"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(RepositoryError, match="symbolic link"):
        AttemptWorktrees(coordinator, linked)

    forbidden = coordinator / "autoform-state"
    with pytest.raises(RepositoryError, match="outside the coordinator checkout"):
        AttemptWorktrees(coordinator, forbidden)
    assert not forbidden.exists()

    queue_forbidden = coordinator / "queue-state"
    with pytest.raises(RepositoryError, match="outside the coordinator checkout"):
        RemoteMergeQueue(
            manager,
            remote_url=remote,
            state_root=queue_forbidden,
            worker_id="worker-a",
        )
    assert not queue_forbidden.exists()


def test_linked_coordinator_git_entry_substitution_fails_before_git_mutation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    linked = tmp_path / "linked"
    _git("worktree", "add", "--detach", "--quiet", str(linked), base, cwd=coordinator)
    manager = AttemptWorktrees(linked, tmp_path / "attempt-state")

    foreign = tmp_path / "foreign"
    foreign_linked = tmp_path / "foreign-linked"
    _git("clone", "--quiet", str(remote), str(foreign))
    _git("worktree", "add", "--detach", "--quiet", str(foreign_linked), base, cwd=foreign)
    foreign_before = _git("worktree", "list", "--porcelain", cwd=foreign)

    (linked / ".git").rename(linked / ".git.original")
    (linked / ".git").write_bytes((foreign_linked / ".git").read_bytes())

    with pytest.raises(RepositoryError, match=r"\.git entry was replaced"):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    assert _git("worktree", "list", "--porcelain", cwd=foreign) == foreign_before
    assert not (manager.worktree_root / "run-1").exists()


@pytest.mark.parametrize("boundary", ("worktree-scaffold-created", "worktree-tree-created"))
def test_pre_marker_worktree_creation_is_resumable(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert receipt.state == "ready"


@pytest.mark.parametrize("boundary", ("worktree-intent-recorded", "worktree-added"))
def test_interrupted_preparation_recovers_only_the_exact_registered_worktree(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    marker = state / "worktrees/run-1/attempt-1/attempt.json"
    assert '"state":"preparing"' in marker.read_text(encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    recovered = AttemptWorktrees(coordinator, state).prepare("run-1", "attempt-1", base_oid=base)
    assert recovered.state == "ready"
    assert recovered.head_oid == base


def test_interrupted_registered_checkout_repairs_only_absent_tracked_paths(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-added":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    tracked = state / "worktrees/run-1/attempt-1/tree/book.txt"
    tracked.unlink()

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    recovered = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert recovered.state == "ready"
    assert tracked.read_text(encoding="utf-8") == "base\n"


def test_interrupted_registered_checkout_preserves_untracked_content(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-added":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    untracked = state / "worktrees/run-1/attempt-1/tree/preserve.txt"
    untracked.write_text("preserve\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    with pytest.raises(WorktreeUncertain, match="changes beyond absent tracked paths"):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    assert untracked.read_text(encoding="utf-8") == "preserve\n"


def test_interrupted_checkout_does_not_overwrite_a_concurrent_path(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-added":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    tracked = state / "worktrees/run-1/attempt-1/tree/book.txt"
    tracked.unlink()

    def create_concurrent_path(name: str) -> None:
        if name == "worktree-missing-path-verified":
            tracked.write_text("foreign concurrent data\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", create_concurrent_path)
    with pytest.raises(WorktreeUncertain, match="could not be restored"):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    assert tracked.read_text(encoding="utf-8") == "foreign concurrent data\n"


def test_interrupted_checkout_repairs_a_pathspec_magic_filename(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    magic_name = ":(literal)proof.txt"
    (coordinator / magic_name).write_text("literal\n", encoding="utf-8")
    _git("add", "--", f"./{magic_name}", cwd=coordinator)
    _git("commit", "--quiet", "-m", "add literal pathspec name", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-added":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    tracked = state / f"worktrees/run-1/attempt-1/tree/{magic_name}"
    tracked.unlink()

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    recovered = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert recovered.state == "ready"
    assert tracked.read_text(encoding="utf-8") == "literal\n"


def test_pre_marker_atomic_write_orphan_does_not_block_worktree_recovery(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-tree-created":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    marker = state / "worktrees/run-1/attempt-1/attempt.json"
    orphan = marker.parent / f"{repository_module._atomic_write_prefix(marker)}deadbeef.tmp"
    orphan.write_bytes(b'{"partial":')
    orphan.chmod(0o600)

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    recovered = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert recovered.state == "ready"
    assert not orphan.exists()


def test_pre_marker_atomic_write_recovery_preserves_unsafe_reserved_path(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-tree-created":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    marker = state / "worktrees/run-1/attempt-1/attempt.json"
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    reserved = marker.parent / f"{repository_module._atomic_write_prefix(marker)}foreign.tmp"
    reserved.symlink_to(sentinel)

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    with pytest.raises(RepositoryError, match="not a safe orphan"):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    assert reserved.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_atomic_write_orphan_swap_is_quarantined_and_preserved(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == "worktree-tree-created":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    marker = state / "worktrees/run-1/attempt-1/attempt.json"
    prefix = repository_module._atomic_write_prefix(marker)
    orphan = marker.parent / f"{prefix}deadbeef.tmp"
    orphan.write_bytes(b'{"partial":')
    orphan.chmod(0o600)
    original = tmp_path / "original-orphan"
    replacement = tmp_path / "replacement"
    replacement.write_text("foreign replacement\n", encoding="utf-8")
    replacement.chmod(0o600)
    original_rename_noreplace = repository_module._rename_noreplace

    def swap_before_quarantine(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(source) == orphan:
            os.rename(orphan, original)
            os.rename(replacement, orphan)
        original_rename_noreplace(Path(source), Path(destination))

    monkeypatch.setattr(repository_module, "_rename_noreplace", swap_before_quarantine)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    with pytest.raises(RepositoryError, match="changed while being quarantined"):
        manager.prepare("run-1", "attempt-1", base_oid=base)

    quarantined = list(marker.parent.glob(f"{prefix}quarantine-*.tmp"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "foreign replacement\n"
    assert original.read_bytes() == b'{"partial":'


def test_atomic_write_orphan_quarantine_is_resumable(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt_creation(name: str) -> None:
        if name == "worktree-tree-created":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt_creation)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    marker = state / "worktrees/run-1/attempt-1/attempt.json"
    prefix = repository_module._atomic_write_prefix(marker)
    orphan = marker.parent / f"{prefix}deadbeef.tmp"
    orphan.write_bytes(b'{"partial":')
    orphan.chmod(0o600)
    original_rename_noreplace = repository_module._rename_noreplace

    def interrupt_after_quarantine(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        original_rename_noreplace(Path(source), Path(destination))
        if Path(source) == orphan:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_rename_noreplace", interrupt_after_quarantine)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)
    quarantined = list(marker.parent.glob(f"{prefix}quarantine-*.tmp"))
    assert len(quarantined) == 1

    monkeypatch.setattr(repository_module, "_rename_noreplace", original_rename_noreplace)
    recovered = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert recovered.state == "ready"
    assert not quarantined[0].exists()


def test_atomic_write_orphan_swap_after_validation_preserves_both_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = state / "attempt.json"
    prefix = repository_module._atomic_write_prefix(target)
    orphan = state / f"{prefix}deadbeef.tmp"
    orphan.write_bytes(b'{"partial":')
    orphan.chmod(0o600)
    original = tmp_path / "original-orphan"
    replacement = tmp_path / "replacement"
    replacement.write_text("foreign replacement\n", encoding="utf-8")
    replacement.chmod(0o600)
    original_snapshot = repository_module._atomic_state_file_snapshot
    swapped = False

    def swap_after_validation(
        path: Path,
        *,
        expected: tuple[int, int] | None,
        label: str,
    ) -> repository_module._CandidateFileSnapshot:
        nonlocal swapped
        snapshot = original_snapshot(path, expected=expected, label=label)
        if not swapped and "quarantine-" in path.name:
            os.rename(path, original)
            os.rename(replacement, path)
            swapped = True
        return snapshot

    monkeypatch.setattr(repository_module, "_atomic_state_file_snapshot", swap_after_validation)
    with pytest.raises(RepositoryError, match="changed while being quarantined"):
        repository_module._remove_atomic_write_orphans(target)

    quarantined = list(state.glob(f"{prefix}quarantine-*.tmp"))
    assert swapped
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "foreign replacement\n"
    assert original.read_bytes() == b'{"partial":'


def test_atomic_write_final_cleanup_preserves_replacement_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    original = tmp_path / "original-temporary"
    replacement = tmp_path / "replacement"
    replacement.write_text("foreign replacement\n", encoding="utf-8")
    replacement.chmod(0o600)
    original_replace = os.replace
    temporary: Path | None = None

    def replace_after_validation(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        nonlocal temporary
        if Path(destination) == target:
            temporary = Path(source)
            os.rename(source, original)
            os.rename(replacement, source)
            raise OSError("injected replacement")
        original_replace(source, destination)

    monkeypatch.setattr(repository_module.os, "replace", replace_after_validation)
    with pytest.raises(RepositoryError, match="contains a replacement"):
        repository_module._write_bytes_file(target, b"owned state\n")

    assert temporary is not None
    assert temporary.read_text(encoding="utf-8") == "foreign replacement\n"
    assert original.read_bytes() == b"owned state\n"
    assert not target.exists()


@pytest.mark.parametrize(
    "boundary",
    (
        "worktree-scaffold-created",
        "worktree-tree-created",
        "worktree-intent-recorded",
        "worktree-added",
    ),
)
def test_cleanup_resumes_interrupted_preparation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _, _, coordinator, base = git_repository
    state = tmp_path / "state"
    manager = AttemptWorktrees(coordinator, state)

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare("run-1", "attempt-1", base_oid=base)

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    manager.cleanup("run-1", "attempt-1")
    assert not (state / "worktrees/run-1").exists()
    assert str(state / "worktrees/run-1/attempt-1/tree") not in _git("worktree", "list", "--porcelain", cwd=coordinator)


def test_cleanup_refuses_replaced_or_foreign_attempt_directories(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(receipt.path)
    original = tree.with_name("original-tree")
    tree.rename(original)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    tree.symlink_to(foreign, target_is_directory=True)

    with pytest.raises((RepositoryError, WorktreeConflict)):
        manager.cleanup("run-1", "attempt-1")
    assert sentinel.read_text(encoding="utf-8") == "keep"

    tree.unlink()
    original.rename(tree)
    manager.cleanup("run-1", "attempt-1")


def test_cleanup_refuses_untracked_and_ignored_content_inside_owned_worktree(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(receipt.path)
    untracked = tree / "foreign/keep.txt"
    ignored = tree / "ignored/keep.txt"
    untracked.parent.mkdir()
    ignored.parent.mkdir()
    untracked.write_text("keep", encoding="utf-8")
    ignored.write_text("keep", encoding="utf-8")

    with pytest.raises(WorktreeConflict, match="untracked, or ignored"):
        manager.cleanup("run-1", "attempt-1")
    assert untracked.read_text(encoding="utf-8") == "keep"
    assert ignored.read_text(encoding="utf-8") == "keep"

    untracked.unlink()
    ignored.unlink()
    ignored.parent.rmdir()
    with pytest.raises(WorktreeConflict, match="unowned path"):
        manager.cleanup("run-1", "attempt-1")
    assert untracked.parent.is_dir()
    untracked.parent.rmdir()
    manager.cleanup("run-1", "attempt-1")


def test_cleanup_quarantines_late_foreign_content_instead_of_deleting_it(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    quarantine = Path(receipt.path).with_name("tree-cleaning")
    late = quarantine / "ignored/late.txt"

    def inject(name: str) -> None:
        if name == "worktree-quarantined":
            late.parent.mkdir()
            late.write_text("preserve\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", inject)
    with pytest.raises(WorktreeConflict, match="preserves foreign state"):
        manager.cleanup("run-1", "attempt-1")
    assert late.read_text(encoding="utf-8") == "preserve\n"

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    late.unlink()
    late.parent.rmdir()
    manager.cleanup("run-1", "attempt-1")
    assert not quarantine.exists()


def test_cleanup_preserves_late_tracked_mode_change(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    quarantine = Path(receipt.path).with_name("tree-cleaning")
    tracked = quarantine / "book.txt"

    def change_mode(name: str) -> None:
        if name == "worktree-quarantined":
            tracked.chmod(0o755)

    monkeypatch.setattr(repository_module, "_checkpoint", change_mode)
    with pytest.raises(WorktreeConflict, match="type or mode changed"):
        manager.cleanup("run-1", "attempt-1")
    assert tracked.is_file()
    assert tracked.stat().st_mode & stat.S_IXUSR

    tracked.chmod(0o644)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    manager.cleanup("run-1", "attempt-1")
    assert not quarantine.exists()


def test_cleanup_preserves_worktree_path_replacement_before_quarantine(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(receipt.path)
    owned_tree = tmp_path / "owned-tree"
    cleanup_tree = tree.with_name("tree-cleaning")

    def replace_tree(name: str) -> None:
        if name == "worktree-cleanup-before-quarantine":
            tree.rename(owned_tree)
            tree.mkdir(mode=0o700)
            tree.joinpath("foreign.txt").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_tree)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    assert owned_tree.joinpath("book.txt").read_text(encoding="utf-8") == "base\n"
    assert cleanup_tree.joinpath("foreign.txt").read_text(encoding="utf-8") == "foreign\n"


def test_cleanup_preserves_tracked_path_replacement_before_removal(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    cleanup_tree = Path(receipt.path).with_name("tree-cleaning")
    owned_book = tmp_path / "owned-book.txt"

    def replace_book(name: str) -> None:
        if name == "worktree-cleanup-before-path-quarantine:book.txt":
            book = cleanup_tree / "book.txt"
            book.rename(owned_book)
            book.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_book)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    preserved = list(cleanup_tree.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "foreign\n"
    assert owned_book.read_text(encoding="utf-8") == "base\n"


def test_cleanup_preserves_git_file_replacement_before_removal(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    cleanup_tree = Path(receipt.path).with_name("tree-cleaning")
    owned_git = tmp_path / "owned-dot-git"

    def replace_git(name: str) -> None:
        if name == "worktree-cleanup-before-git-quarantine":
            dot_git = cleanup_tree / ".git"
            dot_git.rename(owned_git)
            dot_git.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_git)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    preserved = list(cleanup_tree.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "foreign\n"
    assert owned_git.read_text(encoding="utf-8").startswith("gitdir: ")


def test_cleanup_preserves_final_tree_replacement_before_removal(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(receipt.path)
    cleanup_tree = tree.with_name("tree-cleaning")
    owned_tree = tmp_path / "owned-empty-tree"

    def replace_tree(name: str) -> None:
        if name == "worktree-cleanup-before-final-quarantine":
            cleanup_tree.rename(owned_tree)
            cleanup_tree.mkdir(mode=0o700)
            cleanup_tree.joinpath("foreign.txt").write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_tree)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    preserved = list(cleanup_tree.parent.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].joinpath("foreign.txt").read_text(encoding="utf-8") == "foreign\n"
    assert owned_tree.is_dir()


def test_cleanup_preserves_marker_replacement_before_removal(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    attempt_root = Path(receipt.path).parent
    marker = attempt_root / "attempt.json"
    owned_marker = tmp_path / "owned-attempt.json"

    def replace_marker(name: str) -> None:
        if name == "worktree-cleanup-before-marker-quarantine":
            marker.rename(owned_marker)
            marker.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_marker)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    preserved = list(attempt_root.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "foreign\n"
    assert json.loads(owned_marker.read_bytes())["state"] == "cleaning"


def test_cleanup_preserves_candidate_journal_replacement_before_removal(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )
    journal = tree.parent / "candidate.json"
    owned_journal = tmp_path / "owned-candidate.json"

    def replace_journal(name: str) -> None:
        if name == "candidate-cleanup-before-journal-quarantine":
            journal.rename(owned_journal)
            journal.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_journal)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    preserved = list(journal.parent.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "foreign\n"
    assert json.loads(owned_journal.read_bytes())["state"] == "ready"


def test_private_file_removal_preserves_replacement_after_quarantine_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "owned.json"
    target.write_bytes(b"owned\n")
    expected = repository_module._candidate_private_file_snapshot(target, label="test file")
    owned = tmp_path / "saved-owned.json"
    original_snapshot = repository_module._candidate_bound_file_snapshot
    swapped = False

    def swap_after_snapshot(
        path: Path,
        *,
        expected: tuple[int, int],
        label: str,
    ) -> object:
        nonlocal swapped
        snapshot = original_snapshot(path, expected=expected, label=label)
        if not swapped and path.name.startswith(".autoform-removing-"):
            path.rename(owned)
            path.write_bytes(b"foreign\n")
            swapped = True
        return snapshot

    monkeypatch.setattr(repository_module, "_candidate_bound_file_snapshot", swap_after_snapshot)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        repository_module._remove_bound_private_file(
            target,
            expected,
            label="test file",
            checkpoint="test-file-quarantine",
        )

    preserved = list(tmp_path.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == b"foreign\n"
    assert owned.read_bytes() == b"owned\n"


def test_empty_directory_removal_preserves_replacement_after_quarantine_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "owned-directory"
    target.mkdir()
    expected = repository_module._directory_identity(target)
    owned = tmp_path / "saved-owned-directory"
    original_identity = repository_module._directory_identity
    swapped = False

    def swap_after_identity(path: Path) -> tuple[int, int]:
        nonlocal swapped
        identity = original_identity(path)
        if not swapped and path.name.startswith(".autoform-removing-"):
            path.rename(owned)
            path.mkdir()
            path.joinpath("foreign.txt").write_text("foreign\n", encoding="utf-8")
            swapped = True
        return identity

    monkeypatch.setattr(repository_module, "_directory_identity", swap_after_identity)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        repository_module._remove_bound_empty_directory(
            target,
            expected,
            label="test directory",
            checkpoint="test-directory-quarantine",
        )

    preserved = list(tmp_path.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].joinpath("foreign.txt").read_text(encoding="utf-8") == "foreign\n"
    assert owned.is_dir()


def test_cleanup_preserves_tracked_replacement_after_quarantine_validation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    cleanup_tree = Path(receipt.path).with_name("tree-cleaning")
    owned_book = tmp_path / "saved-owned-book.txt"
    original_read = repository_module._read_candidate_regular_file
    swapped = False

    def swap_after_read(
        path: Path,
        expected: os.stat_result,
        relative: str,
    ) -> tuple[bytes, tuple[int, ...]]:
        nonlocal swapped
        result = original_read(path, expected, relative)
        if not swapped and relative == "book.txt" and path.name.startswith(".autoform-removing-"):
            path.rename(owned_book)
            path.write_text("foreign\n", encoding="utf-8")
            swapped = True
        return result

    monkeypatch.setattr(repository_module, "_read_candidate_regular_file", swap_after_read)
    with pytest.raises(WorktreeConflict, match="replaced; its quarantine is preserved"):
        manager.cleanup("run-1", "attempt-1")

    preserved = list(cleanup_tree.glob(".autoform-removing-*"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "foreign\n"
    assert owned_book.read_text(encoding="utf-8") == "base\n"


def test_cleanup_accepts_canonical_executable_and_symlink_modes(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, _ = git_repository
    executable = coordinator / "build.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (coordinator / "book-link").symlink_to("book.txt")
    _git("add", "build.sh", "book-link", cwd=coordinator)
    _git("commit", "--quiet", "-m", "add mode fixtures", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")

    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    manager.cleanup("run-1", "attempt-1")
    assert not Path(receipt.path).exists()


def test_cleanup_quarantine_name_does_not_expand_long_tracked_filename(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, _ = git_repository
    long_name = "x" * 240
    coordinator.joinpath(long_name).write_text("long path\n", encoding="utf-8")
    _git("add", long_name, cwd=coordinator)
    _git("commit", "--quiet", "-m", "long path fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")

    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    manager.cleanup("run-1", "attempt-1")

    assert not Path(receipt.path).exists()


@pytest.mark.parametrize(
    "boundary",
    (
        "worktree-cleanup-intent-recorded",
        "worktree-cleanup-before-quarantine",
        "worktree-quarantined",
        "worktree-cleanup-before-path-quarantine:.gitignore",
        "worktree-cleanup-before-git-quarantine",
        "worktree-cleanup-before-final-quarantine",
        "worktree-quarantine-removed",
        "worktree-removed",
        "worktree-cleanup-before-marker-quarantine",
        "worktree-marker-removed",
        "worktree-cleanup-before-attempt-quarantine",
        "worktree-cleanup-before-run-quarantine",
    ),
)
def test_cleanup_resumes_at_every_durable_boundary(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.cleanup("run-1", "attempt-1")
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    manager.cleanup("run-1", "attempt-1")
    assert not Path(receipt.path).exists()
    assert Path(receipt.path) not in manager._listed_worktree_paths()


@pytest.mark.parametrize("already_unregistered", (False, True))
def test_cleanup_restores_parent_mode_after_process_kill(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    already_unregistered: bool,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    attempt_root = Path(receipt.path).parent

    def interrupt(name: str) -> None:
        if name == "worktree-quarantine-removed":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.cleanup("run-1", "attempt-1")
    if already_unregistered:
        _git("worktree", "remove", receipt.path, cwd=coordinator)
    attempt_root.chmod(0o500)

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    manager.cleanup("run-1", "attempt-1")
    assert not attempt_root.exists()


def test_empty_pre_marker_attempt_scaffolds_are_resumable(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    scaffold = manager.worktree_root / "run-1/attempt-1"
    (scaffold / "tree").mkdir(parents=True)

    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    assert receipt.state == "ready"
    manager.cleanup("run-1", "attempt-1")

    empty = manager.worktree_root / "run-2/attempt-1"
    empty.mkdir(parents=True)
    manager.cleanup("run-2", "attempt-1")
    assert not empty.exists()


def test_index_flags_cannot_hide_modified_content_or_cleanup_data_loss(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(receipt.path)
    tracked = tree / "book.txt"

    _git("update-index", "--assume-unchanged", "book.txt", cwd=tree)
    tracked.write_text("hidden modification\n", encoding="utf-8")
    assert _git("status", "--porcelain=v1", cwd=tree) == ""
    with pytest.raises(WorktreeConflict, match="noncanonical flags"):
        manager.candidate_oid("run-1", "attempt-1")
    with pytest.raises(WorktreeConflict, match="noncanonical flags"):
        manager.cleanup("run-1", "attempt-1")
    assert tracked.read_text(encoding="utf-8") == "hidden modification\n"

    _git("update-index", "--no-assume-unchanged", "book.txt", cwd=tree)
    tracked.write_text("base\n", encoding="utf-8")
    _git("update-index", "--skip-worktree", "book.txt", cwd=tree)
    with pytest.raises(WorktreeConflict, match="noncanonical flags"):
        manager.candidate_oid("run-1", "attempt-1")
    with pytest.raises(WorktreeConflict, match="noncanonical flags"):
        manager.cleanup("run-1", "attempt-1")
    assert tracked.exists()

    _git("update-index", "--no-skip-worktree", "book.txt", cwd=tree)
    manager.cleanup("run-1", "attempt-1")


def test_repository_fsmonitor_hook_is_disabled_for_integrity_checks(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    manager.prepare("run-1", "attempt-1", base_oid=base)
    sentinel = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text('#!/bin/sh\ntouch "$(dirname "$0")/fsmonitor-ran"\n', encoding="utf-8")
    hook.chmod(0o700)
    _git("config", "core.fsmonitor", str(hook), cwd=coordinator)

    assert manager.candidate_oid("run-1", "attempt-1") == base
    assert not sentinel.exists()
    manager.cleanup("run-1", "attempt-1")
    assert not sentinel.exists()


def test_candidate_commit_is_deterministic_idempotent_and_preserves_allowed_changes(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, _ = git_repository
    (coordinator / "delete.txt").write_text("delete me\n", encoding="utf-8")
    script = coordinator / "script.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    _git("add", "delete.txt", "script.sh", cwd=coordinator)
    _git("commit", "--quiet", "-m", "candidate fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    allowed = {"book.txt", "delete.txt", "script.sh", ":(literal)proof.txt"}
    arguments = {
        "allowed_paths": allowed,
        "message": "Autoform candidate\n",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    first = manager.prepare("run-1", "attempt-1", base_oid=base)
    tree = Path(first.path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    (tree / "delete.txt").unlink()
    (tree / "script.sh").chmod(0o755)
    (tree / ":(literal)proof.txt").write_text("literal path\n", encoding="utf-8")

    receipt = manager.commit_candidate("run-1", "attempt-1", **arguments)
    repeated = manager.commit_candidate("run-1", "attempt-1", **arguments)
    inspected = manager.inspect_candidate("run-1", "attempt-1")

    assert receipt == repeated == inspected
    assert receipt.state == "ready"
    assert _git("rev-list", "--parents", "-n", "1", receipt.candidate_oid, cwd=tree).split() == [
        receipt.candidate_oid,
        base,
    ]
    assert _git("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all", cwd=tree) == ""
    assert _git("ls-tree", receipt.candidate_oid, "script.sh", cwd=tree).startswith("100755 blob ")
    assert _git("ls-tree", receipt.candidate_oid, "delete.txt", cwd=tree) == ""
    assert _git("show", f"{receipt.candidate_oid}:./:(literal)proof.txt", cwd=tree) == "literal path"
    with pytest.raises(CandidateUncertain, match="different message_sha256"):
        manager.commit_candidate("run-1", "attempt-1", **{**arguments, "message": "different"})
    with pytest.raises(CandidateUncertain, match="different allowed_paths"):
        manager.commit_candidate("run-1", "attempt-1", **{**arguments, "allowed_paths": {"book.txt"}})

    second = manager.prepare("run-2", "attempt-1", base_oid=base)
    second_tree = Path(second.path)
    (second_tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    (second_tree / "delete.txt").unlink()
    (second_tree / "script.sh").chmod(0o755)
    (second_tree / ":(literal)proof.txt").write_text("literal path\n", encoding="utf-8")
    deterministic = manager.commit_candidate("run-2", "attempt-1", **arguments)
    assert deterministic.candidate_oid == receipt.candidate_oid

    manager.cleanup("run-1", "attempt-1")
    with pytest.raises(CandidateNotFound):
        manager.inspect_candidate("run-1", "attempt-1")


def test_candidate_creation_batches_object_requests_and_limits_full_snapshots(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    for index in range(99):
        coordinator.joinpath(f"tracked-{index:03}.txt").write_text(f"base {index}\n", encoding="utf-8")
    _git("add", ".", cwd=coordinator)
    _git("commit", "--quiet", "-m", "scale fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    allowed = {f"tracked-{index:03}.txt" for index in range(20)}
    for relative in allowed:
        tree.joinpath(relative).write_text(f"candidate {relative}\n", encoding="utf-8")

    snapshot_count = 0
    object_batch_count = 0
    largest_batch = 0
    original_snapshot = manager._candidate_snapshot
    original_batch = manager._verify_candidate_object_batch

    def counted_snapshot(*args: object, **kwargs: object) -> object:
        nonlocal snapshot_count
        snapshot_count += 1
        return original_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    def counted_batch(
        objects: tuple[tuple[str, str], ...],
        *,
        label: str,
        expected_contents: tuple[bytes, ...] | None = None,
        object_directory: Path | None = None,
    ) -> tuple[str | None, ...]:
        nonlocal object_batch_count, largest_batch
        object_batch_count += 1
        largest_batch = max(largest_batch, len(objects))
        return original_batch(
            objects,
            label=label,
            expected_contents=expected_contents,
            object_directory=object_directory,
        )

    monkeypatch.setattr(manager, "_candidate_snapshot", counted_snapshot)
    monkeypatch.setattr(manager, "_verify_candidate_object_batch", counted_batch)
    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths=allowed,
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert candidate.state == "ready"
    assert snapshot_count == 2
    assert object_batch_count <= 12
    assert largest_batch >= len(allowed)


def test_candidate_durable_closure_does_not_scale_with_repository_history(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    history = coordinator / "history.txt"
    for index in range(25):
        history.write_text(f"history {index}\n", encoding="utf-8")
        _git("add", "history.txt", cwd=coordinator)
        _git("commit", "--quiet", "-m", f"history {index}", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    historical_oids = set(_git("rev-list", "--all", cwd=coordinator).splitlines())
    _git("gc", "--quiet", "--prune=now", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    imported: list[tuple[tuple[str, str], ...]] = []
    verified_packs = 0
    digested_pack_bytes = 0
    original_import = manager._import_candidate_closure
    original_verify_pack = manager._verify_primary_pack_output
    original_pack_digest = repository_module._primary_pack_file_digest

    def capture_import(objects: tuple[tuple[str, str], ...]) -> None:
        imported.append(objects)
        original_import(objects)

    def capture_verify_pack(pack: Path, index: Path) -> frozenset[tuple[str, str]]:
        nonlocal verified_packs
        verified_packs += 1
        return original_verify_pack(pack, index)

    def capture_pack_digest(path: Path, *, label: str) -> tuple[tuple[int, ...], str]:
        nonlocal digested_pack_bytes
        digested_pack_bytes += path.stat().st_size
        return original_pack_digest(path, label=label)

    monkeypatch.setattr(manager, "_import_candidate_closure", capture_import)
    monkeypatch.setattr(manager, "_verify_primary_pack_output", capture_verify_pack)
    monkeypatch.setattr(repository_module, "_primary_pack_file_digest", capture_pack_digest)
    first = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )
    second_tree = Path(manager.prepare("run-2", "attempt-1", base_oid=base).path)
    second_tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    second = manager.commit_candidate(
        "run-2",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert first.candidate_oid == second.candidate_oid
    assert len(imported) == 2
    assert all(len(objects) <= 8 for objects in imported)
    assert all(historical_oids.difference(oid for oid, _ in objects) for objects in imported)
    assert verified_packs == 0
    assert digested_pack_bytes == 0


def test_candidate_types_are_exported_from_worker_package() -> None:
    assert autoform_worker.CandidateError is repository_module.CandidateError
    assert autoform_worker.CandidateNotFound is repository_module.CandidateNotFound
    assert autoform_worker.CandidateReceipt is repository_module.CandidateReceipt
    assert autoform_worker.CandidateUncertain is repository_module.CandidateUncertain
    assert {
        "CandidateError",
        "CandidateNotFound",
        "CandidateReceipt",
        "CandidateUncertain",
    } <= set(autoform_worker.__all__)


def test_candidate_commit_rejects_unallowed_ignored_symlink_special_and_reserved_paths(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    arguments = {
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    outside = Path(manager.prepare("run-1", "outside", base_oid=base).path)
    (outside / "book.txt").write_text("allowed\n", encoding="utf-8")
    (outside / ".gitignore").write_text("changed outside allowlist\n", encoding="utf-8")
    with pytest.raises(CandidateUncertain, match="outside the allowed set"):
        manager.commit_candidate("run-1", "outside", allowed_paths={"book.txt"}, **arguments)

    ignored = Path(manager.prepare("run-1", "ignored", base_oid=base).path)
    (ignored / "book.txt").write_text("allowed\n", encoding="utf-8")
    (ignored / "ignored").mkdir()
    (ignored / "ignored/output.txt").write_text("foreign\n", encoding="utf-8")
    with pytest.raises(CandidateUncertain, match="outside the allowed set"):
        manager.commit_candidate("run-1", "ignored", allowed_paths={"book.txt"}, **arguments)

    linked = Path(manager.prepare("run-1", "linked", base_oid=base).path)
    (linked / "book.txt").unlink()
    (linked / "book.txt").symlink_to(".gitignore")
    with pytest.raises(CandidateUncertain, match="symbolic link"):
        manager.commit_candidate("run-1", "linked", allowed_paths={"book.txt"}, **arguments)

    special = Path(manager.prepare("run-1", "special", base_oid=base).path)
    os.mkfifo(special / "pipe")
    with pytest.raises(CandidateUncertain, match="special file"):
        manager.commit_candidate("run-1", "special", allowed_paths={"pipe"}, **arguments)

    with pytest.raises(RepositoryError, match="reserved"):
        manager.commit_candidate("run-1", "special", allowed_paths={".git/config"}, **arguments)
    with pytest.raises(RepositoryError, match="explicit finite set"):
        manager.commit_candidate("run-1", "special", allowed_paths=["pipe"], **arguments)  # type: ignore[arg-type]


def test_candidate_commit_ignores_ordinary_generated_lake_output_before_and_after_snapshot(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    coordinator.joinpath(".gitignore").write_text("ignored/\n.lake/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=coordinator)
    _git("commit", "--quiet", "-m", "ignore Lake output", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    before = tree / ".lake/build/lib/lean/Before.olean"
    before.parent.mkdir(parents=True)
    before.write_bytes(b"generated before snapshot")
    after = tree / ".lake/build/lib/lean/After.olean"

    def generate_after_snapshot(name: str) -> None:
        if name == "candidate-objects-written":
            after.write_bytes(b"generated after snapshot")

    monkeypatch.setattr(repository_module, "_checkpoint", generate_after_snapshot)
    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert candidate.state == "ready"
    assert manager.inspect_candidate("run-1", "attempt-1") == candidate
    assert before.read_bytes() == b"generated before snapshot"
    assert after.read_bytes() == b"generated after snapshot"
    assert _git("ls-tree", candidate.candidate_oid, ".lake", cwd=tree) == ""


def test_candidate_commit_rejects_changed_tracked_file_under_generated_directory(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, _ = git_repository
    tracked = coordinator / ".lake/tracked.txt"
    tracked.parent.mkdir()
    tracked.write_text("tracked base\n", encoding="utf-8")
    _git("add", "-f", ".lake/tracked.txt", cwd=coordinator)
    _git("commit", "--quiet", "-m", "tracked lake fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    tree.joinpath(".lake/tracked.txt").write_text("changed outside allowlist\n", encoding="utf-8")

    with pytest.raises(CandidateUncertain, match="outside the allowed set"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )


def test_candidate_commit_ignores_new_generated_output_beside_tracked_generated_path(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    tracked = coordinator / ".lake/tracked.txt"
    tracked.parent.mkdir()
    tracked.write_text("tracked base\n", encoding="utf-8")
    _git("add", "-f", ".lake/tracked.txt", cwd=coordinator)
    _git("commit", "--quiet", "-m", "tracked lake fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    generated = tree / ".lake/build/lib/lean/Generated.olean"

    def generate_after_snapshot(name: str) -> None:
        if name == "candidate-objects-written":
            generated.parent.mkdir(parents=True)
            generated.write_bytes(b"generated after snapshot")

    monkeypatch.setattr(repository_module, "_checkpoint", generate_after_snapshot)
    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert candidate.state == "ready"
    assert generated.read_bytes() == b"generated after snapshot"
    assert _git("show", f"{candidate.candidate_oid}:.lake/tracked.txt", cwd=tree) == "tracked base"


def test_candidate_commit_rejects_hardlinked_allowed_file_and_preserves_evidence(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    outside = tmp_path / "outside.txt"
    outside.write_text("candidate from outside\n", encoding="utf-8")
    (tree / "book.txt").unlink()
    os.link(outside, tree / "book.txt")

    with pytest.raises(CandidateUncertain, match="private regular file"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert _git("rev-parse", "HEAD", cwd=tree) == base
    assert outside.read_text(encoding="utf-8") == "candidate from outside\n"
    assert (tree / "book.txt").samefile(outside)


@pytest.mark.parametrize("replacement", ("symlink", "hardlink"))
def test_candidate_commit_rejects_nonprivate_worktree_index(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    replacement: str,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / f"state-{replacement}")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    index = git_dir / "index"
    outside = tmp_path / f"outside-index-{replacement}"
    if replacement == "symlink":
        index.rename(outside)
        index.symlink_to(outside)
    else:
        os.link(index, outside)
    before = outside.read_bytes()

    with pytest.raises(CandidateUncertain, match="index|private regular file"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert outside.read_bytes() == before
    assert (tree / "book.txt").read_text(encoding="utf-8") == "candidate\n"


@pytest.mark.parametrize(
    ("control", "replacement"),
    (("HEAD", "hardlink"), ("commondir", "symlink"), ("gitdir", "hardlink"), ("logs/HEAD", "hardlink")),
)
def test_candidate_commit_rejects_nonprivate_worktree_admin_controls(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    control: str,
    replacement: str,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / f"state-{control}-{replacement}")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    target = git_dir.joinpath(*control.split("/"))
    outside = tmp_path / f"outside-{control.replace('/', '-')}"
    target.rename(outside)
    if replacement == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    before = outside.read_bytes()

    with pytest.raises(CandidateUncertain, match="private regular file"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert outside.read_bytes() == before


@pytest.mark.parametrize("control", ("logs", "refs"))
def test_candidate_commit_rejects_aliased_worktree_admin_directories(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    control: str,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / f"state-{control}")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    target = git_dir / control
    target.mkdir(exist_ok=True)
    outside = tmp_path / f"outside-{control}"
    target.rename(outside)
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CandidateUncertain, match="real canonical directory"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert outside.is_dir()


def test_candidate_commit_rejects_foreign_head_lock(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    lock = git_dir / "HEAD.lock"
    lock.write_bytes(b"foreign HEAD transaction")

    with pytest.raises(CandidateUncertain, match="HEAD lock contains foreign state"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert lock.read_bytes() == b"foreign HEAD transaction"


def test_candidate_commit_rechecks_head_reflog_before_ref_update(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    head_log = git_dir / "logs" / "HEAD"
    outside = tmp_path / "outside-head-log"
    before = head_log.read_bytes()

    def replace_head_log(name: str) -> None:
        if name == "candidate-objects-written":
            head_log.rename(outside)
            os.link(outside, head_log)

    monkeypatch.setattr(repository_module, "_checkpoint", replace_head_log)
    with pytest.raises(CandidateUncertain, match="private regular file"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert outside.read_bytes() == before
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_commit_preserves_and_rejects_foreign_index_lock(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    lock = git_dir / "index.lock"
    lock.write_bytes(b"foreign index transaction")

    with pytest.raises(CandidateUncertain, match="index lock already exists"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert lock.read_bytes() == b"foreign index transaction"
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_recovery_rejects_same_byte_foreign_index_lock(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"book.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    def interrupt(name: str) -> None:
        if name == "candidate-index-staged":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.commit_candidate("run-1", "attempt-1", **arguments)

    journal = manager.worktree_root / "run-1" / "attempt-1" / "candidate.json"
    record = json.loads(journal.read_text(encoding="utf-8"))
    git_dir = Path(record["git_dir"])
    stage = git_dir / record["candidate_index_stage_name"]
    lock = git_dir / "index.lock"
    lock.write_bytes(stage.read_bytes())
    stage.unlink()
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    with pytest.raises(CandidateUncertain, match="foreign state"):
        manager.commit_candidate("run-1", "attempt-1", **arguments)


def test_candidate_recovery_preserves_replaced_unfinished_index_stage(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"book.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    def interrupt(name: str) -> None:
        if name == "candidate-index-stage-created":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.commit_candidate("run-1", "attempt-1", **arguments)

    journal = manager.worktree_root / "run-1" / "attempt-1" / "candidate.json"
    record = json.loads(journal.read_text(encoding="utf-8"))
    stage = Path(record["git_dir"]) / record["candidate_index_stage_name"]
    owned = stage.with_name(stage.name + ".owned")
    stage.rename(owned)
    foreign = b"foreign stage bytes that must survive recovery"
    stage.write_bytes(foreign)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    with pytest.raises(CandidateUncertain, match="foreign state"):
        manager.commit_candidate("run-1", "attempt-1", **arguments)

    assert stage.read_bytes() == foreign
    assert owned.read_bytes() == b""


def test_candidate_recovery_preserves_prejournal_index_stage_and_uses_a_new_name(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"book.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    def interrupt(name: str) -> None:
        if name == "candidate-index-stage-created-before-journal":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.commit_candidate("run-1", "attempt-1", **arguments)

    journal = manager.worktree_root / "run-1" / "attempt-1" / "candidate.json"
    interrupted = json.loads(journal.read_text(encoding="utf-8"))
    old_stage = Path(interrupted["git_dir"]) / interrupted["candidate_index_stage_name"]
    old_identity = (old_stage.stat().st_dev, old_stage.stat().st_ino)
    assert interrupted["candidate_index_stage_device"] is None
    assert old_stage.read_bytes() == b""

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    candidate = manager.commit_candidate("run-1", "attempt-1", **arguments)
    recovered = json.loads(journal.read_text(encoding="utf-8"))

    assert candidate.state == "ready"
    assert recovered["candidate_index_stage_name"] != interrupted["candidate_index_stage_name"]
    assert recovered["candidate_index_abandoned_stages"] == [
        {
            "name": interrupted["candidate_index_stage_name"],
            "device": old_identity[0],
            "inode": old_identity[1],
        }
    ]
    assert old_stage.read_bytes() == b""
    assert manager.inspect_candidate("run-1", "attempt-1") == candidate


def test_candidate_index_staging_preserves_a_racing_stage_replacement(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    foreign = b"foreign stage bytes that must survive installation"
    replaced: tuple[Path, Path] | None = None

    def replace_stage(name: str) -> None:
        nonlocal replaced
        if name != "candidate-index-stage-retained":
            return
        journal = manager.worktree_root / "run-1" / "attempt-1" / "candidate.json"
        record = json.loads(journal.read_text(encoding="utf-8"))
        stage = Path(record["git_dir"]) / record["candidate_index_stage_name"]
        owned = stage.with_name(stage.name + ".owned")
        stage.rename(owned)
        stage.write_bytes(foreign)
        replaced = stage, owned

    monkeypatch.setattr(repository_module, "_checkpoint", replace_stage)
    with pytest.raises(CandidateUncertain, match="foreign state"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert replaced is not None
    stage, owned = replaced
    assert stage.read_bytes() == foreign
    assert owned.read_bytes() != foreign


def test_candidate_index_exchange_restores_a_racing_replacement(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    index = git_dir / "index"
    foreign = b"foreign index bytes that must survive installation"
    raced = False

    def replace_after_check(name: str) -> None:
        nonlocal raced
        if name == "candidate-index-before-exchange" and not raced:
            replacement = git_dir / "foreign-index"
            replacement.write_bytes(foreign)
            os.replace(replacement, index)
            raced = True

    monkeypatch.setattr(repository_module, "_checkpoint", replace_after_check)
    with pytest.raises(CandidateUncertain, match="foreign state|recorded base"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert raced
    assert index.read_bytes() == foreign
    assert not (git_dir / "foreign-index").exists()


def test_candidate_index_displace_preserves_a_racing_lock_replacement(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    index = git_dir / "index"
    lock = git_dir / "index.lock"
    preserved_base = git_dir / "preserved-base-index"
    foreign = b"foreign displaced-index bytes"
    raced = False

    def replace_before_displace(name: str) -> None:
        nonlocal raced
        if name == "candidate-index-before-displace" and not raced:
            lock.rename(preserved_base)
            lock.write_bytes(foreign)
            raced = True

    monkeypatch.setattr(repository_module, "_checkpoint", replace_before_displace)
    with pytest.raises(CandidateUncertain, match="foreign state"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert raced
    assert index.read_bytes() == foreign
    assert preserved_base.read_bytes()
    assert lock.read_bytes() != foreign


def test_candidate_commit_rejects_explicitly_allowed_ignored_output(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    output = tree / "ignored" / "output.bin"
    output.parent.mkdir()
    output.write_bytes(b"preserve me")

    with pytest.raises(CandidateUncertain, match="allowed path is ignored"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"ignored/output.bin"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert output.read_bytes() == b"preserve me"
    assert _git("rev-parse", "HEAD", cwd=tree) == base


@pytest.mark.parametrize(
    "directive",
    (
        "[include]\n\tpath = {included}\n",
        '[includeIf "gitdir:**/tree"]\n\tpath = {included}\n',
    ),
)
def test_candidate_commit_rejects_repository_config_includes(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    directive: str,
) -> None:
    _, _, coordinator, base = git_repository
    included = tmp_path / "included.config"
    included.write_text("[core]\n\tfilemode = false\n", encoding="utf-8")
    config = Path(_git("rev-parse", "--git-path", "config", cwd=coordinator))
    if not config.is_absolute():
        config = coordinator / config
    with config.open("a", encoding="utf-8") as stream:
        stream.write("\n" + directive.format(included=included))
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")

    with pytest.raises(CandidateUncertain, match="configuration includes"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert _git("rev-parse", "HEAD", cwd=tree) == base
    assert (tree / "book.txt").read_text(encoding="utf-8") == "candidate\n"


def test_candidate_commit_rejects_worktree_config_include(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    _git("config", "extensions.worktreeConfig", "true", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    included = tmp_path / "included.config"
    included.write_text("[core]\n\tfilemode = false\n", encoding="utf-8")
    _git("config", "--worktree", "include.path", str(included), cwd=tree)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")

    with pytest.raises(CandidateUncertain, match="configuration includes"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_recovery_rejects_base_index_aba(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")

    def interrupt(name: str) -> None:
        if name == "candidate-head-updated":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    index = git_dir / "index"
    replacement = git_dir / "replacement-index"
    replacement.write_bytes(index.read_bytes())
    os.replace(replacement, index)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    with pytest.raises(CandidateUncertain, match="index conflicts"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert (tree / "book.txt").read_text(encoding="utf-8") == "candidate\n"


def test_ready_candidate_rejects_same_byte_index_replacement(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    index = git_dir / "index"
    replacement = git_dir / "replacement-index"
    replacement.write_bytes(index.read_bytes())
    os.replace(replacement, index)

    with pytest.raises(CandidateUncertain, match="identity|topology"):
        manager.inspect_candidate("run-1", "attempt-1")


def test_candidate_commit_rejects_index_replacement_before_head_update(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    index = git_dir / "index"

    def replace_index(name: str) -> None:
        if name == "candidate-objects-written":
            replacement = git_dir / "replacement-index"
            replacement.write_bytes(index.read_bytes())
            os.replace(replacement, index)

    monkeypatch.setattr(repository_module, "_checkpoint", replace_index)
    with pytest.raises(CandidateUncertain, match="index changed before candidate HEAD"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert _git("rev-parse", "HEAD", cwd=tree) == base
    assert (tree / "book.txt").read_text(encoding="utf-8") == "candidate\n"


def test_candidate_commit_rejects_submodule_base_entries(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, original_base = git_repository
    _git("update-index", "--add", "--cacheinfo", f"160000,{original_base},nested-repository", cwd=coordinator)
    _git("commit", "--quiet", "-m", "gitlink fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    manager.prepare("run-1", "attempt-1", base_oid=base)

    with pytest.raises(CandidateUncertain, match="submodules"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )


def test_candidate_commit_does_not_execute_filters_hooks_signing_or_external_diff(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, _ = git_repository
    (coordinator / ".gitattributes").write_text("*.txt filter=malicious diff=malicious\n", encoding="utf-8")
    _git("add", ".gitattributes", cwd=coordinator)
    _git("commit", "--quiet", "-m", "attributes fixture", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    receipt = manager.prepare("run-1", "attempt-1", base_oid=base)

    sentinel = tmp_path / "malicious-git-feature-ran"
    malicious = tmp_path / "malicious-command"
    malicious.write_text(f'#!/bin/sh\ntouch "{sentinel}"\ncat\n', encoding="utf-8")
    malicious.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "reference-transaction").symlink_to(malicious)
    (hooks / "post-index-change").symlink_to(malicious)
    _git("config", "filter.malicious.clean", str(malicious), cwd=coordinator)
    _git("config", "filter.malicious.smudge", str(malicious), cwd=coordinator)
    _git("config", "commit.gpgSign", "true", cwd=coordinator)
    _git("config", "gpg.program", str(malicious), cwd=coordinator)
    _git("config", "diff.external", str(malicious), cwd=coordinator)
    _git("config", "core.hooksPath", str(hooks), cwd=coordinator)
    tree = Path(receipt.path)
    (tree / "book.txt").write_text("candidate bytes\n", encoding="utf-8")

    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="deterministic candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert candidate.state == "ready"
    assert not sentinel.exists()
    manager.cleanup("run-1", "attempt-1")
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "boundary,inspect_state",
    (
        ("candidate-intent-recorded", None),
        ("candidate-objects-written", None),
        ("candidate-head-updated", "recoverable"),
        ("candidate-head-recorded", "recoverable"),
        ("candidate-index-stage-created-before-journal", "recoverable"),
        ("candidate-index-stage-created", "recoverable"),
        ("candidate-index-stage-written", "recoverable"),
        ("candidate-index-staged", "recoverable"),
        ("candidate-index-locked", "recoverable"),
        ("candidate-index-stage-retained", "recoverable"),
        ("candidate-index-before-exchange", "recoverable"),
        ("candidate-index-exchanged", "recoverable"),
        ("candidate-index-before-displace", "recoverable"),
        ("candidate-index-displaced", "recoverable"),
        ("candidate-index-updated", "recoverable"),
        ("candidate-result-recorded", "ready"),
    ),
)
def test_candidate_commit_recovers_every_durable_crash_boundary(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    inspect_state: str | None,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / f"state-{boundary}")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"book.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.commit_candidate("run-1", "attempt-1", **arguments)

    if inspect_state is None:
        with pytest.raises(CandidateUncertain):
            manager.inspect_candidate("run-1", "attempt-1")
    else:
        assert manager.inspect_candidate("run-1", "attempt-1").state == inspect_state

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    recovered = manager.commit_candidate("run-1", "attempt-1", **arguments)
    assert recovered.state == "ready"
    assert manager.commit_candidate("run-1", "attempt-1", **arguments) == recovered
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--absolute-git-dir", cwd=tree))
    record = json.loads((manager.worktree_root / "run-1" / "attempt-1" / "candidate.json").read_text(encoding="utf-8"))
    assert git_dir.joinpath(record["candidate_index_stage_name"]).is_file()
    assert not (git_dir / "index.lock").exists()


def test_candidate_commit_detects_path_and_config_replacement_before_ref_update(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "path-swap", base_oid=base).path)
    target = tree / "book.txt"
    target.write_text("candidate\n", encoding="utf-8")

    def replace_path(name: str) -> None:
        if name == "candidate-objects-written":
            old = target.with_name("book.old")
            target.rename(old)
            target.write_text("candidate\n", encoding="utf-8")
            old.unlink()

    monkeypatch.setattr(repository_module, "_checkpoint", replace_path)
    with pytest.raises(CandidateUncertain, match="replaced or changed"):
        manager.commit_candidate(
            "run-1",
            "path-swap",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base

    config_tree = Path(manager.prepare("run-1", "config-swap", base_oid=base).path)
    (config_tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    config = manager.common_git_dir / "config"

    def replace_config(name: str) -> None:
        if name == "candidate-intent-recorded":
            with config.open("a", encoding="utf-8") as stream:
                stream.write("\n[autoform-test]\n\tchanged = true\n")

    monkeypatch.setattr(repository_module, "_checkpoint", replace_config)
    with pytest.raises(CandidateUncertain, match="configuration changed"):
        manager.commit_candidate(
            "run-1",
            "config-swap",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=config_tree) == base


def test_candidate_recovery_rejects_repository_config_drift(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"book.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }

    def interrupt(name: str) -> None:
        if name == "candidate-intent-recorded":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.commit_candidate("run-1", "attempt-1", **arguments)

    _git("config", "autoform.testSetting", "changed", cwd=coordinator)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    with pytest.raises(CandidateUncertain, match="configuration changed"):
        manager.commit_candidate("run-1", "attempt-1", **arguments)
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_snapshot_rejects_directory_replacement_after_open(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    chapter = tree / "chapter"
    chapter.mkdir()
    (chapter / "proof.lean").write_text("candidate\n", encoding="utf-8")
    replaced = False

    def replace_directory(name: str) -> None:
        nonlocal replaced
        if name == "candidate-directory-listed:chapter" and not replaced:
            original = tree / "chapter-original"
            chapter.rename(original)
            chapter.mkdir()
            (chapter / "proof.lean").write_text("replacement\n", encoding="utf-8")
            replaced = True

    monkeypatch.setattr(repository_module, "_checkpoint", replace_directory)
    with pytest.raises(CandidateUncertain, match="directory (?:changed|was replaced)"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"chapter/proof.lean"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )


def test_ready_candidate_allows_benign_repository_config_drift(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "book.txt").write_text("candidate\n", encoding="utf-8")
    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    _git("config", "autoform.testSetting", "benign", cwd=coordinator)

    assert manager.inspect_candidate("run-1", "attempt-1") == candidate
    manager.cleanup("run-1", "attempt-1")
    with pytest.raises(CandidateNotFound):
        manager.inspect_candidate("run-1", "attempt-1")


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
def test_candidate_commit_supports_repository_object_formats(tmp_path: Path, object_format: str) -> None:
    repository = tmp_path / f"repository-{object_format}"
    initialized = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", f"--object-format={object_format}", str(repository)],
        capture_output=True,
        text=True,
    )
    if initialized.returncode != 0 and object_format == "sha256":
        pytest.skip("installed Git does not support SHA-256 repositories")
    initialized.check_returncode()
    (repository / "file.txt").write_text("base\n", encoding="utf-8")
    _git("add", "file.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "base", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    manager = AttemptWorktrees(repository, tmp_path / f"state-{object_format}")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    (tree / "file.txt").write_text("candidate\n", encoding="utf-8")

    receipt = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"file.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert len(receipt.candidate_oid) == (40 if object_format == "sha1" else 64)
    assert _git("rev-parse", "--show-object-format", cwd=tree) == object_format
    _git("fsck", "--full", cwd=repository)


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
def test_candidate_commit_and_inspection_reject_missing_reachable_base_blob(
    tmp_path: Path,
    object_format: str,
) -> None:
    repository = tmp_path / f"repository-{object_format}"
    initialized = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", f"--object-format={object_format}", str(repository)],
        capture_output=True,
        text=True,
    )
    if initialized.returncode != 0 and object_format == "sha256":
        pytest.skip("installed Git does not support SHA-256 repositories")
    initialized.check_returncode()
    (repository / "changed.txt").write_text("original changed content\n", encoding="utf-8")
    (repository / "unchanged.txt").write_text("required unchanged content\n", encoding="utf-8")
    _git("add", "changed.txt", "unchanged.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "base", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    manager = AttemptWorktrees(repository, tmp_path / "state")
    ready_tree = Path(manager.prepare("run-1", "ready", base_oid=base).path)
    pending_tree = Path(manager.prepare("run-1", "pending", base_oid=base).path)
    ready_tree.joinpath("changed.txt").write_text("ready candidate\n", encoding="utf-8")
    pending_tree.joinpath("changed.txt").write_text("pending candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"changed.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }
    ready = manager.commit_candidate("run-1", "ready", **arguments)

    blob_oid = _git("rev-parse", f"{base}:unchanged.txt", cwd=repository)
    object_path = manager.common_git_dir / "objects" / blob_oid[:2] / blob_oid[2:]
    assert object_path.is_file()
    object_path.unlink()

    with pytest.raises(CandidateUncertain, match="tree closure is incomplete"):
        manager.inspect_candidate("run-1", "ready")
    with pytest.raises(CandidateUncertain, match="tree closure is incomplete"):
        manager.commit_candidate("run-1", "pending", **arguments)
    assert _git("rev-parse", "HEAD", cwd=pending_tree) == base
    assert _git("rev-parse", "HEAD", cwd=ready_tree) == ready.candidate_oid


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
def test_candidate_commit_rejects_corrupt_reachable_base_blob(
    tmp_path: Path,
    object_format: str,
) -> None:
    repository = tmp_path / f"repository-{object_format}"
    initialized = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", f"--object-format={object_format}", str(repository)],
        capture_output=True,
        text=True,
    )
    if initialized.returncode != 0 and object_format == "sha256":
        pytest.skip("installed Git does not support SHA-256 repositories")
    initialized.check_returncode()
    (repository / "changed.txt").write_text("original changed content\n", encoding="utf-8")
    (repository / "unchanged.txt").write_text("required unchanged content\n", encoding="utf-8")
    _git("add", "changed.txt", "unchanged.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "base", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    manager = AttemptWorktrees(repository, tmp_path / "state")
    ready_tree = Path(manager.prepare("run-1", "ready", base_oid=base).path)
    pending_tree = Path(manager.prepare("run-1", "pending", base_oid=base).path)
    ready_tree.joinpath("changed.txt").write_text("ready candidate\n", encoding="utf-8")
    pending_tree.joinpath("changed.txt").write_text("pending candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"changed.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }
    ready = manager.commit_candidate("run-1", "ready", **arguments)

    blob_oid = _git("rev-parse", f"{base}:unchanged.txt", cwd=repository)
    object_path = manager.common_git_dir / "objects" / blob_oid[:2] / blob_oid[2:]
    assert object_path.is_file()
    corrupt = b"corrupt object content\n"
    object_path.chmod(0o600)
    object_path.write_bytes(zlib.compress(f"blob {len(corrupt)}\0".encode() + corrupt))
    assert _git("cat-file", "-t", blob_oid, cwd=repository) == "blob"

    with pytest.raises(CandidateUncertain, match="tree closure is incomplete: blob identity mismatch"):
        manager.inspect_candidate("run-1", "ready")
    with pytest.raises(CandidateUncertain, match="tree closure is incomplete: blob identity mismatch"):
        manager.commit_candidate("run-1", "pending", **arguments)
    assert _git("rev-parse", "HEAD", cwd=pending_tree) == base
    assert _git("rev-parse", "HEAD", cwd=ready_tree) == ready.candidate_oid


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
def test_candidate_commit_rejects_corrupt_base_commit(
    tmp_path: Path,
    object_format: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / f"repository-{object_format}"
    initialized = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", f"--object-format={object_format}", str(repository)],
        capture_output=True,
        text=True,
    )
    if initialized.returncode != 0 and object_format == "sha256":
        pytest.skip("installed Git does not support SHA-256 repositories")
    initialized.check_returncode()
    repository.joinpath("changed.txt").write_text("base changed content\n", encoding="utf-8")
    _git("add", "changed.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "base", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    base_content = _git_bytes("cat-file", "commit", base, cwd=repository)
    manager = AttemptWorktrees(repository, tmp_path / "state")
    ready_tree = Path(manager.prepare("run-1", "ready", base_oid=base).path)
    pending_tree = Path(manager.prepare("run-1", "pending", base_oid=base).path)
    ready_tree.joinpath("changed.txt").write_text("ready candidate\n", encoding="utf-8")
    pending_tree.joinpath("changed.txt").write_text("pending candidate\n", encoding="utf-8")
    arguments = {
        "allowed_paths": {"changed.txt"},
        "message": "candidate",
        "author_name": "Autoform Bot",
        "author_email": "autoform@example.invalid",
    }
    ready = manager.commit_candidate("run-1", "ready", **arguments)
    corrupt_content = base_content + b"forged commit bytes\n"
    original_candidate_base_entries = manager._candidate_base_entries

    def corrupt_before_base_inspection(base_oid: str) -> dict[str, tuple[str, str]]:
        _replace_loose_object(manager, base, "commit", corrupt_content)
        return original_candidate_base_entries(base_oid)

    monkeypatch.setattr(manager, "_candidate_base_entries", corrupt_before_base_inspection)
    with pytest.raises(CandidateUncertain, match="base commit.*commit identity mismatch"):
        manager.inspect_candidate("run-1", "ready")
    assert _git("rev-parse", "HEAD", cwd=ready_tree) == ready.candidate_oid

    _replace_loose_object(manager, base, "commit", base_content)
    with pytest.raises(CandidateUncertain, match="base commit.*commit identity mismatch"):
        manager.commit_candidate(
            "run-1",
            "pending",
            **arguments,
        )
    assert _git("rev-parse", "HEAD", cwd=pending_tree) == base


@pytest.mark.parametrize("object_format", ("sha1", "sha256"))
def test_candidate_commit_rejects_corrupt_nested_base_tree(
    tmp_path: Path,
    object_format: str,
) -> None:
    repository = tmp_path / f"repository-{object_format}"
    initialized = subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", f"--object-format={object_format}", str(repository)],
        capture_output=True,
        text=True,
    )
    if initialized.returncode != 0 and object_format == "sha256":
        pytest.skip("installed Git does not support SHA-256 repositories")
    initialized.check_returncode()
    repository.joinpath("nested").mkdir()
    repository.joinpath("changed.txt").write_text("base changed content\n", encoding="utf-8")
    repository.joinpath("nested/stable.txt").write_text("base stable content\n", encoding="utf-8")
    _git("add", "changed.txt", "nested/stable.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "base", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    original_nested_tree_oid = _git("rev-parse", f"{base}:nested", cwd=repository)
    manager = AttemptWorktrees(repository, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("changed.txt").write_text("candidate\n", encoding="utf-8")
    tree.joinpath("nested/stable.txt").write_text("forged stable content\n", encoding="utf-8")
    _git("add", "nested/stable.txt", cwd=tree)

    repository.joinpath("nested/stable.txt").write_text("forged stable content\n", encoding="utf-8")
    _git("add", "nested/stable.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "forged nested tree source", cwd=repository)
    forged_nested_tree_oid = _git("rev-parse", "HEAD:nested", cwd=repository)
    forged_nested_tree = _git_bytes("cat-file", "tree", forged_nested_tree_oid, cwd=repository)
    _replace_loose_object(manager, original_nested_tree_oid, "tree", forged_nested_tree)
    assert _git("cat-file", "-t", original_nested_tree_oid, cwd=repository) == "tree"

    with pytest.raises(CandidateUncertain, match="base commit tree object has an invalid identity"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"changed.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base


@pytest.mark.parametrize(
    ("malformation", "message"),
    (
        ("truncated-header", "truncated batch header"),
        ("ambiguous-header", "invalid batch header"),
        ("truncated-content", "truncated blob content"),
        ("invalid-delimiter", "invalid blob delimiter"),
        ("trailing-data", "trailing batch output"),
    ),
)
def test_candidate_tree_closure_rejects_malformed_batch_output(
    malformation: str,
    message: str,
) -> None:
    content = b"candidate\n"
    oid = repository_module._git_blob_oid(content, "sha1")
    valid = f"{oid} blob {len(content)}\n".encode() + content + b"\n"
    if malformation == "truncated-header":
        output = valid.split(b"\n", 1)[0]
    elif malformation == "ambiguous-header":
        output = valid.replace(b" blob ", b"  blob ", 1)
    elif malformation == "truncated-content":
        output = valid[:-1]
    elif malformation == "invalid-delimiter":
        output = valid[:-1] + b"x"
    else:
        output = valid + b"foreign"
    with pytest.raises(CandidateUncertain, match=message):
        repository_module._verify_candidate_blob_batch_output(io.BytesIO(output), (oid,), "sha1")


def test_candidate_tree_closure_streams_large_unallowed_blob_without_retaining_it(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    large = coordinator / "large.bin"
    with large.open("wb") as stream:
        stream.seek(repository_module._CANDIDATE_BLOB_CHUNK_BYTES * 3)
        stream.write(b"x")
    _git("add", "large.bin", cwd=coordinator)
    _git("commit", "--quiet", "-m", "large base blob", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")

    base_entries = manager._candidate_base_entries(base)
    snapshot = manager._candidate_snapshot(tree, base_entries, ("book.txt",))
    assert {oid for oid, _ in snapshot.blobs} == {dict(snapshot.entries)["book.txt"][1]}
    assert sum(len(content) for _, content in snapshot.blobs) < repository_module._CANDIDATE_BLOB_CHUNK_BYTES

    original_popen = subprocess.Popen
    guarded_streams: list[object] = []

    class GuardedReader:
        def __init__(self, stream: object) -> None:
            self.stream = stream
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= repository_module._CANDIDATE_BLOB_CHUNK_BYTES
            self.read_sizes.append(size)
            return self.stream.read(size)  # type: ignore[union-attr,no-any-return]

        def readline(self, size: int = -1) -> bytes:
            assert 0 <= size <= repository_module._CANDIDATE_BATCH_HEADER_BYTES + 1
            return self.stream.readline(size)  # type: ignore[union-attr,no-any-return]

        def close(self) -> None:
            self.stream.close()  # type: ignore[union-attr]

    def guarded_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get("args")
        if (
            isinstance(command, list)
            and "cat-file" in command
            and any(isinstance(argument, str) and argument.startswith("--batch=") for argument in command)
        ):
            assert "--no-replace-objects" in command
            environment = kwargs.get("env")
            assert isinstance(environment, dict) and environment.get("GIT_NO_LAZY_FETCH") == "1"
            assert process.stdout is not None
            guarded = GuardedReader(process.stdout)
            process.stdout = guarded  # type: ignore[assignment]
            guarded_streams.append(guarded)
        return process

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert candidate.state == "ready"
    assert guarded_streams
    assert any(
        repository_module._CANDIDATE_BLOB_CHUNK_BYTES in guarded.read_sizes  # type: ignore[union-attr]
        for guarded in guarded_streams
    )


def test_candidate_snapshot_rejects_nonallowed_file_growth_during_streaming(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    stable = coordinator / "stable.bin"
    with stable.open("wb") as stream:
        stream.seek(repository_module._CANDIDATE_BLOB_CHUNK_BYTES * 2)
        stream.write(b"x")
    _git("add", "stable.bin", cwd=coordinator)
    _git("commit", "--quiet", "-m", "large base blob", cwd=coordinator)
    base = _git("rev-parse", "HEAD", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    mutated = False

    def grow_during_read(name: str) -> None:
        nonlocal mutated
        if name == "candidate-file-chunk:stable.bin" and not mutated:
            with tree.joinpath("stable.bin").open("ab") as stream:
                stream.write(b"growth")
            mutated = True

    monkeypatch.setattr(repository_module, "_checkpoint", grow_during_read)
    with pytest.raises(CandidateUncertain, match="changed while being read"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_snapshot_rejects_oversized_allowed_file(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    with tree.joinpath("oversized.lean").open("wb") as stream:
        stream.truncate(repository_module._MAX_CANDIDATE_BLOB_BYTES + 1)

    with pytest.raises(CandidateUncertain, match="allowed file exceeds the 16 MiB safety limit"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"oversized.lean"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_snapshot_rejects_oversized_allowed_files_in_aggregate(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("first.lean").write_text("1234", encoding="utf-8")
    tree.joinpath("second.lean").write_text("5678", encoding="utf-8")
    monkeypatch.setattr(repository_module, "_MAX_CANDIDATE_BLOB_BYTES", 8)
    monkeypatch.setattr(repository_module, "_MAX_CANDIDATE_TOTAL_BLOB_BYTES", 6)

    with pytest.raises(CandidateUncertain, match="allowed files exceed the .* aggregate safety limit"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"first.lean", "second.lean"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_tree_closure_reaps_git_after_protocol_failure(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")

    class MalformedProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"malformed\n")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.killed = False
            self.waited = False

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            assert timeout is not None
            return self.returncode if self.returncode is not None else 0

    process = MalformedProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(CandidateUncertain, match="invalid batch header"):
        manager._verify_candidate_blob_batch(("0" * len(base),))
    assert process.killed
    assert process.waited
    assert process.stdout.closed
    assert process.stderr.closed


def test_candidate_tree_closure_progress_extends_inactivity_deadline(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, _ = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    content = b"continuous bounded progress"
    oid = repository_module._git_blob_oid(content, "sha1")
    output = f"{oid} blob {len(content)}\n".encode() + content + b"\n"

    class SlowReader(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            time.sleep(0.05)
            return super().read(size)

        def readline(self, size: int = -1) -> bytes:
            time.sleep(0.05)
            return super().readline(size)

    class ProgressProcess:
        def __init__(self) -> None:
            self.stdout = SlowReader(output)
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.killed = False

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    process = ProgressProcess()
    monkeypatch.setattr(repository_module, "_CANDIDATE_BLOB_CHUNK_BYTES", 1)
    monkeypatch.setattr(repository_module, "_CANDIDATE_BATCH_TIMEOUT_S", 1)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    manager._verify_candidate_blob_batch((oid,))

    assert not process.killed
    assert process.returncode == 0


def test_candidate_tree_closure_kills_and_reaps_inactive_git(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    released = threading.Event()

    class BlockingReader:
        def __init__(self) -> None:
            self.closed = False

        def readline(self, size: int = -1) -> bytes:
            assert size > 0
            assert released.wait(timeout=5)
            return b""

        def read(self, size: int = -1) -> bytes:
            assert size > 0
            assert released.wait(timeout=5)
            return b""

        def close(self) -> None:
            self.closed = True

    class InactiveProcess:
        def __init__(self) -> None:
            self.stdout = BlockingReader()
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.killed = False
            self.waited = False

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            released.set()

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            assert timeout is not None
            return self.returncode if self.returncode is not None else 0

    process = InactiveProcess()
    monkeypatch.setattr(repository_module, "_CANDIDATE_BATCH_TIMEOUT_S", 0.01)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(CandidateUncertain, match="Git process timed out"):
        manager._verify_candidate_blob_batch(("0" * len(base),))
    assert process.killed
    assert process.waited
    assert process.stdout.closed
    assert process.stderr.closed


def test_candidate_tree_closure_ignores_replacement_refs(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    blob_oid = _git("rev-parse", f"{base}:.gitignore", cwd=coordinator)
    replacement = tmp_path / "replacement-blob"
    replacement.write_text("replacement\n", encoding="utf-8")
    replacement_oid = _git("hash-object", "-w", str(replacement), cwd=coordinator)
    _git("replace", blob_oid, replacement_oid, cwd=coordinator)
    object_path = manager.common_git_dir / "objects" / blob_oid[:2] / blob_oid[2:]
    assert object_path.is_file()
    object_path.unlink()

    with pytest.raises(CandidateUncertain, match="tree closure is incomplete"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_ready_candidate_survives_removal_of_alternate_object_storage(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    blob_oid = _git("rev-parse", f"{base}:.gitignore", cwd=coordinator)
    object_path = manager.common_git_dir / "objects" / blob_oid[:2] / blob_oid[2:]
    alternate_objects = tmp_path / "alternate-objects"
    alternate_path = alternate_objects / blob_oid[:2] / blob_oid[2:]
    alternate_path.parent.mkdir(parents=True)
    object_path.replace(alternate_path)
    alternates = manager.common_git_dir / "objects" / "info" / "alternates"
    alternates.write_text(f"{alternate_objects}\n", encoding="utf-8")
    verified_inventories: list[frozenset[tuple[str, str]]] = []
    original_verify_pack = manager._verify_primary_pack_output

    def capture_verify_pack(pack: Path, index: Path) -> frozenset[tuple[str, str]]:
        inventory = original_verify_pack(pack, index)
        verified_inventories.append(inventory)
        return inventory

    monkeypatch.setattr(manager, "_verify_primary_pack_output", capture_verify_pack)

    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert manager.inspect_candidate("run-1", "attempt-1") == candidate
    assert verified_inventories == [frozenset({(blob_oid, "blob")})]
    packs_after_first_import = {
        path.name: path.stat().st_size for path in (manager.object_dir / "pack").glob("pack-*.pack")
    }
    second_tree = Path(manager.prepare("run-2", "attempt-1", base_oid=base).path)
    second_tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    second = manager.commit_candidate(
        "run-2",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )
    assert second.candidate_oid == candidate.candidate_oid
    assert {
        path.name: path.stat().st_size for path in (manager.object_dir / "pack").glob("pack-*.pack")
    } == packs_after_first_import
    alternates.unlink()
    alternate_path.unlink()
    alternate_path.parent.rmdir()
    alternate_objects.rmdir()

    assert manager.inspect_candidate("run-1", "attempt-1") == candidate
    assert manager.inspect_candidate("run-2", "attempt-1") == second
    manager.cleanup("run-1", "attempt-1")
    manager.cleanup("run-2", "attempt-1")
    with pytest.raises(CandidateNotFound):
        manager.inspect_candidate("run-1", "attempt-1")


def test_candidate_primary_pack_probe_does_not_change_hardlink_counts(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    _, _, coordinator, base = git_repository
    _git("gc", "--quiet", "--prune=now", cwd=coordinator)
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    pack_dir = manager.object_dir / "pack"
    original_links = {path: path.stat().st_nlink for path in pack_dir.iterdir() if path.suffix in {".idx", ".pack"}}
    assert original_links
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")

    candidate = manager.commit_candidate(
        "run-1",
        "attempt-1",
        allowed_paths={"book.txt"},
        message="candidate",
        author_name="Autoform Bot",
        author_email="autoform@example.invalid",
    )

    assert candidate.state == "ready"
    assert {path: path.stat().st_nlink for path in original_links} == original_links
    assert not list(manager.object_dir.glob(".autoform-pack-view-*"))


def test_candidate_primary_pack_probe_rejects_forged_index_and_alternate_fallback(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    blob_oid = _git("rev-parse", f"{base}:.gitignore", cwd=coordinator)
    object_path = manager.object_dir / blob_oid[:2] / blob_oid[2:]
    alternate_objects = tmp_path / "alternate-objects"
    alternate_path = alternate_objects / blob_oid[:2] / blob_oid[2:]
    alternate_path.parent.mkdir(parents=True)
    object_path.replace(alternate_path)
    manager.object_dir.joinpath("info/alternates").write_text(f"{alternate_objects}\n", encoding="utf-8")
    forged = False

    def forge_primary_index(name: str) -> None:
        nonlocal forged
        if name != "candidate-objects-written" or forged:
            return
        forged = True
        decoy = tmp_path / "decoy"
        decoy.write_bytes(b"decoy object")
        decoy_oid = _git("hash-object", "-w", str(decoy), cwd=coordinator)
        prefix = manager.object_dir / "pack" / "pack-autoform-forged"
        packed = subprocess.run(
            ["git", "pack-objects", str(prefix)],
            cwd=coordinator,
            input=f"{decoy_oid}\n",
            capture_output=True,
            text=True,
            check=True,
        )
        pack_hash = packed.stdout.strip()
        index = Path(f"{prefix}-{pack_hash}.idx")
        content = bytearray(index.read_bytes())
        assert content[:8] == b"\xfftOc\x00\x00\x00\x02"
        assert struct.unpack(">I", content[8 + 255 * 4 : 8 + 256 * 4])[0] == 1
        raw_oid = bytes.fromhex(blob_oid)
        content[8 : 8 + 256 * 4] = b"".join(struct.pack(">I", int(byte >= raw_oid[0])) for byte in range(256))
        oid_offset = 8 + 256 * 4
        content[oid_offset : oid_offset + len(raw_oid)] = raw_oid
        content[-len(raw_oid) :] = hashlib.sha1(content[: -len(raw_oid)]).digest()
        index.chmod(0o600)
        index.write_bytes(content)

    monkeypatch.setattr(repository_module, "_checkpoint", forge_primary_index)
    with pytest.raises(CandidateUncertain, match="primary Git pack|candidate closure"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )

    assert forged
    assert alternate_path.read_bytes()
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_candidate_tree_closure_rechecks_repository_config(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "state")
    tree = Path(manager.prepare("run-1", "attempt-1", base_oid=base).path)
    tree.joinpath("book.txt").write_text("candidate\n", encoding="utf-8")
    original_verify_candidate_blob_batch = manager._verify_candidate_blob_batch

    def mutate_after_closure_check(oids: tuple[str, ...]) -> None:
        original_verify_candidate_blob_batch(oids)
        _git("config", "autoform.closureProbe", "changed", cwd=coordinator)

    monkeypatch.setattr(manager, "_verify_candidate_blob_batch", mutate_after_closure_check)
    with pytest.raises(CandidateUncertain, match="configuration changed"):
        manager.commit_candidate(
            "run-1",
            "attempt-1",
            allowed_paths={"book.txt"},
            message="candidate",
            author_name="Autoform Bot",
            author_email="autoform@example.invalid",
        )
    assert _git("rev-parse", "HEAD", cwd=tree) == base


def test_merge_queue_paths_are_disjoint_before_state_creation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, _ = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    remote_entries = {entry.name for entry in remote.iterdir()}

    for state in (remote, remote / "queue-state"):
        with pytest.raises(RepositoryError, match="disjoint"):
            _queue(manager, remote, state, "worker-a")
        assert {entry.name for entry in remote.iterdir()} == remote_entries

    with pytest.raises(RepositoryError, match="outside the attempt state"):
        _queue(manager, remote, manager.state_root, "worker-a")

    for index, overlapping_remote in enumerate(
        (manager.repository_root, manager.common_git_dir, manager.state_root),
    ):
        state = tmp_path / f"rejected-state-{index}"
        with pytest.raises(RepositoryError, match="must be disjoint"):
            _queue(manager, overlapping_remote, state, "worker-a")
        assert not state.exists()


@pytest.mark.parametrize(
    "boundary",
    (
        "transport-intent-recorded",
        "transport-staging-created",
        "transport-staging-recorded",
        "transport-marker-recorded",
    ),
)
def test_transport_initialization_resumes_at_durable_boundaries(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    remote, _, coordinator, _ = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    state = tmp_path / "queue-state"

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _queue(manager, remote, state, "worker-a")
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    queue = _queue(manager, remote, state, "worker-a")
    assert queue.transport_root.is_dir()
    assert queue.transport_marker.is_file()
    assert not queue.transport_staging.exists()
    assert not queue.transport_intent.exists()
    queue.close()


def test_local_transport_uses_and_verifies_canonical_python_executable(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, _ = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    assert queue._transport_python == Path(sys.executable).resolve(strict=True)

    replacement = tmp_path / "python-replacement"
    replacement.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    replacement.chmod(0o755)
    monkeypatch.setattr(queue, "_transport_python", replacement)
    with pytest.raises(PublicationUncertain, match="Python executable was replaced"):
        queue._remote_oid("refs/heads/main")


def test_remote_merge_queue_publishes_exact_cas_and_is_idempotent(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="published\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/run-1-attempt-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }

    receipt = queue.publish("queue-1", **arguments)
    repeated = queue.publish("queue-1", **arguments)

    assert receipt.status == "integrated"
    assert repeated == receipt
    assert receipt.remote_kind == "local"
    assert (receipt.remote_device, receipt.remote_inode) == (remote.stat().st_dev, remote.stat().st_ino)
    assert receipt.claim_oid is not None
    assert receipt.observed_claim_oid is None
    assert receipt.observed_target_oid == candidate
    assert receipt.observed_queue_oid is None
    assert receipt.observed_article_claim_oid is None
    assert receipt.observed_article_handoff_oid is None
    assert receipt.article_claim_oid == arguments["article_claim"].oid
    assert hashlib.sha256(receipt.evidence_bytes()).hexdigest() == receipt.evidence_sha256
    with RunLedger(tmp_path / "ledger/run.sqlite3") as ledger:
        evidence = ledger.put_artifact("merge-receipt", receipt.evidence_bytes())
        assert evidence == receipt.evidence_sha256
        assert ledger.read_artifact(evidence) == receipt.evidence_bytes()
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidate
    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/run-1-attempt-1", cwd=remote) == ""
    assert _git("for-each-ref", "--format=%(refname)", receipt.article_handoff_ref, cwd=remote) == ""
    assert _git("for-each-ref", "--format=%(refname)", receipt.claim_ref, cwd=remote) == ""
    _git("fsck", "--full", cwd=remote)


def test_remote_merge_queue_rejects_article_claim_for_wrong_object_format(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = ClaimFence(
        key="author/article-0123456789abcdef",
        ref="refs/autoform-claims/author/article-0123456789abcdef",
        oid="1" * 64,
        lease_id="2" * 64,
    )

    with pytest.raises(MergeQueueError, match="object format"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )

    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/queue-1", cwd=remote) == ""


@pytest.mark.parametrize("boundary", ("publication-staging-created", "publication-staging-recorded"))
def test_publication_staging_resumes_before_final_directory_rename(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    state = tmp_path / "queue-state"
    queue = _queue(manager, remote, state, "worker-a")
    article_claim = _article_claim(queue)

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )
    staging_journal = state / "publications/.queue-1.preparing/publication.json"
    assert staging_journal.is_file() == (boundary == "publication-staging-recorded")
    assert not (state / "publications/queue-1").exists()

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    recovered = queue.recover(
        "queue-1",
        target_ref="refs/heads/main",
        queue_ref="refs/autoform/queue/queue-1",
        expected_target_oid=base,
        candidate_oid=candidate,
        article_claim=article_claim,
    )
    assert recovered.status == "prepared"
    receipt = queue.publish(
        "queue-1",
        target_ref="refs/heads/main",
        queue_ref="refs/autoform/queue/queue-1",
        expected_target_oid=base,
        candidate_oid=candidate,
        article_claim=article_claim,
    )
    assert receipt.status == "integrated"
    assert not (state / "publications/.queue-1.preparing").exists()


def test_recovery_binds_stable_article_lease_across_prepublication_renewal(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    original_claim = _article_claim(queue)
    assert queue.claim_board.renew(
        original_claim.key,
        ttl=600,
        lease_id=original_claim.lease_id,
    )
    renewed_claim = queue.claim_board.held_claim_fence(original_claim.key)
    assert renewed_claim is not None
    assert renewed_claim.oid != original_claim.oid
    assert renewed_claim.lease_id == original_claim.lease_id

    def interrupt(name: str) -> None:
        if name == "publication-intent-recorded":
            raise KeyboardInterrupt

    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
    }
    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish("queue-1", article_claim=renewed_claim, **arguments)

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    wrong_lease = ClaimFence(
        key=original_claim.key,
        ref=original_claim.ref,
        oid=original_claim.oid,
        lease_id="f" * 64,
    )
    with pytest.raises(PublicationUncertain, match="different article_claim_lease_id"):
        queue.recover("queue-1", article_claim=wrong_lease, **arguments)
    recovered = queue.recover("queue-1", article_claim=original_claim, **arguments)
    assert recovered.status == "prepared"
    assert recovered.article_claim == renewed_claim
    receipt = queue.publish("queue-1", article_claim=recovered.article_claim, **arguments)
    assert receipt.status == "integrated"


def test_pre_journal_atomic_write_orphan_does_not_block_publication_recovery(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    state = tmp_path / "queue-state"
    queue = _queue(manager, remote, state, "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }

    def interrupt(name: str) -> None:
        if name == "publication-staging-created":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish("queue-1", **arguments)
    journal = state / "publications/.queue-1.preparing/publication.json"
    orphan = journal.parent / f"{repository_module._atomic_write_prefix(journal)}deadbeef.tmp"
    orphan.write_bytes(b'{"partial":')
    orphan.chmod(0o600)

    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)
    receipt = queue.publish("queue-1", **arguments)
    assert receipt.status == "integrated"
    assert not orphan.exists()


def test_remote_merge_queue_rejects_stale_remote_without_publishing_queue_ref(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, seed, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="stale candidate\n",
    )
    (seed / "book.txt").write_text("remote moved\n", encoding="utf-8")
    _git("add", "book.txt", cwd=seed)
    _git("commit", "--quiet", "-m", "remote moved", cwd=seed)
    moved = _git("rev-parse", "HEAD", cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")

    with pytest.raises(RemoteDrift, match="drift"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )

    assert _git("rev-parse", "refs/heads/main", cwd=remote) == moved
    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/queue-1", cwd=remote) == ""


@pytest.mark.parametrize("substitution", ("replace", "graft"))
def test_candidate_ancestry_ignores_git_graph_substitution(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    substitution: str,
) -> None:
    remote, _, coordinator, base = git_repository
    tree = _git("rev-parse", f"{base}^{{tree}}", cwd=coordinator)
    descendant = _git("commit-tree", tree, "-p", base, "-m", "descendant", cwd=coordinator)
    unrelated = _git("commit-tree", tree, "-m", "unrelated", cwd=coordinator)
    if substitution == "replace":
        _git("replace", unrelated, descendant, cwd=coordinator)
    else:
        git_dir = Path(_git("rev-parse", "--absolute-git-dir", cwd=coordinator))
        graft = git_dir / "info/grafts"
        graft.parent.mkdir(parents=True, exist_ok=True)
        graft.write_text(f"{unrelated} {base}\n", encoding="utf-8")

    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    with pytest.raises(MergeQueueError, match="not descended"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=unrelated,
            article_claim=_article_claim(queue),
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/queue-1", cwd=remote) == ""


def test_concurrent_publishers_cannot_overwrite_each_other(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidates = [
        _candidate(
            manager,
            run_id="run-1",
            attempt_id=f"attempt-{index}",
            base=base,
            content=f"candidate {index}\n",
        )
        for index in (1, 2)
    ]
    queues = [_queue(manager, remote, tmp_path / f"queue-state-{index}", f"worker-{index}") for index in (1, 2)]
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, object]] = []

    def publish(index: int) -> None:
        barrier.wait(timeout=5)
        try:
            result: object = queues[index].publish(
                f"queue-{index}",
                target_ref="refs/heads/main",
                queue_ref=f"refs/autoform/queue/queue-{index}",
                expected_target_oid=base,
                candidate_oid=candidates[index],
                article_claim=_article_claim(queues[index]),
            )
        except (MergeQueueBusy, RemoteDrift) as error:
            result = error
        outcomes.append((index, result))

    threads = [threading.Thread(target=publish, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    assert len([value for _, value in outcomes if not isinstance(value, Exception)]) == 1

    winner = next(index for index, value in outcomes if not isinstance(value, Exception))
    loser = 1 - winner
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidates[winner]
    with pytest.raises(RemoteDrift):
        queues[loser].publish(
            f"queue-{loser}",
            target_ref="refs/heads/main",
            queue_ref=f"refs/autoform/queue/queue-{loser}",
            expected_target_oid=base,
            candidate_oid=candidates[loser],
            article_claim=_article_claim(queues[loser]),
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidates[winner]


@pytest.mark.parametrize(
    ("boundary", "recovered_status"),
    (
        ("publication-intent-recorded", "prepared"),
        ("queue-push-attempted", "prepared"),
        ("queue-pushed", "queued"),
        ("claim-fence-recorded", "queued"),
        ("target-push-attempted", "queued"),
        ("target-pushed", "integrated"),
        ("target-verified", "integrated"),
    ),
)
def test_publication_interruption_is_classified_from_remote_evidence(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    recovered_status: str,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }

    def interrupt(name: str) -> None:
        if name == boundary:
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish("queue-1", **arguments)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    recovered = queue.recover("queue-1", **arguments)
    assert recovered.status == recovered_status
    final = queue.publish("queue-1", **arguments)
    assert final.status == "integrated"
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidate


def test_atomic_push_disconnect_is_uncertain_then_recovers_from_remote_evidence(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }
    original_remote_git = queue._remote_git

    def apply_then_report_disconnect(
        git_arguments: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = original_remote_git(git_arguments, check=check)
        if (
            git_arguments[0] == "push"
            and "--atomic" in git_arguments
            and f"{candidate}:refs/heads/main" in git_arguments
        ):
            return subprocess.CompletedProcess(
                result.args,
                1,
                stdout=result.stdout,
                stderr="connection reset after remote update",
            )
        return result

    monkeypatch.setattr(queue, "_remote_git", apply_then_report_disconnect)
    with pytest.raises(PublicationUncertain, match="outcome is uncertain"):
        queue.publish("queue-1", **arguments)

    monkeypatch.setattr(queue, "_remote_git", original_remote_git)
    recovered = queue.recover("queue-1", **arguments)
    assert recovered.status == "integrated"
    assert recovered.observed_target_oid == candidate
    assert recovered.observed_queue_oid is None


def test_queue_handoff_disconnect_recovers_from_three_ref_remote_evidence(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = _article_claim(queue)
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": article_claim,
    }
    original_remote_git = queue._remote_git

    def apply_handoff_then_report_disconnect(
        git_arguments: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = original_remote_git(git_arguments, check=check)
        if (
            git_arguments[0] == "push"
            and "--atomic" in git_arguments
            and f"{candidate}:refs/autoform/queue/queue-1" in git_arguments
        ):
            return subprocess.CompletedProcess(
                result.args,
                1,
                stdout=result.stdout,
                stderr="connection reset after remote update",
            )
        return result

    monkeypatch.setattr(queue, "_remote_git", apply_handoff_then_report_disconnect)
    with pytest.raises(PublicationUncertain, match="handoff outcome is uncertain"):
        queue.publish("queue-1", **arguments)

    monkeypatch.setattr(queue, "_remote_git", original_remote_git)
    recovered = queue.recover("queue-1", **arguments)
    assert recovered.status == "queued"
    assert recovered.observed_queue_oid == candidate
    assert recovered.observed_article_handoff_oid == candidate
    assert recovered.observed_article_claim_oid is None
    receipt = queue.publish("queue-1", **arguments)
    assert receipt.status == "integrated"
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidate
    assert _git("for-each-ref", "--format=%(refname)", arguments["queue_ref"], cwd=remote) == ""


def test_article_claim_steal_before_atomic_handoff_cannot_create_queue_ref(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = _article_claim(queue)
    thief = _FencedTestClaimBoard(remote, "worker-b", tmp_path / "thief-claims")
    original_handoff = queue._atomic_queue_handoff

    def steal_then_handoff(**arguments: object) -> bool:
        assert thief.acquire(article_claim.key, ttl=600, steal=True)
        return original_handoff(**arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(queue, "_atomic_queue_handoff", steal_then_handoff)
    with pytest.raises(PublicationUncertain, match="article claim .* changed during queue handoff"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )

    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/queue-1", cwd=remote) == ""
    assert thief.holds(article_claim.key)
    assert thief.release(article_claim.key)


def test_incoherent_article_claim_fence_cannot_create_queue_ref(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = _article_claim(queue)
    original_fence = queue.claim_board.held_claim_fence

    def incoherent_fence(key: str) -> ClaimFence | None:
        fence = original_fence(key)
        if key != article_claim.key or fence is None:
            return fence
        return ClaimFence(key=fence.key, ref=fence.ref, oid=fence.oid, lease_id="f" * 64)

    monkeypatch.setattr(queue.claim_board, "held_claim_fence", incoherent_fence)
    with pytest.raises(PublicationUncertain, match="not owned by this exact controller session"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )

    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/queue-1", cwd=remote) == ""


def test_author_handoff_barrier_blocks_successor_until_publication_finishes(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = _article_claim(queue)
    successor = _FencedTestClaimBoard(remote, "worker-b", tmp_path / "successor-claims")

    blocked_during_handoff: list[bool] = []

    def acquire_successor(name: str) -> None:
        if name == "queue-pushed":
            blocked_during_handoff.append(not successor.acquire(article_claim.key, ttl=600))

    monkeypatch.setattr(repository_module, "_checkpoint", acquire_successor)
    receipt = queue.publish(
        "queue-1",
        target_ref="refs/heads/main",
        queue_ref="refs/autoform/queue/queue-1",
        expected_target_oid=base,
        candidate_oid=candidate,
        article_claim=article_claim,
    )

    assert blocked_during_handoff == [True]
    assert receipt.status == "integrated"
    assert receipt.observed_article_claim_oid is None
    assert receipt.observed_article_handoff_oid is None
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidate
    assert successor.acquire(article_claim.key, ttl=600)
    assert successor.release(article_claim.key)


def test_article_claim_reappearance_while_queued_blocks_publication(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = _article_claim(queue)
    replacement = _git("hash-object", "-w", "book.txt", cwd=coordinator)

    def restore_claim(name: str) -> None:
        if name == "queue-pushed":
            _git("update-ref", article_claim.ref, replacement, cwd=remote)

    monkeypatch.setattr(repository_module, "_checkpoint", restore_claim)
    with pytest.raises(PublicationUncertain, match="article claim .* changed"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("rev-parse", claim_handoff_ref(article_claim.key), cwd=remote) == candidate


def test_queue_ref_collision_is_preserved_as_uncertain_state(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    _git("update-ref", "refs/autoform/queue/queue-1", base, cwd=remote)
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")

    with pytest.raises(PublicationUncertain, match="collision"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("rev-parse", "refs/autoform/queue/queue-1", cwd=remote) == base


def test_target_candidate_does_not_hide_a_colliding_queue_ref(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    _git("push", "--quiet", str(remote), f"{candidate}:refs/heads/main", cwd=coordinator)
    _git("update-ref", "refs/autoform/queue/queue-1", base, cwd=remote)
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")

    with pytest.raises(PublicationUncertain, match="collision"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidate
    assert _git("rev-parse", "refs/autoform/queue/queue-1", cwd=remote) == base


def test_queue_ref_change_during_target_cas_never_records_integration(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")

    def replace_queue(name: str) -> None:
        if name == "target-push-attempted":
            _git("update-ref", "refs/autoform/queue/queue-1", base, cwd=remote)

    monkeypatch.setattr(repository_module, "_checkpoint", replace_queue)
    with pytest.raises(PublicationUncertain, match="changed during target publication"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("rev-parse", "refs/autoform/queue/queue-1", cwd=remote) == base


def test_author_handoff_change_during_target_cas_never_records_integration(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    article_claim = _article_claim(queue)
    handoff_ref = claim_handoff_ref(article_claim.key)

    def replace_handoff(name: str) -> None:
        if name == "target-push-attempted":
            _git("update-ref", handoff_ref, base, cwd=remote)

    monkeypatch.setattr(repository_module, "_checkpoint", replace_handoff)
    with pytest.raises(PublicationUncertain, match="handoff ref .* changed"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert _git("rev-parse", handoff_ref, cwd=remote) == base


def test_target_and_queue_changes_without_merge_claim_consumption_are_uncertain(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }

    def interrupt(name: str) -> None:
        if name == "claim-fence-recorded":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish("queue-1", **arguments)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    journal = tmp_path / "queue-state/publications/queue-1/publication.json"
    record = queue._read_journal(journal)
    _git("update-ref", str(record["claim_ref"]), str(record["claim_oid"]), cwd=remote)
    _git("update-ref", "refs/heads/main", candidate, base, cwd=remote)
    _git("update-ref", "-d", arguments["queue_ref"], candidate, cwd=remote)
    _git("update-ref", "-d", str(record["article_handoff_ref"]), candidate, cwd=remote)
    recovered = queue.recover("queue-1", **arguments)
    assert recovered.status == "uncertain"
    assert "without consuming the exact merge claim" in recovered.detail


def test_recovery_without_recorded_merge_fence_rejects_live_merge_claim(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }

    def interrupt(name: str) -> None:
        if name == "queue-pushed":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish("queue-1", **arguments)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    merge_key = repository_module._merge_claim_key("refs/heads/main")
    other_publisher = _FencedTestClaimBoard(remote, "worker-b", tmp_path / "other-claims")
    assert other_publisher.acquire(merge_key, ttl=600)
    _git("update-ref", "refs/heads/main", candidate, base, cwd=remote)
    _git("update-ref", "-d", arguments["queue_ref"], candidate, cwd=remote)
    _git(
        "update-ref",
        "-d",
        claim_handoff_ref(arguments["article_claim"].key),
        candidate,
        cwd=remote,
    )

    recovered = queue.recover("queue-1", **arguments)
    assert recovered.status == "uncertain"
    assert "without consuming the exact merge claim" in recovered.detail


def test_claim_steal_before_atomic_publication_cannot_advance_target(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    thief = _FencedTestClaimBoard(remote, "worker-b", tmp_path / "thief-claims")
    claim_key = repository_module._merge_claim_key("refs/heads/main")
    original_push = queue._atomic_target_push

    def steal_then_push(**arguments: object) -> bool:
        assert thief.acquire(claim_key, ttl=600, steal=True)
        return original_push(**arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(queue, "_atomic_target_push", steal_then_push)
    with pytest.raises(PublicationUncertain, match="claim ref .* changed during target publication"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
    assert thief.holds(claim_key)
    assert thief.release(claim_key)


@pytest.mark.parametrize("successor_worker", ("worker-b", "worker-a"))
def test_successor_claim_after_atomic_publication_does_not_obscure_success(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    successor_worker: str,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    successor = _FencedTestClaimBoard(remote, successor_worker, tmp_path / "successor-claims")
    claim_key = repository_module._merge_claim_key("refs/heads/main")

    def acquire_successor(name: str) -> None:
        if name == "target-pushed":
            assert successor.acquire(claim_key, ttl=600)

    monkeypatch.setattr(repository_module, "_checkpoint", acquire_successor)
    receipt = queue.publish(
        "queue-1",
        target_ref="refs/heads/main",
        queue_ref="refs/autoform/queue/queue-1",
        expected_target_oid=base,
        candidate_oid=candidate,
        article_claim=_article_claim(queue),
    )
    assert receipt.status == "integrated"
    assert receipt.observed_claim_oid not in {None, receipt.claim_oid}
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == candidate
    assert successor.holds(claim_key)
    assert successor.release(claim_key)


def test_remote_target_oid_cannot_substitute_for_local_candidate_validation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, seed, coordinator, base = git_repository
    (seed / "book.txt").write_text("remote-only\n", encoding="utf-8")
    _git("add", "book.txt", cwd=seed)
    _git("commit", "--quiet", "-m", "remote-only", cwd=seed)
    remote_only = _git("rev-parse", "HEAD", cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")

    with pytest.raises(RepositoryError, match="does not resolve exactly"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=remote_only,
            article_claim=_article_claim(queue),
        )
    assert _git("for-each-ref", "--format=%(refname)", "refs/autoform/queue/queue-1", cwd=remote) == ""


def test_local_remote_replacement_is_rejected_before_any_ref_mutation(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    original = remote.with_name("original.git")
    remote.rename(original)
    _git("init", "--bare", "--quiet", str(remote))

    with pytest.raises(PublicationUncertain, match="remote was replaced"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )
    assert _git("for-each-ref", "--format=%(refname)", cwd=remote) == ""


def test_local_remote_replacement_across_restart_is_rejected(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    state = tmp_path / "queue-state"
    queue = _queue(manager, remote, state, "worker-a")
    original_identity = (remote.stat().st_dev, remote.stat().st_ino)
    queue.close()

    original = remote.with_name("original.git")
    remote.rename(original)
    _git("clone", "--bare", "--quiet", str(original), str(remote))
    assert (remote.stat().st_dev, remote.stat().st_ino) != original_identity

    with pytest.raises(PublicationUncertain, match="replaced across restart"):
        _queue(manager, remote, state, "worker-b")
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base


def test_publication_directory_substitution_is_read_only_and_fails_closed(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    state = tmp_path / "queue-state"
    queue = _queue(manager, remote, state, "worker-a")
    arguments = {
        "target_ref": "refs/heads/main",
        "queue_ref": "refs/autoform/queue/queue-1",
        "expected_target_oid": base,
        "candidate_oid": candidate,
        "article_claim": _article_claim(queue),
    }

    def interrupt(name: str) -> None:
        if name == "publication-intent-recorded":
            raise KeyboardInterrupt

    monkeypatch.setattr(repository_module, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        queue.publish("queue-1", **arguments)
    monkeypatch.setattr(repository_module, "_checkpoint", lambda _name: None)

    publication = state / "publications/queue-1"
    original = publication.with_name("queue-1-original")
    publication.rename(original)
    foreign = tmp_path / "foreign-publication"
    foreign.mkdir()
    (foreign / "publication.json").write_bytes((original / "publication.json").read_bytes())
    sentinel = foreign / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    publication.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(PublicationUncertain, match="directory was replaced"):
        queue.recover("queue-1", **arguments)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base


def test_publication_does_not_adopt_a_foreign_empty_directory(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )
    queue = _queue(manager, remote, tmp_path / "queue-state", "worker-a")
    foreign = tmp_path / "queue-state/publications/queue-1"
    foreign.mkdir()

    with pytest.raises(PublicationUncertain, match="without a durable ownership journal"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=_article_claim(queue),
        )
    assert list(foreign.iterdir()) == []
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base


def test_claim_is_released_when_fencing_receipt_lookup_fails(
    tmp_path: Path,
    git_repository: tuple[Path, Path, Path, str],
) -> None:
    remote, _, coordinator, base = git_repository
    manager = AttemptWorktrees(coordinator, tmp_path / "attempt-state")
    candidate = _candidate(
        manager,
        run_id="run-1",
        attempt_id="attempt-1",
        base=base,
        content="candidate\n",
    )

    class FailingReceiptBoard:
        repo_url = str(remote)
        released = False

        def acquire(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def held_lease_id(self, _key: str) -> str:
            raise RuntimeError("receipt unavailable")

        def held_claim_oid(self, _key: str) -> str:
            return base

        def held_claim_fence(self, _key: str) -> ClaimFence:
            raise RuntimeError("receipt unavailable")

        def release(self, _key: str) -> bool:
            self.released = True
            return True

    board = FailingReceiptBoard()
    queue = RemoteMergeQueue(
        manager,
        remote_url=remote,
        state_root=tmp_path / "queue-state",
        worker_id="worker-a",
        claim_board=board,
    )
    article_claim = ClaimFence(
        key="author/af_0123456789abcdef01234567",
        ref="refs/autoform-claims/author/af_0123456789abcdef01234567",
        oid=base,
        lease_id="1" * 64,
    )
    _git("update-ref", article_claim.ref, article_claim.oid, cwd=remote)
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        queue.publish(
            "queue-1",
            target_ref="refs/heads/main",
            queue_ref="refs/autoform/queue/queue-1",
            expected_target_oid=base,
            candidate_oid=candidate,
            article_claim=article_claim,
        )
    assert board.released
    assert _git("rev-parse", "refs/heads/main", cwd=remote) == base
