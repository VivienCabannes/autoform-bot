"""Tests for host-neutral Git-ref claim leases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
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


def test_held_claim_fence_returns_one_coherent_remote_receipt(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("author/node", ttl=600)

    first = board.held_claim_fence("author/node")
    assert first is not None
    assert first.key == "author/node"
    assert first.ref == claims.CLAIM_REF_PREFIX + first.key
    assert first.oid == board.held_claim_oid(first.key)
    assert first.lease_id == board.held_lease_id(first.key)
    assert first.as_dict() == {
        "key": first.key,
        "lease_id": first.lease_id,
        "oid": first.oid,
        "ref": first.ref,
    }

    assert board.renew(first.key, ttl=600, lease_id=first.lease_id)
    renewed = board.held_claim_fence(first.key)
    assert renewed is not None
    assert renewed.lease_id == first.lease_id
    assert renewed.oid != first.oid

    assert board.release(first.key)
    assert board.held_claim_fence(first.key) is None


def test_claim_fence_rejects_mismatched_or_malformed_fields() -> None:
    with pytest.raises(ValueError, match="does not match"):
        claims.ClaimFence("author/node", "refs/heads/main", "1" * 40, "2" * 64)
    with pytest.raises(ValueError, match="OID"):
        claims.ClaimFence("author/node", claims.CLAIM_REF_PREFIX + "author/node", "bad", "2" * 64)
    with pytest.raises(ValueError, match="lease_id"):
        claims.ClaimFence("author/node", claims.CLAIM_REF_PREFIX + "author/node", "1" * 40, "bad")
    with pytest.raises(ValueError, match="identify an object"):
        claims.ClaimFence(
            "author/node",
            claims.CLAIM_REF_PREFIX + "author/node",
            "0" * 40,
            "2" * 64,
        )


def test_sha256_repository_uses_matching_claim_scratch(tmp_path: Path) -> None:
    repo = tmp_path / "claims-sha256.git"
    _git("init", "--bare", "--quiet", "--object-format=sha256", str(repo))
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(
        repo,
        "worker-a",
        scratch,
        session_id="session-a",
        expected_object_format="sha256",
    )

    assert board.acquire("article", ttl=600)
    oid = board.held_claim_oid("article")
    assert oid is not None and len(oid) == 64
    assert _git("--git-dir", str(scratch), "rev-parse", "--show-object-format") == "sha256"
    assert board.release("article")


def test_local_sha256_repository_format_is_detected(tmp_path: Path) -> None:
    repo = tmp_path / "claims-sha256.git"
    _git("init", "--bare", "--quiet", "--object-format=sha256", str(repo))
    board = claims.ClaimBoard(repo, "worker-a", tmp_path / "scratch")

    assert board.acquire("article", ttl=600)
    assert len(board.held_claim_oid("article") or "") == 64


def test_claim_repository_object_format_mismatch_fails_before_push(tmp_path: Path) -> None:
    repo = tmp_path / "claims-sha256.git"
    _git("init", "--bare", "--quiet", "--object-format=sha256", str(repo))
    board = claims.ClaimBoard(
        repo,
        "worker-a",
        tmp_path / "scratch",
        expected_object_format="sha1",
    )

    with pytest.raises(claims.ClaimTransportError, match="does not match expected"):
        board.acquire("article", ttl=600)
    assert _git("--git-dir", str(repo), "for-each-ref", claims.CLAIM_REF_PREFIX) == ""


def test_remote_expected_object_format_is_verified_against_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = "" if command[-1] == "HEAD" else f"{'a' * 64}\trefs/autoform-claims/existing\n"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(claims, "_run_bounded_process", run)
    board = claims.ClaimBoard(
        "https://example.invalid/claims.git",
        "worker-a",
        tmp_path / "scratch",
        expected_object_format="sha1",
    )

    with pytest.raises(claims.ClaimTransportError, match="does not match expected"):
        board._repository_object_format()

    assert commands == [
        ["git", "ls-remote", "https://example.invalid/claims.git", "HEAD"],
        [
            "git",
            "ls-remote",
            "--refs",
            "https://example.invalid/claims.git",
            claims.CLAIM_REF_PREFIX + "*",
        ],
    ]


def test_remote_claim_only_ref_detects_object_format_without_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "" if command[-1] == "HEAD" else f"{'a' * 64}\trefs/autoform-claims/existing\n"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(claims, "_run_bounded_process", run)
    board = claims.ClaimBoard(
        "https://example.invalid/claims.git",
        "worker-a",
        tmp_path / "scratch",
    )

    assert board._repository_object_format() == "sha256"


def test_remote_detached_head_detects_object_format_without_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = "" if "--refs" in command else f"{'a' * 64}\tHEAD\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(claims, "_run_bounded_process", run)
    board = claims.ClaimBoard(
        "https://example.invalid/claims.git",
        "worker-a",
        tmp_path / "scratch",
    )

    assert board._repository_object_format() == "sha256"
    assert commands == [
        ["git", "ls-remote", "https://example.invalid/claims.git", "HEAD"],
    ]


def test_ls_remote_parser_accepts_git_valid_non_ascii_whitespace() -> None:
    oid = "a" * 40

    for separator in (
        "\N{NO-BREAK SPACE}",
        "\N{NEXT LINE}",
        "\N{LINE SEPARATOR}",
        "\N{PARAGRAPH SEPARATOR}",
    ):
        ref = f"refs/heads/valid{separator}name"
        assert claims._parse_ls_remote_output(f"{oid}\t{ref}\n") == [(oid, ref)]


def test_ls_remote_parser_accepts_non_utf8_ref_bytes_via_surrogateescape() -> None:
    oid = "a" * 40
    ref = b"refs/heads/non-utf8-\xff".decode("utf-8", errors="surrogateescape")

    assert claims._parse_ls_remote_output(f"{oid}\t{ref}\n") == [(oid, ref)]


@pytest.mark.skipif(os.name != "posix", reason="raw Git ref bytes require POSIX argv semantics")
def test_list_rejects_non_utf8_claim_ref_without_decode_error(tmp_path: Path, board_repo: Path) -> None:
    oid = _plant_message(board_repo, "valid", "not used")
    raw_ref = claims.CLAIM_REF_PREFIX.encode() + b"invalid-\xff"
    (board_repo / "packed-refs").write_bytes(
        b"# pack-refs with: peeled fully-peeled sorted\n" + oid.encode() + b" " + raw_ref + b"\n"
    )
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.ClaimTransportError, match="invalid claim ref"):
        board.list()


def test_existing_claim_scratch_must_match_repository_object_format(tmp_path: Path) -> None:
    repo = tmp_path / "claims-sha256.git"
    scratch = tmp_path / "scratch"
    _git("init", "--bare", "--quiet", "--object-format=sha256", str(repo))
    _git("init", "--bare", "--quiet", "--object-format=sha1", str(scratch))
    board = claims.ClaimBoard(
        repo,
        "worker-a",
        scratch,
        expected_object_format="sha256",
    )

    with pytest.raises(claims.ClaimTransportError, match="scratch object format"):
        board.acquire("article", ttl=600)


def test_invalid_expected_object_format_creates_no_scratch(tmp_path: Path, board_repo: Path) -> None:
    scratch = tmp_path / "scratch"

    with pytest.raises(ValueError, match="object format"):
        claims.ClaimBoard(
            board_repo,
            "worker-a",
            scratch,
            expected_object_format="sha512",
        )

    assert not scratch.exists()


def test_unknown_empty_remote_format_is_not_guessed(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(board_repo, "worker-a", scratch)
    monkeypatch.setattr(board, "_repository_object_format", lambda: None)

    with pytest.raises(claims.ClaimTransportError, match="pass expected_object_format"):
        board.acquire("article", ttl=600)

    assert not (scratch / "HEAD").exists()


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


@pytest.mark.parametrize(
    ("new", "remote_output"),
    [
        ("b" * 40, f"{'b' * 40}\t{claims.CLAIM_REF_PREFIX}race\n"),
        ("", ""),
    ],
)
def test_ambiguous_cas_failure_accepts_the_desired_remote_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new: str,
    remote_output: str,
) -> None:
    board = claims.ClaimBoard(tmp_path / "claims.git", "worker", tmp_path / "scratch")
    board._object_format = "sha1"
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="remote failure"),
            subprocess.CompletedProcess([], 0, stdout=remote_output, stderr=""),
        ]
    )
    monkeypatch.setattr(board, "_git", lambda *args, **kwargs: next(responses))

    assert board._cas_push("race", "a" * 40, new)


@pytest.mark.parametrize(
    ("remote_oid", "expected"),
    [("a" * 40, "raises"), ("c" * 40, "contended")],
)
def test_ambiguous_cas_failure_distinguishes_transport_error_from_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_oid: str,
    expected: str,
) -> None:
    board = claims.ClaimBoard(tmp_path / "claims.git", "worker", tmp_path / "scratch")
    board._object_format = "sha1"
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="remote failure"),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=f"{remote_oid}\t{claims.CLAIM_REF_PREFIX}race\n",
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(board, "_git", lambda *args, **kwargs: next(responses))

    if expected == "raises":
        with pytest.raises(claims.ClaimTransportError, match="CAS push failed"):
            board._cas_push("race", "a" * 40, "b" * 40)
    else:
        assert not board._cas_push("race", "a" * 40, "b" * 40)


@pytest.mark.parametrize(
    "output",
    [
        "malformed\n",
        (f"{'a' * 40}\t{claims.CLAIM_REF_PREFIX}race\n{'b' * 40}\t{claims.CLAIM_REF_PREFIX}race\n"),
    ],
)
def test_exact_remote_oid_rejects_malformed_or_multiple_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    board = claims.ClaimBoard(tmp_path / "claims.git", "worker", tmp_path / "scratch")
    board._object_format = "sha1"
    monkeypatch.setattr(
        board,
        "_git",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )

    with pytest.raises(claims.ClaimTransportError):
        board._remote_oid("race")


def test_expired_lease_can_be_taken_over(tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(claims.time, "time", lambda: now)
    first = _board(tmp_path, board_repo, "worker-a")
    second = _board(tmp_path, board_repo, "worker-b")

    assert first.acquire("expired", ttl=10)
    first_lease_id = first.read("expired")["lease_id"]
    monkeypatch.setattr(claims.time, "time", lambda: now + 11)
    assert not first.holds("expired")
    assert not first.renew("expired", ttl=60)
    assert not second.acquire("expired", ttl=60)
    assert second.cleanup() == 0

    monkeypatch.setattr(claims.time, "time", lambda: now + 10 + claims.CLAIM_CLOCK_SKEW_S)
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
def test_acquire_rejects_nonfinite_ttl_without_mutating_remote(tmp_path: Path, board_repo: Path, ttl: float) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="finite positive number"):
        board.acquire("nonfinite", ttl=ttl)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX + "nonfinite", cwd=board_repo) == ""


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), float("-inf")])
def test_renew_rejects_nonfinite_ttl_without_replacing_lease(tmp_path: Path, board_repo: Path, ttl: float) -> None:
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

    with pytest.raises(ValueError, match="steal"):
        peer.acquire("owned", ttl=30, steal=True)

    assert owner._remote_oid("owned") == oid
    assert owner.holds("owned")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_explicit_comparison_clock(now: float) -> None:
    lease = {"expires_at": 200.0}

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired(lease, now=now)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_expired_rejects_nonfinite_default_comparison_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: now)

    with pytest.raises(ValueError, match="comparison clock must be finite"):
        claims.ClaimBoard.expired({"expires_at": 200.0})


def test_expiry_honors_positive_clock_skew_at_the_exact_boundary() -> None:
    lease = {"expires_at": 1_060.0}

    assert not claims.ClaimBoard.expired(
        lease,
        now=1_060.0 + claims.CLAIM_CLOCK_SKEW_S - 0.001,
    )
    assert claims.ClaimBoard.expired(
        lease,
        now=1_060.0 + claims.CLAIM_CLOCK_SKEW_S,
    )


def test_fast_observer_cannot_steal_or_cleanup_a_live_lease(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    owner = _board(tmp_path, board_repo, "owner")
    observer = _board(tmp_path, board_repo, "fast-observer")
    assert owner.acquire("clock-skew", ttl=60)

    monkeypatch.setattr(claims.time, "time", lambda: 1_061.0)

    assert not owner.holds("clock-skew")
    assert not owner.renew("clock-skew", ttl=60)
    assert not observer.acquire("clock-skew", ttl=60)
    assert observer.list()[0]["_expired"] is False
    assert observer.cleanup() == 0
    assert owner.read("clock-skew")["owner"] == "owner"


def test_renewal_after_benign_backward_clock_step_stays_monotonic(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    board = _board(tmp_path, board_repo, "owner")
    assert board.acquire("backward-renew", ttl=60)

    monkeypatch.setattr(claims.time, "time", lambda: 950.0)

    assert board.renew("backward-renew", ttl=60)
    lease = board.read("backward-renew")
    assert lease is not None
    assert lease["renewed_at"] >= lease["acquired_at"]
    assert lease["expires_at"] - lease["renewed_at"] == 60
    assert board.list()[0]["_malformed"] is False
    assert board.holds("backward-renew")


def test_refresh_never_regresses_renewal_or_expiry_timestamps(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    board = _board(tmp_path, board_repo, "owner")
    assert board.acquire("monotonic", ttl=600)
    original = board.read("monotonic")
    assert original is not None

    monkeypatch.setattr(claims.time, "time", lambda: 950.0)

    assert board.acquire("monotonic", ttl=30)
    refreshed = board.read("monotonic")
    assert refreshed is not None
    assert refreshed["renewed_at"] >= original["renewed_at"]
    assert refreshed["expires_at"] >= original["expires_at"]


def test_renew_refuses_a_lease_that_expires_during_commit_preflight(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/expires-during-preflight"
    oid = _plant_lease(
        board_repo,
        key,
        acquired_at=100.0,
        renewed_at=100.0,
        expires_at=110.0,
    )
    board = _board(tmp_path, board_repo, "original-owner", session_id="session")
    assert board.list()[0]["_malformed"] is False
    with board._operation():
        board._ensure_scratch()
        board._record_receipt(key, oid, expected=None)
    clocks = iter((105.0, 105.0, 1_000.0))
    monkeypatch.setattr(board, "_operation_time", lambda: next(clocks))

    assert not board.renew(key, ttl=600)
    assert board._remote_oid(key) == oid
    assert board._receipt_oid(key) == oid


def test_cleanup_removes_only_expired_snapshot_entries(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("dead", ttl=5)
    assert board.acquire("live", ttl=500)
    monkeypatch.setattr(claims.time, "time", lambda: 1_310.0)

    assert board.cleanup() == 1
    assert [lease["_key"] for lease in board.list()] == ["live"]


def test_cleanup_cas_does_not_delete_renewed_lease(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims.time, "time", lambda: 1_000.0)
    cleaner = _board(tmp_path, board_repo, "worker-a")
    assert cleaner.acquire("lease", ttl=5)
    monkeypatch.setattr(claims.time, "time", lambda: 1_310.0)

    original_list = cleaner.list
    owner = _board(tmp_path, board_repo, "worker-b")

    def list_then_renew() -> list[dict[str, object]]:
        snapshot = original_list()
        assert owner.acquire("lease", ttl=500)
        return snapshot

    monkeypatch.setattr(cleaner, "list", list_then_renew)
    assert cleaner.cleanup() == 0
    assert cleaner.read("lease")["expires_at"] == 1_810.0


def test_cleanup_replaces_unknown_expired_v1_author_ref_with_a_tombstone(
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
    monkeypatch.setattr(claims.time, "time", lambda: 500.0)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="blueprint is required"):
        board.cleanup()
    assert board.read(key)["schema"] == claims.LEGACY_CLAIM_SCHEMA
    assert board.cleanup(legacy_mappings={}) == 1
    block = board.read(key)
    assert block is not None
    assert block["schema"] == claims.LEGACY_TOMBSTONE_SCHEMA
    assert "canonical_resource" not in block


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
    claim_oid = owner.held_claim_oid("article")
    assert claim_oid is not None
    assert claim_oid == owner._remote_oid("article")
    assert owner.holds("article")
    assert peer.held_claim_oid("article") is None
    assert not peer.holds("article")
    assert not peer.acquire("article", ttl=600)
    assert not peer.renew("article", ttl=600)
    assert not peer.release("article")
    assert owner.holds("article")
    assert owner.held_claim_oid("article") == claim_oid


def test_exact_receipt_is_fenced_after_another_copy_renews(tmp_path: Path, board_repo: Path) -> None:
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
    stale.list()
    stale._git(
        ["fetch", "--quiet", str(board_repo), f"+{claims.CLAIM_REF_PREFIX}article:{claims.CLAIM_REF_PREFIX}article"]
    )
    stale._record_receipt("article", original, expected=None)
    assert stale.holds("article")

    assert owner.renew("article", ttl=600)
    assert owner.held_claim_oid("article") != original
    assert stale.held_claim_oid("article") is None
    assert not stale.holds("article")
    assert not stale.renew("article", ttl=600)
    assert not stale.release("article")


def test_receipt_failure_after_remote_acquire_rolls_back_the_remote_claim(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a", session_id="session-a")

    def fail_receipt(*args: object, **kwargs: object) -> None:
        raise claims.ClaimTransportError("receipt unavailable")

    monkeypatch.setattr(board, "_record_receipt", fail_receipt)
    with pytest.raises(claims.ClaimTransportError, match="receipt unavailable"):
        board.acquire("article", ttl=600)

    assert board.read("article") is None
    assert not board.holds("article")


def test_receipt_failure_after_remote_renewal_restores_the_prior_lease(
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

    assert board._remote_oid("article") == old
    assert board._receipt_oid("article") == old
    assert board.holds("article")


@pytest.mark.parametrize("operation", ["acquire", "renew"])
def test_receipt_failure_after_local_update_restores_both_generations(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    board = _board(tmp_path, board_repo, "worker-a", session_id="session-a")
    old = None
    if operation == "renew":
        assert board.acquire("article", ttl=600)
        old = board._remote_oid("article")
    record = board._record_receipt

    def record_then_fail(*args: object, **kwargs: object) -> None:
        record(*args, **kwargs)
        raise claims.ClaimTransportError("receipt result lost")

    monkeypatch.setattr(board, "_record_receipt", record_then_fail)
    with pytest.raises(claims.ClaimTransportError, match="receipt result lost"):
        getattr(board, operation)("article", ttl=600)

    assert board._remote_oid("article") == old
    assert board._receipt_oid("article") == old
    assert board.holds("article") is (old is not None)


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
    releaser.list()
    original_remote_oid = releaser._remote_oid
    acquired_oid: str | None = None

    def absent_then_acquire(key: str) -> None:
        nonlocal acquired_oid
        assert original_remote_oid(key) is None
        now = claims.time.time()
        acquired_oid = _plant_lease(
            board_repo,
            key,
            acquired_at=now,
            renewed_at=now,
            expires_at=now + 600,
        )
        releaser._git(
            [
                "fetch",
                "--quiet",
                str(board_repo),
                f"+{claims.CLAIM_REF_PREFIX}{key}:{claims.CLAIM_REF_PREFIX}{key}",
            ]
        )
        releaser._record_receipt(key, acquired_oid, expected=None)
        return None

    monkeypatch.setattr(releaser, "_remote_oid", absent_then_acquire)

    with pytest.raises(claims.ClaimTransportError, match="receipt could not be cleared"):
        releaser.release("article")

    assert acquired_oid is not None
    assert original_remote_oid("article") == acquired_oid
    assert releaser._receipt_oid("article") == acquired_oid


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

    monkeypatch.setattr(claims.time, "time", lambda: 500.0)
    assert board.acquire(key, ttl=600)
    lease = board.read(key)
    assert lease["schema"] == claims.CLAIM_SCHEMA
    assert claims.LEASE_ID_RE.fullmatch(lease["lease_id"])


def test_historical_live_v1_blocks_direct_v2_renew_and_heartbeat(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    canonical_key = claims.author_claim_key("af_0123456789abcdef01234567")
    historical_key = claims.author_claim_key("chapter/old-name")
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire(canonical_key, ttl=600)
    original_oid = board._remote_oid(canonical_key)
    now = claims.time.time()
    _plant_lease(
        board_repo,
        historical_key,
        schema=claims.LEGACY_CLAIM_SCHEMA,
        lease_id=None,
        acquired_at=now,
        renewed_at=None,
        expires_at=now + 600,
        resource=historical_key,
    )

    assert not board.renew(canonical_key, ttl=600)
    assert board.held_lease_id(canonical_key) is None
    with pytest.raises(claims.ClaimTransportError, match="lost before heartbeat entry"):
        with board.heartbeat(canonical_key, interval=1, ttl=600):
            pytest.fail("mixed-version ownership must not authorize work")
    peer = _board(tmp_path, board_repo, "worker-b", scratch=tmp_path / "peer-scratch")
    assert not peer.acquire(claims.author_claim_key("af_abcdef0123456789abcdef01"), ttl=600)
    assert board._remote_oid(canonical_key) == original_oid
    assert board.release(canonical_key)


def test_v2_acquire_rolls_back_if_a_live_v1_appears_during_push(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_key = claims.author_claim_key("af_0123456789abcdef01234567")
    historical_key = claims.author_claim_key("chapter/old-name")
    board = _board(tmp_path, board_repo, "worker-a")
    checks = 0

    def race(_key: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            return False
        now = claims.time.time()
        _plant_lease(
            board_repo,
            historical_key,
            schema=claims.LEGACY_CLAIM_SCHEMA,
            lease_id=None,
            acquired_at=now,
            renewed_at=None,
            expires_at=now + 600,
            resource=historical_key,
        )
        return True

    monkeypatch.setattr(board, "_legacy_author_claim_blocks_v2", race)

    assert not board.acquire(canonical_key, ttl=600)
    assert checks == 2
    assert board._remote_oid(canonical_key) is None


def test_legacy_v1_ttl_above_v2_limit_remains_live(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    key = "legacy-long-ttl"
    _plant_lease(
        board_repo,
        key,
        schema=claims.LEGACY_CLAIM_SCHEMA,
        lease_id=None,
        acquired_at=100.0,
        renewed_at=None,
        expires_at=now + claims.CLAIM_MAX_TTL_S + 1,
    )
    monkeypatch.setattr(claims.time, "time", lambda: now)
    board = _board(tmp_path, board_repo, "worker-a")

    lease = board.read(key)
    assert lease is not None
    assert not board.recovery_required(lease)
    assert not board.expired(lease)
    assert board.cleanup() == 0
    assert not board.acquire(key, ttl=600)


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
                lease.get("schema") == claims.LEGACY_CLAIM_SCHEMA and claims.ClaimBoard._lease_is_valid(lease, key)
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
    board.list()
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
                lease.get("schema") == claims.LEGACY_CLAIM_SCHEMA and claims.ClaimBoard._lease_is_valid(lease, key)
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


def test_scp_like_repository_without_user_is_not_resolved_as_local_path() -> None:
    repo = "git.example.test:team/claims.git"

    assert claims.claim_repository_is_remote(repo)
    assert claims.normalize_claim_repository(repo) == repo
    assert claims.pin_claim_repository(repo) == (repo, None)


def test_inherited_git_namespace_cannot_split_claim_refs(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_NAMESPACE", "other-claim-universe")
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("resource/namespaced", ttl=600)

    monkeypatch.delenv("GIT_NAMESPACE")
    refs = _git(
        "for-each-ref",
        "--format=%(refname)",
        "refs/autoform-claims",
        "refs/namespaces",
        cwd=board_repo,
    ).splitlines()
    assert refs == [claims.CLAIM_REF_PREFIX + "resource/namespaced"]


def test_git_subprocess_environment_drops_repository_control_variables(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_NAMESPACE", "other-claim-universe")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "objects"))
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.bad.invalid.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(board_repo))
    real_spawn = claims._spawn_process
    environments: list[dict[str, str]] = []

    def capture_environment(command, *args, **kwargs):
        environments.append(dict(kwargs["env"]))
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", capture_environment)
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("resource/minimal-env", ttl=600)
    assert environments
    for environment in environments:
        assert "GIT_NAMESPACE" not in environment
        assert "GIT_OBJECT_DIRECTORY" not in environment
        assert "GIT_CONFIG_KEY_0" not in environment
        assert "GIT_CONFIG_VALUE_0" not in environment
        assert environment["GIT_CONFIG_COUNT"] == "0"
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_inherited_git_config_cannot_redirect_claim_repository(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirected = tmp_path / "redirected.git"
    _git("init", "--bare", "--quiet", str(redirected))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{redirected}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(board_repo))
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("resource/config-redirect", ttl=600)

    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.delenv("GIT_CONFIG_KEY_0")
    monkeypatch.delenv("GIT_CONFIG_VALUE_0")
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=board_repo,
        )
        == claims.CLAIM_REF_PREFIX + "resource/config-redirect"
    )
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=redirected,
        )
        == ""
    )


def test_existing_scratch_url_rewrite_is_removed_before_remote_use(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    redirected = tmp_path / "redirected.git"
    scratch = tmp_path / "scratch"
    _git("init", "--bare", "--quiet", str(redirected))
    _git("init", "--bare", "--quiet", str(scratch))
    _git("config", f"url.{redirected}.insteadOf", str(board_repo), cwd=scratch)
    pre_push = scratch / "hooks/pre-push"
    pre_push.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    pre_push.chmod(0o700)
    board = _board(tmp_path, board_repo, "worker-a", scratch=scratch)

    assert board.acquire("resource/local-config-redirect", ttl=600)

    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=board_repo,
        )
        == claims.CLAIM_REF_PREFIX + "resource/local-config-redirect"
    )
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=redirected,
        )
        == ""
    )
    config = (scratch / "config").read_text(encoding="utf-8")
    assert "insteadOf" not in config
    assert str(redirected) not in config


def test_remote_oid_rejects_suffix_match_for_a_different_ref(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = "resource/exact"
    oid = _plant_message(board_repo, "temporary", "payload")
    _git("update-ref", f"refs/heads/{claims.CLAIM_REF_PREFIX}{key}", oid, cwd=board_repo)
    _git("update-ref", "-d", claims.CLAIM_REF_PREFIX + "temporary", cwd=board_repo)
    board = _board(tmp_path, board_repo, "worker-a")

    with board._operation():
        board._ensure_scratch()
        with pytest.raises(claims.ClaimTransportError, match="exact requested ref"):
            board._remote_oid(key)


def test_sibling_churn_does_not_strand_a_successful_claim(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    real_spawn = claims._spawn_process
    intercepted = False

    def run_during_sibling_churn(command, *args, **kwargs):
        nonlocal intercepted
        if not intercepted and "push" in command:
            intercepted = True
            sibling = tmp_path / "unrelated-sibling"
            sibling.mkdir()
            try:
                return real_spawn(command, *args, **kwargs)
            finally:
                sibling.rmdir()
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", run_during_sibling_churn)

    assert board.acquire("resource/sibling-churn", ttl=600)
    assert intercepted
    assert board.holds("resource/sibling-churn")


def test_file_url_repo_is_pinned_before_its_symlink_is_redirected(tmp_path: Path) -> None:
    original = tmp_path / "original.git"
    redirected = tmp_path / "redirected.git"
    _git("init", "--bare", "--quiet", str(original))
    _git("init", "--bare", "--quiet", str(redirected))
    alias = tmp_path / "claims.git"
    alias.symlink_to(original, target_is_directory=True)
    board = claims.ClaimBoard(alias.absolute().as_uri(), "worker-a", tmp_path / "scratch")
    assert board.acquire("file-url", ttl=600)

    alias.unlink()
    alias.symlink_to(redirected, target_is_directory=True)

    assert board.release("file-url")
    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX, cwd=original) == ""
    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX, cwd=redirected) == ""


def test_canonical_local_repo_replacement_fails_before_remote_mutation(tmp_path: Path) -> None:
    original = tmp_path / "claims.git"
    redirected = tmp_path / "redirected.git"
    _git("init", "--bare", "--quiet", str(original))
    _git("init", "--bare", "--quiet", str(redirected))
    board = claims.ClaimBoard(original, "worker-a", tmp_path / "scratch")
    original.rename(tmp_path / "original.git")
    original.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(claims.ClaimTransportError, match="local claim repository"):
        board.acquire("repo-replaced", ttl=600)

    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX, cwd=redirected) == ""
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=tmp_path / "original.git",
        )
        == ""
    )


def test_repo_aba_during_git_subprocess_uses_pinned_repository(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirected = tmp_path / "redirected.git"
    _git("init", "--bare", "--quiet", str(redirected))
    scratch_parent = tmp_path / "scratch-parent"
    scratch_parent.mkdir()
    board = claims.ClaimBoard(board_repo, "worker-a", scratch_parent / "scratch")
    board.list()
    parked = tmp_path / "parked.git"
    real_spawn = claims._spawn_process
    intercepted = False

    def run_during_aba(command, *args, **kwargs):
        nonlocal intercepted
        if not intercepted and "push" in command:
            intercepted = True
            board_repo.rename(parked)
            board_repo.symlink_to(redirected, target_is_directory=True)
            try:
                return real_spawn(command, *args, **kwargs)
            finally:
                board_repo.unlink()
                parked.rename(board_repo)
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", run_during_aba)

    assert board.acquire("repo-aba", ttl=600)

    assert intercepted
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=board_repo,
        )
        == claims.CLAIM_REF_PREFIX + "repo-aba"
    )
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=redirected,
        )
        == ""
    )


def test_repo_ancestor_aba_during_git_subprocess_uses_pinned_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo-root"
    intended_scope = repo_root / "scope"
    redirected_scope = repo_root / "other"
    intended = intended_scope / "inner" / "claims.git"
    redirected = redirected_scope / "inner" / "claims.git"
    intended.parent.mkdir(parents=True)
    redirected.parent.mkdir(parents=True)
    _git("init", "--bare", "--quiet", str(intended))
    _git("init", "--bare", "--quiet", str(redirected))
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    board = claims.ClaimBoard(intended, "worker-a", scratch_root / "scratch")
    board.list()
    parked = repo_root / "parked"
    real_spawn = claims._spawn_process
    intercepted = False

    def run_during_ancestor_aba(command, *args, **kwargs):
        nonlocal intercepted
        if not intercepted and "push" in command:
            intercepted = True
            intended_scope.rename(parked)
            intended_scope.symlink_to(redirected_scope, target_is_directory=True)
            try:
                return real_spawn(command, *args, **kwargs)
            finally:
                intended_scope.unlink()
                parked.rename(intended_scope)
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", run_during_ancestor_aba)

    assert board.acquire("repo-ancestor-aba", ttl=600)

    assert intercepted
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=intended,
        )
        == claims.CLAIM_REF_PREFIX + "repo-ancestor-aba"
    )
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=redirected,
        )
        == ""
    )


def test_open_filesystem_boundary_prevents_redirect_above_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    intended_scope = workspace / "scope"
    intended_repo = intended_scope / "claims.git"
    intended_scratch = intended_scope / "scratch"
    intended_scope.mkdir(parents=True)
    _git("init", "--bare", "--quiet", str(intended_repo))
    redirected_workspace = tmp_path / "redirected-workspace"
    redirected_scope = redirected_workspace / "scope"
    redirected_scope.mkdir(parents=True)
    redirected_repo = redirected_scope / "claims.git"
    redirected_scratch = redirected_scope / "scratch"
    _git("init", "--bare", "--quiet", str(redirected_repo))
    _git("init", "--bare", "--quiet", str(redirected_scratch))
    board = claims.ClaimBoard(
        intended_repo,
        "worker-a",
        intended_scratch,
    )
    parked = tmp_path / "parked-workspace"
    real_spawn = claims._spawn_process
    interceptions = 0
    redirecting = True

    def run_during_outer_ancestor_aba(command, *args, **kwargs):
        nonlocal interceptions, redirecting
        if not redirecting:
            return real_spawn(command, *args, **kwargs)
        interceptions += 1
        workspace.rename(parked)
        workspace.symlink_to(redirected_workspace, target_is_directory=True)
        try:
            return real_spawn(command, *args, **kwargs)
        finally:
            workspace.unlink()
            parked.rename(workspace)

    monkeypatch.setattr(claims, "_spawn_process", run_during_outer_ancestor_aba)

    assert board.acquire("anchored", ttl=600)
    redirecting = False
    assert interceptions > 0
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=intended_repo,
        )
        == claims.CLAIM_REF_PREFIX + "anchored"
    )
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=redirected_repo,
        )
        == ""
    )


def test_remote_board_anchors_scratch_leaf_with_directory_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(
        "https://example.invalid/claims.git",
        "worker-a",
        scratch,
        expected_object_format="sha1",
    )
    monkeypatch.setattr(board, "_repository_object_format", lambda: "sha1")
    with board._operation():
        board._ensure_scratch()
        oid = board._git(["hash-object", "-w", "--stdin"], input_text="payload").stdout.strip()
    redirected = tmp_path / "redirected-scratch"
    shutil.copytree(scratch, redirected)
    parked = tmp_path / "parked-scratch"
    real_spawn = claims._spawn_process
    intercepted = False

    def run_during_leaf_aba(command, *args, **kwargs):
        nonlocal intercepted
        if not intercepted and "update-ref" in command:
            intercepted = True
            scratch.rename(parked)
            scratch.symlink_to(redirected, target_is_directory=True)
            try:
                return real_spawn(command, *args, **kwargs)
            finally:
                scratch.unlink()
                parked.rename(scratch)
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", run_during_leaf_aba)

    board._git(["update-ref", "refs/test/anchored", oid])

    assert intercepted
    assert _git("rev-parse", "refs/test/anchored", cwd=scratch) == oid
    assert _git("for-each-ref", "--format=%(refname)", "refs/test", cwd=redirected) == ""


def test_scratch_symlink_is_pinned_before_bare_repo_initialization(tmp_path: Path, board_repo: Path) -> None:
    original = tmp_path / "original-scratch"
    redirected = tmp_path / "redirected-scratch"
    original.mkdir()
    redirected.mkdir()
    alias = tmp_path / "scratch"
    alias.symlink_to(original, target_is_directory=True)
    board = claims.ClaimBoard(board_repo, "worker-a", alias)

    alias.unlink()
    alias.symlink_to(redirected, target_is_directory=True)

    assert board.acquire("scratch-link", ttl=600)
    assert (original / "HEAD").is_file()
    assert not (redirected / "HEAD").exists()


def test_existing_scratch_replacement_before_first_use_fails_closed(tmp_path: Path, board_repo: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    board = claims.ClaimBoard(board_repo, "worker-a", scratch)
    scratch.rename(tmp_path / "original-scratch")
    scratch.mkdir()

    with pytest.raises(claims.ClaimTransportError, match="scratch directory was replaced"):
        board.acquire("scratch-replaced-before-use", ttl=600)

    assert not (scratch / "HEAD").exists()
    assert _git("for-each-ref", "--format=%(refname)", claims.CLAIM_REF_PREFIX, cwd=board_repo) == ""


def test_replacing_pinned_scratch_fails_closed_without_reinitializing(tmp_path: Path, board_repo: Path) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(board_repo, "worker-a", scratch)
    assert board.acquire("scratch-replaced", ttl=600)
    original_oid = board._remote_oid("scratch-replaced")
    scratch.rename(tmp_path / "original-scratch")
    scratch.mkdir()

    with pytest.raises(claims.ClaimTransportError, match="scratch directory was replaced"):
        board.renew("scratch-replaced", ttl=600)

    assert not (scratch / "HEAD").exists()
    inspector = claims.ClaimBoard(board_repo, "inspector", tmp_path / "inspect")
    inspector.list()
    assert inspector._remote_oid("scratch-replaced") == original_oid


def test_scratch_aba_during_git_subprocess_uses_pinned_scratch(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch_parent = tmp_path / "scratch-parent"
    scratch_parent.mkdir()
    scratch = scratch_parent / "scratch"
    board = claims.ClaimBoard(board_repo, "worker-a", scratch)
    assert board.acquire("scratch-aba", ttl=600)
    original_oid = board._remote_oid("scratch-aba")
    redirected = scratch_parent / "redirected"
    shutil.copytree(scratch, redirected)
    parked = scratch_parent / "parked"
    real_spawn = claims._spawn_process
    intercepted = False

    def run_during_aba(command, *args, **kwargs):
        nonlocal intercepted
        if not intercepted and "cat-file" in command and "-e" in command:
            intercepted = True
            scratch.rename(parked)
            scratch.symlink_to(redirected, target_is_directory=True)
            try:
                return real_spawn(command, *args, **kwargs)
            finally:
                scratch.unlink()
                parked.rename(scratch)
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", run_during_aba)

    assert board.renew("scratch-aba", ttl=600)

    assert intercepted
    inspector = claims.ClaimBoard(board_repo, "inspector", tmp_path / "inspect")
    inspector.list()
    assert inspector._remote_oid("scratch-aba") != original_oid


def test_scratch_ancestor_aba_during_git_subprocess_uses_pinned_scratch(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch_root = tmp_path / "scratch-root"
    intended_scope = scratch_root / "scope"
    redirected_scope = scratch_root / "other"
    scratch = intended_scope / "inner" / "scratch"
    scratch.parent.mkdir(parents=True)
    board = claims.ClaimBoard(board_repo, "worker-a", scratch)
    assert board.acquire("scratch-ancestor-aba", ttl=600)
    original_oid = board._remote_oid("scratch-ancestor-aba")
    redirected = redirected_scope / "inner" / "scratch"
    redirected.parent.mkdir(parents=True)
    shutil.copytree(scratch, redirected)
    parked = scratch_root / "parked"
    real_spawn = claims._spawn_process
    intercepted = False

    def run_during_ancestor_aba(command, *args, **kwargs):
        nonlocal intercepted
        if not intercepted and "cat-file" in command and "-e" in command:
            intercepted = True
            intended_scope.rename(parked)
            intended_scope.symlink_to(redirected_scope, target_is_directory=True)
            try:
                return real_spawn(command, *args, **kwargs)
            finally:
                intended_scope.unlink()
                parked.rename(intended_scope)
        return real_spawn(command, *args, **kwargs)

    monkeypatch.setattr(claims, "_spawn_process", run_during_ancestor_aba)

    assert board.renew("scratch-ancestor-aba", ttl=600)

    assert intercepted
    inspector = claims.ClaimBoard(board_repo, "inspector", tmp_path / "inspect-ancestor")
    inspector.list()
    assert inspector._remote_oid("scratch-ancestor-aba") != original_oid


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
        {"resource": "x" * (claims.CLAIM_KEY_MAX_BYTES + 1)},
        {"owner": ""},
        {"host": ""},
        {"pid": 0},
        {"pid": -1},
        {"pid": claims.CLAIM_PID_MAX + 1},
        {"note": "x" * (claims.CLAIM_NOTE_MAX_BYTES + 1)},
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


def test_planted_nonfinite_lease_is_rejected_by_strict_json_parser(tmp_path: Path, board_repo: Path) -> None:
    message = (
        '{"schema":"autoform-claim/v2","lease_id":"' + "1" * 64 + '",'
        '"owner":"worker-a","resource":"strict-json",'
        '"acquired_at":0,"renewed_at":0,"expires_at":NaN}'
    )
    _plant_message(board_repo, "strict-json", message)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease JSON"):
        board.read("strict-json")


def test_deeply_nested_lease_json_is_reported_as_malformed(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = "nested-json"
    _plant_message(board_repo, key, "[" * 2_000 + "]" * 2_000)
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(claims.MalformedLeaseError, match="invalid lease"):
        board.read(key)


def test_acquire_rejects_nonfinite_clock_before_commit_or_push(
    tmp_path: Path, board_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    monkeypatch.setattr(claims.time, "time", lambda: float("nan"))

    with pytest.raises(ValueError, match="claim operation clock must be finite"):
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


def test_steal_argument_is_explicitly_rejected(tmp_path: Path, board_repo: Path) -> None:
    board = _board(tmp_path, board_repo, "worker-a")

    with pytest.raises(ValueError, match="steal"):
        board.acquire("author/node", steal=True)


def test_markerless_nonempty_scratch_is_not_converted_to_a_git_repository(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "not-a-claim-cache"
    scratch.mkdir()
    sentinel = scratch / "keep.txt"
    sentinel.write_text("user data", encoding="utf-8")

    board = claims.ClaimBoard(board_repo, "worker-a", scratch, session_id="session-a")
    with pytest.raises(claims.ClaimTransportError, match="not an Autoform claim scratch"):
        board.list()

    assert sentinel.read_text(encoding="utf-8") == "user data"
    assert not (scratch / "HEAD").exists()


def test_two_sessions_can_initialize_and_share_one_claim_scratch(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "shared-scratch"
    barrier = threading.Barrier(2)
    results: list[list[dict[str, object]]] = []
    errors: list[BaseException] = []

    def inspect(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            with _board(
                tmp_path,
                board_repo,
                f"worker-{index}",
                session_id=f"session-{index}",
                scratch=scratch,
            ) as board:
                results.append(board.list())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=inspect, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == [], [(repr(error), repr(error.__cause__)) for error in errors]
    assert results == [[], []]
    assert (scratch / "autoform-claim-scratch.json").is_file()


def test_claim_scratch_cannot_be_reused_for_another_repository(tmp_path: Path) -> None:
    first_repo = tmp_path / "first.git"
    second_repo = tmp_path / "second.git"
    _git("init", "--bare", "--quiet", str(first_repo))
    _git("init", "--bare", "--quiet", str(second_repo))
    scratch = tmp_path / "shared-scratch"
    assert claims.ClaimBoard(first_repo, "first", scratch).list() == []

    second = claims.ClaimBoard(second_repo, "second", scratch)
    with pytest.raises(claims.ClaimTransportError, match="does not match this repository"):
        second.list()

    assert _git("for-each-ref", "--format=%(refname)", cwd=second_repo) == ""


def test_list_classifies_every_lease_against_one_clock_snapshot(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plant_lease(board_repo, "author/first", expires_at=200.0)
    _plant_lease(board_repo, "author/second", expires_at=200.0)
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 100.0

    monkeypatch.setattr(claims.time, "time", clock)
    board = _board(tmp_path, board_repo, "worker-a")

    assert len(board.list()) == 2
    assert calls == 1


def test_one_board_operation_passes_one_absolute_deadline_to_every_process(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines: list[float] = []
    run = claims._run_bounded_process

    def record_deadline(command, *, deadline, **kwargs):
        deadlines.append(deadline)
        return run(command, deadline=deadline, **kwargs)

    monkeypatch.setattr(claims, "_run_bounded_process", record_deadline)
    board = _board(tmp_path, board_repo, "worker-a")

    assert board.acquire("author/deadline", ttl=600)
    assert len(deadlines) > 1
    assert len(set(deadlines)) == 1


def test_bounded_process_rejects_combined_output_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claims, "CLAIM_GIT_OUTPUT_MAX_BYTES", 1024)

    with pytest.raises(claims.ClaimTransportError, match="output exceeds"):
        claims._run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 4096)",
            ],
            deadline=time.monotonic() + 5,
        )


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX")
def test_bounded_process_timeout_terminates_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived"
    child = f"import pathlib,time; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('survived')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"

    with pytest.raises(claims.ClaimTransportError, match="exceeded"):
        claims._run_bounded_process(
            [sys.executable, "-c", parent],
            deadline=time.monotonic() + 0.1,
        )
    time.sleep(0.7)
    assert not marker.exists()


def test_bounded_process_input_cannot_block_past_deadline() -> None:
    with pytest.raises(claims.ClaimTransportError, match="exceeded"):
        claims._run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            input_text="x" * (1 << 20),
            input_max_bytes=1 << 20,
            deadline=time.monotonic() + 0.1,
        )


def test_local_receipt_creation_uses_compare_and_swap(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    board = _board(tmp_path, board_repo, "worker-a")
    assert board.acquire("author/receipt", ttl=600)
    original = board._receipt_oid("author/receipt")
    assert original is not None

    with board._operation():
        replacement = board._make_lease_commit("author/receipt", 600)
        with pytest.raises(claims.ClaimTransportError, match="could not be recorded"):
            board._record_receipt("author/receipt", replacement, expected=None)

    assert board._receipt_oid("author/receipt") == original


def test_local_repository_and_scratch_must_be_disjoint(tmp_path: Path) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))

    with pytest.raises(claims.ClaimTransportError, match="disjoint"):
        claims.ClaimBoard(repo, "worker", repo)

    assert not (repo / "autoform-claim-scratch.json").exists()


def test_scratch_cannot_be_inside_local_repository(tmp_path: Path) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    scratch = repo / "claim-cache"

    with pytest.raises(claims.ClaimTransportError, match="disjoint"):
        claims.ClaimBoard(repo, "worker", scratch)

    assert not scratch.exists()


def test_local_repository_cannot_be_inside_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scope"
    repo = scratch / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))

    with pytest.raises(claims.ClaimTransportError, match="disjoint"):
        claims.ClaimBoard(repo, "worker", scratch)

    assert not (scratch / "autoform-claim-scratch.json").exists()


def test_symlink_alias_cannot_hide_repository_scratch_overlap(tmp_path: Path) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)

    with pytest.raises(claims.ClaimTransportError, match="disjoint"):
        claims.ClaimBoard(repo, "worker", alias)


def test_scratch_parent_replacement_cannot_redirect_creation_into_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "claims.git"
    _git("init", "--bare", "--quiet", str(repo))
    scratch_parent = tmp_path / "scratch-parent"
    scratch_parent.mkdir()
    scratch = scratch_parent / "cache"
    parked = tmp_path / "parked-scratch-parent"
    bind_directory = claims.ClaimBoard._bind_directory

    def bind_then_replace(path, *, expected, label):
        binding = bind_directory(path, expected=expected, label=label)
        if label == "local claim repository":
            scratch_parent.rename(parked)
            scratch_parent.symlink_to(repo, target_is_directory=True)
        return binding

    monkeypatch.setattr(
        claims.ClaimBoard,
        "_bind_directory",
        staticmethod(bind_then_replace),
    )

    with pytest.raises(claims.ClaimTransportError, match="pinned safely"):
        claims.ClaimBoard(repo, "worker", scratch)
    assert not (repo / "cache").exists()


@pytest.mark.skipif(os.name != "posix", reason="FIFO safety requires POSIX")
def test_scratch_marker_fifo_is_rejected_without_blocking(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    os.mkfifo(scratch / "autoform-claim-scratch.json")
    board = claims.ClaimBoard(board_repo, "worker", scratch)

    with pytest.raises(claims.ClaimTransportError, match="marker"):
        board.list()


def test_scratch_marker_hardlink_is_rejected(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = tmp_path / "marker-source"
    source.write_text("not an owned marker", encoding="utf-8")
    os.link(source, scratch / "autoform-claim-scratch.json")
    board = claims.ClaimBoard(board_repo, "worker", scratch)

    with pytest.raises(claims.ClaimTransportError, match="marker"):
        board.list()


def test_scratch_marker_recovers_a_crashed_owned_link_install(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(board_repo, "worker", scratch)
    assert board.list() == []
    marker = scratch / "autoform-claim-scratch.json"
    temporary = scratch / (".autoform-claim-scratch.json-123-" + "a" * 16)
    os.link(marker, temporary)
    assert marker.stat().st_nlink == 2

    assert board.list() == []
    assert marker.stat().st_nlink == 1
    assert not temporary.exists()


@pytest.mark.skipif(os.name != "posix", reason="FIFO safety requires POSIX")
def test_scratch_config_fifo_is_rejected_without_running_git(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(board_repo, "worker", scratch)
    assert board.list() == []
    (scratch / "config").unlink()
    os.mkfifo(scratch / "config")

    with pytest.raises(claims.ClaimTransportError, match="unsupported filesystem entry"):
        board.list()


def test_scratch_config_size_is_bounded_before_git_reads_it(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(board_repo, "worker", scratch)
    assert board.list() == []
    (scratch / "config").write_bytes(b"x" * (claims.CLAIM_SCRATCH_CONFIG_MAX_BYTES + 1))

    with pytest.raises(claims.ClaimTransportError, match="configuration"):
        board.list()


def test_scratch_config_hardlink_is_rejected_before_git_reads_it(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    scratch = tmp_path / "scratch"
    board = claims.ClaimBoard(board_repo, "worker", scratch)
    assert board.list() == []
    os.link(scratch / "config", tmp_path / "config-link")

    with pytest.raises(claims.ClaimTransportError, match="unsupported filesystem entry"):
        board.list()


def test_prepare_v2_migrates_only_the_explicit_legacy_key(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = claims.author_claim_key("chapter/current")
    unrelated = claims.author_claim_key("chapter/old")
    for key in (explicit, unrelated):
        _plant_lease(
            board_repo,
            key,
            schema=claims.LEGACY_CLAIM_SCHEMA,
            lease_id=None,
        )
    monkeypatch.setattr(claims.time, "time", lambda: 500.0)
    board = _board(tmp_path, board_repo, "worker")

    assert board.prepare_v2_claim("author/durable", [explicit])
    assert board.read(explicit)["canonical_resource"] == "author/durable"
    assert board.read(unrelated)["schema"] == claims.LEGACY_CLAIM_SCHEMA


def test_existing_legacy_mapping_must_match_requested_canonical_key(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = claims.author_claim_key("chapter/result")
    board = _board(tmp_path, board_repo, "worker")
    assert board.install_legacy_compatibility(key, canonical_key="author/first")

    with pytest.raises(claims.ClaimTransportError, match="different canonical key"):
        board.install_legacy_compatibility(key, canonical_key="author/second")


def test_cleanup_rejects_an_existing_different_legacy_mapping(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = claims.author_claim_key("chapter/result")
    board = _board(tmp_path, board_repo, "worker")
    assert board.install_legacy_compatibility(key, canonical_key="author/first")

    with pytest.raises(claims.ClaimTransportError, match="different canonical key"):
        board.cleanup(legacy_mappings={key: "author/second"})


def test_cleanup_uses_exact_legacy_mapping(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = claims.author_claim_key("chapter/result")
    canonical = claims.author_claim_key("af_aaaaaaaaaaaaaaaaaaaaaaaa")
    _plant_lease(
        board_repo,
        legacy,
        schema=claims.LEGACY_CLAIM_SCHEMA,
        lease_id=None,
    )
    monkeypatch.setattr(claims.time, "time", lambda: 500.0)
    board = _board(tmp_path, board_repo, "worker")

    assert board.cleanup(legacy_mappings={legacy: canonical}) == 1
    assert board.read(legacy)["canonical_resource"] == canonical


def test_quarantine_requires_remote_filter_support(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/filter"
    _plant_lease(board_repo, key)
    board = _board(tmp_path, board_repo, "worker")
    monkeypatch.setattr(board, "_local_transport_args", lambda args: args)

    with pytest.raises(claims.ClaimTransportError, match="required tree:0 fetch filter"):
        board.read(key)


def test_quarantine_enforces_file_size_limit(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/oversized-pack"
    _plant_lease(board_repo, key)
    monkeypatch.setattr(claims, "CLAIM_QUARANTINE_MAX_FILE_BYTES", 512)
    board = _board(tmp_path, board_repo, "worker")

    with pytest.raises(claims.ClaimTransportError, match="bounded claim fetch failed"):
        board.read(key)


def test_quarantine_directory_and_persistent_mirror_refs_are_cleaned(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/quarantine-cleanup"
    _plant_lease(board_repo, key)
    quarantine_parent = tmp_path / "quarantines"
    quarantine_parent.mkdir()
    monkeypatch.setattr(claims.tempfile, "tempdir", str(quarantine_parent))
    board = _board(tmp_path, board_repo, "worker")

    assert board.read(key)["schema"] == claims.CLAIM_SCHEMA
    assert list(quarantine_parent.iterdir()) == []
    _entries, scratch_bytes = board._enforce_scratch_filesystem_budget()
    assert scratch_bytes <= claims.CLAIM_SCRATCH_MAX_BYTES
    board._git(
        [
            "fetch",
            "--quiet",
            str(board_repo),
            f"+{claims.CLAIM_REF_PREFIX}{key}:{claims.CLAIM_REF_PREFIX}{key}",
        ]
    )
    _git("update-ref", "-d", claims.CLAIM_REF_PREFIX + key, cwd=board_repo)

    assert board.list() == []
    assert (
        _git(
            "for-each-ref",
            "--format=%(refname)",
            claims.CLAIM_REF_PREFIX,
            cwd=board.scratch,
        )
        == ""
    )


def test_list_fetches_hundreds_of_claims_in_one_bounded_batch(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []
    claim_count = 256
    for index in range(claim_count):
        key = f"resource/bulk-{index:04d}"
        message = json.dumps(
            {
                "schema": claims.CLAIM_SCHEMA,
                "lease_id": f"{index + 1:064x}",
                "owner": "bulk-worker",
                "host": "test-host",
                "pid": 1,
                "acquired_at": 100.0,
                "renewed_at": 100.0,
                "expires_at": 200.0,
                "resource": key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        commands.extend(
            [
                f"commit {claims.CLAIM_REF_PREFIX}{key}\n",
                "committer test <test@example.com> 100 +0000\n",
                f"data {len(message.encode('utf-8'))}\n",
                f"{message}\n",
                "deleteall\n\n",
            ]
        )
    commands.append("done\n")
    subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=board_repo,
        input="".join(commands),
        capture_output=True,
        text=True,
        check=True,
    )
    board = _board(tmp_path, board_repo, "worker")
    fetch = board._fetch_claims_quarantined
    fetches = 0

    def count_fetch(entries):
        nonlocal fetches
        fetches += 1
        fetch(entries)

    monkeypatch.setattr(board, "_fetch_claims_quarantined", count_fetch)

    listed = board.list()
    assert len(listed) == claim_count
    assert not any(item["_malformed"] for item in listed)
    assert fetches == 1


def test_list_retries_quarantine_churn_instead_of_marking_unreadable(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/churn"
    _plant_lease(board_repo, key)
    board = _board(tmp_path, board_repo, "worker")
    fetch = board._fetch_claims_quarantined
    calls = 0

    def churn_once(entries):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise claims._ClaimChurnError("changed")
        fetch(entries)

    monkeypatch.setattr(board, "_fetch_claims_quarantined", churn_once)

    listed = board.list()
    assert listed[0]["_malformed"] is False
    assert calls == 2


def test_failed_fetch_is_retried_as_churn_when_advertised_tip_changed(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/fetch-churn"
    _plant_lease(board_repo, key)
    board = _board(tmp_path, board_repo, "worker")
    run = board._quarantine_git
    fetches = 0

    def fail_changed_fetch(binding, args, **kwargs):
        nonlocal fetches
        if args and args[0] == "fetch":
            fetches += 1
            if fetches == 1:
                _plant_lease(board_repo, key, owner="replacement")
                return subprocess.CompletedProcess(
                    args,
                    128,
                    stdout="",
                    stderr="localized transport failure",
                )
        return run(binding, args, **kwargs)

    monkeypatch.setattr(board, "_quarantine_git", fail_changed_fetch)

    listed = board.list()
    assert listed[0]["owner"] == "replacement"
    assert listed[0]["_malformed"] is False
    assert fetches == 2


def test_acquire_rolls_back_if_post_mutation_legacy_verification_fails(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/rollback"
    board = _board(tmp_path, board_repo, "worker")
    checks = 0

    def fail_second_check(_key: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise claims.ClaimTransportError("verification failed")
        return False

    monkeypatch.setattr(board, "_legacy_author_claim_blocks_v2", fail_second_check)

    with pytest.raises(claims.ClaimTransportError, match="verification failed"):
        board.acquire(key, ttl=600)
    assert board._remote_oid(key) is None


def test_renew_restores_prior_lease_if_post_mutation_legacy_verification_fails(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/renew-rollback"
    board = _board(tmp_path, board_repo, "worker")
    assert board.acquire(key, ttl=600)
    original = board._remote_oid(key)
    checks = 0

    def fail_second_check(_key: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise claims.ClaimTransportError("verification failed")
        return False

    monkeypatch.setattr(board, "_legacy_author_claim_blocks_v2", fail_second_check)

    with pytest.raises(claims.ClaimTransportError, match="verification failed"):
        board.renew(key, ttl=600)
    assert board._remote_oid(key) == original
    assert board._receipt_oid(key) == original


def test_list_bounds_retries_when_fetched_tips_never_match_advertisement(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "author/repeated-churn"
    _plant_lease(board_repo, key)
    board = _board(tmp_path, board_repo, "worker")
    run = board._quarantine_git
    mismatches = 0

    def mismatched_tip(binding, args, **kwargs):
        nonlocal mismatches
        if args and args[0] == "for-each-ref" and args[-1].startswith(claims.CLAIM_QUARANTINE_REF_PREFIX):
            mismatches += 1
            destination = f"{args[-1]}/0"
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"{destination} {'f' * 40}\n",
                stderr="",
            )
        return run(binding, args, **kwargs)

    monkeypatch.setattr(board, "_quarantine_git", mismatched_tip)

    with pytest.raises(claims.ClaimTransportError, match="kept changing"):
        board.list()
    assert mismatches == claims.CLAIM_LIST_RETRIES


def test_claim_commit_must_be_parentless_over_empty_tree(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = "author/parented"
    parent = _plant_lease(board_repo, "temporary")
    tree = _git("mktree", cwd=board_repo, input_text="")
    child = _git("commit-tree", tree, "-p", parent, "-m", "{}", cwd=board_repo)
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, child, cwd=board_repo)
    _git("update-ref", "-d", claims.CLAIM_REF_PREFIX + "temporary", cwd=board_repo)
    board = _board(tmp_path, board_repo, "worker")

    with pytest.raises(claims.MalformedLeaseError, match="parentless commit"):
        board.read(key)
    listed = board.list()
    assert listed[0]["_key"] == key
    assert listed[0]["_malformed"] is True
    assert "parentless commit" in listed[0]["_error"]


def test_claim_commit_must_use_the_canonical_empty_tree(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = "author/nonempty-tree"
    blob = _git("hash-object", "-w", "--stdin", cwd=board_repo, input_text="payload")
    tree = _git("mktree", cwd=board_repo, input_text=f"100644 blob {blob}\tfile\n")
    commit = _git("commit-tree", tree, "-m", "{}", cwd=board_repo)
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, commit, cwd=board_repo)
    board = _board(tmp_path, board_repo, "worker")

    with pytest.raises(claims.MalformedLeaseError, match="canonical empty tree"):
        board.read(key)
    listed = board.list()
    assert listed[0]["_key"] == key
    assert listed[0]["_malformed"] is True
    assert "canonical empty tree" in listed[0]["_error"]


def test_list_classifies_a_noncommit_claim_tip_as_malformed(
    tmp_path: Path,
    board_repo: Path,
) -> None:
    key = "author/blob"
    blob = _git("hash-object", "-w", "--stdin", cwd=board_repo, input_text="payload")
    _git("update-ref", claims.CLAIM_REF_PREFIX + key, blob, cwd=board_repo)
    board = _board(tmp_path, board_repo, "worker")

    with pytest.raises(claims.MalformedLeaseError, match="does not point to a commit"):
        board.read(key)
    listed = board.list()
    assert listed[0]["_key"] == key
    assert listed[0]["_malformed"] is True
    assert "does not point to a commit" in listed[0]["_error"]


def test_mutation_timestamp_is_sampled_after_local_commit_preflight(
    tmp_path: Path,
    board_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _board(tmp_path, board_repo, "worker")
    events: list[str] = []
    require_capacity = board._require_scratch_capacity
    run_git = board._git

    def record_capacity(*, entries: int, size: int) -> None:
        events.append("capacity")
        require_capacity(entries=entries, size=size)

    def record_git(args, **kwargs):
        if args and args[0] == "commit-tree":
            events.append("commit")
        return run_git(args, **kwargs)

    def record_clock() -> float:
        events.append("clock")
        return 100.0

    monkeypatch.setattr(board, "_require_scratch_capacity", record_capacity)
    monkeypatch.setattr(board, "_git", record_git)
    monkeypatch.setattr(board, "_operation_time", record_clock)

    with board._operation():
        board._ensure_scratch()
        events.clear()
        board._make_lease_commit("author/timestamp", 600)

    assert events[-2:] == ["clock", "commit"]
    assert "capacity" in events[:-2]
