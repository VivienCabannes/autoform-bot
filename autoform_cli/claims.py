"""Host-neutral, fail-closed Git-ref leases for cooperative work claims.

Each claim is stored under ``refs/autoform-claims/`` and points to an orphan
commit whose message is the lease JSON. Mutations use an exact observed object
ID as a compare-and-swap precondition, so concurrent claimants cannot silently
overwrite one another.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar, cast
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ._directory_binding import RetainedDirectory, open_directory

try:
    import fcntl
except ImportError:  # pragma: no cover - descriptor binding is currently POSIX-only
    fcntl = None  # type: ignore[assignment]

CLAIM_REF_PREFIX = "refs/autoform-claims/"
CLAIM_RECEIPT_REF_PREFIX = "refs/autoform-claim-receipts/"
CLAIM_QUARANTINE_REF_PREFIX = "refs/autoform-quarantine/"
CLAIM_SCHEMA = "autoform-claim/v2"
LEGACY_CLAIM_SCHEMA = "autoform-claim/v1"
LEGACY_BLOCK_SCHEMA = "autoform-claim/legacy-block/v1"
LEGACY_TOMBSTONE_SCHEMA = "autoform-claim/legacy-tombstone/v1"
_PERMANENT_BLOCK_SCHEMAS = frozenset({LEGACY_BLOCK_SCHEMA, LEGACY_TOMBSTONE_SCHEMA})
CLAIM_TTL_S = 1500
CLAIM_HEARTBEAT_S = 300
CLAIM_MAX_TTL_S = 3600
CLAIM_CLOCK_SKEW_S = 300
CLAIM_OPERATION_TIMEOUT_S = 120.0
CLAIM_GIT_OUTPUT_MAX_BYTES = 4 << 20
CLAIM_LEASE_MAX_BYTES = 64 << 10
CLAIM_MAX_REFS = 16_384
CLAIM_FETCH_BATCH_SIZE = 512
CLAIM_OBJECT_READ_BATCH_SIZE = 32
CLAIM_FETCH_INPUT_MAX_BYTES = 512 << 10
CLAIM_PROMOTION_MAX_COMMITS = 8
CLAIM_KEY_MAX_BYTES = 512
CLAIM_KEY_COMPONENT_MAX_BYTES = 240
CLAIM_WORKER_ID_MAX_BYTES = 256
CLAIM_SESSION_ID_MAX_BYTES = 1024
CLAIM_NOTE_MAX_BYTES = 4096
CLAIM_REPOSITORY_MAX_BYTES = 4096
CLAIM_PID_MAX = (1 << 31) - 1
CLAIM_SCRATCH_MAX_BYTES = 64 << 20
CLAIM_SCRATCH_MAX_ENTRIES = 100_000
CLAIM_SCRATCH_CONFIG_MAX_BYTES = 64 << 10
CLAIM_QUARANTINE_MAX_FILE_BYTES = 16 << 20
CLAIM_QUARANTINE_MAX_BYTES = 24 << 20
CLAIM_QUARANTINE_MAX_ENTRIES = 1024
CLAIM_QUARANTINE_CPU_S = 30
CLAIM_QUARANTINE_OPEN_FILES = 256
CLAIM_LIST_RETRIES = 3
CLAIM_PROCESS_TERM_S = 0.5
CLAIM_PROCESS_KILL_S = 1.0
CLAIM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
LEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OBJECT_FORMAT_LENGTHS = {"sha1": 40, "sha256": 64}

_SCP_REPOSITORY_RE = re.compile(r"^(?:[^/@:]+@)?(?:\[[^\]]+\]|[^/:]+):.+$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "autoform",
    "GIT_AUTHOR_EMAIL": "autoform@localhost",
    "GIT_COMMITTER_NAME": "autoform",
    "GIT_COMMITTER_EMAIL": "autoform@localhost",
    "GIT_CONFIG_COUNT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
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
_CAS_REJECTIONS = (
    "stale info",
    "fetch first",
    "remote ref updated since checkout",
    "cannot lock ref",
)
_UNPINNED_REPOSITORY = object()
_UNPINNED_SCRATCH = object()
_SCRATCH_CLAIM_RESERVE_ENTRIES = CLAIM_KEY_MAX_BYTES + 16
_SCRATCH_CLAIM_RESERVE_BYTES = CLAIM_LEASE_MAX_BYTES + 4096
_SCRATCH_MARKER = "autoform-claim-scratch.json"
_SCRATCH_SCHEMA = "autoform-claim-scratch/v1"
_FCHDIR_EXEC = "import os,sys; os.fchdir(int(sys.argv[1])); os.execvp(sys.argv[2], sys.argv[2:])"
_F = TypeVar("_F", bound=Callable[..., Any])


class ClaimTransportError(RuntimeError):
    """A claim board operation could not be completed or verified."""


class MalformedLeaseError(ClaimTransportError):
    """A claim ref exists, but its lease cannot be verified safely."""


class _ClaimChurnError(ClaimTransportError):
    """A fetched claim generation no longer matches its advertisement."""


class _LeaseExpiredDuringMutation(RuntimeError):
    """The observed lease lost authority after preflight but before commit."""


@dataclass(frozen=True, slots=True)
class ClaimFence:
    """One coherent, exact remote ownership receipt for an acquired claim."""

    key: str
    ref: str
    oid: str
    lease_id: str

    def __post_init__(self) -> None:
        key = _validate_key(self.key)
        if self.ref != CLAIM_REF_PREFIX + key:
            raise ValueError("claim fence ref does not match its key")
        if not isinstance(self.oid, str) or OBJECT_ID_RE.fullmatch(self.oid) is None:
            raise ValueError("claim fence OID must be a full Git object ID")
        if set(self.oid) == {"0"}:
            raise ValueError("claim fence OID must identify an object")
        if not isinstance(self.lease_id, str) or LEASE_ID_RE.fullmatch(self.lease_id) is None:
            raise ValueError("claim fence lease_id must be 64 lowercase hexadecimal characters")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _ClaimMutation:
    """One exact ref transition that can be rolled back by its creator."""

    key: str
    old_oid: str | None
    new_oid: str | None
    old_receipt_oid: str | None = None
    new_receipt_oid: str | None = None
    old_commit: str | None = None

    def __post_init__(self) -> None:
        _validate_key(self.key)
        for label, oid in (
            ("old claim", self.old_oid),
            ("new claim", self.new_oid),
            ("old receipt", self.old_receipt_oid),
            ("new receipt", self.new_receipt_oid),
        ):
            if oid is not None and (OBJECT_ID_RE.fullmatch(oid) is None or set(oid) == {"0"}):
                raise ValueError(f"{label} OID must identify a Git object")
        if (self.old_oid is None) != (self.old_commit is None):
            raise ValueError("rollback commit content must match the prior claim OID")
        if self.old_commit is not None:
            try:
                size = len(self.old_commit.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("rollback commit must be valid UTF-8") from exc
            if size > CLAIM_LEASE_MAX_BYTES:
                raise ValueError("rollback commit exceeds the lease message limit")


def _validate_key(key: str) -> str:
    if (
        not isinstance(key, str)
        or not CLAIM_KEY_RE.fullmatch(key)
        or ".." in key
        or len(key.encode("ascii")) > CLAIM_KEY_MAX_BYTES
    ):
        raise ValueError(f"invalid claim key {key!r}")
    parts = key.split("/")
    if any(
        part.startswith(".")
        or part.endswith(".")
        or part.endswith(".lock")
        or len(part.encode("ascii")) > CLAIM_KEY_COMPONENT_MAX_BYTES
        for part in parts
    ):
        raise ValueError(f"invalid claim key {key!r}")
    return key


def _bounded_text(value: object, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label} must be a{'' if allow_empty else ' nonempty'} string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} UTF-8 bytes")
    return value


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_ttl(ttl: int | float) -> int | float:
    if not _is_finite_number(ttl) or ttl <= 0:
        raise ValueError("claim TTL must be a finite positive number")
    if ttl > CLAIM_MAX_TTL_S:
        raise ValueError(f"claim TTL must not exceed {CLAIM_MAX_TTL_S} seconds")
    return ttl


def _validate_object_format(value: str) -> str:
    if not isinstance(value, str) or value not in _OBJECT_FORMAT_LENGTHS:
        choices = ", ".join(sorted(_OBJECT_FORMAT_LENGTHS))
        raise ValueError(f"Git object format must be one of: {choices}")
    return value


def _bounded_keys(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    keys: list[str] = []
    for value in values:
        if len(keys) >= CLAIM_MAX_REFS:
            raise ValueError(f"{label} contains more than {CLAIM_MAX_REFS} keys")
        keys.append(_validate_key(value))
    return tuple(keys)


def _canonical_scratch_config(object_format: str) -> bytes:
    object_format = _validate_object_format(object_format)
    version = 0 if object_format == "sha1" else 1
    extension = "" if object_format == "sha1" else "[extensions]\n\tobjectFormat = sha256\n"
    return (
        f"[core]\n\trepositoryformatversion = {version}\n\tbare = true\n\thooksPath = {os.devnull}\n{extension}"
    ).encode()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _resolve_local_path(value: str | os.PathLike[str], *, label: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path cannot be resolved safely") from exc


def _directory_identity(
    path: Path,
    *,
    label: str,
    allow_missing: bool,
) -> tuple[int, int] | None:
    try:
        binding = open_directory(path)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ClaimTransportError(f"{label} directory is no longer available") from None
    except OSError as exc:
        if allow_missing:
            try:
                path.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                pass
        raise ClaimTransportError(f"{label} directory cannot be inspected safely") from exc
    try:
        return binding.identity
    finally:
        binding.close()


def claim_repository_is_remote(repo_url: str | os.PathLike[str]) -> bool:
    """Return whether Git will treat this repository name as a remote transport."""
    value = os.fspath(repo_url)
    if _WINDOWS_DRIVE_RE.match(value):
        return False
    return "://" in value or bool(_SCP_REPOSITORY_RE.match(value))


def normalize_claim_repository(repo_url: str | os.PathLike[str]) -> str:
    """Return a stable transport identity, resolving local paths and file URLs."""
    raw_repo_url = os.fspath(repo_url)
    _bounded_text(
        raw_repo_url,
        label="claim repository",
        maximum=CLAIM_REPOSITORY_MAX_BYTES,
    )
    parsed = urlsplit(raw_repo_url)
    if parsed.scheme.lower() == "file":
        if parsed.query or parsed.fragment or parsed.netloc.lower() not in {"", "localhost"}:
            raise ValueError("file repository URL must identify an absolute local path")
        local_path = Path(url2pathname(parsed.path))
        if not local_path.is_absolute():
            raise ValueError("file repository URL must identify an absolute local path")
        return str(_resolve_local_path(local_path, label="claim repository"))
    if not claim_repository_is_remote(raw_repo_url):
        return str(_resolve_local_path(raw_repo_url, label="claim repository"))
    return raw_repo_url


def pin_claim_repository(
    repo_url: str | os.PathLike[str],
) -> tuple[str, tuple[int, int] | None]:
    """Resolve a claim repository and capture its local filesystem identity."""
    normalized = normalize_claim_repository(repo_url)
    local_path = None if claim_repository_is_remote(normalized) else Path(normalized)
    identity = (
        _directory_identity(
            local_path,
            label="local claim repository",
            allow_missing=True,
        )
        if local_path is not None
        else None
    )
    return normalized, identity


def pin_claim_scratch(
    scratch: str | os.PathLike[str],
) -> tuple[Path, tuple[int, int] | None]:
    """Resolve a scratch path and capture an existing directory's identity."""
    path = _resolve_local_path(scratch, label="claim scratch")
    return path, _directory_identity(
        path,
        label="claim scratch",
        allow_missing=True,
    )


