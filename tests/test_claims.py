"""Tests for host-neutral Git-ref claim leases."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from autoform_cli import claims


def _git(*args: str, cwd: Path | None = None, input_text: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return proc.stdout.strip()


@pytest.fixture
def board_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    return repo


def _board(
    tmp_path: Path,
    repo: Path,
    owner: str,
    *,
    session_id: str | None = None,
    scratch: Path | None = None,
) -> claims.ClaimBoard:
    return claims.ClaimBoard(
        repo,
        owner,
        scratch or tmp_path / f"scratch-{owner}",
        session_id=session_id,
    )


def _plant_message(repo: Path, key: str, message: str) -> str:
    tree = _git("mktree", cwd=repo, input_text="")
    commit = _git("commit-tree", tree, "-m", message, cwd=repo)
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, commit, cwd=repo)
    return commit


def _plant_lease(repo: Path, key: str, **changes: object) -> str:
    lease: dict[str, object] = {
        "schema": claims.CLAIM_SCHEMA,
        "lease_id": "1" * 64,
        "owner": "original-owner",
        "host": "test-host",
        "pid": 1,
        "acquired_at": 100.0,
        "renewed_at": 100.0,
        "expires_at": 200.0,
        "resource": key,
    }
    lease.update(changes)
    return _plant_message(repo, key, json.dumps(lease))


def test_acquire_read_list_and_release_round_trip(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("author/node", ttl=600, note="proof")
    lease = board.read("author/node")
    assert lease is not None
    assert lease["schema"] == claims.CLAIM_SCHEMA
    assert claims.LEASE_ID_RE.fullmatch(lease["lease_id"])
    assert lease["owner"] == "worker-a"
    assert lease["resource"] == "author/node"
    assert lease["note"] == "proof"
    assert board.holds("author/node")

    listed = board.list()
    assert [(item["_key"], item["_expired"]) for item in listed] == [("author/node", False)]
    assert board.release("author/node")
    assert board.read("author/node") is None
    assert board.release("author/node")


def test_cas_acquire_race_has_exactly_one_winner(tmp_path: Path, board_repo: Path) -> None:
    boards = [_board(tmp_path, board_repo, owner) for owner in ("worker-a", "worker-b")]
    barrier = threading.Barrier(2)
    original_remote_oid = claims.ClaimBoard._remote_oid

    def synchronized_remote_oid(self: claims.ClaimBoard, key: str) -> str | None:
        oid = original_remote_oid(self, key)
        barrier.wait(timeout=5)
        return oid

    for board in boards:
        board._remote_oid = synchronized_remote_oid.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]

    results: list[bool] = []
    errors: list[BaseException] = []

    def acquire(board: claims.ClaimBoard) -> None:
        try:
            results.append(board.acquire("race", ttl=600))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=acquire, args=(board,)) for board in boards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    for board in boards:
        board._remote_oid = original_remote_oid.__get__(board, claims.ClaimBoard)  # type: ignore[method-assign]
    assert boards[0].read("race")["owner"] in {"worker-a", "worker-b"}


def test_expired_lease_can_be_taken_over(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    first = _board(tmp_path, board_repo, "worker-a")
    second = _board(tmp_path, board_repo, "worker-b")

    assert first.acquire("expired", ttl=10)
    first_lease_id = first.read("expired")["lease_id"]
    monkeypatch.setattr(claims.time, "time", lambda: now + 11)
    assert not first.holds("expired")
    assert second.acquire("expired", ttl=60)
    assert second.read("expired")["owner"] == "worker-b"
    assert second.read("expired")["lease_id"] != first_lease_id


def test_malformed_lease_is_unverifiable_and_not_takeover_eligible(tmp_path: Path, board_repo: Path) -> None:
    _plant_message(board_repo, "malformed", "not json")
    board = _board(tmp_path, board_repo, "worker-a")

    for operation in (
        lambda: board.read("malformed"),
        lambda: board.renew("malformed"),
        lambda: board.release("malformed"),
        lambda: board.acquire("malformed", ttl=600),
    ):
        with pytest.raises(claims.MalformedLeaseError):
            operation()

    listed = board.list()
    assert listed[0]["schema"] == "unreadable"
    assert listed[0]["_malformed"] is True
    assert listed[0]["_expired"] is False
    assert board.cleanup() == 0


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_acquire_rejects_nonfinite_ttl_without_mutating_remote(
    tmp_path: Path, board_repo: Path, ttl: float
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="finite positive number"):
        board.acquire("nonfinite", ttl=ttl)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "nonfinite", cwd=board_repo) == ""


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_renew_rejects_nonfinite_ttl_without_replacing_lease(
    tmp_path: Path, board_repo: Path, ttl: float
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("owned", ttl=30)
    oid = board._remote_oid("owned")

    with pytest.raises(ValueError, match="finite positive number"):
        board.renew("owned", ttl=ttl)

    assert board._remote_oid("owned") == oid


def test_owner_only_renew_and_release(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    owner = _board(tmp_path, board_repo, "owner")
    peer = _board(tmp_path, board_repo, "peer")
    assert owner.acquire("owned", ttl=30)
    first_expiry = owner.read("owned")["expires_at"]

    assert not peer.renew("owned")
    assert not peer.release("owned")
    assert not peer.acquire("owned", ttl=30)
    monkeypatch.setattr(claims.time, "time", lambda: now + 5)
    assert owner.renew("owned", ttl=30)
    assert owner.read("owned")["expires_at"] > first_expiry
    assert owner.release("owned")


def test_steal_cannot_replace_a_live_peer_lease(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(tmp_path, board_repo, "owner", session_id="owner-session")
    peer = _board(tmp_path, board_repo, "peer", session_id="peer-session")
    assert owner.acquire("owned", ttl=30)
    oid = owner._remote_oid("owned")

    assert not peer.acquire("owned", ttl=30, steal=True)

    assert owner._remote_oid("owned") == oid
    assert owner.holds("owned")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_explicit_comparison_clock(now: float) -> None:
    lease = {"expires_at": 200.0}

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired(lease, now=now)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_default_comparison_clock(
    monkeypatch: pytest.MonkeyPatch, now: float
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: now)

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired({"expires_at": 200.0})


def test_cleanup_removes_only_expired_snapshot_entries(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("dead", ttl=5)
    assert board.acquire("live", ttl=500)
    monkeypatch.setattr(claims.time, "time", lambda: 1_010.0)

    assert board.cleanup() == 1
    assert [lease["_key"] for lease in board.list()] == ["live"]


def test_cleanup_cas_does_not_delete_renewed_lease(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    cleaner = _board(tmp_path, board_repo, "worker-a")
    assert cleaner.acquire("lease", ttl=5)
    monkeypatch.setattr(claims.time, "time", lambda: 1_010.0)

    original_list = cleaner.list
    owner = _board(tmp_path, board_repo, "worker-a")

    def list_then_renew() -> list[dict[str, object]]:
        snapshot = original_list()
        assert owner.acquire("lease", ttl=500)
        return snapshot

    monkeypatch.setattr(cleaner, "list", list_then_renew)
    assert cleaner.cleanup() == 0
    assert cleaner.read("lease")["expires_at"] == 1_510.0


def test_cleanup_replaces_expired_v1_author_ref_with_a_compatibility_block(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = claims.author_claim_key("chapter/old-path")
    _plant_lease(
        board_repo,
        key,
        schema=claims.LEGACY_CLAIM_SCHEMA,
        lease_id=None,
    )
    monkeypatch.setattr(claims.time, "time", lambda: 201.0)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="blueprint is required"):
        board.cleanup()
    assert board.read(key)["schema"] == claims.LEGACY_CLAIM_SCHEMA
    assert board.cleanup(canonical_keys=[]) == 1
    block = board.read(key)
    assert block is not None
    assert block["schema"] == claims.LEGACY_BLOCK_SCHEMA
    assert block["canonical_resource"] == "legacy-rollout"


def test_worker_id_is_metadata_not_lease_authority(tmp_path: Path, board_repo: Path) -> None:
    owner = _board(
        tmp_path,
        board_repo,
        "same-worker",
        session_id="session-a",
        scratch=tmp_path / "session-a",
    )
    peer = _board(
        tmp_path,
        board_repo,
        "same-worker",
        session_id="session-b",
        scratch=tmp_path / "session-b",
    )

    assert owner.acquire("article", ttl=600)
    assert owner.holds("article")
    assert not peer.holds("article")
    assert not peer.acquire("article", ttl=600)
    assert not peer.renew("article", ttl=600)
    assert not peer.release("article")
    assert owner.holds("article")


def test_exact_receipt_is_fenced_after_another_copy_renews(
    tmp_path: Path, board_repo: Path
) -> None:
    owner = _board(
        tmp_path,
        board_repo,
        "worker-a",
        session_id="shared-session",
        scratch=tmp_path / "owner",
    )
    stale = _board(
        tmp_path,
        board_repo,
        "worker-a",
        session_id="shared-session",
        scratch=tmp_path / "stale",
    )
    assert owner.acquire("article", ttl=600)
    original = owner._remote_oid("article")
    assert original is not None
    stale._ensure_scratch()
    stale._git(["fetch", "--quiet", str(board_repo), f"+{claims.CLAIM_REF_PREFIX}article:{claims.CLAIM_REF_PREFIX}article"])
    stale._record_receipt("article", original)
    assert stale.holds("article")

    assert owner.renew("article", ttl=600)
    assert not stale.holds("article")
    assert not stale.renew("article", ttl=600)
    assert not stale.release("article")


def test_receipt_failure_after_remote_acquire_is_uncertain_and_fails_closed(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a", session_id="session-a")

    def fail_receipt(*args: object, **kwargs: object) -> None:
        raise claims.ClaimTransportError("receipt unavailable")

    monkeypatch.setattr(board, "_record_receipt", fail_receipt)
    with pytest.raises(claims.ClaimTransportError, match="receipt unavailable"):
        board.acquire("article", ttl=600)

    assert board.read("article")["schema"] == claims.CLAIM_SCHEMA
    assert not board.holds("article")


def test_receipt_failure_after_remote_renewal_leaves_the_old_receipt_fenced(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a", session_id="session-a")
    assert board.acquire("article", ttl=600)
    old = board._remote_oid("article")

    def fail_receipt(*args: object, **kwargs: object) -> None:
        raise claims.ClaimTransportError("receipt unavailable")

    monkeypatch.setattr(board, "_record_receipt", fail_receipt)
    with pytest.raises(claims.ClaimTransportError, match="receipt unavailable"):
        board.renew("article", ttl=600)

    assert board._remote_oid("article") != old
    assert board._receipt_oid("article") == old
    assert not board.holds("article")


def test_release_of_absent_remote_cannot_clear_a_concurrent_acquire_receipt(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "shared"
    releaser = _board(
        tmp_path,
        board_repo,
        "worker-a",
        session_id="shared-session",
        scratch=scratch,
    )
    acquirer = _board(
        tmp_path,
        board_repo,
        "worker-a",
        session_id="shared-session",
        scratch=scratch,
    )
    releaser._ensure_scratch()
    original_remote_oid = releaser._remote_oid

    def absent_then_acquire(key: str) -> None:
        assert original_remote_oid(key) is None
        assert acquirer.acquire(key, ttl=600)
        return None

    monkeypatch.setattr(releaser, "_remote_oid", absent_then_acquire)

    with pytest.raises(claims.ClaimTransportError, match="receipt could not be cleared"):
        releaser.release("article")

    assert acquirer.holds("article")


def test_live_v1_blocks_v2_but_expired_v1_can_be_replaced(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "legacy"
    _plant_lease(
        board_repo,
        key,
        schema=claims.LEGACY_CLAIM_SCHEMA,
        lease_id=None,
    )
    board = _board(tmp_path, board_repo, "original-owner")
    monkeypatch.setattr(claims.time, "time", lambda: 150.0)

    assert not board.holds(key)
    assert not board.acquire(key, ttl=600)
    assert not board.renew(key, ttl=600)
    assert not board.release(key)

    monkeypatch.setattr(claims.time, "time", lambda: 201.0)
    assert board.acquire(key, ttl=600)
    lease = board.read(key)
    assert lease["schema"] == claims.CLAIM_SCHEMA
    assert claims.LEASE_ID_RE.fullmatch(lease["lease_id"])


def test_legacy_compatibility_block_is_permanent_and_rejected_by_v1_clients(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = claims.author_claim_key("chapter/result")
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.install_legacy_compatibility(key, canonical_key="author/durable")
    block = board.read(key)
    assert block is not None
    assert block["schema"] == claims.LEGACY_BLOCK_SCHEMA
    assert not board.expired(block)
    assert board.cleanup() == 0

    class D9Client(claims.ClaimBoard):
        @staticmethod
        def _lease_is_valid(lease: dict[str, object], key: str | None = None) -> bool:
            return bool(
                lease.get("schema") == claims.LEGACY_CLAIM_SCHEMA
                and claims.ClaimBoard._lease_is_valid(lease, key)
            )

    old_client = D9Client(board_repo, "old-worker", tmp_path / "old-client")
    with pytest.raises(claims.MalformedLeaseError, match="invalid lease schema"):
        old_client.acquire(key, ttl=600)


def test_legacy_compatibility_install_cannot_overwrite_a_racing_v1_acquire(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = claims.author_claim_key("chapter/result")
    board = _board(tmp_path, board_repo, "worker-a")
    board._ensure_scratch()
    original_remote_oid = board._remote_oid

    def absent_then_legacy_acquire(candidate: str) -> None:
        assert original_remote_oid(candidate) is None
        now = claims.time.time()
        _plant_lease(
            board_repo,
            candidate,
            schema=claims.LEGACY_CLAIM_SCHEMA,
            lease_id=None,
            acquired_at=now,
            renewed_at=None,
            expires_at=now + 600,
        )
        return None

    monkeypatch.setattr(board, "_remote_oid", absent_then_legacy_acquire)

    assert not board.install_legacy_compatibility(key, canonical_key="author/durable")
    inspector = _board(tmp_path, board_repo, "inspector")
    assert inspector.read(key)["schema"] == claims.LEGACY_CLAIM_SCHEMA


def test_v2_lease_is_rejected_by_the_v1_schema_contract(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("article", ttl=600)

    class V1Client(claims.ClaimBoard):
        @staticmethod
        def _lease_is_valid(lease: dict[str, object], key: str | None = None) -> bool:
            return bool(
                lease.get("schema") == claims.LEGACY_CLAIM_SCHEMA
                and claims.ClaimBoard._lease_is_valid(lease, key)
            )

    old_client = V1Client(board_repo, "worker-a", tmp_path / "old-client")
    with pytest.raises(claims.MalformedLeaseError, match="invalid lease schema"):
        old_client.read("article")


def test_resource_claim_keys_are_distinct_from_article_claim_keys() -> None:
    assert claims.resource_claim_key("lake-build") != claims.author_claim_key("lake-build")


def test_author_claim_keys_are_ref_safe_and_resist_slug_collisions() -> None:
    node_ids = ["a b", "a-b", "A/B", "A B", "Évariste Galois", "!!!", "x" * 200]
    keys = [claims.author_claim_key(node_id) for node_id in node_ids]

    assert len(keys) == len(set(keys))
    assert all(key.startswith("author/") for key in keys)
    assert all(claims.CLAIM_KEY_RE.fullmatch(key) for key in keys)
    assert all(".." not in key for key in keys)


@pytest.mark.parametrize(
    "key",
    ["has space", "a/../b", "/leading", "trailing/", "refs/heads/x@{1}", ".hidden", "ends.", "lease.lock"],
)
def test_invalid_keys_are_rejected(tmp_path: Path, board_repo: Path, key: str) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    with pytest.raises(ValueError, match="invalid claim key"):
        board.acquire(key)


def test_relative_local_repo_path_is_resolved_before_entering_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    monkeypatch.chdir(tmp_path)
    board = claims.ClaimBoard("claims.git", "worker-a", tmp_path / "scratch")

    assert board.acquire("relative", ttl=600)
    assert board.read("relative")["owner"] == "worker-a"


def test_transport_failure_raises_without_local_fallback(tmp_path: Path) -> None:
    board = claims.ClaimBoard(tmp_path / "missing" / "claims.git", "worker-a", tmp_path / "scratch")

    with pytest.raises(claims.ClaimTransportError):
        board.acquire("key")
    assert not (board.scratch / claims.CLAIM_REF_PREFIX / "key").exists()


def test_heartbeat_verifies_ownership_immediately_on_entry() -> None:
    class LostBoard:
        def held_lease_id(self, key: str) -> str | None:
            return None

        def renew(self, key: str, ttl: int | float, *, lease_id: str | None = None) -> bool:
            raise AssertionError("an unheld lease must not be renewed")

    heartbeat = claims.Heartbeat(LostBoard(), "key", interval=1, ttl=30)  # type: ignore[arg-type]
    with pytest.raises(claims.ClaimTransportError, match="lost before"):
        with heartbeat:
            pytest.fail("unowned work must not enter the protected context")

    assert heartbeat.lost.is_set()
    assert heartbeat._thread is None


def test_heartbeat_rejects_interval_that_can_outlive_lease() -> None:
    with pytest.raises(ValueError, match="shorter than"):
        claims.Heartbeat(object(), "key", interval=30, ttl=30)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("interval", "ttl", "message"),
    [
        (float("nan"), 30, "heartbeat interval must be a finite positive number"),
        (float("inf"), 30, "heartbeat interval must be a finite positive number"),
        (1, float("nan"), "claim TTL must be a finite positive number"),
        (1, float("inf"), "claim TTL must be a finite positive number"),
        (1, float("-inf"), "claim TTL must be a finite positive number"),
    ],
)
def test_heartbeat_rejects_nonfinite_timing(interval: float, ttl: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        claims.Heartbeat(object(), "key", interval=interval, ttl=ttl)  # type: ignore[arg-type]


def test_heartbeat_marks_ownership_lost_on_transport_failure() -> None:
    attempted = threading.Event()

    class FailingBoard:
        calls = 0

        def held_lease_id(self, key: str) -> str | None:
            return "a" * 64

        def renew(self, key: str, ttl: int | float, *, lease_id: str | None = None) -> bool:
            assert lease_id == "a" * 64
            self.calls += 1
            if self.calls == 1:
                return True
            attempted.set()
            raise claims.ClaimTransportError("board unavailable")

    heartbeat = claims.Heartbeat(FailingBoard(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert isinstance(heartbeat.error, claims.ClaimTransportError)


def test_heartbeat_marks_ownership_lost_when_renew_is_refused() -> None:
    attempted = threading.Event()

    class LostBoard:
        calls = 0

        def held_lease_id(self, key: str) -> str | None:
            return "b" * 64

        def renew(self, key: str, ttl: int | float, *, lease_id: str | None = None) -> bool:
            assert lease_id == "b" * 64
            self.calls += 1
            if self.calls == 1:
                return True
            attempted.set()
            return False

    heartbeat = claims.Heartbeat(LostBoard(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert heartbeat.error is None


def test_heartbeat_captures_one_lease_id_for_every_renewal() -> None:
    renewed: list[str | None] = []
    attempted = threading.Event()

    class Board:
        def held_lease_id(self, key: str) -> str | None:
            return "c" * 64

        def renew(self, key: str, ttl: int | float, *, lease_id: str | None = None) -> bool:
            renewed.append(lease_id)
            if len(renewed) > 1:
                attempted.set()
                return False
            return True

    heartbeat = claims.Heartbeat(Board(), "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    with heartbeat:
        assert attempted.wait(timeout=2)
        assert heartbeat.lost.wait(timeout=2)

    assert renewed == ["c" * 64, "c" * 64]


def test_heartbeat_exit_waits_for_inflight_renew_and_no_renewal_runs_after_exit() -> None:
    entered = threading.Event()
    allow_return = threading.Event()
    exited = threading.Event()

    class Board:
        calls = 0

        def held_lease_id(self, key: str) -> str | None:
            return "d" * 64

        def renew(self, key: str, ttl: int | float, *, lease_id: str | None = None) -> bool:
            self.calls += 1
            if self.calls >= 2:
                entered.set()
                if self.calls == 2:
                    assert allow_return.wait(timeout=2)
            return True

    board = Board()
    heartbeat = claims.Heartbeat(board, "key", interval=0.01, ttl=30)  # type: ignore[arg-type]
    heartbeat.__enter__()
    assert entered.wait(timeout=2)

    closer = threading.Thread(target=lambda: (heartbeat.__exit__(), exited.set()))
    closer.start()
    assert not exited.wait(timeout=0.05)
    allow_return.set()
    assert exited.wait(timeout=2)
    closer.join(timeout=2)
    assert not closer.is_alive()
    calls_at_exit = board.calls
    entered.clear()
    assert not entered.wait(timeout=0.05)
    assert board.calls == calls_at_exit


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "other"},
        {"resource": "different"},
        {"owner": ""},
        {"expires_at": "later"},
        {"renewed_at": "later"},
        {"renewed_at": 50.0},
        {"renewed_at": 201.0},
        {"acquired_at": float("nan")},
        {"acquired_at": float("inf")},
        {"acquired_at": float("-inf")},
        {"expires_at": float("nan")},
        {"expires_at": float("inf")},
        {"expires_at": float("-inf")},
        {"renewed_at": float("nan")},
        {"renewed_at": float("inf")},
        {"renewed_at": float("-inf")},
    ],
)
def test_schema_resource_or_required_field_mismatch_is_malformed(
    tmp_path: Path,
    board_repo: Path,
    changes: dict[str, object],
) -> None:
    _plant_lease(board_repo, "wrong", **changes)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError):
        board.holds("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.renew("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.release("wrong")
    with pytest.raises(claims.MalformedLeaseError):
        board.acquire("wrong", ttl=600)
    assert board.list()[0]["_malformed"] is True
    assert board.cleanup() == 0


@pytest.mark.parametrize(
    "field",
    ["schema", "lease_id", "owner", "resource", "acquired_at", "renewed_at", "expires_at"],
)
def test_planted_lease_with_duplicate_decision_field_is_rejected_by_strict_json_parser(
    tmp_path: Path, board_repo: Path, field: str
) -> None:
    values = {
        "schema": '"autoform-claim/v2"',
        "lease_id": '"' + "1" * 64 + '"',
        "owner": '"worker-a"',
        "resource": '"duplicate"',
        "acquired_at": "100.0",
        "renewed_at": "100.0",
        "expires_at": "200.0",
    }
    pairs = [f'"{name}":{value}' for name, value in values.items()]
    pairs.append(f'"{field}":{values[field]}')
    _plant_message(board_repo, "duplicate", "{" + ",".join(pairs) + "}")
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("duplicate")

    assert board.list()[0]["_malformed"] is True


def test_planted_nonfinite_lease_is_rejected_by_strict_json_parser(
    tmp_path: Path, board_repo: Path
) -> None:
    message = (
        '{"schema":"autoform-claim/v2","lease_id":"'
        + "1" * 64
        + '",'
        '"owner":"worker-a","resource":"strict-json",'
        '"acquired_at":0,"renewed_at":0,"expires_at":NaN}'
    )
    _plant_message(board_repo, "strict-json", message)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("strict-json")


def test_acquire_rejects_nonfinite_clock_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: float("nan"))

    with pytest.raises(ValueError, match="claim timestamp must be finite"):
        board.acquire("bad-clock", ttl=30)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "bad-clock", cwd=board_repo) == ""


def test_acquire_rejects_nonfinite_expiry_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: 1e308)

    with pytest.raises(ValueError, match="must not exceed"):
        board.acquire("bad-expiry", ttl=1e308)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "bad-expiry", cwd=board_repo) == ""


def test_ttl_is_bounded_before_commit_or_push(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match=f"must not exceed {claims.CLAIM_MAX_TTL_S}"):
        board.acquire("too-long", ttl=claims.CLAIM_MAX_TTL_S + 1)

    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX + "too-long",
            cwd=board_repo,
        )
        == ""
    )


def test_far_future_lease_fails_closed_until_explicit_cleanup_recovery(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    key = "future"
    _plant_lease(
        board_repo,
        key,
        acquired_at=now + claims.CLAIM_CLOCK_SKEW_S + 1,
        renewed_at=now + claims.CLAIM_CLOCK_SKEW_S + 1,
        expires_at=now + claims.CLAIM_CLOCK_SKEW_S + 601,
    )
    monkeypatch.setattr(claims.time, "time", lambda: now)
    board = _board(tmp_path, board_repo, "worker-a")

    assert not board.acquire(key, ttl=600)
    assert not board.holds(key)
    listed = board.list()
    assert listed[0]["_recovery_required"] is True
    assert listed[0]["_expired"] is False

    assert board.cleanup() == 1
    assert board.acquire(key, ttl=600)


def test_oversized_remote_ttl_fails_closed_until_explicit_cleanup_recovery(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    key = "oversized"
    _plant_lease(
        board_repo,
        key,
        acquired_at=now,
        renewed_at=now,
        expires_at=now + claims.CLAIM_MAX_TTL_S + 1,
    )
    monkeypatch.setattr(claims.time, "time", lambda: now)
    board = _board(tmp_path, board_repo, "worker-a")

    assert not board.acquire(key, ttl=600)
    assert board.list()[0]["_recovery_required"] is True
    assert board.cleanup() == 1
    assert board.acquire(key, ttl=600)
