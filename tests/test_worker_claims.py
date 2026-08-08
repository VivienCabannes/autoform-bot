"""Tests for the claim board (autoform_worker/claims.py) — git-ref leases.

A claim is a ref ``refs/autoform-claims/<key>`` in a (here: local bare) board
repo pointing at an orphan commit whose message is the lease JSON. All board
traffic is plain git against tmp_path — no network, no gh.
"""
import os
import subprocess
import time

import pytest

from autoform_worker import claims
from autoform_worker.constants import (
    CLAIM_HEARTBEAT_S,
    CLAIM_KEY_RE,
    CLAIM_REF_PREFIX,
    CLAIM_SCHEMA,
    CLAIM_TTL_S,
)
from autoform_worker.errors import ClaimTransportError


def _run(args, cwd=None):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _make_board_repo(tmp_path, name="board.git"):
    url = tmp_path / name
    _run(["init", "--bare", "--quiet", str(url)])
    return str(url)


def _board(tmp_path, url, worker_id):
    return claims.ClaimBoard(url, worker_id, tmp_path / f"scratch-{worker_id}")


# -- constants ---------------------------------------------------------------


def test_claim_constants_shape():
    assert CLAIM_SCHEMA == "autoform-claim/v1"
    assert CLAIM_REF_PREFIX == "refs/autoform-claims/"
    assert CLAIM_TTL_S > CLAIM_HEARTBEAT_S  # a lease outlives missed heartbeats
    for key in ("author/node-1", "branch/42", "progress"):  # documented key shapes
        assert CLAIM_KEY_RE.match(key)


# -- acquire / holds / read --------------------------------------------------