def _open_or_create_claim_scratch(
    path: Path,
    *,
    repo_binding: RetainedDirectory | None,
) -> RetainedDirectory:
    """Create a missing scratch through retained parent descriptors."""

    missing: list[str] = []
    ancestor = path
    while True:
        try:
            ancestor_binding = open_directory(ancestor)
            break
        except OSError as exc:
            try:
                ancestor.lstat()
            except FileNotFoundError:
                if ancestor == ancestor.parent:
                    raise ClaimTransportError("claim scratch directory cannot be pinned safely") from exc
                missing.append(ancestor.name)
                ancestor = ancestor.parent
                continue
            except OSError:
                pass
            raise ClaimTransportError("claim scratch directory cannot be pinned safely") from exc

    created_descriptors: list[int] = []
    created_identities: list[tuple[int, int]] = []
    created_links: list[tuple[int, str, int, tuple[int, int]]] = []
    prefix_descriptors: list[int] = []
    try:
        if repo_binding is not None:
            repo_binding.verify()
            if repo_binding.identity in ancestor_binding.identities:
                raise ClaimTransportError("claim repository and scratch must be disjoint directories")
        if not missing:
            if repo_binding is not None and (
                repo_binding.identity in ancestor_binding.identities
                or ancestor_binding.identity in repo_binding.identities
            ):
                raise ClaimTransportError("claim repository and scratch must be disjoint directories")
            return ancestor_binding

        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        parent_descriptor = ancestor_binding.descriptor
        identities = [*ancestor_binding.identities]
        for name in reversed(missing):
            ancestor_binding.verify()
            if repo_binding is not None:
                repo_binding.verify()
                if repo_binding.identity in identities:
                    raise ClaimTransportError("claim repository and scratch must be disjoint directories")
            for link_parent, link_name, child_descriptor, identity in created_links:
                opened = os.fstat(child_descriptor)
                named = os.stat(
                    link_name,
                    dir_fd=link_parent,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or (opened.st_dev, opened.st_ino) != identity
                    or (named.st_dev, named.st_ino) != identity
                ):
                    raise OSError("claim scratch directory chain changed")
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            named_before = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(named_before.st_mode):
                raise OSError("claim scratch path component is not a directory")
            child_descriptor = os.open(name, directory_flags, dir_fd=parent_descriptor)
            opened = os.fstat(child_descriptor)
            named_after = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or identity != (named_before.st_dev, named_before.st_ino)
                or identity != (named_after.st_dev, named_after.st_ino)
            ):
                os.close(child_descriptor)
                raise OSError("claim scratch path component changed while opening")
            if repo_binding is not None and identity in repo_binding.identities:
                os.close(child_descriptor)
                raise ClaimTransportError("claim repository and scratch must be disjoint directories")
            created_descriptors.append(child_descriptor)
            created_identities.append(identity)
            created_links.append((parent_descriptor, name, child_descriptor, identity))
            identities.append(identity)
            parent_descriptor = child_descriptor

        for descriptor in ancestor_binding.descriptors:
            prefix_descriptors.append(os.dup(descriptor))
        descriptors = tuple([*prefix_descriptors, *created_descriptors])
        prefix_descriptors = []
        created_descriptors = []
        result = RetainedDirectory(
            path,
            descriptors,
            tuple([*ancestor_binding.identities, *created_identities]),
        )
        try:
            result.verify()
            if repo_binding is not None:
                repo_binding.verify()
                if repo_binding.identity in result.identities or result.identity in repo_binding.identities:
                    raise ClaimTransportError("claim repository and scratch must be disjoint directories")
            return result
        except BaseException:
            result.close()
            raise
    except ClaimTransportError:
        raise
    except OSError as exc:
        raise ClaimTransportError("claim scratch directory cannot be created safely") from exc
    finally:
        for descriptor in reversed(prefix_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        for descriptor in reversed(created_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if ancestor_binding.path != path:
            ancestor_binding.close()


def author_claim_key(node_id: str) -> str:
    """Return a readable, ref-safe, collision-resistant author claim key."""
    if not isinstance(node_id, str):
        raise TypeError("node_id must be a string")
    encoded = node_id.encode("utf-8")
    slug = re.sub(r"[^a-z0-9-]+", "-", node_id.lower()).strip("-")[:48] or "node"
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    return f"author/{slug}-{digest}"


def resource_claim_key(resource: str) -> str:
    """Return a ref-safe key in the namespace for non-article resources."""
    if not isinstance(resource, str):
        raise TypeError("resource must be a string")
    _bounded_text(resource, label="resource", maximum=CLAIM_KEY_MAX_BYTES)
    slug = re.sub(r"[^a-z0-9-]+", "-", resource.lower()).strip("-")[:48] or "resource"
    digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:16]
    return f"resource/{slug}-{digest}"


def _claim_git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key in _GIT_ENV_ALLOWLIST or key.startswith("LC_")
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(_GIT_ENV)
    return environment


def _stop_process_tree(process: subprocess.Popen[bytes], process_group: int | None) -> None:
    """Terminate one Git process and every descendant without waiting indefinitely."""

    if process_group is not None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=CLAIM_PROCESS_TERM_S)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (AttributeError, ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=CLAIM_PROCESS_KILL_S)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return
    try:  # pragma: no cover - descriptor-bound claims currently require POSIX
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        try:
            parent.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        _, alive = psutil.wait_procs([parent, *children], timeout=CLAIM_PROCESS_TERM_S)
        for member in alive:
            try:
                member.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        psutil.wait_procs(alive, timeout=CLAIM_PROCESS_KILL_S)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=CLAIM_PROCESS_KILL_S)
        except Exception:
            pass


def _spawn_process(command: list[str], **options: Any) -> subprocess.Popen[bytes]:
    """Start one bounded subprocess through a narrow, testable seam."""

    return subprocess.Popen(command, **options)


