"""In-container evaluator for one immutable candidate-gate invocation."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from autoform_cli.execution_input import load_execution_input

from .gate_provider import GateInvocationRequest, GateProviderError, encode_gate_result_frame
from .gates import CandidateGateResult, _work_item_sha256, run_candidate_gates
from .scheduler import WorkItem, WorkPhase


_INPUT_PACK = Path("/autoform/input/repository.pack")
_WORK_ROOT = Path("/autoform/work")
_MAX_GIT_OUTPUT = 64 * 1024
_GIT_TIMEOUT_SECONDS = 120
_PACK_ID = re.compile(rb"(?:pack\t)?(?P<oid>[0-9a-f]{40}|[0-9a-f]{64})\n")

GateEvaluator = Callable[[str | Path, str | Path, WorkItem], CandidateGateResult]


def evaluate_gate_request(
    request: GateInvocationRequest,
    *,
    input_pack: Path = _INPUT_PACK,
    work_root: Path = _WORK_ROOT,
    gate_runner: GateEvaluator = run_candidate_gates,
) -> CandidateGateResult:
    """Copy, rehash, materialize, and evaluate one exact repository pack."""

    root = _empty_real_directory(work_root, label="gate work root")
    copied_pack, source_identity = _copy_verified_pack(request, input_pack, root)
    base, candidate = _materialize_worktrees(request, copied_pack, root)
    if _pack_identity(input_pack) != source_identity:
        raise GateProviderError("repository pack changed while worktrees were materialized")
    if _hash_file(input_pack, maximum=request.repository_pack_bytes) != request.repository_pack_sha256:
        raise GateProviderError("repository pack changed while worktrees were materialized")

    execution_input = load_execution_input(base, lean_root=base)
    node = execution_input.runtime.get(request.node_id)
    if node is None or node.article_id != request.article_id:
        raise GateProviderError("repository pack does not contain the requested roadmap node")
    if execution_input.runtime.source_revision != request.source_revision:
        raise GateProviderError("repository pack does not match the requested source revision")
    if execution_input.source_contract_sha256 != request.source_contract_sha256:
        raise GateProviderError("repository pack does not match the requested source contract")
    item = WorkItem(
        node=node,
        phase=WorkPhase(request.phase),
        attempt=request.attempt,
        source_revision=request.source_revision,
        source_contract_sha256=request.source_contract_sha256,
        protected_roadmap_sha256=request.protected_roadmap_sha256,
    )
    if _work_item_sha256(item) != request.work_item_sha256:
        raise GateProviderError("repository pack does not match the requested work item")

    result = gate_runner(base, candidate, item)
    if not isinstance(result, CandidateGateResult):
        raise GateProviderError("gate evaluator returned an invalid result")
    if (
        result.node_id != request.node_id
        or result.article_id != request.article_id
        or result.phase != request.phase
        or result.attempt != request.attempt
        or result.source_revision != request.source_revision
        or result.source_contract_sha256 != request.source_contract_sha256
        or result.protected_roadmap_sha256 != request.protected_roadmap_sha256
        or result.work_item_sha256 != request.work_item_sha256
    ):
        raise GateProviderError("gate evaluator result does not match its invocation")
    if len(result.evidence_bytes()) > request.result_bytes_limit:
        raise GateProviderError("gate evaluator result exceeds its configured limit")
    return result


def _empty_real_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise GateProviderError(f"{label} must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    if resolved != path or not stat.S_ISDIR(info.st_mode):
        raise GateProviderError(f"{label} must be one canonical real directory")
    try:
        if any(path.iterdir()):
            raise GateProviderError(f"{label} must start empty")
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    return path


def _pack_identity(path: Path) -> tuple[int, int, int, int, int]:
    if path.is_symlink():
        raise GateProviderError("repository pack must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError("repository pack cannot be inspected") from error
    if (
        resolved != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise GateProviderError("repository pack must be one private regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode)


def _copy_verified_pack(
    request: GateInvocationRequest,
    source: Path,
    work_root: Path,
) -> tuple[Path, tuple[int, int, int, int, int]]:
    before = _pack_identity(source)
    if before[2] != request.repository_pack_bytes:
        raise GateProviderError("repository pack size does not match its invocation")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    destination = work_root / "repository.pack"
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        destination_flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as error:
        raise GateProviderError("repository pack cannot be opened for an isolated copy") from error
    try:
        destination_fd = os.open(destination, destination_flags, 0o600)
    except OSError as error:
        os.close(source_fd)
        raise GateProviderError("repository pack cannot be opened for an isolated copy") from error
    source_digest = hashlib.sha256()
    copied = 0
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino, opened.st_size) != before[:3]:
            raise GateProviderError("repository pack changed while it was opened")
        while True:
            chunk = os.read(source_fd, min(1024 * 1024, request.repository_pack_bytes + 1 - copied))
            if not chunk:
                break
            copied += len(chunk)
            if copied > request.repository_pack_bytes:
                raise GateProviderError("repository pack exceeds its invocation size")
            source_digest.update(chunk)
            _write_all(destination_fd, chunk)
        if copied != request.repository_pack_bytes:
            raise GateProviderError("repository pack size changed while it was copied")
        os.fsync(destination_fd)
        destination_info = os.fstat(destination_fd)
        if not stat.S_ISREG(destination_info.st_mode) or destination_info.st_size != copied:
            raise GateProviderError("copied repository pack has an invalid identity")
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    try:
        destination.chmod(0o400)
    except OSError as error:
        raise GateProviderError("copied repository pack cannot be made read-only") from error
    if source_digest.hexdigest() != request.repository_pack_sha256:
        raise GateProviderError("repository pack digest does not match its invocation")
    if _pack_identity(source) != before:
        raise GateProviderError("repository pack changed while it was copied")
    if _hash_file(destination, maximum=request.repository_pack_bytes) != request.repository_pack_sha256:
        raise GateProviderError("copied repository pack does not match its invocation")
    return destination, before


def _hash_file(path: Path, *, maximum: int) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GateProviderError("repository pack cannot be reopened safely") from error
    try:
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise GateProviderError("repository pack exceeds its invocation size")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if size != maximum:
        raise GateProviderError("repository pack size does not match its invocation")
    return digest.hexdigest()


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except OSError as error:
            raise GateProviderError("gate evidence could not be written") from error
        if written <= 0:
            raise GateProviderError("gate evidence could not be written")
        offset += written


def _materialize_worktrees(
    request: GateInvocationRequest,
    pack: Path,
    work_root: Path,
) -> tuple[Path, Path]:
    object_format = "sha1" if len(request.base_oid) == 40 else "sha256"
    repository = work_root / "repository"
    base = work_root / "base"
    candidate = work_root / "candidate"
    temporary = work_root / "tmp"
    temporary.mkdir(mode=0o700)
    _run_git(["init", "--quiet", f"--object-format={object_format}", str(repository)], cwd=work_root)
    with pack.open("rb") as stream:
        indexed = _run_git(
            ["index-pack", "--stdin", "--strict"],
            cwd=repository,
            stdin=stream,
        )
    match = _PACK_ID.fullmatch(indexed.stdout)
    if match is None or len(match.group("oid")) != len(request.base_oid):
        raise GateProviderError("git index-pack returned an invalid pack identity")
    for label, oid in (("base", request.base_oid), ("candidate", request.candidate_oid)):
        resolved = _run_git(
            ["rev-parse", "--verify", f"{oid}^{{commit}}"],
            cwd=repository,
        ).stdout
        if resolved != f"{oid}\n".encode("ascii"):
            raise GateProviderError(f"repository pack does not contain the exact {label} commit")
    _run_git(["worktree", "add", "--detach", str(base), request.base_oid], cwd=repository)
    _run_git(
        ["worktree", "add", "--detach", str(candidate), request.candidate_oid],
        cwd=repository,
    )
    for label, path, oid in (
        ("base", base, request.base_oid),
        ("candidate", candidate, request.candidate_oid),
    ):
        resolved = _run_git(["rev-parse", "HEAD"], cwd=path).stdout
        dirty = _run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=path,
        ).stdout
        if resolved != f"{oid}\n".encode("ascii") or dirty:
            raise GateProviderError(f"materialized {label} worktree does not match its commit")
    return base, candidate


def _run_git(
    arguments: Sequence[str],
    *,
    cwd: Path,
    stdin: object | None = None,
) -> subprocess.CompletedProcess[bytes]:
    temporary = cwd / "tmp"
    if not temporary.is_dir():
        temporary = cwd.parent / "tmp"
    if not temporary.is_dir():
        raise GateProviderError("isolated Git temporary directory is unavailable")
    environment = {
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_GRAFT_FILE": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "PATH": os.defpath,
        "TMPDIR": str(temporary),
    }
    command = [
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
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateProviderError(f"isolated Git command failed: {type(error).__name__}") from error
    if len(completed.stdout) > _MAX_GIT_OUTPUT or len(completed.stderr) > _MAX_GIT_OUTPUT:
        raise GateProviderError("isolated Git command produced excessive output")
    if completed.returncode != 0:
        raise GateProviderError(f"isolated Git command failed with exit {completed.returncode}")
    return completed


def _decode_request(value: str) -> GateInvocationRequest:
    if not isinstance(value, str) or not value or len(value) > 128 * 1024:
        raise GateProviderError("gate invocation argument has an invalid size")
    try:
        content = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise GateProviderError("gate invocation argument is not strict URL-safe base64") from error
    request = GateInvocationRequest.from_bytes(content)
    if request.evidence_bytes() != content:
        raise GateProviderError("gate invocation argument is not canonical evidence")
    return request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoform-gate-evaluator")
    parser.add_argument("--request-base64", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        request = _decode_request(args.request_base64)
        result = evaluate_gate_request(request)
        evidence = result.evidence_bytes()
        if len(evidence) > request.result_bytes_limit:
            raise GateProviderError("gate evaluator result exceeds its configured limit")
        _write_all(sys.stdout.fileno(), encode_gate_result_frame(evidence))
        return 0
    except GateProviderError as error:
        detail = f"gate evaluator rejected invocation: {error}\n".encode("utf-8")[:4096]
        try:
            _write_all(sys.stderr.fileno(), detail)
        except GateProviderError:
            pass
        return 2
    except Exception as error:
        detail = f"gate evaluator failed: {type(error).__name__}\n".encode("ascii")
        try:
            _write_all(sys.stderr.fileno(), detail)
        except GateProviderError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["evaluate_gate_request", "main"]
