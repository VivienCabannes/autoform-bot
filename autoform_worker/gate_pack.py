"""Create and revalidate bounded Git packs for isolated candidate gates."""

from __future__ import annotations

import ctypes
import errno
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
    parent_device: int
    parent_inode: int
    parent_mode: int
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
            ("repository pack parent device", self.parent_device),
            ("repository pack parent inode", self.parent_inode),
        ):
            if type(value) is not int or value <= 0:
                raise GateProviderError(f"{label} must be a positive integer")
        if type(self.mode) is not int or self.mode != 0o444:
            raise GateProviderError("repository pack mode must be 0444")
        if type(self.parent_mode) is not int or self.parent_mode != 0o700:
            raise GateProviderError("repository pack parent mode must be 0700")
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
    parent_identity = _private_directory_identity(parent)
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

    staging_name = f".autoform-gate-pack-{secrets.token_hex(16)}.stage"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        parent_descriptor = _open_bound_private_directory(parent, parent_identity)
    except OSError as error:
        raise GateProviderError("repository pack directory cannot be opened safely") from error
    try:
        descriptor = os.open(staging_name, flags, 0o600, dir_fd=parent_descriptor)
    except OSError as error:
        _close_descriptor(parent_descriptor, label="repository pack directory")
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
        try:
            os.fsync(descriptor)
            info = os.fstat(descriptor)
        except OSError as error:
            raise GateProviderError("repository pack destination cannot be synchronized") from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
            raise GateProviderError("repository pack destination changed while it was written")
        try:
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            info = os.fstat(descriptor)
        except OSError as error:
            raise GateProviderError("repository pack destination cannot be finalized") from error
        if _directory_identity(root) != repository_identity:
            raise GateProviderError("repository root changed while its pack was created")
        _fsync_descriptor(parent_descriptor, label="repository pack directory")
        _publish_repository_pack(
            parent_descriptor,
            staging_name,
            target.name,
            descriptor,
            info,
        )
        try:
            opened_parent = os.fstat(parent_descriptor)
        except OSError as error:
            raise GateProviderError("repository pack directory cannot be revalidated") from error
        if (
            opened_parent.st_dev,
            opened_parent.st_ino,
            stat.S_IMODE(opened_parent.st_mode),
        ) != parent_identity or _private_directory_identity(parent) != parent_identity:
            raise GateProviderError("repository pack directory changed during publication")
    finally:
        try:
            _close_descriptor(descriptor, label="repository pack")
        finally:
            _close_descriptor(parent_descriptor, label="repository pack directory")
    pack = RepositoryPack(
        path=os.fspath(target),
        sha256=digest.hexdigest(),
        size=size,
        device=info.st_dev,
        inode=info.st_ino,
        mode=stat.S_IMODE(info.st_mode),
        parent_device=parent_identity[0],
        parent_inode=parent_identity[1],
        parent_mode=parent_identity[2],
        object_format=object_format,
        base_oid=base_oid,
        candidate_oid=candidate_oid,
    )
    verify_repository_pack(pack)
    return pack


def verify_repository_pack(pack: RepositoryPack) -> None:
    """Reopen and hash the exact pack pathname before and after Docker create."""

    path = Path(pack.path)
    parent = _private_directory(path.parent, label="repository pack directory")
    if parent != path.parent:
        raise GateProviderError("repository pack parent path changed")
    expected_parent = (pack.parent_device, pack.parent_inode, pack.parent_mode)
    if _private_directory_identity(parent) != expected_parent:
        raise GateProviderError("repository pack parent identity changed")
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
    verification_failed = False
    try:
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
        except OSError as error:
            raise GateProviderError("repository pack cannot be verified safely") from error
    except BaseException:
        verification_failed = True
        raise
    finally:
        try:
            _close_descriptor(descriptor, label="repository pack")
        except GateProviderError:
            if not verification_failed:
                raise
    if size != pack.size or digest.hexdigest() != pack.sha256:
        raise GateProviderError("repository pack content changed")
    if _regular_file_identity(path)[:4] != expected:
        raise GateProviderError("repository pack pathname changed")
    if _private_directory_identity(parent) != expected_parent:
        raise GateProviderError("repository pack parent identity changed")


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
    operation_failed = False
    try:
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
    except BaseException:
        operation_failed = True
        raise
    finally:
        _close_process_pipes(process, preserve_error=operation_failed)
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
    try:
        if process.poll() is not None:
            return
    except Exception:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=1)
    except Exception:
        pass


