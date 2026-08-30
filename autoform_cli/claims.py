"""Host-neutral, fail-closed Git-ref leases for cooperative work claims.

Each claim is stored under ``refs/autoform-claims/`` and points to an orphan
commit whose message is the lease JSON. Mutations use an exact observed object
ID as a compare-and-swap precondition, so concurrent claimants cannot silently
overwrite one another.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

CLAIM_REF_PREFIX = "refs/autoform-claims/"
CLAIM_RECEIPT_REF_PREFIX = "refs/autoform-claim-receipts/"
CLAIM_SCHEMA = "autoform-claim/v2"
LEGACY_CLAIM_SCHEMA = "autoform-claim/v1"
CLAIM_TTL_S = 1500
CLAIM_HEARTBEAT_S = 300
CLAIM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
LEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "autoform",
    "GIT_AUTHOR_EMAIL": "autoform@localhost",
    "GIT_COMMITTER_NAME": "autoform",
    "GIT_COMMITTER_EMAIL": "autoform@localhost",
}
_CAS_REJECTIONS = (
    "stale info",
    "fetch first",
    "remote ref updated since checkout",
    "cannot lock ref",
)


class ClaimTransportError(RuntimeError):
    """A claim board operation could not be completed or verified."""


class MalformedLeaseError(ClaimTransportError):
    """A claim ref exists, but its lease cannot be verified safely."""


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not CLAIM_KEY_RE.fullmatch(key) or ".." in key:
        raise ValueError(f"invalid claim key {key!r}")
    parts = key.split("/")
    if any(part.startswith(".") or part.endswith(".") or part.endswith(".lock") for part in parts):
        raise ValueError(f"invalid claim key {key!r}")
    return key


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_ttl(ttl: int | float) -> int | float:
    if not _is_finite_number(ttl) or ttl <= 0:
        raise ValueError("claim TTL must be a finite positive number")
    return ttl


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def author_claim_key(node_id: str) -> str:
    """Return a readable, ref-safe, collision-resistant author claim key."""
    if not isinstance(node_id, str):
        raise TypeError("node_id must be a string")
    slug = re.sub(r"[^a-z0-9-]+", "-", node_id.lower()).strip("-")[:48] or "node"
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    return f"author/{slug}-{digest}"


def resource_claim_key(resource: str) -> str:
    """Return a ref-safe key in the namespace for non-article resources."""
    if not isinstance(resource, str):
        raise TypeError("resource must be a string")
    if not resource:
        raise ValueError("resource must not be empty")
    slug = re.sub(r"[^a-z0-9-]+", "-", resource.lower()).strip("-")[:48] or "resource"
    digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:16]
    return f"resource/{slug}-{digest}"


class ClaimBoard:
    """Lease operations against a Git repository via a local bare object store."""

    def __init__(
        self,
        repo_url: str | os.PathLike[str],
        worker_id: str,
        scratch: str | os.PathLike[str],
        *,
        session_id: str | None = None,
    ):
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        raw_repo_url = os.fspath(repo_url)
        if "://" not in raw_repo_url and not re.match(r"^[^/]+@[^:]+:", raw_repo_url):
            raw_repo_url = str(Path(raw_repo_url).expanduser().resolve())
        self.repo_url = raw_repo_url
        self.worker_id = worker_id
        self.scratch = Path(scratch)
        if session_id is None:
            session_id = f"scratch:{self.scratch.expanduser().resolve()}"
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self._session_key = hashlib.sha256(
            f"{self.repo_url}\0{session_id}".encode("utf-8")
        ).hexdigest()

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.scratch,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=120,
                env={**os.environ, **_GIT_ENV},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaimTransportError(f"git claim-board operation failed: {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(f"git {' '.join(args[:2])} failed against claim board: {detail}")
        return proc

    def _ensure_scratch(self) -> None:
        if (self.scratch / "HEAD").is_file():
            return
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._git(["init", "--bare", "--quiet", "."])

    @staticmethod
    def _ref(key: str) -> str:
        return CLAIM_REF_PREFIX + _validate_key(key)

    def _receipt_ref(self, key: str) -> str:
        return f"{CLAIM_RECEIPT_REF_PREFIX}{self._session_key}/{_validate_key(key)}"

    def _remote_oid(self, key: str) -> str | None:
        proc = self._git(["ls-remote", self.repo_url, self._ref(key)])
        line = proc.stdout.strip()
        return line.split("\t", 1)[0] if line else None

    def _receipt_oid(self, key: str) -> str | None:
        proc = self._git(
            ["rev-parse", "--verify", "--quiet", self._receipt_ref(key)],
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
        if proc.returncode == 1:
            return None
        detail = (proc.stderr or proc.stdout).strip()[:300]
        raise ClaimTransportError(f"could not read local claim receipt: {detail}")

    def _record_receipt(self, key: str, oid: str, *, expected: str | None = None) -> None:
        args = ["update-ref", self._receipt_ref(key), oid]
        if expected is not None:
            args.append(expected)
        proc = self._git(args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "remote claim changed but its exact local ownership receipt could not be recorded"
                + (f": {detail}" if detail else "")
            )

    def _clear_receipt(self, key: str, *, expected: str | None = None) -> None:
        args = ["update-ref", "-d", self._receipt_ref(key)]
        if expected is not None:
            args.append(expected)
        proc = self._git(args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "remote claim changed but its local ownership receipt could not be cleared"
                + (f": {detail}" if detail else "")
            )

    def _read_lease(self, key: str, oid: str) -> dict[str, Any]:
        ref = self._ref(key)
        if self._git(["cat-file", "-e", f"{oid}^{{commit}}"], check=False).returncode != 0:
            self._git(["fetch", "--quiet", self.repo_url, f"+{ref}:{ref}"])
        proc = self._git(["cat-file", "commit", oid], check=False)
        if proc.returncode != 0:
            raise MalformedLeaseError(f"claim {key!r} does not point to a readable commit")
        _, separator, message = proc.stdout.partition("\n\n")
        if not separator:
            raise MalformedLeaseError(f"claim {key!r} has no lease message")
        try:
            lease = json.loads(
                message,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedLeaseError(f"claim {key!r} has invalid lease JSON") from exc
        if not isinstance(lease, dict) or not self._lease_is_valid(lease, key):
            raise MalformedLeaseError(f"claim {key!r} has an invalid lease schema")
        return lease

    @staticmethod
    def _lease_is_valid(lease: Mapping[str, Any], key: str | None = None) -> bool:
        acquired_at = lease.get("acquired_at")
        expires_at = lease.get("expires_at")
        valid = (
            lease.get("schema") in {CLAIM_SCHEMA, LEGACY_CLAIM_SCHEMA}
            and isinstance(lease.get("owner"), str)
            and bool(lease.get("owner"))
            and isinstance(lease.get("resource"), str)
            and _is_finite_number(acquired_at)
            and _is_finite_number(expires_at)
            and acquired_at <= expires_at
        )
        if lease.get("schema") == CLAIM_SCHEMA:
            valid = valid and isinstance(lease.get("lease_id"), str) and bool(
                LEASE_ID_RE.fullmatch(str(lease.get("lease_id")))
            )
        return bool(valid and (key is None or lease.get("resource") == key))

    def _make_lease_commit(
        self,
        key: str,
        ttl: int | float,
        note: str = "",
        *,
        lease_id: str | None = None,
        acquired_at: int | float | None = None,
    ) -> str:
        key = _validate_key(key)
        ttl = _validate_ttl(ttl)
        now = time.time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        try:
            expires_at = now + ttl
        except OverflowError as exc:
            raise ValueError("claim expiry must be finite") from exc
        if not math.isfinite(expires_at):
            raise ValueError("claim expiry must be finite")
        if acquired_at is None:
            acquired_at = now
        if not _is_finite_number(acquired_at) or acquired_at > now:
            raise ValueError("claim acquisition timestamp must be finite and not in the future")
        if lease_id is None:
            lease_id = secrets.token_hex(32)
        if not isinstance(lease_id, str) or not LEASE_ID_RE.fullmatch(lease_id):
            raise ValueError("claim lease_id must be 64 lowercase hexadecimal characters")
        lease: dict[str, Any] = {
            "schema": CLAIM_SCHEMA,
            "lease_id": lease_id,
            "owner": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "expires_at": expires_at,
            "resource": key,
        }
        if note:
            lease["note"] = note
        tree = self._git(["mktree"], input_text="").stdout.strip()
        message = json.dumps(lease, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _cas_push(self, key: str, old: str | None, new: str) -> bool:
        ref = self._ref(key)
        source = new if new else ""
        proc = self._git(
            [
                "push",
                "--quiet",
                "--porcelain",
                f"--force-with-lease={ref}:{old or ''}",
                self.repo_url,
                f"{source}:{ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if any(marker in detail.lower() for marker in _CAS_REJECTIONS):
            return False
        raise ClaimTransportError(f"claim CAS push failed: {detail[:300]}")

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the current parsed lease, including an expired lease, or ``None``."""
        self._ensure_scratch()
        oid = self._remote_oid(key)
        return self._read_lease(key, oid) if oid else None

    @classmethod
    def expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether a lease is malformed or no longer live."""
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        return expires_at <= comparison_time

    def acquire(self, key: str, ttl: int | float = CLAIM_TTL_S, steal: bool = False, note: str = "") -> bool:
        """CAS-acquire a free or expired lease, or refresh this exact session's lease."""
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        old = self._remote_oid(key)
        lease_id: str | None = None
        acquired_at: int | float | None = None
        if old is not None:
            lease = self._read_lease(key, old)
            if not self.expired(lease):
                if lease.get("schema") == LEGACY_CLAIM_SCHEMA:
                    return False
                if not self._receipt_matches(key, old, lease):
                    if not steal:
                        return False
                else:
                    lease_id = str(lease["lease_id"])
                    acquired_at = lease["acquired_at"]
        new = self._make_lease_commit(
            key,
            ttl,
            note,
            lease_id=lease_id,
            acquired_at=acquired_at,
        )
        if not self._cas_push(key, old, new):
            return False
        self._record_receipt(key, new, expected=old if lease_id is not None else None)
        return True

    def renew(
        self,
        key: str,
        ttl: int | float = CLAIM_TTL_S,
        *,
        lease_id: str | None = None,
    ) -> bool:
        """CAS-renew this session's exact lease, returning ``False`` if it was lost."""
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is None:
            return False
        lease = self._read_lease(key, old)
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.expired(lease)
            or not self._receipt_matches(key, old, lease)
            or (lease_id is not None and lease.get("lease_id") != lease_id)
        ):
            return False
        new = self._make_lease_commit(
            key,
            ttl,
            str(lease.get("note", "")),
            lease_id=str(lease["lease_id"]),
            acquired_at=lease["acquired_at"],
        )
        if not self._cas_push(key, old, new):
            return False
        self._record_receipt(key, new, expected=old)
        return True

    def release(self, key: str) -> bool:
        """CAS-delete this session's lease; refuse stale or unverifiable ownership."""
        key = _validate_key(key)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is None:
            self._clear_receipt(key)
            return True
        lease = self._read_lease(key, old)
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.expired(lease)
            or not self._receipt_matches(key, old, lease)
        ):
            return False
        if not self._cas_push(key, old, ""):
            return False
        self._clear_receipt(key, expected=old)
        return True

    def holds(self, key: str) -> bool:
        """Return whether this session has the exact receipt for the live lease."""
        return self.held_lease_id(key) is not None

    def held_lease_id(self, key: str) -> str | None:
        """Return the fenced lease id held by this session, or ``None``."""
        key = _validate_key(key)
        self._ensure_scratch()
        oid = self._remote_oid(key)
        if oid is None:
            return None
        lease = self._read_lease(key, oid)
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.expired(lease)
            or not self._receipt_matches(key, oid, lease)
        ):
            return None
        return str(lease["lease_id"])

    def _receipt_matches(self, key: str, oid: str, lease: Mapping[str, Any]) -> bool:
        """Return whether this session recorded this exact v2 lease commit."""
        receipt_oid = self._receipt_oid(key)
        if receipt_oid != oid or lease.get("schema") != CLAIM_SCHEMA:
            return False
        receipt = self._read_lease(key, receipt_oid)
        return bool(receipt.get("lease_id") == lease.get("lease_id"))

    def list(self) -> list[dict[str, Any]]:
        """Return all claim refs, including malformed and expired entries."""
        self._ensure_scratch()
        proc = self._git(["ls-remote", self.repo_url, CLAIM_REF_PREFIX + "*"])
        leases: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            oid, separator, ref = line.partition("\t")
            if not separator or not ref.startswith(CLAIM_REF_PREFIX):
                continue
            key = ref[len(CLAIM_REF_PREFIX) :]
            try:
                _validate_key(key)
            except ValueError:
                continue
            try:
                lease = dict(self._read_lease(key, oid))
            except MalformedLeaseError as exc:
                lease = {
                    "resource": key,
                    "schema": "unreadable",
                    "_error": str(exc),
                    "_malformed": True,
                }
            else:
                lease["_malformed"] = False
                lease["_legacy"] = lease.get("schema") == LEGACY_CLAIM_SCHEMA
            lease["_key"] = key
            lease["_oid"] = oid
            lease["_expired"] = not lease["_malformed"] and self.expired(lease)
            leases.append(lease)
        return sorted(leases, key=lambda lease: str(lease["_key"]))

    def cleanup(self) -> int:
        """CAS-delete leases expired at snapshot time and return the deletion count."""
        removed = 0
        for lease in self.list():
            if not lease["_malformed"] and lease["_expired"] and self._cas_push(
                str(lease["_key"]), str(lease["_oid"]), ""
            ):
                removed += 1
        return removed

    def gc(self) -> int:
        """Compatibility alias for :meth:`cleanup`."""
        return self.cleanup()

    def heartbeat(
        self,
        key: str,
        *,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> Heartbeat:
        """Create a fail-closed heartbeat for an already acquired lease."""
        return Heartbeat(self, key, interval=interval, ttl=ttl)


class Heartbeat:
    """Renew a lease in a daemon thread and permanently record any uncertainty."""

    def __init__(
        self,
        board: ClaimBoard,
        key: str,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> None:
        if not _is_finite_number(interval) or interval <= 0:
            raise ValueError("heartbeat interval must be a finite positive number")
        _validate_key(key)
        _validate_ttl(ttl)
        if interval >= ttl:
            raise ValueError("heartbeat interval must be shorter than the claim TTL")
        self.board = board
        self.key = key
        self.interval = interval
        self.ttl = ttl
        self.lost = threading.Event()
        self.error: Exception | None = None
        self.lease_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Heartbeat:
        if self._thread is not None:
            raise RuntimeError("heartbeat cannot be started more than once")
        try:
            self.lease_id = self.board.held_lease_id(self.key)
            renewed = self.lease_id is not None and self.board.renew(
                self.key,
                ttl=self.ttl,
                lease_id=self.lease_id,
            )
        except Exception as exc:
            self.error = exc
            self.lost.set()
            raise ClaimTransportError("claim ownership could not be verified before heartbeat entry") from exc
        if not renewed:
            self.lost.set()
            raise ClaimTransportError("claim ownership was lost before heartbeat entry")
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"claim-heartbeat-{self.key}")
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                renewed = self.board.renew(
                    self.key,
                    ttl=self.ttl,
                    lease_id=self.lease_id,
                )
            except Exception as exc:
                self.error = exc
                self.lost.set()
                return
            if not renewed:
                self.lost.set()
                return


__all__ = [
    "CLAIM_HEARTBEAT_S",
    "CLAIM_KEY_RE",
    "CLAIM_RECEIPT_REF_PREFIX",
    "CLAIM_REF_PREFIX",
    "CLAIM_SCHEMA",
    "CLAIM_TTL_S",
    "ClaimBoard",
    "ClaimTransportError",
    "Heartbeat",
    "LEGACY_CLAIM_SCHEMA",
    "LEASE_ID_RE",
    "MalformedLeaseError",
    "author_claim_key",
    "resource_claim_key",
]
