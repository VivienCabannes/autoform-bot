"""The claim board — cooperative git-ref leases ([COOP] layer).

A claim is a custom ref ``refs/autoform-claims/<key>`` in the claims repo,
pointing at an orphan commit (empty tree) whose commit *message* is the lease
JSON. Acquire/renew/release are CAS pushes of that ref
(``--force-with-lease=<ref>:<observed-oid>``), so claims inherit git's atomic
ref update — and work on any git host with no server-side setup and no Issues
requirement.

Claims are cooperative: losing one, or the board being unreachable, never
corrupts anything — safety comes from the branch-level CAS in
:mod:`autoform_worker.gitutil`. Board errors raise
:class:`~autoform_worker.errors.ClaimTransportError`; callers log loudly and
continue uncoordinated.

Lease JSON (the commit message)::

    {"schema": "autoform-claim/v1", "owner": "<worker-id>", "host": "<hostname>",
     "pid": 12345, "acquired_at": 1690000000, "expires_at": 1690001500,
     "resource": "<key>", "note": ""}

Keys in use: ``author/<node-id>``, ``branch/<pr-number>``, ``progress``.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from .constants import CLAIM_HEARTBEAT_S, CLAIM_KEY_RE, CLAIM_REF_PREFIX, CLAIM_SCHEMA, CLAIM_TTL_S
from .errors import ClaimTransportError

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "autoform-worker",
    "GIT_AUTHOR_EMAIL": "autoform-worker@localhost",
    "GIT_COMMITTER_NAME": "autoform-worker",
    "GIT_COMMITTER_EMAIL": "autoform-worker@localhost",
}


def _validate_key(key: str) -> str:
    if not CLAIM_KEY_RE.match(key) or ".." in key:
        raise ValueError(f"invalid claim key {key!r}")
    return key


def author_claim_key(node_id: str) -> str:
    """The canonical ``author/…`` claim key for a graph node.

    Node ids are free text (spaces, apostrophes, unicode); a claim key must be
    ref-safe. Slug + short hash keeps keys readable AND collision-resistant, and
    every caller (survey avoid-list, prove, docs) derives it identically.
    """
    import hashlib
    import re as _re

    slug = _re.sub(r"[^a-z0-9-]+", "-", node_id.lower()).strip("-")[:48] or "node"
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:8]
    return f"author/{slug}-{digest}"


class ClaimBoard:
    """Lease operations against one claims repo, via a local bare scratch repo."""

    def __init__(self, repo_url: str, worker_id: str, scratch: Path):
        self.repo_url = repo_url
        self.worker_id = worker_id
        self.scratch = scratch

    # -- plumbing -----------------------------------------------------------

    def _git(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, **_GIT_ENV},
        )
        if check and proc.returncode != 0:
            raise ClaimTransportError(
                f"git {' '.join(args[:2])}… failed against claim board: {proc.stderr.strip()[:300]}"
            )
        return proc

    def _ensure_scratch(self) -> None:
        if not (self.scratch / "HEAD").exists():
            self.scratch.mkdir(parents=True, exist_ok=True)
            self._git(["init", "--bare", "--quiet", str(self.scratch)])

    def _remote_oid(self, key: str) -> str | None:
        ref = CLAIM_REF_PREFIX + _validate_key(key)
        proc = self._git(["ls-remote", self.repo_url, ref])
        line = proc.stdout.strip()
        return line.split("\t", 1)[0] if line else None

    def _read_lease(self, key: str, oid: str) -> dict | None:
        """The lease JSON at ``oid`` (fetching the ref if the object is absent)."""
        ref = CLAIM_REF_PREFIX + key
        if self._git(["cat-file", "-e", f"{oid}^{{commit}}"], check=False).returncode != 0:
            self._git(["fetch", "--quiet", self.repo_url, f"+{ref}:{ref}"])
        proc = self._git(["cat-file", "commit", oid], check=False)
        if proc.returncode != 0:
            return None
        raw = proc.stdout
        body = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
        try:
            lease = json.loads(body)
        except json.JSONDecodeError:
            return None
        return lease if isinstance(lease, dict) else None

    def _make_lease_commit(self, key: str, ttl: int, note: str = "") -> str:
        now = int(time.time())
        lease = {
            "schema": CLAIM_SCHEMA,
            "owner": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": now,
            "expires_at": now + ttl,
            "resource": key,
        }
        if note:
            lease["note"] = note
        tree = self._git(["hash-object", "-w", "-t", "tree", os.devnull]).stdout.strip()
        return self._git(["commit-tree", tree, "-m", json.dumps(lease)]).stdout.strip()

    def _cas_push(self, key: str, old: str | None, new: str) -> bool:
        """Push ``new`` (or delete when ``new`` empty) with CAS on ``old``."""
        ref = CLAIM_REF_PREFIX + key
        lease = f"{ref}:{old or ''}"
        src = new if new else ""
        proc = self._git(["push", "--quiet", f"--force-with-lease={lease}", self.repo_url, f"{src}:{ref}"],
                         check=False)
        if proc.returncode == 0:
            return True
        err = (proc.stderr or "") + (proc.stdout or "")
        if "stale info" in err or "[rejected]" in err or "[remote rejected]" in err:
            return False  # lost the race — someone else moved the ref
        raise ClaimTransportError(f"claim push failed: {err.strip()[:300]}")

    # -- the lease API ------------------------------------------------------

    def read(self, key: str) -> dict | None:
        """The current lease for ``key`` (may be expired), or None."""
        self._ensure_scratch()
        oid = self._remote_oid(key)
        return self._read_lease(key, oid) if oid else None

    @staticmethod
    def expired(lease: dict, now: float | None = None) -> bool:
        exp = lease.get("expires_at")
        return not isinstance(exp, (int, float)) or exp <= (now if now is not None else time.time())

    def acquire(self, key: str, ttl: int = CLAIM_TTL_S, steal: bool = False, note: str = "") -> bool:
        """Take (or re-take/renew) the lease. False iff held live by another.

        Takeover is allowed when the existing lease is expired, malformed, or
        ``steal`` is set. Ties race through the CAS — exactly one winner.
        """
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is not None:
            lease = self._read_lease(key, old)
            if (
                lease is not None
                and lease.get("owner") != self.worker_id
                and not self.expired(lease)
                and not steal
            ):
                return False
        new = self._make_lease_commit(key, ttl, note)
        return self._cas_push(key, old, new)

    def renew(self, key: str, ttl: int = CLAIM_TTL_S) -> bool:
        """Extend our own lease. False iff the lease was lost (owner changed/gone)."""
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is None:
            return False
        lease = self._read_lease(key, old)
        if lease is None or lease.get("owner") != self.worker_id:
            return False
        new = self._make_lease_commit(key, ttl, str(lease.get("note", "")))
        return self._cas_push(key, old, new)

    def release(self, key: str) -> bool:
        """Delete our own lease. True if deleted or already absent."""
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is None:
            return True
        lease = self._read_lease(key, old)
        if lease is not None and lease.get("owner") != self.worker_id:
            return False  # not ours — leave it alone
        return self._cas_push(key, old, "")

    def holds(self, key: str) -> bool:
        """Whether we hold an unexpired lease on ``key`` right now."""
        lease = self.read(key)
        return lease is not None and lease.get("owner") == self.worker_id and not self.expired(lease)

    def list(self) -> list[dict]:
        """All current leases (including expired ones, flagged)."""
        self._ensure_scratch()
        proc = self._git(["ls-remote", self.repo_url, CLAIM_REF_PREFIX + "*"])
        leases: list[dict] = []
        for line in proc.stdout.splitlines():
            if "\t" not in line:
                continue
            oid, ref = line.split("\t", 1)
            key = ref[len(CLAIM_REF_PREFIX):]
            if not CLAIM_KEY_RE.match(key):
                continue
            lease = self._read_lease(key, oid) or {"resource": key, "schema": "unreadable"}
            lease["_key"] = key
            lease["_oid"] = oid
            lease["_expired"] = self.expired(lease) if "expires_at" in lease else True
            leases.append(lease)
        return sorted(leases, key=lambda x: str(x.get("_key")))

    def gc(self) -> int:
        """CAS-delete expired leases; returns how many were removed.

        The CAS expect is the OID *at which expiry was judged* — a lease renewed
        or taken over between the listing and the delete makes the CAS lose,
        never deletes a live lease.
        """
        removed = 0
        for lease in self.list():
            if lease.get("_expired") and lease.get("_oid"):
                if self._cas_push(lease["_key"], lease["_oid"], ""):
                    removed += 1
        return removed

    # -- heartbeat ----------------------------------------------------------

    def heartbeat(self, key: str) -> "Heartbeat":
        return Heartbeat(self, key)


class Heartbeat:
    """Daemon-thread renewal of one lease while a long unit runs.

    Use as a context manager. ``lost`` is set the moment a renewal fails —
    callers MUST check ``heartbeat.lost.is_set()`` before any CAS push (the
    git-safe-push discipline: never push on a lost lease).
    """

    def __init__(self, board: ClaimBoard, key: str, interval: int = CLAIM_HEARTBEAT_S):
        self.board = board
        self.key = key
        self.interval = interval
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Heartbeat":
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"claim-heartbeat-{self.key}")
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not self.board.renew(self.key):
                    self.lost.set()
                    return
            except ClaimTransportError:
                # Transient board trouble is not a lost lease; keep trying —
                # the lease TTL (> heartbeat interval) absorbs brief outages.
                continue