def _close_process_pipes(
    process: subprocess.Popen[bytes],
    *,
    preserve_error: bool,
) -> None:
    first_error: OSError | None = None
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is not None and not preserve_error:
        raise GateProviderError("repository pack process pipes could not be closed") from first_error


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
    try:
        if path.is_symlink():
            raise GateProviderError(f"{label} must not be a symbolic link")
        resolved = path.expanduser().resolve(strict=True)
        info = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    if not stat.S_ISDIR(info.st_mode):
        raise GateProviderError(f"{label} must be a real directory")
    return resolved


def _private_directory(path: Path, *, label: str) -> Path:
    resolved = _real_directory(path, label=label)
    try:
        info = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise GateProviderError(f"{label} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise GateProviderError(f"{label} must have mode 0700")
    return resolved


def _private_directory_identity(path: Path) -> tuple[int, int, int]:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError("repository pack directory cannot be revalidated") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        raise GateProviderError("repository pack directory identity is unsafe")
    return info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)


def _open_bound_private_directory(path: Path, expected: tuple[int, int, int]) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
    except OSError:
        _close_descriptor(descriptor, label="repository pack directory")
        raise
    observed = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
    if (
        not stat.S_ISDIR(info.st_mode)
        or observed != expected
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        _close_descriptor(descriptor, label="repository pack directory")
        raise GateProviderError("repository pack directory changed while it was opened")
    return descriptor


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
    try:
        if path.is_symlink():
            raise GateProviderError("repository pack pathname became a symbolic link")
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
    parent_descriptor: int,
    staging_name: str,
    target_name: str,
    descriptor: int,
    prepared: os.stat_result,
) -> None:
    expected = (
        prepared.st_dev,
        prepared.st_ino,
        prepared.st_size,
        stat.S_IMODE(prepared.st_mode),
    )
    try:
        staged = os.stat(staging_name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        raise GateProviderError("repository pack staging entry cannot be revalidated") from error
    if (
        staged.st_dev,
        staged.st_ino,
        staged.st_size,
        stat.S_IMODE(staged.st_mode),
    ) != expected or staged.st_nlink != 1:
        raise GateProviderError("repository pack staging identity changed")
    try:
        opened = os.fstat(descriptor)
    except OSError as error:
        raise GateProviderError("repository pack staging descriptor cannot be revalidated") from error
    if (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        stat.S_IMODE(opened.st_mode),
    ) != expected or opened.st_nlink != 1:
        raise GateProviderError("repository pack staging descriptor changed")

    _rename_noreplace(parent_descriptor, staging_name, target_name)
    _fsync_descriptor(parent_descriptor, label="repository pack directory")
    try:
        published = os.stat(target_name, dir_fd=parent_descriptor, follow_symlinks=False)
        after = os.fstat(descriptor)
    except OSError as error:
        raise GateProviderError("published repository pack cannot be revalidated") from error
    for observed in (published, after):
        if (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            stat.S_IMODE(observed.st_mode),
        ) != expected or observed.st_nlink != 1:
            raise GateProviderError("published repository pack has the wrong identity")
    try:
        os.stat(staging_name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise GateProviderError("repository pack staging absence cannot be verified") from error
    else:
        raise GateProviderError("repository pack staging entry remains after publication")


def _rename_noreplace(parent_descriptor: int, source: str, target: str) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        raise GateProviderError("atomic repository pack publication is unavailable") from error
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if hasattr(library, "renameatx_np"):
        function = library.renameatx_np
        flag = 0x00000004
    elif hasattr(library, "renameat2"):
        function = library.renameat2
        flag = 1
    else:
        raise GateProviderError("atomic repository pack publication is unavailable")
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        parent_descriptor,
        source_bytes,
        parent_descriptor,
        target_bytes,
        flag,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise GateProviderError("repository pack destination cannot be created exclusively")
    if error in {errno.ENOSYS, errno.ENOTSUP}:
        raise GateProviderError("atomic repository pack publication is unavailable")
    raise GateProviderError("repository pack cannot be published atomically") from OSError(
        error,
        os.strerror(error),
        target,
    )


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


def _fsync_descriptor(descriptor: int, *, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise GateProviderError(f"{label} could not be synchronized") from error


def _close_descriptor(descriptor: int, *, label: str) -> None:
    try:
        os.close(descriptor)
    except OSError as error:
        raise GateProviderError(f"{label} descriptor could not be closed") from error


def _diagnostic(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()[:1000] or "no diagnostic"


__all__ = ["RepositoryPack", "prepare_repository_pack", "verify_repository_pack"]