def _run_bounded_process(
    command: list[str],
    *,
    deadline: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    input_max_bytes: int = CLAIM_LEASE_MAX_BYTES,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one subprocess with an absolute deadline and a shared output cap."""

    input_bytes = None if input_text is None else input_text.encode("utf-8")
    if input_bytes is not None and len(input_bytes) > input_max_bytes:
        raise ClaimTransportError("Git subprocess input exceeds its bounded limit")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ClaimTransportError(f"claim operation exceeded {CLAIM_OPERATION_TIMEOUT_S:g} seconds")
    popen_options: dict[str, Any] = {}
    process_group: int | None = None
    if os.name == "posix":
        popen_options["start_new_session"] = True
        popen_options["pass_fds"] = pass_fds
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # pragma: no cover - Windows
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = _spawn_process(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_options,
        )
    except OSError as exc:
        raise ClaimTransportError(f"git claim-board operation failed: {exc}") from exc
    if os.name == "posix":
        process_group = process.pid

    stdout = bytearray()
    stderr = bytearray()
    output_lock = threading.Lock()
    output_too_large = threading.Event()
    input_error: list[OSError] = []
    stop_lock = threading.Lock()
    stopped = False

    def stop() -> None:
        nonlocal stopped
        with stop_lock:
            if stopped:
                return
            stopped = True
            _stop_process_tree(process, process_group)

    def drain(stream: Any, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 << 10)
                if not chunk:
                    return
                with output_lock:
                    used = len(stdout) + len(stderr)
                    keep = max(0, CLAIM_GIT_OUTPUT_MAX_BYTES + 1 - used)
                    destination.extend(chunk[:keep])
                    if used + len(chunk) > CLAIM_GIT_OUTPUT_MAX_BYTES:
                        output_too_large.set()
                        stop()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    assert process.stdout is not None and process.stderr is not None
    readers = (
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    )
    writer: threading.Thread | None = None
    if input_bytes is not None:
        assert process.stdin is not None

        def feed() -> None:
            remaining_input = memoryview(input_bytes)
            try:
                while remaining_input:
                    written = os.write(process.stdin.fileno(), remaining_input)
                    if written <= 0:
                        raise OSError("short write to Git subprocess")
                    remaining_input = remaining_input[written:]
            except BrokenPipeError:
                pass
            except OSError as exc:
                input_error.append(exc)
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=feed, daemon=True)
    for reader in readers:
        reader.start()
    if writer is not None:
        writer.start()
    try:
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            stop()
            raise ClaimTransportError(f"claim operation exceeded {CLAIM_OPERATION_TIMEOUT_S:g} seconds") from exc
        for reader in readers:
            reader.join(timeout=max(0.001, deadline - time.monotonic()))
        if writer is not None:
            writer.join(timeout=max(0.001, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers) or (writer is not None and writer.is_alive()):
            stop()
            raise ClaimTransportError("Git subprocess pipes did not close before the deadline")
        if input_error and process.returncode == 0:
            raise ClaimTransportError(f"could not write Git subprocess input: {input_error[0]}")
        if output_too_large.is_set():
            raise ClaimTransportError(f"Git subprocess output exceeds {CLAIM_GIT_OUTPUT_MAX_BYTES} bytes")
    except BaseException:
        stop()
        raise
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout.decode("utf-8", errors="surrogateescape"),
        stderr.decode("utf-8", errors="surrogateescape"),
    )


def _parse_ls_remote_output(
    output: str,
    *,
    allow_head: bool = False,
) -> list[tuple[str, str]]:
    if not output:
        return []
    entries: list[tuple[str, str]] = []
    lines = output.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        if len(entries) >= CLAIM_MAX_REFS:
            raise ClaimTransportError(f"claim board returned more than {CLAIM_MAX_REFS} refs")
        oid, separator, ref = line.partition("\t")
        if (
            not separator
            or not OBJECT_ID_RE.fullmatch(oid)
            or (not ref.startswith("refs/") and not (allow_head and ref == "HEAD"))
            or any(character == " " or ord(character) < 32 or ord(character) == 127 for character in ref)
        ):
            raise ClaimTransportError("claim board returned malformed ls-remote output")
        entries.append((oid, ref))
    return entries


def _claim_operation(method: _F) -> _F:
    """Run a public board method under one local lock and deadline."""

    @wraps(method)
    def wrapped(self: ClaimBoard, *args: Any, **kwargs: Any) -> Any:
        with self._operation():
            return method(self, *args, **kwargs)

    return cast(_F, wrapped)


class ClaimBoard:
    """Lease operations against a Git repository via a local bare object store."""

    def __init__(
        self,
        repo_url: str | os.PathLike[str],
        worker_id: str,
        scratch: str | os.PathLike[str],
        *,
        session_id: str | None = None,
        expected_object_format: str | None = None,
        expected_repo_identity: object = _UNPINNED_REPOSITORY,
        expected_scratch_identity: object = _UNPINNED_SCRATCH,
    ):
        self._closed = False
        self._scratch_binding: RetainedDirectory | None = None
        self._repo_binding: RetainedDirectory | None = None
        self.worker_id = _bounded_text(
            worker_id,
            label="worker_id",
            maximum=CLAIM_WORKER_ID_MAX_BYTES,
        )
        validated_session_id = (
            _bounded_text(
                session_id,
                label="session_id",
                maximum=CLAIM_SESSION_ID_MAX_BYTES,
            )
            if session_id is not None
            else None
        )
        validated_object_format = (
            _validate_object_format(expected_object_format) if expected_object_format is not None else None
        )
        self.repo_url, current_repo_identity = pin_claim_repository(repo_url)
        self._repo_path = None if claim_repository_is_remote(self.repo_url) else Path(self.repo_url)
        if expected_repo_identity is not _UNPINNED_REPOSITORY and current_repo_identity != expected_repo_identity:
            raise ClaimTransportError("local claim repository was replaced")
        self._repo_identity = current_repo_identity
        self.scratch, current_scratch_identity = pin_claim_scratch(scratch)
        if self._repo_path is not None and (
            self._repo_path == self.scratch
            or self._repo_path in self.scratch.parents
            or self.scratch in self._repo_path.parents
        ):
            raise ClaimTransportError("claim repository and scratch must be disjoint directories")
        if (
            expected_scratch_identity is not _UNPINNED_SCRATCH
            and expected_scratch_identity is not None
            and current_scratch_identity != expected_scratch_identity
        ):
            raise ClaimTransportError("claim scratch directory was replaced")
        try:
            self._repo_binding = (
                self._bind_directory(
                    self._repo_path,
                    expected=self._repo_identity,
                    label="local claim repository",
                )
                if self._repo_path is not None and self._repo_identity is not None
                else None
            )
            if current_scratch_identity is None:
                self._scratch_binding = _open_or_create_claim_scratch(
                    self.scratch,
                    repo_binding=self._repo_binding,
                )
            else:
                self._scratch_binding = self._bind_directory(
                    self.scratch,
                    expected=current_scratch_identity,
                    label="claim scratch directory",
                )
            if self._repo_binding is not None and (
                self._repo_binding.identity in self._scratch_binding.identities
                or self._scratch_binding.identity in self._repo_binding.identities
            ):
                raise ClaimTransportError("claim repository and scratch must be disjoint directories")
        except BaseException:
            if self._scratch_binding is not None:
                self._scratch_binding.close()
            if self._repo_binding is not None:
                self._repo_binding.close()
            self._closed = True
            raise
        assert self._scratch_binding is not None
        self._scratch_identity = self._scratch_binding.identity
        self._scratch_fd = self._scratch_binding.descriptor
        self._repo_fd = self._repo_binding.descriptor if self._repo_binding is not None else None
        self._transport_helper = Path(__file__).with_name("_git_fd_transport.py").resolve()
        self._scratch_ready = False
        self._scratch_needs_marker = False
        self._expected_object_format = validated_object_format
        self._object_format: str | None = None
        if validated_session_id is None:
            scratch_digest = hashlib.sha256(os.fsencode(self.scratch)).hexdigest()
            validated_session_id = f"scratch:{scratch_digest}"
        self.session_id = _bounded_text(
            validated_session_id,
            label="session_id",
            maximum=CLAIM_SESSION_ID_MAX_BYTES,
        )
        self._session_key = hashlib.sha256(f"{self.repo_url}\0{self.session_id}".encode("utf-8")).hexdigest()
        self._thread_lock = threading.RLock()
        self._operation_state = threading.local()

    @staticmethod
    def _bind_directory(
        path: Path,
        *,
        expected: tuple[int, int] | None,
        label: str,
    ) -> RetainedDirectory:
        try:
            binding = open_directory(path)
        except OSError as exc:
            raise ClaimTransportError(f"{label} cannot be pinned safely") from exc
        if expected is not None and binding.identity != expected:
            binding.close()
            raise ClaimTransportError(f"{label} was replaced")
        return binding

    def close(self) -> None:
        """Release retained filesystem descriptors owned by this board."""

        if self._closed:
            return
        self._closed = True
        if self._scratch_binding is not None:
            self._scratch_binding.close()
        if self._repo_binding is not None:
            self._repo_binding.close()

    def __enter__(self) -> ClaimBoard:
        if self._closed:
            raise ClaimTransportError("claim board is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def _operation(self) -> Iterator[None]:
        """Serialize one bounded operation across threads and CLI processes."""

        with self._thread_lock:
            depth = getattr(self._operation_state, "depth", 0)
            if depth:
                self._operation_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._operation_state.depth -= 1
                return
            if self._closed:
                raise ClaimTransportError("claim board is closed")
            self._operation_state.depth = 1
            self._operation_state.deadline = time.monotonic() + CLAIM_OPERATION_TIMEOUT_S
            self._operation_state.commit_cache = {}
            self._operation_state.malformed_commit_cache = {}
            self._operation_state.promotions_remaining = CLAIM_PROMOTION_MAX_COMMITS
            self._operation_state.empty_tree_oid = None
            lock_descriptor: int | None = None
            try:
                lock_descriptor = self._acquire_scratch_lock()
                self._verify_scratch_owner()
                self._enforce_scratch_filesystem_budget()
                yield
            finally:
                if lock_descriptor is not None:
                    self._release_scratch_lock(lock_descriptor)
                self._operation_state.depth = 0
                self._operation_state.deadline = None
                self._operation_state.commit_cache = None
                self._operation_state.malformed_commit_cache = None
                self._operation_state.promotions_remaining = 0
                self._operation_state.empty_tree_oid = None

    def _remaining_seconds(self) -> float:
        deadline = self._operation_deadline()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClaimTransportError(f"claim operation exceeded {CLAIM_OPERATION_TIMEOUT_S:g} seconds")
        return remaining

    def _operation_deadline(self) -> float:
        deadline = getattr(self._operation_state, "deadline", None)
        if deadline is None:
            return time.monotonic() + CLAIM_OPERATION_TIMEOUT_S
        return float(deadline)

    def _operation_time(self) -> float:
        now = time.time()
        if not _is_finite_number(now):
            raise ValueError("claim operation clock must be finite")
        return now

    def _acquire_scratch_lock(self) -> int:
        if fcntl is None:
            raise ClaimTransportError("claim scratch locking is unavailable on this platform")
        descriptor: int | None = None
        try:
            descriptor = os.dup(self._scratch_fd)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or (
                    info.st_dev,
                    info.st_ino,
                )
                != self._scratch_identity
            ):
                raise OSError("claim scratch lock is not bound to the scratch directory")
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._verify_scratch_identity()
                    return descriptor
                except BlockingIOError:
                    time.sleep(min(0.02, self._remaining_seconds()))
        except BaseException as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if isinstance(exc, ClaimTransportError):
                raise
            raise ClaimTransportError(f"claim scratch lock cannot be acquired safely: {exc}") from exc

    @staticmethod
    def _release_scratch_lock(descriptor: int) -> None:
        try:
            assert fcntl is not None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _verify_scratch_owner(self) -> None:
        self._repair_scratch_marker_install()
        encoded = self._scratch_marker_bytes()
        try:
            data = self._read_scratch_regular_file(
                _SCRATCH_MARKER,
                maximum=len(encoded),
                label="claim scratch marker",
            )
        except FileNotFoundError:
            try:
                names: set[str] = set()
                with os.scandir(self._scratch_fd) as entries:
                    for entry in entries:
                        names.add(entry.name)
                        if len(names) > 16:
                            raise ClaimTransportError("claim scratch is not an Autoform claim scratch")
            except OSError as exc:
                raise ClaimTransportError("claim scratch cannot be inspected safely") from exc
            legacy_bare = {
                "HEAD",
                "branches",
                "config",
                "description",
                "hooks",
                "info",
                "objects",
                "packed-refs",
                "refs",
            }
            unexpected = names
            if unexpected and not ({"HEAD", "objects", "refs"} <= unexpected <= legacy_bare):
                raise ClaimTransportError("claim scratch is not an Autoform claim scratch")
            if unexpected:
                self._scratch_needs_marker = True
                return
            self._write_scratch_marker(encoded)
            return
        if data != encoded:
            raise ClaimTransportError("claim scratch ownership marker does not match this repository")
        self._scratch_needs_marker = False

    def _repair_scratch_marker_install(self) -> None:
        temporary_pattern = re.compile(rf"\.{re.escape(_SCRATCH_MARKER)}-[0-9]+-[0-9a-f]{{16}}")
        try:
            try:
                marker = os.stat(
                    _SCRATCH_MARKER,
                    dir_fd=self._scratch_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 2:
                return
            names = os.listdir(self._scratch_fd)
            if len(names) > CLAIM_SCRATCH_MAX_ENTRIES:
                raise ClaimTransportError("claim scratch exceeds its bounded filesystem entry budget")
            candidates: list[str] = []
            for name in names:
                if temporary_pattern.fullmatch(name) is None:
                    continue
                info = os.stat(name, dir_fd=self._scratch_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == (marker.st_dev, marker.st_ino):
                    candidates.append(name)
            if len(candidates) != 1:
                return
            os.unlink(candidates[0], dir_fd=self._scratch_fd)
            os.fsync(self._scratch_fd)
        except ClaimTransportError:
            raise
        except OSError as exc:
            raise ClaimTransportError("claim scratch marker installation cannot be recovered safely") from exc

    def _read_scratch_regular_file(
        self,
        name: str,
        *,
        maximum: int,
        label: str,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            named_before = os.stat(name, dir_fd=self._scratch_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ClaimTransportError(f"{label} cannot be inspected safely") from exc
        try:
            descriptor = os.open(name, flags, dir_fd=self._scratch_fd)
            opened_before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_nlink != 1
                or opened_before.st_size > maximum
            ):
                raise OSError(f"{label} is not a bounded private regular file")
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(descriptor, min(64 << 10, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            opened_after = os.fstat(descriptor)
            named_after = os.stat(name, dir_fd=self._scratch_fd, follow_symlinks=False)
        except OSError as exc:
            raise ClaimTransportError(f"{label} changed while it was read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        opened_identity = identity(opened_before)
        if (
            len(data) > maximum
            or opened_identity != identity(named_before)
            or opened_identity != identity(opened_after)
            or opened_identity != identity(named_after)
            or len(data) != opened_before.st_size
        ):
            raise ClaimTransportError(f"{label} changed while it was read")
        return bytes(data)

    def _scratch_marker_bytes(self) -> bytes:
        marker = {
            "repository_hash": hashlib.sha256(self.repo_url.encode("utf-8")).hexdigest(),
            "schema": _SCRATCH_SCHEMA,
        }
        return json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _write_scratch_marker(self, encoded: bytes) -> None:
        temporary = f".{_SCRATCH_MARKER}-{os.getpid()}-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=self._scratch_fd)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while installing claim scratch marker")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.link(
                temporary,
                _SCRATCH_MARKER,
                src_dir_fd=self._scratch_fd,
                dst_dir_fd=self._scratch_fd,
                follow_symlinks=False,
            )
            os.fsync(self._scratch_fd)
        except FileExistsError:
            self._verify_scratch_owner()
        except OSError as exc:
            raise ClaimTransportError("claim scratch marker cannot be installed safely") from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.unlink(temporary, dir_fd=self._scratch_fd)
            except OSError:
                pass

    def _scratch_usage(self) -> tuple[int, int]:
        proc = self._git(["count-objects", "-v"])
        fields: dict[str, int] = {}
        for line in proc.stdout.splitlines():
            name, separator, raw_value = line.partition(": ")
            if name == "alternate" and separator:
                raise ClaimTransportError("claim scratch must not use external object alternates")
            if separator and raw_value.isascii() and raw_value.isdecimal():
                fields[name] = int(raw_value)
        required = {
            "count",
            "size",
            "in-pack",
            "size-pack",
            "garbage",
            "size-garbage",
        }
        if not required <= fields.keys():
            raise ClaimTransportError("claim scratch returned malformed size accounting")
        refs = self._git(["for-each-ref", "--format=%(refname)"]).stdout.splitlines()
        if len(refs) > CLAIM_SCRATCH_MAX_ENTRIES:
            raise ClaimTransportError("claim scratch exceeds its bounded ref budget")
        if any(not ref.startswith(CLAIM_RECEIPT_REF_PREFIX) for ref in refs):
            raise ClaimTransportError("claim scratch contains refs outside its owned namespaces")
        entries = fields["count"] + fields["in-pack"] + fields["garbage"] + len(refs)
        size = (fields["size"] + fields["size-pack"] + fields["size-garbage"]) * 1024
        return entries, size

    def _enforce_scratch_filesystem_budget(self) -> tuple[int, int]:
        self._verify_scratch_identity()
        entries = 0
        total = 0
        pending = [self.scratch]
        while pending:
            directory = pending.pop()
            try:
                iterator = os.scandir(directory)
            except OSError as exc:
                raise ClaimTransportError("claim scratch cannot be inspected safely") from exc
            with iterator:
                for entry in iterator:
                    entries += 1
                    if entries > CLAIM_SCRATCH_MAX_ENTRIES:
                        raise ClaimTransportError("claim scratch exceeds its bounded filesystem entry budget")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise ClaimTransportError("claim scratch cannot be inspected safely") from exc
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        total += info.st_size
                        if total > CLAIM_SCRATCH_MAX_BYTES:
                            raise ClaimTransportError("claim scratch exceeds its bounded filesystem byte budget")
                    else:
                        raise ClaimTransportError("claim scratch contains an unsupported filesystem entry")
        self._verify_scratch_identity()
        return entries, total

    def _enforce_scratch_budget(self) -> None:
        self._enforce_scratch_filesystem_budget()
        entries, size = self._scratch_usage()
        if entries <= CLAIM_SCRATCH_MAX_ENTRIES and size <= CLAIM_SCRATCH_MAX_BYTES:
            return
        self._git(["gc", "--prune=now", "--quiet"])
        entries, size = self._scratch_usage()
        if entries > CLAIM_SCRATCH_MAX_ENTRIES or size > CLAIM_SCRATCH_MAX_BYTES:
            raise ClaimTransportError("claim scratch exceeds its bounded object budget after garbage collection")
        self._enforce_scratch_filesystem_budget()

    def _require_scratch_capacity(self, *, entries: int, size: int) -> None:
        if not self._scratch_has_capacity(entries=entries, size=size):
            raise ClaimTransportError("claim scratch lacks bounded capacity for a new claim")

    def _scratch_has_capacity(self, *, entries: int, size: int) -> bool:
        self._enforce_scratch_budget()
        current_entries, current_size = self._enforce_scratch_filesystem_budget()
        return not (
            current_entries + entries > CLAIM_SCRATCH_MAX_ENTRIES or current_size + size > CLAIM_SCRATCH_MAX_BYTES
        )

    def _prune_transient_refs(self) -> None:
        proc = self._git(
            [
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                CLAIM_REF_PREFIX,
                CLAIM_QUARANTINE_REF_PREFIX,
            ]
        )
        lines = proc.stdout.splitlines()
        if len(lines) > CLAIM_MAX_REFS:
            raise ClaimTransportError("claim scratch contains too many transient refs")
        for line in lines:
            oid, separator, ref = line.partition(" ")
            if (
                not separator
                or not OBJECT_ID_RE.fullmatch(oid)
                or not ref.startswith((CLAIM_REF_PREFIX, CLAIM_QUARANTINE_REF_PREFIX))
            ):
                raise ClaimTransportError("claim scratch contains malformed transient refs")
            deleted = self._git(["update-ref", "-d", ref, oid], check=False)
            if deleted.returncode != 0:
                raise ClaimTransportError("claim scratch transient ref changed during pruning")

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        remote: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        display_args = args
        if remote and self._repo_fd is not None:
            args = self._local_transport_args(args)
        self._verify_scratch_identity()
        if remote:
            self._verify_repo_identity()
        environment = {**_claim_git_environment(), "GIT_DIR": "."}
        command = ["git", *args]
        cwd: Path | None = self.scratch
        descriptors = tuple(
            descriptor for descriptor in (self._scratch_fd, self._repo_fd if remote else None) if descriptor is not None
        )
        if self._scratch_fd is not None:
            command = [
                sys.executable,
                "-c",
                _FCHDIR_EXEC,
                str(self._scratch_fd),
                "git",
                *args,
            ]
            cwd = None
        proc = _run_bounded_process(
            command,
            cwd=cwd,
            env=environment,
            input_text=input_text,
            pass_fds=descriptors,
            deadline=self._operation_deadline(),
        )
        self._verify_scratch_identity()
        if remote:
            self._verify_repo_identity()
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(f"git {' '.join(display_args[:2])} failed against claim board: {detail}")
        return proc

    def _local_transport_args(self, args: list[str]) -> list[str]:
        if self._repo_fd is None or not args:
            return args
        operation = args[0]
        if operation in {"ls-remote", "fetch"}:
            mode = "upload"
            option = "--upload-pack"
        elif operation == "push":
            mode = "receive"
            option = "--receive-pack"
        else:
            raise ClaimTransportError(f"unsupported local claim transport operation {operation!r}")
        helper = shlex.join(
            (
                sys.executable,
                os.fspath(self._transport_helper),
                mode,
                str(self._repo_fd),
            )
        )
        rewritten = ["." if arg == self.repo_url else arg for arg in args]
        if rewritten == args:
            raise ClaimTransportError("local claim transport target was not explicit")
        rewritten.insert(1, f"{option}={helper}")
        return rewritten

    def _verify_repo_identity(self) -> None:
        if self._repo_path is None:
            return
        if self._repo_binding is None:
            current = _directory_identity(
                self._repo_path,
                label="local claim repository",
                allow_missing=True,
            )
            if current == self._repo_identity:
                return
            raise ClaimTransportError("local claim repository was replaced")
        try:
            self._repo_binding.verify()
        except OSError as exc:
            raise ClaimTransportError("local claim repository was replaced") from exc

    def _remote_git(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._verify_repo_identity()
        proc = self._git(args, check=check, remote=True)
        self._verify_repo_identity()
        return proc

    def _verify_scratch_identity(self) -> None:
        if self._closed:
            raise ClaimTransportError("claim board is closed")
        assert self._scratch_binding is not None
        try:
            self._scratch_binding.verify()
        except OSError as exc:
            raise ClaimTransportError("claim scratch directory was replaced") from exc

    def _repository_object_format(self) -> str | None:
        self._verify_repo_identity()
        if self._repo_path is not None:
            command = ["git", "rev-parse", "--show-object-format"]
            run_options: dict[str, Any] = {"cwd": self._repo_path}
            if self._repo_fd is not None:
                command = [
                    sys.executable,
                    "-c",
                    _FCHDIR_EXEC,
                    str(self._repo_fd),
                    "git",
                    "rev-parse",
                    "--show-object-format",
                ]
                run_options = {"pass_fds": (self._repo_fd,)}
            proc = _run_bounded_process(
                command,
                cwd=run_options.get("cwd"),
                env=_claim_git_environment(),
                pass_fds=run_options.get("pass_fds", ()),
                deadline=self._operation_deadline(),
            )
            self._verify_repo_identity()
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:300]
                raise ClaimTransportError(f"cannot inspect claim repository object format: {detail}")
            detected = proc.stdout.strip()
        else:
            entries: list[tuple[str, str]] = []
            commands = (
                (["git", "ls-remote", self.repo_url, "HEAD"], True),
                (["git", "ls-remote", "--refs", self.repo_url, CLAIM_REF_PREFIX + "*"], False),
            )
            for command, allow_head in commands:
                proc = _run_bounded_process(
                    command,
                    env=_claim_git_environment(),
                    deadline=self._operation_deadline(),
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout).strip()[:300]
                    raise ClaimTransportError(f"cannot inspect claim repository object format: {detail}")
                entries = _parse_ls_remote_output(proc.stdout, allow_head=allow_head)
                if entries:
                    break
            widths = {len(oid) for oid, _ref in entries}
            if not widths:
                return self._expected_object_format
            if len(widths) != 1:
                raise ClaimTransportError("claim repository returned mixed object formats")
            width = widths.pop()
            detected = next(name for name, length in _OBJECT_FORMAT_LENGTHS.items() if length == width)
        try:
            detected = _validate_object_format(detected)
        except ValueError as exc:
            raise ClaimTransportError("claim repository has an unsupported object format") from exc
        if self._expected_object_format is not None and detected != self._expected_object_format:
            raise ClaimTransportError(
                f"claim repository object format {detected!r} does not match expected {self._expected_object_format!r}"
            )
        return detected

    def _scratch_object_format(self) -> str:
        proc = self._git(["rev-parse", "--show-object-format"], check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(f"cannot inspect claim scratch object format: {detail}")
        try:
            return _validate_object_format(proc.stdout.strip())
        except ValueError as exc:
            raise ClaimTransportError("claim scratch has an unsupported object format") from exc

    def _install_canonical_scratch_config(self, object_format: str) -> None:
        canonical_config = _canonical_scratch_config(object_format)
        try:
            content = self._read_scratch_regular_file(
                "config",
                maximum=CLAIM_SCRATCH_CONFIG_MAX_BYTES,
                label="claim scratch Git configuration",
            )
        except FileNotFoundError as exc:
            raise ClaimTransportError("claim scratch Git configuration is missing") from exc
        if content == canonical_config:
            return
        temporary_name = f".autoform-config-{os.getpid()}-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            if self._scratch_fd is not None:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=self._scratch_fd,
                )
            else:
                descriptor = os.open(self.scratch / temporary_name, flags, 0o600)
            remaining = memoryview(canonical_config)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while installing claim scratch config")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if self._scratch_fd is not None:
                os.replace(
                    temporary_name,
                    "config",
                    src_dir_fd=self._scratch_fd,
                    dst_dir_fd=self._scratch_fd,
                )
                os.fsync(self._scratch_fd)
            else:
                os.replace(self.scratch / temporary_name, self.scratch / "config")
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if self._scratch_fd is not None:
                    os.unlink(temporary_name, dir_fd=self._scratch_fd)
                else:
                    (self.scratch / temporary_name).unlink()
            except OSError:
                pass
            raise ClaimTransportError("claim scratch Git configuration cannot be installed safely") from exc

    def _existing_scratch_object_format(self) -> str:
        try:
            content = self._read_scratch_regular_file(
                "config",
                maximum=CLAIM_SCRATCH_CONFIG_MAX_BYTES,
                label="claim scratch Git configuration",
            )
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(content.decode("utf-8"))
            if not parser.getboolean("core", "bare"):
                raise ValueError("scratch is not bare")
            version = parser.getint("core", "repositoryformatversion")
            object_format = parser.get("extensions", "objectformat", fallback="sha1").lower()
            object_format = _validate_object_format(object_format)
            expected_version = 0 if object_format == "sha1" else 1
            if version != expected_version:
                raise ValueError("scratch repository format version is inconsistent")
            return object_format
        except (configparser.Error, FileNotFoundError, UnicodeDecodeError, ValueError) as exc:
            raise ClaimTransportError("claim scratch has an invalid Git configuration") from exc

    def _ensure_scratch(self) -> None:
        self._verify_scratch_identity()
        try:
            head = self._read_scratch_regular_file(
                "HEAD",
                maximum=4096,
                label="claim scratch HEAD",
            )
        except FileNotFoundError:
            head = None
        if self._scratch_ready:
            if head is None:
                raise ClaimTransportError("claim scratch is no longer a bare Git repository")
            self._install_canonical_scratch_config(self._object_format or "")
            object_format = self._scratch_object_format()
            if self._object_format != object_format:
                raise ClaimTransportError("claim scratch object format changed")
            self._prune_transient_refs()
            self._enforce_scratch_budget()
            return
        if head is not None:
            object_format = self._existing_scratch_object_format()
            expected = self._repository_object_format()
            if expected is not None and object_format != expected:
                raise ClaimTransportError(
                    f"claim scratch object format {object_format!r} does not match repository "
                    f"object format {expected!r}"
                )
            if self._scratch_needs_marker:
                self._write_scratch_marker(self._scratch_marker_bytes())
                self._scratch_needs_marker = False
            self._install_canonical_scratch_config(object_format)
            proc = self._git(["rev-parse", "--is-bare-repository"], check=False)
            if proc.returncode != 0 or proc.stdout.strip() != "true":
                raise ClaimTransportError("claim scratch must be a bare Git repository")
            if self._scratch_object_format() != object_format:
                raise ClaimTransportError("claim scratch object format changed")
            self._object_format = object_format
            self._scratch_ready = True
            self._prune_transient_refs()
            self._enforce_scratch_budget()
            return
        object_format = self._repository_object_format()
        if object_format is None:
            raise ClaimTransportError(
                "cannot determine an empty remote claim repository's object format; pass expected_object_format"
            )
        self._git(
            [
                "init",
                "--bare",
                "--quiet",
                "--template=",
                f"--object-format={object_format}",
            ]
        )
        try:
            self._read_scratch_regular_file(
                "HEAD",
                maximum=4096,
                label="claim scratch HEAD",
            )
        except (FileNotFoundError, ClaimTransportError) as exc:
            raise ClaimTransportError("claim scratch initialization could not be verified") from exc
        actual_format = self._existing_scratch_object_format()
        if actual_format != object_format:
            raise ClaimTransportError("claim scratch initialized with the wrong object format")
        self._install_canonical_scratch_config(actual_format)
        if self._scratch_object_format() != actual_format:
            raise ClaimTransportError("claim scratch object format changed")
        self._object_format = actual_format
        self._scratch_ready = True
        self._prune_transient_refs()
        self._enforce_scratch_budget()

    @staticmethod
    def _ref(key: str) -> str:
        return CLAIM_REF_PREFIX + _validate_key(key)

    def _receipt_ref(self, key: str) -> str:
        return f"{CLAIM_RECEIPT_REF_PREFIX}{self._session_key}/{_validate_key(key)}"

    def _verify_object_id_format(self, oid: str) -> None:
        if self._object_format is None or len(oid) != _OBJECT_FORMAT_LENGTHS[self._object_format]:
            raise ClaimTransportError("claim repository object format changed")

    def _remote_ref_oid(self, ref: str) -> str | None:
        proc = self._remote_git(["ls-remote", self.repo_url, ref])
        entries = _parse_ls_remote_output(proc.stdout)
        if not entries:
            return None
        if len(entries) != 1 or entries[0][1] != ref:
            raise ClaimTransportError(f"claim board did not resolve exact requested ref {ref!r}")
        self._verify_object_id_format(entries[0][0])
        return entries[0][0]

    def _remote_oid(self, key: str) -> str | None:
        return self._remote_ref_oid(self._ref(key))

    def _advertised_claims(self) -> list[tuple[str, str]]:
        proc = self._remote_git(["ls-remote", self.repo_url, CLAIM_REF_PREFIX + "*"])
        entries = _parse_ls_remote_output(proc.stdout)
        seen_refs: set[str] = set()
        for oid, ref in entries:
            self._verify_object_id_format(oid)
            if not ref.startswith(CLAIM_REF_PREFIX) or ref in seen_refs:
                raise ClaimTransportError("claim board returned an unexpected or duplicate claim ref")
            seen_refs.add(ref)
            try:
                _validate_key(ref[len(CLAIM_REF_PREFIX) :])
            except ValueError as exc:
                raise ClaimTransportError(f"claim board returned invalid claim ref {ref!r}") from exc
        return entries

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

    def _zero_oid(self) -> str:
        if self._object_format is None:
            raise ClaimTransportError("claim scratch object format is not initialized")
        return "0" * _OBJECT_FORMAT_LENGTHS[self._object_format]

    def _record_receipt(self, key: str, oid: str, *, expected: str | None) -> None:
        self._verify_object_id_format(oid)
        args = ["update-ref", self._receipt_ref(key), oid, expected or self._zero_oid()]
        proc = self._git(args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "remote claim changed but its exact local ownership receipt could not be recorded"
                + (f": {detail}" if detail else "")
            )

    def _clear_receipt(self, key: str, *, expected: str | None) -> None:
        zero_oid = self._zero_oid()
        args = ["update-ref", self._receipt_ref(key), zero_oid, expected or zero_oid]
        proc = self._git(args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "remote claim changed but its local ownership receipt could not be cleared"
                + (f": {detail}" if detail else "")
            )

    def _restore_receipt_generation(
        self,
        key: str,
        *,
        changed_oid: str,
        prior_oid: str | None,
    ) -> None:
        current = self._receipt_oid(key)
        if current == prior_oid:
            return
        if current != changed_oid:
            raise ClaimTransportError("local claim receipt changed before its prior generation could be restored")
        zero_oid = self._zero_oid()
        proc = self._git(
            [
                "update-ref",
                self._receipt_ref(key),
                prior_oid or zero_oid,
                changed_oid,
            ],
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "local claim receipt could not be restored to its prior generation" + (f": {detail}" if detail else "")
            )

    def _read_lease(self, key: str, oid: str) -> dict[str, Any]:
        ref = self._ref(key)
        cache = self._commit_cache()
        malformed_cache = self._malformed_commit_cache()
        malformed = malformed_cache.get((oid, key))
        if malformed is not None:
            raise MalformedLeaseError(malformed)
        commit = cache.get(oid)
        if commit is None:
            proc = self._git(["cat-file", "commit", oid], check=False)
            if proc.returncode == 0:
                commit = proc.stdout
            else:
                self._fetch_claims_quarantined([(oid, ref)])
                malformed = malformed_cache.get((oid, key))
                if malformed is not None:
                    raise MalformedLeaseError(malformed)
                commit = cache.get(oid)
        if commit is None:
            raise MalformedLeaseError(f"claim {key!r} does not point to a readable commit")
        message = self._validated_claim_message(key, commit)
        try:
            lease = json.loads(
                message,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (RecursionError, ValueError, UnicodeDecodeError) as exc:
            raise MalformedLeaseError(f"claim {key!r} has invalid lease JSON") from exc
        if not isinstance(lease, dict) or not self._lease_is_valid(lease, key):
            raise MalformedLeaseError(f"claim {key!r} has an invalid lease schema")
        return lease

    def _commit_cache(self) -> dict[str, str]:
        cache = getattr(self._operation_state, "commit_cache", None)
        if cache is None:
            cache = {}
            self._operation_state.commit_cache = cache
        return cast(dict[str, str], cache)

    def _malformed_commit_cache(self) -> dict[tuple[str, str], str]:
        cache = getattr(self._operation_state, "malformed_commit_cache", None)
        if cache is None:
            cache = {}
            self._operation_state.malformed_commit_cache = cache
        return cast(dict[tuple[str, str], str], cache)

    def _canonical_empty_tree_oid(self) -> str:
        cached = getattr(self._operation_state, "empty_tree_oid", None)
        if cached is not None and self._git(["cat-file", "-e", f"{cached}^{{tree}}"], check=False).returncode == 0:
            return cast(str, cached)
        self._require_scratch_capacity(entries=2, size=1024)
        oid = self._git(["mktree"], input_text="").stdout.strip()
        self._verify_object_id_format(oid)
        self._operation_state.empty_tree_oid = oid
        return oid

    def _validated_claim_message(
        self,
        key: str,
        commit: str,
        *,
        empty_tree_oid: str | None = None,
    ) -> str:
        try:
            encoded = commit.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MalformedLeaseError(f"claim {key!r} commit is not valid UTF-8") from exc
        if len(encoded) > CLAIM_LEASE_MAX_BYTES:
            raise MalformedLeaseError(f"claim {key!r} exceeds the lease message limit")
        headers, separator, message = commit.partition("\n\n")
        if not separator:
            raise MalformedLeaseError(f"claim {key!r} has no lease message")
        header_lines = headers.splitlines()
        tree_headers = [line for line in header_lines if line.startswith("tree ")]
        if empty_tree_oid is None:
            empty_tree_oid = self._canonical_empty_tree_oid()
        if (
            len(tree_headers) != 1
            or tree_headers[0] != f"tree {empty_tree_oid}"
            or any(line.startswith("parent ") for line in header_lines)
        ):
            raise MalformedLeaseError(f"claim {key!r} must be a parentless commit over the canonical empty tree")
        return message

    def _quarantine_commits(
        self,
        binding: RetainedDirectory,
        entries: list[tuple[str, str]],
    ) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
        proc = self._quarantine_git(
            binding,
            ["cat-file", "--batch"],
            input_text="".join(f"{oid}\n" for oid, _ref in entries),
            check=False,
        )
        if proc.returncode != 0:
            raise ClaimTransportError("claim quarantine could not read fetched commits")
        payload = proc.stdout.encode("utf-8", errors="surrogateescape")
        offset = 0
        commits: dict[str, str] = {}
        malformed: dict[tuple[str, str], str] = {}
        for oid, ref in entries:
            key = ref[len(CLAIM_REF_PREFIX) :]
            header_end = payload.find(b"\n", offset)
            if header_end < 0:
                raise ClaimTransportError("claim quarantine returned truncated object data")
            header = payload[offset:header_end].split(b" ")
            if header == [oid.encode("ascii"), b"missing"]:
                malformed[(oid, key)] = f"claim {key!r} does not point to a readable commit"
                offset = header_end + 1
                continue
            if len(header) != 3 or header[0] != oid.encode("ascii"):
                raise ClaimTransportError("claim quarantine returned malformed object data")
            try:
                size = int(header[2])
            except ValueError as exc:
                raise ClaimTransportError("claim quarantine returned malformed object data") from exc
            if size < 0:
                raise ClaimTransportError("claim quarantine returned malformed object data")
            start = header_end + 1
            end = start + size
            if end >= len(payload) or payload[end : end + 1] != b"\n":
                raise ClaimTransportError("claim quarantine returned truncated object data")
            if header[1] != b"commit":
                malformed[(oid, key)] = f"claim {key!r} does not point to a commit"
                offset = end + 1
                continue
            if size > CLAIM_LEASE_MAX_BYTES:
                malformed[(oid, key)] = f"claim {key!r} exceeds the lease message limit"
                offset = end + 1
                continue
            try:
                commit = payload[start:end].decode("utf-8")
            except UnicodeDecodeError:
                malformed[(oid, key)] = f"claim {key!r} commit is not valid UTF-8"
                offset = end + 1
                continue
            commits[oid] = commit
            offset = end + 1
        if offset != len(payload):
            raise ClaimTransportError("claim quarantine returned excess object data")
        return commits, malformed

    def _quarantine_git(
        self,
        binding: RetainedDirectory,
        args: list[str],
        *,
        remote: bool = False,
        check: bool = True,
        input_text: str | None = None,
        input_max_bytes: int = CLAIM_LEASE_MAX_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        display_args = args
        if remote and self._repo_fd is not None:
            args = self._local_transport_args(args)
        descriptors = tuple(
            descriptor
            for descriptor in (binding.descriptor, self._repo_fd if remote else None)
            if descriptor is not None
        )
        command = [
            sys.executable,
            os.fspath(self._transport_helper),
            "run-limited",
            str(binding.descriptor),
            str(CLAIM_QUARANTINE_MAX_FILE_BYTES),
            str(CLAIM_QUARANTINE_CPU_S),
            str(CLAIM_QUARANTINE_OPEN_FILES),
            "git",
            *args,
        ]
        environment = {
            **_claim_git_environment(),
            "GIT_DIR": ".",
            "GIT_NO_LAZY_FETCH": "1",
        }
        if remote:
            self._verify_repo_identity()
        proc = _run_bounded_process(
            command,
            deadline=self._operation_deadline(),
            env=environment,
            input_text=input_text,
            input_max_bytes=input_max_bytes,
            pass_fds=descriptors,
        )
        binding.verify()
        if remote:
            self._verify_repo_identity()
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(f"git quarantine {' '.join(display_args[:2])} failed: {detail}")
        return proc

    @staticmethod
    def _quarantine_usage(root: Path) -> tuple[int, int, int]:
        entries = 0
        total = 0
        promisor_files = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                iterator = os.scandir(directory)
            except OSError as exc:
                raise ClaimTransportError("claim quarantine cannot be inspected safely") from exc
            with iterator:
                for entry in iterator:
                    entries += 1
                    if entries > CLAIM_QUARANTINE_MAX_ENTRIES:
                        raise ClaimTransportError("claim quarantine exceeds its bounded file count")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise ClaimTransportError("claim quarantine cannot be inspected safely") from exc
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        if info.st_size > CLAIM_QUARANTINE_MAX_FILE_BYTES:
                            raise ClaimTransportError("claim quarantine contains an oversized file")
                        total += info.st_size
                        if total > CLAIM_QUARANTINE_MAX_BYTES:
                            raise ClaimTransportError("claim quarantine exceeds its bounded byte budget")
                        if entry.name.endswith(".promisor"):
                            promisor_files += 1
                    else:
                        raise ClaimTransportError("claim quarantine contains an unsupported filesystem entry")
        return entries, total, promisor_files

    def _fetch_claims_quarantined(self, entries: list[tuple[str, str]]) -> None:
        if not entries:
            return
        if len(entries) > CLAIM_FETCH_BATCH_SIZE:
            raise ClaimTransportError("claim quarantine batch exceeds its bounded size")
        with tempfile.TemporaryDirectory(prefix="autoform-claim-quarantine-") as temporary:
            root = Path(temporary).resolve()
            binding = open_directory(root)
            try:
                self._quarantine_git(
                    binding,
                    [
                        "init",
                        "--bare",
                        "--quiet",
                        "--template=",
                        f"--object-format={self._object_format}",
                    ],
                )
                namespace = f"refs/autoform-quarantine/{secrets.token_hex(16)}"
                destinations = [f"{namespace}/{index}" for index in range(len(entries))]
                fetch = self._quarantine_git(
                    binding,
                    [
                        "fetch",
                        "--quiet",
                        "--depth=1",
                        "--filter=tree:0",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "--stdin",
                        self.repo_url,
                    ],
                    remote=True,
                    check=False,
                    input_text="".join(
                        f"+{ref}:{destination}\n" for (_oid, ref), destination in zip(entries, destinations)
                    ),
                    input_max_bytes=CLAIM_FETCH_INPUT_MAX_BYTES,
                )
                filter_error = "filtering not recognized by server" in fetch.stderr.lower()
                if filter_error:
                    raise ClaimTransportError("claim repository does not support the required tree:0 fetch filter")
                if fetch.returncode != 0:
                    detail = (fetch.stderr or fetch.stdout).strip()
                    current = {ref: oid for oid, ref in self._advertised_claims()}
                    if any(current.get(ref) != oid for oid, ref in entries):
                        raise _ClaimChurnError("claim board changed during bounded fetch")
                    raise ClaimTransportError(f"bounded claim fetch failed: {detail[:300]}")
                _entry_count, _total, promisor_files = self._quarantine_usage(root)
                if promisor_files == 0:
                    raise ClaimTransportError("claim repository did not honor the required tree:0 fetch filter")
                tips = self._quarantine_git(
                    binding,
                    ["for-each-ref", "--format=%(refname) %(objectname)", namespace],
                )
                observed: dict[str, str] = {}
                for line in tips.stdout.splitlines():
                    ref, separator, oid = line.partition(" ")
                    if not separator or ref in observed or not OBJECT_ID_RE.fullmatch(oid):
                        raise ClaimTransportError("claim quarantine returned malformed refs")
                    observed[ref] = oid
                expected = dict(zip(destinations, (oid for oid, _ref in entries)))
                if observed != expected:
                    raise _ClaimChurnError("claim board changed during bounded fetch")

                commits: dict[str, str] = {}
                malformed: dict[tuple[str, str], str] = {}
                for offset in range(0, len(entries), CLAIM_OBJECT_READ_BATCH_SIZE):
                    fetched, rejected = self._quarantine_commits(
                        binding,
                        entries[offset : offset + CLAIM_OBJECT_READ_BATCH_SIZE],
                    )
                    commits.update(fetched)
                    malformed.update(rejected)
                malformed_cache = self._malformed_commit_cache()
                malformed_cache.update(malformed)
                empty_tree_oid = self._canonical_empty_tree_oid()
                promotable: dict[str, str] = {}
                for oid, ref in entries:
                    key = ref[len(CLAIM_REF_PREFIX) :]
                    commit = commits.get(oid)
                    if commit is None:
                        continue
                    try:
                        self._validated_claim_message(
                            key,
                            commit,
                            empty_tree_oid=empty_tree_oid,
                        )
                    except MalformedLeaseError as exc:
                        malformed_cache[(oid, key)] = str(exc)
                    else:
                        promotable[oid] = commit
                self._commit_cache().update(commits)
                remaining = int(getattr(self._operation_state, "promotions_remaining", 0))
                promotions = list(promotable.items())[:remaining]
                promotion_size = sum(len(commit.encode("utf-8")) + 1024 for _oid, commit in promotions)
                if promotions and self._scratch_has_capacity(
                    entries=3 * len(promotions),
                    size=promotion_size,
                ):
                    for oid, commit in promotions:
                        promoted = self._git(
                            ["hash-object", "-t", "commit", "-w", "--stdin"],
                            input_text=commit,
                        ).stdout.strip()
                        if promoted != oid:
                            raise ClaimTransportError("verified claim changed while it was promoted from quarantine")
                    self._operation_state.promotions_remaining = remaining - len(promotions)
                self._enforce_scratch_budget()
            finally:
                binding.close()

    def _prefetch_claims(self, entries: list[tuple[str, str]]) -> None:
        """Fetch advertised claim objects in bounded batches instead of one process per ref."""

        missing_entries: list[tuple[str, str]] = []
        for offset in range(0, len(entries), CLAIM_OBJECT_READ_BATCH_SIZE):
            batch = entries[offset : offset + CLAIM_OBJECT_READ_BATCH_SIZE]
            proc = self._git(
                ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                input_text="".join(f"{oid}\n" for oid, _ref in batch),
            )
            results = proc.stdout.splitlines()
            if len(results) != len(batch):
                raise ClaimTransportError("claim scratch returned malformed object inventory")
            for (oid, ref), result in zip(batch, results):
                fields = result.split(" ")
                if fields == [oid, "commit"]:
                    continue
                if fields != [oid, "missing"]:
                    raise ClaimTransportError("claim scratch returned malformed object inventory")
                missing_entries.append((oid, ref))
        if missing_entries:
            self._fetch_claims_quarantined(missing_entries)

    @staticmethod
    def _lease_is_valid(lease: Mapping[str, Any], key: str | None = None) -> bool:
        if lease.get("schema") == LEGACY_TOMBSTONE_SCHEMA:
            try:
                resource = _validate_key(lease.get("resource"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False
            return bool(_is_finite_number(lease.get("blocked_at")) and (key is None or resource == key))
        if lease.get("schema") == LEGACY_BLOCK_SCHEMA:
            try:
                resource = _validate_key(lease.get("resource"))  # type: ignore[arg-type]
                _validate_key(lease.get("canonical_resource"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False
            return bool(_is_finite_number(lease.get("blocked_at")) and (key is None or resource == key))
        acquired_at = lease.get("acquired_at")
        renewed_at = lease.get("renewed_at", acquired_at)
        expires_at = lease.get("expires_at")
        try:
            resource = _validate_key(lease.get("resource"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        valid = (
            lease.get("schema") in {CLAIM_SCHEMA, LEGACY_CLAIM_SCHEMA}
            and isinstance(lease.get("owner"), str)
            and bool(lease.get("owner"))
            and _is_finite_number(acquired_at)
            and _is_finite_number(expires_at)
            and acquired_at <= expires_at
        )
        if lease.get("schema") == CLAIM_SCHEMA:
            try:
                _bounded_text(
                    lease.get("owner"),
                    label="claim owner",
                    maximum=CLAIM_WORKER_ID_MAX_BYTES,
                )
                _bounded_text(
                    lease.get("host"),
                    label="claim host",
                    maximum=CLAIM_WORKER_ID_MAX_BYTES,
                )
                if "note" in lease:
                    _bounded_text(
                        lease["note"],
                        label="claim note",
                        maximum=CLAIM_NOTE_MAX_BYTES,
                        allow_empty=True,
                    )
            except ValueError:
                return False
            valid = bool(
                valid
                and isinstance(lease.get("pid"), int)
                and not isinstance(lease.get("pid"), bool)
                and 0 < lease["pid"] <= CLAIM_PID_MAX
                and _is_finite_number(renewed_at)
                and acquired_at <= renewed_at <= expires_at
                and isinstance(lease.get("lease_id"), str)
                and LEASE_ID_RE.fullmatch(str(lease.get("lease_id")))
            )
        return bool(valid and (key is None or resource == key))

    def _make_legacy_block_commit(
        self,
        key: str,
        canonical_key: str,
    ) -> str:
        key = _validate_key(key)
        canonical_key = _validate_key(canonical_key)
        self._require_scratch_capacity(
            entries=_SCRATCH_CLAIM_RESERVE_ENTRIES,
            size=_SCRATCH_CLAIM_RESERVE_BYTES,
        )
        tree = self._canonical_empty_tree_oid()
        now = self._operation_time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        block = {
            "blocked_at": now,
            "canonical_resource": canonical_key,
            "resource": key,
            "schema": LEGACY_BLOCK_SCHEMA,
        }
        message = json.dumps(block, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _make_legacy_tombstone_commit(
        self,
        key: str,
    ) -> str:
        key = _validate_key(key)
        self._require_scratch_capacity(
            entries=_SCRATCH_CLAIM_RESERVE_ENTRIES,
            size=_SCRATCH_CLAIM_RESERVE_BYTES,
        )
        tree = self._canonical_empty_tree_oid()
        now = self._operation_time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        tombstone = {
            "blocked_at": now,
            "resource": key,
            "schema": LEGACY_TOMBSTONE_SCHEMA,
        }
        message = json.dumps(tombstone, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _make_lease_commit(
        self,
        key: str,
        ttl: int | float,
        note: str = "",
        *,
        lease_id: str | None = None,
        acquired_at: int | float | None = None,
        previous_renewed_at: int | float | None = None,
        previous_expires_at: int | float | None = None,
    ) -> str:
        key = _validate_key(key)
        ttl = _validate_ttl(ttl)
        note = _bounded_text(
            note,
            label="claim note",
            maximum=CLAIM_NOTE_MAX_BYTES,
            allow_empty=True,
        )
        self._require_scratch_capacity(
            entries=_SCRATCH_CLAIM_RESERVE_ENTRIES,
            size=_SCRATCH_CLAIM_RESERVE_BYTES,
        )
        tree = self._canonical_empty_tree_oid()
        now = self._operation_time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        if acquired_at is None:
            acquired_at = now
        if not _is_finite_number(acquired_at) or acquired_at > now + CLAIM_CLOCK_SKEW_S:
            raise ValueError("claim acquisition timestamp must be finite and within the allowed clock skew")
        if previous_renewed_at is None:
            previous_renewed_at = acquired_at
        if (
            not _is_finite_number(previous_renewed_at)
            or previous_renewed_at < acquired_at
            or previous_renewed_at > now + CLAIM_CLOCK_SKEW_S
        ):
            raise ValueError("claim renewal timestamp must be monotonic and within the allowed clock skew")
        renewed_at = max(now, acquired_at, previous_renewed_at)
        if previous_expires_at is not None and not _is_finite_number(previous_expires_at):
            raise ValueError("claim expiry timestamp must be finite")
        if previous_expires_at is not None and previous_expires_at <= now:
            raise _LeaseExpiredDuringMutation
        expiry_floor = renewed_at if previous_expires_at is None else previous_expires_at
        try:
            expires_at = max(renewed_at + ttl, expiry_floor)
        except OverflowError as exc:
            raise ValueError("claim expiry must be finite") from exc
        if not math.isfinite(expires_at):
            raise ValueError("claim expiry must be finite")
        if lease_id is None:
            lease_id = secrets.token_hex(32)
        if not isinstance(lease_id, str) or not LEASE_ID_RE.fullmatch(lease_id):
            raise ValueError("claim lease_id must be 64 lowercase hexadecimal characters")
        host = _bounded_text(
            socket.gethostname(),
            label="claim host",
            maximum=CLAIM_WORKER_ID_MAX_BYTES,
        )
        lease: dict[str, Any] = {
            "schema": CLAIM_SCHEMA,
            "lease_id": lease_id,
            "owner": self.worker_id,
            "host": host,
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "renewed_at": renewed_at,
            "expires_at": expires_at,
            "resource": key,
        }
        if note:
            lease["note"] = note
        if not self._lease_is_valid(lease, key):
            raise ValueError("generated claim lease violates the claim schema")
        message = json.dumps(lease, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _cas_push(self, key: str, old: str | None, new: str) -> bool:
        ref = self._ref(key)
        source = new if new else ""
        proc = self._remote_git(
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
        # Some Git transports report a compare-and-swap loss only as a generic
        # remote "failed to update ref" error. Re-read the ref: a value that
        # differs from our lease proves another claimant won, while an unchanged
        # value remains a genuine transport failure.
        try:
            current = self._remote_oid(key)
        except ClaimTransportError:
            current = old
        if current == (new or None):
            return True
        if current != old:
            return False
        raise ClaimTransportError(f"claim CAS push failed: {detail[:300]}")

    def _retain_rollback_commit(self, key: str, oid: str) -> str:
        proc = self._git(["cat-file", "commit", oid], check=False)
        commit = proc.stdout if proc.returncode == 0 else self._commit_cache().get(oid)
        if commit is None:
            raise ClaimTransportError(f"claim {key!r} cannot retain its prior generation for rollback")
        self._validated_claim_message(key, commit)
        if proc.returncode != 0:
            self._require_scratch_capacity(
                entries=3,
                size=len(commit.encode("utf-8")) + 1024,
            )
            retained = self._git(
                ["hash-object", "-t", "commit", "-w", "--stdin"],
                input_text=commit,
            ).stdout.strip()
            if retained != oid:
                raise ClaimTransportError(f"claim {key!r} changed while retaining its rollback generation")
        return commit

    def _restore_rollback_commit(self, mutation: _ClaimMutation) -> None:
        if mutation.old_oid is None or mutation.old_commit is None:
            return
        proc = self._git(["cat-file", "-e", f"{mutation.old_oid}^{{commit}}"], check=False)
        if proc.returncode == 0:
            return
        self._require_scratch_capacity(
            entries=3,
            size=len(mutation.old_commit.encode("utf-8")) + 1024,
        )
        restored = self._git(
            ["hash-object", "-t", "commit", "-w", "--stdin"],
            input_text=mutation.old_commit,
        ).stdout.strip()
        if restored != mutation.old_oid:
            raise ClaimTransportError(f"claim {mutation.key!r} rollback content does not match its prior OID")

    def _rollback_mutations(self, mutations: Iterable[_ClaimMutation]) -> None:
        entries = tuple(mutations)
        if len(entries) > CLAIM_MAX_REFS:
            raise ValueError(f"claim rollback contains more than {CLAIM_MAX_REFS} refs")
        for mutation in reversed(entries):
            if not isinstance(mutation, _ClaimMutation):
                raise ValueError("claim rollback contains an invalid mutation")
            self._restore_rollback_commit(mutation)
            if not self._cas_push(
                mutation.key,
                mutation.new_oid,
                mutation.old_oid or "",
            ):
                raise ClaimTransportError(f"claim {mutation.key!r} changed before it could be rolled back")
            if mutation.new_receipt_oid is None:
                continue
            self._restore_receipt_generation(
                mutation.key,
                changed_oid=mutation.new_receipt_oid,
                prior_oid=mutation.old_receipt_oid,
            )

    @_claim_operation
    def _rollback_claim_mutations(self, mutations: Iterable[_ClaimMutation]) -> None:
        """Restore this operation's exact prior ref generations, or fail closed."""

        self._ensure_scratch()
        self._rollback_mutations(mutations)

    @_claim_operation
    def read(self, key: str) -> dict[str, Any] | None:
        """Return the current parsed lease, including an expired lease, or ``None``."""
        self._ensure_scratch()
        oid = self._remote_oid(key)
        return self._read_lease(key, oid) if oid else None

    @classmethod
    def expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether a lease is reclaimable after bounded clock-skew grace."""
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS:
            return False
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        return expires_at <= comparison_time - CLAIM_CLOCK_SKEW_S

    @classmethod
    def _holder_expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether the lease holder's nominal authority has elapsed."""
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS:
            return False
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        return expires_at <= comparison_time

    @classmethod
    def recovery_required(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether bounded lease timing was violated and explicit cleanup is required."""
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim recovery comparison clock must be finite")
        if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS:
            return False
        acquired_at = lease.get("acquired_at")
        renewed_at = lease.get("renewed_at", acquired_at)
        expires_at = lease.get("expires_at")
        if not _is_finite_number(acquired_at) or not _is_finite_number(renewed_at) or not _is_finite_number(expires_at):
            return False
        return bool(
            renewed_at > comparison_time + CLAIM_CLOCK_SKEW_S
            or (lease.get("schema") == CLAIM_SCHEMA and expires_at - renewed_at > CLAIM_MAX_TTL_S)
        )

    @_claim_operation
    def install_legacy_compatibility(
        self,
        key: str,
        *,
        canonical_key: str,
    ) -> bool:
        """Permanently fence a v1 path key before a v2 canonical claim is used."""
        key = _validate_key(key)
        canonical_key = _validate_key(canonical_key)
        self._ensure_scratch()
        old = self._remote_oid(key)
        lease: dict[str, Any] | None = None
        if old is not None:
            lease = self._read_lease(key, old)
            if lease.get("schema") == LEGACY_BLOCK_SCHEMA:
                if lease.get("canonical_resource") != canonical_key:
                    raise ClaimTransportError(f"legacy claim {key!r} is already mapped to a different canonical key")
                return True
        mutation_time = self._operation_time()
        if (
            lease is not None
            and lease.get("schema") != LEGACY_TOMBSTONE_SCHEMA
            and (
                lease.get("schema") not in {LEGACY_CLAIM_SCHEMA, CLAIM_SCHEMA}
                or self.recovery_required(lease, now=mutation_time)
                or not self.expired(lease, now=mutation_time)
            )
        ):
            return False
        new = self._make_legacy_block_commit(key, canonical_key)
        if not self._cas_push(key, old, new):
            return False
        return True

    @_claim_operation
    def prepare_v2_claim(
        self,
        canonical_key: str,
        compatibility_keys: Iterable[str],
        *,
        canonical_keys: Iterable[str] = (),
    ) -> bool:
        """Retire observable v1 keys, then install permanent compatibility fences."""
        canonical_key = _validate_key(canonical_key)
        keys = tuple(
            key
            for key in dict.fromkeys(_bounded_keys(compatibility_keys, label="compatibility keys"))
            if key != canonical_key
        )
        protected = set(_bounded_keys(canonical_keys, label="canonical keys"))
        protected.add(canonical_key)
        collisions = sorted(set(keys) & (protected - {canonical_key}))
        if collisions:
            raise ValueError(
                "legacy compatibility key collides with a durable canonical claim key: " + ", ".join(collisions)
            )

        for key in keys:
            if not self.install_legacy_compatibility(
                key,
                canonical_key=canonical_key,
            ):
                return False
        return True

    def _legacy_author_claim_blocks_v2(self, key: str) -> bool:
        if not key.startswith("author/"):
            return False
        for lease in self.list():
            if not str(lease["_key"]).startswith("author/"):
                continue
            if lease["_malformed"]:
                raise MalformedLeaseError(str(lease["_error"]))
            if lease.get("schema") == LEGACY_CLAIM_SCHEMA:
                if lease["_key"] == key and lease["_expired"] and not lease["_recovery_required"]:
                    continue
                return True
        return False

    @_claim_operation
    def acquire(
        self,
        key: str,
        ttl: int | float = CLAIM_TTL_S,
        steal: bool = False,
        note: str = "",
        *,
        mutations: list[_ClaimMutation] | None = None,
    ) -> bool:
        """CAS-acquire a free or expired lease, or refresh this exact session's lease."""
        if steal:
            raise ValueError("claim stealing is not supported")
        key = _validate_key(key)
        _validate_ttl(ttl)
        note = _bounded_text(
            note,
            label="claim note",
            maximum=CLAIM_NOTE_MAX_BYTES,
            allow_empty=True,
        )
        self._ensure_scratch()
        if self._legacy_author_claim_blocks_v2(key):
            return False
        receipt = self._receipt_oid(key)
        old = self._remote_oid(key)
        lease_id: str | None = None
        acquired_at: int | float | None = None
        previous_renewed_at: int | float | None = None
        previous_expires_at: int | float | None = None
        rollback_commit: str | None = None
        lease: dict[str, Any] | None = None
        if old is not None:
            lease = self._read_lease(key, old)
        now = self._operation_time()
        if lease is not None:
            if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS or self.recovery_required(lease, now=now):
                return False
            if not self.expired(lease, now=now):
                if lease.get("schema") == LEGACY_CLAIM_SCHEMA:
                    return False
                if self._holder_expired(lease, now=now):
                    return False
                if not self._receipt_matches(key, old, lease):
                    return False
                else:
                    lease_id = str(lease["lease_id"])
                    acquired_at = lease["acquired_at"]
                    previous_renewed_at = lease.get("renewed_at", acquired_at)
                    previous_expires_at = lease["expires_at"]
        if old is not None:
            rollback_commit = self._retain_rollback_commit(key, old)
        try:
            new = self._make_lease_commit(
                key,
                ttl,
                note,
                lease_id=lease_id,
                acquired_at=acquired_at,
                previous_renewed_at=previous_renewed_at,
                previous_expires_at=previous_expires_at,
            )
        except _LeaseExpiredDuringMutation:
            return False
        if not self._cas_push(key, old, new):
            return False
        try:
            legacy_blocked = self._legacy_author_claim_blocks_v2(key)
        except Exception as exc:
            if not self._cas_push(key, new, old or ""):
                raise ClaimTransportError(
                    "legacy compatibility could not be verified after acquisition, and "
                    "the v2 claim could not be rolled back"
                ) from exc
            raise
        if legacy_blocked:
            if not self._cas_push(key, new, old or ""):
                raise ClaimTransportError(
                    "a legacy v1 claim appeared while a v2 claim was acquired, and "
                    "the v2 claim could not be rolled back"
                )
            return False
        try:
            self._record_receipt(key, new, expected=receipt)
        except BaseException as exc:
            if not self._cas_push(key, new, old or ""):
                raise ClaimTransportError(
                    "the claim receipt could not be recorded and the remote claim could not be rolled back"
                ) from exc
            self._restore_receipt_generation(
                key,
                changed_oid=new,
                prior_oid=receipt,
            )
            raise
        if mutations is not None:
            mutations.append(
                _ClaimMutation(
                    key=key,
                    old_oid=old,
                    new_oid=new,
                    old_receipt_oid=receipt,
                    new_receipt_oid=new,
                    old_commit=rollback_commit,
                )
            )
        return True

    @_claim_operation
    def renew(
        self,
        key: str,
        ttl: int | float = CLAIM_TTL_S,
        *,
        lease_id: str | None = None,
        mutations: list[_ClaimMutation] | None = None,
    ) -> bool:
        """CAS-renew this session's exact lease, returning ``False`` if it was lost."""
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        if self._legacy_author_claim_blocks_v2(key):
            return False
        old = self._remote_oid(key)
        if old is None:
            return False
        lease = self._read_lease(key, old)
        now = self._operation_time()
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.recovery_required(lease, now=now)
            or self._holder_expired(lease, now=now)
            or not self._receipt_matches(key, old, lease)
            or (lease_id is not None and lease.get("lease_id") != lease_id)
        ):
            return False
        rollback_commit = self._retain_rollback_commit(key, old)
        try:
            new = self._make_lease_commit(
                key,
                ttl,
                str(lease.get("note", "")),
                lease_id=str(lease["lease_id"]),
                acquired_at=lease["acquired_at"],
                previous_renewed_at=lease.get("renewed_at", lease["acquired_at"]),
                previous_expires_at=lease["expires_at"],
            )
        except _LeaseExpiredDuringMutation:
            return False
        if not self._cas_push(key, old, new):
            return False
        try:
            legacy_blocked = self._legacy_author_claim_blocks_v2(key)
        except Exception as exc:
            if not self._cas_push(key, new, old):
                raise ClaimTransportError(
                    "legacy compatibility could not be verified after renewal, and "
                    "the prior lease could not be restored"
                ) from exc
            raise
        if legacy_blocked:
            if not self._cas_push(key, new, old):
                raise ClaimTransportError(
                    "a legacy v1 claim appeared while a v2 claim was renewed, and the prior lease could not be restored"
                )
            return False
        try:
            self._record_receipt(key, new, expected=old)
        except BaseException as exc:
            if not self._cas_push(key, new, old):
                raise ClaimTransportError(
                    "the renewed claim receipt could not be recorded and the prior lease could not be restored"
                ) from exc
            self._restore_receipt_generation(
                key,
                changed_oid=new,
                prior_oid=old,
            )
            raise
        if mutations is not None:
            mutations.append(
                _ClaimMutation(
                    key=key,
                    old_oid=old,
                    new_oid=new,
                    old_receipt_oid=old,
                    new_receipt_oid=new,
                    old_commit=rollback_commit,
                )
            )
        return True

    @_claim_operation
    def release(self, key: str) -> bool:
        """CAS-delete this session's lease; refuse stale or unverifiable ownership."""
        key = _validate_key(key)
        self._ensure_scratch()
        receipt = self._receipt_oid(key)
        old = self._remote_oid(key)
        if old is None:
            self._clear_receipt(key, expected=receipt)
            return True
        lease = self._read_lease(key, old)
        now = self._operation_time()
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.recovery_required(lease, now=now)
            or self._holder_expired(lease, now=now)
            or not self._receipt_matches(key, old, lease)
        ):
            return False
        if not self._cas_push(key, old, ""):
            return False
        self._clear_receipt(key, expected=old)
        return True

    @_claim_operation
    def holds(self, key: str) -> bool:
        """Return whether this session has the exact receipt for the live lease."""
        return self.held_claim_oid(key) is not None

    @_claim_operation
    def held_claim_oid(self, key: str) -> str | None:
        """Return the exact live claim commit owned by this session, or ``None``.

        Callers must still use this object ID as a remote compare-and-swap lease.
        Ownership can change immediately after this point-in-time validation.
        """
        fence = self.held_claim_fence(key)
        return fence.oid if fence is not None else None

    @_claim_operation
    def held_lease_id(self, key: str) -> str | None:
        """Return the fenced lease id held by this session, or ``None``."""
        fence = self.held_claim_fence(key)
        return fence.lease_id if fence is not None else None

    @_claim_operation
    def held_claim_fence(self, key: str) -> ClaimFence | None:
        """Return one coherent ref/OID/lease receipt for this session's live claim."""

        held = self._held_claim(key)
        if held is None:
            return None
        oid, lease = held
        return ClaimFence(
            key=key,
            ref=self._ref(key),
            oid=oid,
            lease_id=str(lease["lease_id"]),
        )

    def _held_claim(self, key: str) -> tuple[str, dict[str, Any]] | None:
        key = _validate_key(key)
        self._ensure_scratch()
        if self._legacy_author_claim_blocks_v2(key):
            return None
        oid = self._remote_oid(key)
        if oid is None:
            return None
        lease = self._read_lease(key, oid)
        now = self._operation_time()
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.recovery_required(lease, now=now)
            or self._holder_expired(lease, now=now)
            or not self._receipt_matches(key, oid, lease)
        ):
            return None
        return oid, lease

    def _receipt_matches(self, key: str, oid: str, lease: Mapping[str, Any]) -> bool:
        """Return whether this session recorded this exact v2 lease commit."""
        receipt_oid = self._receipt_oid(key)
        if receipt_oid != oid or lease.get("schema") != CLAIM_SCHEMA:
            return False
        receipt = self._read_lease(key, receipt_oid)
        return bool(receipt.get("lease_id") == lease.get("lease_id"))

    @_claim_operation
    def list(self) -> list[dict[str, Any]]:
        """Return all claim refs, including malformed and expired entries."""
        self._ensure_scratch()
        for attempt in range(CLAIM_LIST_RETRIES):
            try:
                return self._list_once()
            except _ClaimChurnError as exc:
                if attempt + 1 == CLAIM_LIST_RETRIES:
                    raise ClaimTransportError("claim board kept changing during bounded list retries") from exc
        raise AssertionError("unreachable claim list retry state")

    def _list_once(self) -> list[dict[str, Any]]:
        entries = self._advertised_claims()
        leases: list[dict[str, Any]] = []
        for offset in range(0, len(entries), CLAIM_FETCH_BATCH_SIZE):
            batch = entries[offset : offset + CLAIM_FETCH_BATCH_SIZE]
            self._prefetch_claims(batch)
            for oid, ref in batch:
                key = ref[len(CLAIM_REF_PREFIX) :]
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
                    lease["_legacy_block"] = lease.get("schema") == LEGACY_BLOCK_SCHEMA
                    lease["_legacy_tombstone"] = lease.get("schema") == LEGACY_TOMBSTONE_SCHEMA
                lease["_key"] = key
                lease["_oid"] = oid
                leases.append(lease)
            self._commit_cache().clear()
            self._malformed_commit_cache().clear()
        now = self._operation_time()
        for lease in leases:
            lease["_expired"] = not lease["_malformed"] and self.expired(lease, now=now)
            lease["_recovery_required"] = not lease["_malformed"] and self.recovery_required(lease, now=now)
        return sorted(leases, key=lambda lease: str(lease["_key"]))

    @_claim_operation
    def cleanup(
        self,
        *,
        legacy_mappings: Mapping[str, str] | None = None,
    ) -> int:
        """CAS-recover expired or unsafe leases and return the changed-ref count."""
        mappings: dict[str, str] | None = None
        canonical_keys: set[str] = set()
        if legacy_mappings is not None:
            if len(legacy_mappings) > CLAIM_MAX_REFS:
                raise ValueError(f"legacy mappings contain more than {CLAIM_MAX_REFS} keys")
            mappings = {
                _validate_key(legacy): _validate_key(canonical) for legacy, canonical in legacy_mappings.items()
            }
            canonical_keys = set(mappings.values())
        leases = self.list()
        if mappings is not None:
            for lease in leases:
                key = str(lease["_key"])
                if (
                    lease.get("schema") == LEGACY_BLOCK_SCHEMA
                    and key in mappings
                    and lease.get("canonical_resource") != mappings[key]
                ):
                    raise ClaimTransportError(f"legacy claim {key!r} is already mapped to a different canonical key")
        if mappings is None and any(
            lease.get("schema") == LEGACY_CLAIM_SCHEMA
            and str(lease["_key"]).startswith("author/")
            and (lease["_expired"] or lease["_recovery_required"])
            for lease in leases
        ):
            raise ValueError(
                "a blueprint is required to recover legacy author claims without blocking durable article IDs"
            )
        recovered = 0
        for lease in leases:
            if lease["_malformed"] or not (lease["_expired"] or lease["_recovery_required"]):
                continue
            key = str(lease["_key"])
            old = str(lease["_oid"])
            if lease.get("schema") == LEGACY_CLAIM_SCHEMA and key.startswith("author/"):
                assert mappings is not None
                if key in canonical_keys:
                    new = ""
                elif key in mappings:
                    new = self._make_legacy_block_commit(key, mappings[key])
                else:
                    new = self._make_legacy_tombstone_commit(key)
            else:
                new = ""
            if self._cas_push(key, old, new):
                recovered += 1
        return recovered

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
            self._thread.join(timeout=CLAIM_OPERATION_TIMEOUT_S + CLAIM_PROCESS_TERM_S + CLAIM_PROCESS_KILL_S)
            if self._thread.is_alive():
                self.lost.set()
                self.error = ClaimTransportError("claim heartbeat did not stop before its deadline")
                raise self.error

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
    "CLAIM_CLOCK_SKEW_S",
    "CLAIM_MAX_TTL_S",
    "CLAIM_RECEIPT_REF_PREFIX",
    "CLAIM_REF_PREFIX",
    "CLAIM_SCHEMA",
    "CLAIM_TTL_S",
    "ClaimBoard",
    "ClaimFence",
    "ClaimTransportError",
    "Heartbeat",
    "LEGACY_CLAIM_SCHEMA",
    "LEGACY_BLOCK_SCHEMA",
    "LEASE_ID_RE",
    "LEGACY_TOMBSTONE_SCHEMA",
    "MalformedLeaseError",
    "author_claim_key",
    "claim_repository_is_remote",
    "normalize_claim_repository",
    "pin_claim_repository",
    "pin_claim_scratch",
    "resource_claim_key",
]
