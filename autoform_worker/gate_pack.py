"""Create and revalidate bounded Git packs for isolated candidate gates."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gate_provider import GateProviderError


_OBJECT_FORMAT_LENGTHS = {"sha1": 40, "sha256": 64}
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MAX_GIT_DIAGNOSTIC = 64 * 1024


@dataclass(frozen=True, slots=True)
class RepositoryPack:
    """Exact host-side identity of one immutable gate input pack."""

    path: str
    sha256: str
    size: int
    device: int
    inode: int
    mode: int
    object_format: str
    base_oid: str
    candidate_oid: str

    def __post_init__(self) -> None:
        _canonical_absolute_path(self.path, label="repository pack path")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise GateProviderError("repository pack SHA-256 is invalid")
        for label, value in (
            ("repository pack size", self.size),
            ("repository pack device", self.device),
            ("repository pack inode", self.inode),
        ):
            if type(value) is not int or value <= 0:
                raise GateProviderError(f"{label} must be a positive integer")
        if type(self.mode) is not int or self.mode != 0o444:
            raise GateProviderError("repository pack mode must be 0444")
        length = _OBJECT_FORMAT_LENGTHS.get(self.object_format)
        if length is None:
            raise GateProviderError("repository pack object format is unsupported")
        for label, oid in (("base", self.base_oid), ("candidate", self.candidate_oid)):
            if not isinstance(oid, str) or _OID.fullmatch(oid) is None or len(oid) != length:
                raise GateProviderError(f"repository pack {label} OID has the wrong format")
            if oid == "0" * len(oid):
                raise GateProviderError(f"repository pack {label} OID must not be all-zero")
        if self.base_oid == self.candidate_oid:
            raise GateProviderError("repository pack candidate OID must differ from its base")


def prepare_repository_pack(
    repository: str | Path,
    destination: str | Path,
    *,
    base_oid: str,
    candidate_oid: str,
    maximum_bytes: int,
    timeout_seconds: float,
) -> RepositoryPack:
    """Write one self-contained base/candidate closure without checkout state."""

    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise GateProviderError("repository pack limit must be a positive integer")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise GateProviderError("repository pack timeout must be a positive number")
    timeout = float(timeout_seconds)
    if not 0 < timeout < float("inf"):
        raise GateProviderError("repository pack timeout must be a positive number")
    root = _real_directory(Path(repository), label="repository root")
    target = Path(destination)
    _canonical_absolute_path(os.fspath(target), label="repository pack path")
    parent = _private_directory(target.parent, label="repository pack directory")
    if target.parent != parent:
        raise GateProviderError("repository pack path has a noncanonical parent")
    if _paths_overlap(root, parent):
        raise GateProviderError("repository pack directory must be outside the source repository")
    repository_identity = _directory_identity(root)
    top_level = _git_text(root, ["rev-parse", "--show-toplevel"], timeout=timeout).strip()
    if top_level != os.fspath(root):
        raise GateProviderError("repository root must be the exact Git worktree top level")
    object_format = _git_text(
        root,
        ["rev-parse", "--show-object-format"],
        timeout=timeout,
    ).strip()
    length = _OBJECT_FORMAT_LENGTHS.get(object_format)
    if length is None:
        raise GateProviderError("repository uses an unsupported Git object format")
    for label, oid in (("base", base_oid), ("candidate", candidate_oid)):
        if not isinstance(oid, str) or _OID.fullmatch(oid) is None or len(oid) != length:
            raise GateProviderError(f"{label} OID does not match the repository object format")
        if oid == "0" * len(oid):
            raise GateProviderError(f"{label} OID must not be all-zero")
        resolved = _git_text(
            root,
            ["rev-parse", "--verify", f"{oid}^{{commit}}"],
            timeout=timeout,
        )
        if resolved != f"{oid}\n":
            raise GateProviderError(f"{label} OID does not resolve to one exact commit")
    if base_oid == candidate_oid:
        raise GateProviderError("candidate OID must differ from base OID")
    _git_text(
        root,
        ["merge-base", "--is-ancestor", base_oid, candidate_oid],
        timeout=timeout,
    )
    if _directory_identity(root) != repository_identity:
        raise GateProviderError("repository root changed while pack inputs were verified")

    staging = parent / f".{target.name}.preparing-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(staging, flags, 0o600)
    except OSError as error:
        raise GateProviderError("repository pack destination cannot be created exclusively") from error
    digest = hashlib.sha256()
    size = 0
    try:
        completed, size = _stream_pack(
            root,
            descriptor,
            digest,
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=maximum_bytes,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise GateProviderError(
                f"repository pack creation failed with exit {completed.returncode}: "
                f"{_diagnostic(completed.stderr)}"
            )
        if size <= 0:
            raise GateProviderError("repository pack creation returned no bytes")
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
            raise GateProviderError("repository pack destination changed while it was written")
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _directory_identity(root) != repository_identity:
        raise GateProviderError("repository root changed while its pack was created")
    _fsync_directory(parent)
    _publish_repository_pack(staging, target, parent, info)
    pack = RepositoryPack(
        path=os.fspath(target),
        sha256=digest.hexdigest(),
        size=size,
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        object_format=object_format,
        base_oid=base_oid,
        candidate_oid=candidate_oid,
    )
    verify_repository_pack(pack)
    return pack


def verify_repository_pack(pack: RepositoryPack) -> None:
    """Reopen and hash the exact pack pathname before and after Docker create."""

    path = Path(pack.path)
    identity = _regular_file_identity(path)
    expected = (pack.device, pack.inode, pack.size, pack.mode)
    if identity[:4] != expected:
        raise GateProviderError("repository pack identity changed")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GateProviderError("repository pack cannot be reopened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, stat.S_IMODE(opened.st_mode)) != expected:
            raise GateProviderError("repository pack identity changed while it was opened")
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, pack.size + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > pack.size:
                raise GateProviderError("repository pack grew after it was prepared")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, stat.S_IMODE(after.st_mode)) != expected:
            raise GateProviderError("repository pack changed while it was hashed")
    finally:
        os.close(descriptor)
    if size != pack.size or digest.hexdigest() != pack.sha256:
        raise GateProviderError("repository pack content changed")
    if _regular_file_identity(path)[:4] != expected:
        raise GateProviderError("repository pack pathname changed")


def _stream_pack(
    repository: Path,
    descriptor: int,
    digest: Any,
    *,
    base_oid: str,
    candidate_oid: str,
    maximum_bytes: int,
    timeout: float,
) -> tuple[subprocess.CompletedProcess[bytes], int]:
    command = _git_command(
        [
            "--no-replace-objects",
            "pack-objects",
            "--stdout",
            "--revs",
            "--no-reuse-delta",
            "--no-reuse-object",
            "--no-thin",
            "--no-include-tag",
            "--window=0",
            "--threads=1",
        ]
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=repository,
            env=_git_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise GateProviderError("repository pack process could not start") from error
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    try:
        process.stdin.write(f"{base_oid}\n{candidate_oid}\n".encode("ascii"))
        process.stdin.close()
        size, stderr = _copy_process_output(
            process,
            descriptor,
            digest,
            maximum_bytes=maximum_bytes,
            timeout=timeout,
        )
        returncode = process.wait(timeout=1)
    except GateProviderError:
        _terminate_process(process)
        raise
    except (OSError, subprocess.SubprocessError) as error:
        _terminate_process(process)
        raise GateProviderError("repository pack process failed") from error
    finally:
        process.stdout.close()
        process.stderr.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    return subprocess.CompletedProcess(command, returncode, b"", stderr), size


def _copy_process_output(
    process: subprocess.Popen[bytes],
    descriptor: int,
    digest: Any,
    *,
    maximum_bytes: int,
    timeout: float,
) -> tuple[int, bytes]:
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout
    size = 0
    diagnostic = bytearray()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GateProviderError("repository pack creation timed out")
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 1024 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    diagnostic.extend(chunk)
                    if len(diagnostic) > _MAX_GIT_DIAGNOSTIC:
                        raise GateProviderError(
                            "repository pack process produced excessive diagnostics"
                        )
                    continue
                size += len(chunk)
                if size > maximum_bytes:
                    raise GateProviderError("repository pack exceeds its configured byte limit")
                digest.update(chunk)
                _write_all(descriptor, chunk)
    finally:
        selector.close()
    return size, bytes(diagnostic)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _git_text(repository: Path, arguments: list[str], *, timeout: float) -> str:
    try:
        completed = subprocess.run(
            _git_command(arguments),
            cwd=repository,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateProviderError(f"Git input verification failed: {type(error).__name__}") from error
    if len(completed.stdout) > _MAX_GIT_DIAGNOSTIC or len(completed.stderr) > _MAX_GIT_DIAGNOSTIC:
        raise GateProviderError("Git input verification produced excessive output")
    if completed.returncode != 0:
        raise GateProviderError(
            f"Git input verification failed with exit {completed.returncode}: "
            f"{_diagnostic(completed.stderr)}"
        )
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GateProviderError("Git input verification returned non-UTF-8 output") from error


def _git_command(arguments: list[str]) -> list[str]:
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
        *arguments,
    ]


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_GRAFT_FILE": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.devnull,
        "LANG": "C.UTF-8",
        "PATH": os.defpath,
    }


def _real_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise GateProviderError(f"{label} must not be a symbolic link")
    try:
        resolved = path.expanduser().resolve(strict=True)
        info = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    if not stat.S_ISDIR(info.st_mode):
        raise GateProviderError(f"{label} must be a real directory")
    return resolved


def _private_directory(path: Path, *, label: str) -> Path:
    resolved = _real_directory(path, label=label)
    info = resolved.stat(follow_symlinks=False)
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise GateProviderError(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise GateProviderError(f"{label} must not grant group or other permissions")
    return resolved


def _canonical_absolute_path(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not Path(value).is_absolute()
        or os.path.normpath(value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GateProviderError(f"{label} must be a canonical absolute path")


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError("repository root cannot be revalidated") from error
    if not stat.S_ISDIR(info.st_mode):
        raise GateProviderError("repository root is no longer a directory")
    return info.st_dev, info.st_ino


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


def _regular_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink():
        raise GateProviderError("repository pack pathname became a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError("repository pack pathname cannot be inspected") from error
    if (
        resolved != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o444
    ):
        raise GateProviderError("repository pack pathname is not one private read-only file")
    return info.st_dev, info.st_ino, info.st_size, stat.S_IMODE(info.st_mode), info.st_mtime_ns


def _publish_repository_pack(
    staging: Path,
    target: Path,
    parent: Path,
    prepared: os.stat_result,
) -> None:
    expected = (
        prepared.st_dev,
        prepared.st_ino,
        prepared.st_size,
        stat.S_IMODE(prepared.st_mode),
    )
    if _regular_file_identity(staging)[:4] != expected:
        raise GateProviderError("repository pack staging identity changed")
    try:
        os.link(staging, target, follow_symlinks=False)
    except OSError as error:
        raise GateProviderError("repository pack destination cannot be created exclusively") from error
    _fsync_directory(parent)

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise GateProviderError("published repository pack cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            stat.S_IMODE(opened.st_mode),
        ) != expected or opened.st_nlink != 2:
            raise GateProviderError("published repository pack has the wrong identity")
        try:
            staging_info = staging.stat(follow_symlinks=False)
            target_info = target.stat(follow_symlinks=False)
        except OSError as error:
            raise GateProviderError("repository pack publication cannot be revalidated") from error
        if (
            staging_info.st_dev,
            staging_info.st_ino,
            staging_info.st_size,
            stat.S_IMODE(staging_info.st_mode),
        ) != expected or (
            target_info.st_dev,
            target_info.st_ino,
            target_info.st_size,
            stat.S_IMODE(target_info.st_mode),
        ) != expected:
            raise GateProviderError("repository pack publication changed identity")
        try:
            os.unlink(staging)
        except OSError as error:
            raise GateProviderError("repository pack staging link cannot be removed") from error
        _fsync_directory(parent)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            stat.S_IMODE(after.st_mode),
        ) != expected or after.st_nlink != 1:
            raise GateProviderError("repository pack publication did not become exclusive")
        if _regular_file_identity(target)[:4] != expected:
            raise GateProviderError("repository pack pathname changed after publication")
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except OSError as error:
            raise GateProviderError("repository pack could not be written") from error
        if written <= 0:
            raise GateProviderError("repository pack could not be written")
        offset += written


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise GateProviderError("repository pack directory could not be synchronized") from error


def _diagnostic(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()[:1000] or "no diagnostic"


__all__ = ["RepositoryPack", "prepare_repository_pack", "verify_repository_pack"]