def test_acquire_free_key_and_read_schema(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    before = int(time.time())
    assert a.acquire("author/node-1", ttl=600) is True
    lease = a.read("author/node-1")
    assert lease is not None
    assert lease["schema"] == CLAIM_SCHEMA
    assert lease["owner"] == "worker-a"
    assert lease["resource"] == "author/node-1"
    assert before + 600 <= lease["expires_at"] <= time.time() + 600
    assert lease["acquired_at"] <= lease["expires_at"]


def test_second_worker_refused_while_live_and_holds_both_sides(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    b = _board(tmp_path, url, "worker-b")
    assert a.acquire("author/n", ttl=600) is True
    assert b.acquire("author/n", ttl=600) is False
    assert a.holds("author/n") is True
    assert b.holds("author/n") is False
    assert a.read("author/n")["owner"] == "worker-a"  # untouched by the refused acquire


def test_owner_reacquire_is_allowed(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.acquire("k", ttl=600) is True
    assert a.acquire("k", ttl=600) is True  # re-take/renew of our own live lease
    assert a.holds("k") is True


def test_read_absent_key_returns_none(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.read("nothing-here") is None
    assert a.holds("nothing-here") is False


# -- renew -------------------------------------------------------------------


def test_renew_extends_and_preserves_ownership(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    b = _board(tmp_path, url, "worker-b")
    assert a.acquire("k", ttl=30) is True
    exp1 = a.read("k")["expires_at"]
    time.sleep(1.1)  # int(now) has advanced, so a same-ttl renewal must move expires_at
    assert a.renew("k", ttl=30) is True
    lease = a.read("k")
    assert lease["owner"] == "worker-a"
    assert lease["expires_at"] > exp1
    assert b.renew("k") is False  # not the owner
    assert a.read("k")["owner"] == "worker-a"


def test_renew_absent_key_returns_false(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.renew("never-acquired") is False


# -- expiry / takeover / steal ----------------------------------------------


def test_expired_lease_taken_over_without_steal(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    b = _board(tmp_path, url, "worker-b")
    assert a.acquire("k", ttl=1) is True
    time.sleep(1.5)
    assert a.holds("k") is False  # expired, even for the owner
    assert b.acquire("k", ttl=600) is True  # no steal needed
    assert b.read("k")["owner"] == "worker-b"
    assert b.holds("k") is True


def test_live_foreign_lease_cannot_be_taken_over(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    b = _board(tmp_path, url, "worker-b")
    assert a.acquire("k", ttl=600) is True
    assert b.acquire("k", ttl=600) is False
    assert b.read("k")["owner"] == "worker-a"


# -- release -----------------------------------------------------------------


def test_release_only_deletes_own_lease(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    b = _board(tmp_path, url, "worker-b")
    assert a.acquire("k", ttl=600) is True
    assert b.release("k") is False  # foreign lease — refuse and leave the ref
    assert a.read("k") is not None
    assert a.read("k")["owner"] == "worker-a"
    assert a.release("k") is True
    assert a.read("k") is None
    assert a.release("k") is True  # already absent is fine


# -- list / gc ---------------------------------------------------------------


def test_list_flags_expired_correctly(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.acquire("dead", ttl=1) is True
    assert a.acquire("live", ttl=600) is True
    time.sleep(1.5)
    leases = {item["_key"]: item for item in a.list()}
    assert set(leases) == {"dead", "live"}
    assert leases["dead"]["_expired"] is True
    assert leases["live"]["_expired"] is False
    assert leases["live"]["owner"] == "worker-a"
    assert leases["live"]["schema"] == CLAIM_SCHEMA


def test_gc_removes_exactly_the_expired_leases(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.acquire("dead/one", ttl=1) is True
    assert a.acquire("dead/two", ttl=1) is True
    assert a.acquire("live", ttl=600) is True
    time.sleep(1.5)
    assert a.gc() == 2
    remaining = [item["_key"] for item in a.list()]
    assert remaining == ["live"]
    assert a.holds("live") is True


# -- the CAS race ------------------------------------------------------------


def test_cas_race_exactly_one_winner(tmp_path):
    # Both boards observe the key absent, both build a lease commit, both push
    # create-only. Sequencing the pushes reproduces the race deterministically:
    # the loser's expected-absent CAS must fail cleanly (False, not an error).
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    b = _board(tmp_path, url, "worker-b")
    a._ensure_scratch()
    b._ensure_scratch()
    assert a._remote_oid("race") is None
    assert b._remote_oid("race") is None
    commit_a = a._make_lease_commit("race", 600)
    commit_b = b._make_lease_commit("race", 600)
    assert a._cas_push("race", None, commit_a) is True
    assert b._cas_push("race", None, commit_b) is False  # lost the race
    lease = b.read("race")
    assert lease["owner"] == "worker-a"  # winner's lease survives
    assert a.holds("race") is True
    assert b.acquire("race", ttl=600) is False  # loser stays refused while live


# -- malformed leases --------------------------------------------------------


def _plant_malformed_lease(board_url, key):
    """Point the claim ref at a commit whose message is not JSON."""
    ident = ["-c", "user.email=t@t.t", "-c", "user.name=t"]
    tree = _run(["-C", board_url, *ident, "hash-object", "-w", "-t", "tree", os.devnull])
    commit = _run(["-C", board_url, *ident, "commit-tree", tree, "-m", "definitely not json"])
    _run(["-C", board_url, *ident, "update-ref", CLAIM_REF_PREFIX + key, commit])


def test_malformed_lease_fails_closed_and_cannot_be_taken_over(tmp_path):
    url = _make_board_repo(tmp_path)
    _plant_malformed_lease(url, "bad")
    a = _board(tmp_path, url, "worker-a")
    with pytest.raises(claims.MalformedLeaseError):
        a.read("bad")
    listed = {item["_key"]: item for item in a.list()}
    assert listed["bad"]["_malformed"] is True
    assert listed["bad"]["_expired"] is False
    with pytest.raises(claims.MalformedLeaseError):
        a.acquire("bad", ttl=600)


# -- key validation / transport errors --------------------------------------


@pytest.mark.parametrize("key", ["has space", "a/../b", "/leading", ".."])
def test_invalid_keys_raise_value_error(tmp_path, key):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    with pytest.raises(ValueError):
        a.acquire(key)
    with pytest.raises(ValueError):
        a.read(key)


def test_unreachable_repo_url_raises_claim_transport_error(tmp_path):
    a = _board(tmp_path, str(tmp_path / "no-such" / "board.git"), "worker-a")
    with pytest.raises(ClaimTransportError):
        a.read("k")
    with pytest.raises(ClaimTransportError):
        a.acquire("k")


# -- heartbeat ---------------------------------------------------------------


def test_heartbeat_renews_lease(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.acquire("k", ttl=30) is True
    exp1 = a.read("k")["expires_at"]
    with claims.Heartbeat(a, "k", interval=0.4) as hb:
        time.sleep(1.5)  # several renewal ticks; int(now) advances past acquire time
    assert not hb.lost.is_set()
    lease = a.read("k")
    assert lease["owner"] == "worker-a"
    assert lease["expires_at"] > exp1
    assert a.holds("k") is True


def test_heartbeat_sets_lost_when_lease_is_replaced(tmp_path):
    url = _make_board_repo(tmp_path)
    a = _board(tmp_path, url, "worker-a")
    assert a.acquire("k", ttl=600) is True
    with claims.Heartbeat(a, "k", interval=0.2) as hb:
        _run(["-C", url, "update-ref", "-d", CLAIM_REF_PREFIX + "k"])
        assert hb.lost.wait(timeout=10) is True
    assert a.holds("k") is False
