from __future__ import annotations

import base64
import hashlib
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import autoform_worker.gate_evaluator as evaluator_module
import autoform_worker.gate_pack as gate_pack_module
from autoform_worker.gate_evaluator import (
    _copy_verified_pack,
    _decode_request,
    _materialize_worktrees,
    evaluate_gate_request,
)
from autoform_worker.gate_pack import RepositoryPack, prepare_repository_pack, verify_repository_pack
from autoform_worker.gate_provider import GateInvocationRequest, GateProviderError
from autoform_worker.gates import CandidateGateResult, _work_item_sha256
from autoform_worker.scheduler import WorkItem, WorkPhase


class _FakePackPipe:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.closed = False
        self.close_attempts = 0
        self._fail_close = fail_close

    def write(self, content: bytes) -> int:
        return len(content)

    def fileno(self) -> int:
        return 10

    def close(self) -> None:
        self.close_attempts += 1
        if self._fail_close:
            raise OSError("injected pipe close failure")
        self.closed = True


class _FakePackProcess:
    def __init__(
        self,
        *,
        failed_pipe: str | None = None,
        poll_error: Exception | None = None,
    ) -> None:
        self.stdin = _FakePackPipe(fail_close=failed_pipe == "stdin")
        self.stdout = _FakePackPipe(fail_close=failed_pipe == "stdout")
        self.stderr = _FakePackPipe(fail_close=failed_pipe == "stderr")
        self.pid = 12345
        self._poll_error = poll_error
        self.kill_attempts = 0

    def poll(self) -> int:
        if self._poll_error is not None:
            raise self._poll_error
        return 0

    def kill(self) -> None:
        self.kill_attempts += 1

    def wait(self, *, timeout: float) -> int:
        return 0


class _FakeSelectorKey:
    def __init__(self, fileobj: _FakePackPipe, data: str) -> None:
        self.fileobj = fileobj
        self.data = data


class _FakePackSelector:
    def __init__(
        self,
        *,
        failed_registration: int | None = None,
        fail_close: bool = False,
    ) -> None:
        self._failed_registration = failed_registration
        self._fail_close = fail_close
        self._keys: list[_FakeSelectorKey] = []
        self.registration_attempts = 0
        self.close_attempts = 0

    def register(self, fileobj: _FakePackPipe, _events: int, data: str) -> None:
        self.registration_attempts += 1
        if self.registration_attempts == self._failed_registration:
            raise OSError("injected selector registration failure")
        self._keys.append(_FakeSelectorKey(fileobj, data))

    def get_map(self) -> dict[int, _FakeSelectorKey]:
        return {index: key for index, key in enumerate(self._keys)}

    def select(self, _timeout: float) -> list[tuple[_FakeSelectorKey, int]]:
        return [(self._keys[0], 1)]

    def unregister(self, fileobj: _FakePackPipe) -> None:
        self._keys = [key for key in self._keys if key.fileobj is not fileobj]

    def close(self) -> None:
        self.close_attempts += 1
        if self._fail_close:
            raise OSError("injected selector close failure")


def _git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return completed.stdout


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--object-format=sha1")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Autoform",
        "-c",
        "user.email=autoform@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "base",
    )
    base_oid = _git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "tracked.txt").write_text("candidate\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Autoform",
        "-c",
        "user.email=autoform@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "candidate",
    )
    candidate_oid = _git(repository, "rev-parse", "HEAD").decode().strip()
    return repository, base_oid, candidate_oid


def _repository_pack(tmp_path: Path) -> tuple[Path, str, str]:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    content = _git(
        repository,
        "--no-replace-objects",
        "pack-objects",
        "--stdout",
        "--revs",
        "--no-reuse-delta",
        "--no-reuse-object",
        "--no-thin",
        "--no-include-tag",
        "--window=0",
        input_bytes=f"{base_oid}\n{candidate_oid}\n".encode("ascii"),
    )
    pack = (tmp_path / "source.pack").resolve()
    pack.write_bytes(content)
    pack.chmod(0o444)
    return pack, base_oid, candidate_oid


def _unrelated_repository_pack(tmp_path: Path) -> tuple[Path, str, str, Path]:
    repository, base_oid, _candidate_oid = _repository(tmp_path)
    _git(repository, "checkout", "--quiet", "--orphan", "unrelated")
    _git(repository, "rm", "--quiet", "-rf", ".")
    (repository / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    _git(
        repository,
        "-c",
        "user.name=Autoform",
        "-c",
        "user.email=autoform@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "unrelated",
    )
    unrelated_oid = _git(repository, "rev-parse", "HEAD").decode().strip()
    content = _git(
        repository,
        "--no-replace-objects",
        "pack-objects",
        "--stdout",
        "--revs",
        "--no-reuse-delta",
        "--no-reuse-object",
        "--no-thin",
        "--no-include-tag",
        "--window=0",
        input_bytes=f"{base_oid}\n{unrelated_oid}\n".encode("ascii"),
    )
    pack = (tmp_path / "unrelated.pack").resolve()
    pack.write_bytes(content)
    pack.chmod(0o444)
    return pack, base_oid, unrelated_oid, repository


def test_host_prepares_and_revalidates_bounded_repository_pack(tmp_path: Path) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)

    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )

    assert pack.path == str(state / "invocation.pack")
    assert pack.base_oid == base_oid
    assert pack.candidate_oid == candidate_oid
    assert pack.object_format == "sha1"
    assert pack.size > 0
    assert Path(pack.path).stat().st_mode & 0o777 == 0o444
    assert Path(pack.path).stat().st_nlink == 1
    assert not list(state.glob(".autoform-gate-pack-*.stage"))
    assert hashlib.sha256(Path(pack.path).read_bytes()).hexdigest() == pack.sha256
    verify_repository_pack(pack)

    request = _request(Path(pack.path), base_oid, candidate_oid)
    work_root = (tmp_path / "work").resolve()
    work_root.mkdir()
    copied, _identity = _copy_verified_pack(request, Path(pack.path), work_root)
    base, candidate = _materialize_worktrees(request, copied, work_root)
    assert (base / "tracked.txt").read_text() == "base\n"
    assert (candidate / "tracked.txt").read_text() == "candidate\n"


def test_host_pack_revalidation_rejects_content_or_path_replacement(tmp_path: Path) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )
    path = Path(pack.path)
    displaced = state / "displaced.pack"
    path.rename(displaced)
    path.write_bytes(displaced.read_bytes())
    path.chmod(0o444)

    with pytest.raises(GateProviderError, match="identity"):
        verify_repository_pack(pack)


def test_host_pack_creation_fails_closed_on_limits_and_unsafe_state(tmp_path: Path) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    with pytest.raises(GateProviderError, match="byte limit"):
        prepare_repository_pack(
            repository,
            state / "too-small.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1,
            timeout_seconds=30,
        )
    assert not (state / "too-small.pack").exists()
    assert len(list(state.glob(".autoform-gate-pack-*.stage"))) == 1
    occupied = state / "occupied.pack"
    occupied.write_bytes(b"foreign")
    with pytest.raises(GateProviderError, match="created exclusively"):
        prepare_repository_pack(
            repository,
            occupied,
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )
    assert occupied.read_bytes() == b"foreign"
    state.chmod(0o755)
    with pytest.raises(GateProviderError, match="mode 0700"):
        prepare_repository_pack(
            repository,
            state / "unsafe.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )
    inside = repository / "private-state"
    inside.mkdir(mode=0o700)
    with pytest.raises(GateProviderError, match="outside"):
        prepare_repository_pack(
            repository,
            inside / "unsafe.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )


def test_host_pack_revalidation_rejects_parent_permission_drift(tmp_path: Path) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )

    state.chmod(0o755)
    try:
        with pytest.raises(GateProviderError, match="mode 0700|identity"):
            verify_repository_pack(pack)
    finally:
        state.chmod(0o700)
    displaced = tmp_path / "displaced-state"
    state.rename(displaced)
    state.mkdir(mode=0o700)
    with pytest.raises(GateProviderError, match="parent identity"):
        verify_repository_pack(pack)


@pytest.mark.parametrize("failure", ["first-fstat", "second-fstat", "read", "close"])
def test_host_pack_revalidation_wraps_descriptor_failures_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )
    original_fstat = gate_pack_module.os.fstat
    original_read = gate_pack_module.os.read
    original_close = gate_pack_module.os.close
    descriptor: int | None = None
    fstat_calls = 0
    close_calls = 0

    def injected_fstat(value: int) -> os.stat_result:
        nonlocal descriptor, fstat_calls
        if descriptor is None:
            descriptor = value
        if value == descriptor:
            fstat_calls += 1
            if failure == "first-fstat" and fstat_calls == 1:
                raise OSError("injected first fstat failure")
            if failure == "second-fstat" and fstat_calls == 2:
                raise OSError("injected second fstat failure")
        return original_fstat(value)

    def injected_read(value: int, length: int) -> bytes:
        if value == descriptor and failure == "read":
            raise OSError("injected read failure")
        return original_read(value, length)

    def injected_close(value: int) -> None:
        nonlocal close_calls, descriptor
        if value == descriptor:
            close_calls += 1
            descriptor = None
            original_close(value)
            if failure == "close":
                raise OSError("injected close failure")
            return
        original_close(value)

    monkeypatch.setattr(gate_pack_module.os, "fstat", injected_fstat)
    monkeypatch.setattr(gate_pack_module.os, "read", injected_read)
    monkeypatch.setattr(gate_pack_module.os, "close", injected_close)

    with pytest.raises(GateProviderError):
        verify_repository_pack(pack)

    assert close_calls == 1


def test_host_pack_revalidation_close_failure_does_not_mask_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )
    original_fstat = gate_pack_module.os.fstat
    original_close = gate_pack_module.os.close
    descriptor: int | None = None
    close_calls = 0

    def capture_fstat(value: int) -> os.stat_result:
        nonlocal descriptor
        descriptor = value
        return original_fstat(value)

    def fail_read(value: int, _length: int) -> bytes:
        if value == descriptor:
            raise OSError("injected primary verification failure")
        raise AssertionError("unexpected descriptor")

    def fail_close(value: int) -> None:
        nonlocal close_calls, descriptor
        if value == descriptor:
            close_calls += 1
            descriptor = None
            original_close(value)
            raise OSError("injected secondary close failure")
        original_close(value)

    monkeypatch.setattr(gate_pack_module.os, "fstat", capture_fstat)
    monkeypatch.setattr(gate_pack_module.os, "read", fail_read)
    monkeypatch.setattr(gate_pack_module.os, "close", fail_close)

    with pytest.raises(GateProviderError, match="verified safely") as captured:
        verify_repository_pack(pack)

    assert "primary verification failure" in str(captured.value.__cause__)
    assert close_calls == 1


@pytest.mark.parametrize("entry_point", ["prepare", "verify"])
def test_host_pack_public_entry_points_wrap_lstat_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )
    watched = repository if entry_point == "prepare" else Path(pack.path)
    original_lstat = Path.lstat

    def fail_lstat(path: Path) -> os.stat_result:
        if path == watched:
            raise OSError("injected lstat failure")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_lstat)

    with pytest.raises(GateProviderError, match="cannot be inspected"):
        if entry_point == "prepare":
            prepare_repository_pack(
                repository,
                state / "second.pack",
                base_oid=base_oid,
                candidate_oid=candidate_oid,
                maximum_bytes=1024 * 1024,
                timeout_seconds=30,
            )
        else:
            verify_repository_pack(pack)


@pytest.mark.parametrize(
    ("repository_path", "message"),
    [
        (
            Path("~autoform-user-that-does-not-exist-9e8844d8/source"),
            "repository root cannot be inspected",
        ),
        (Path("/tmp/\ud800"), "repository root is invalid"),
    ],
    ids=["unknown-user", "unencodable"],
)
def test_host_pack_preparation_wraps_non_os_path_failures(
    tmp_path: Path,
    repository_path: Path,
    message: str,
) -> None:
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)

    with pytest.raises(GateProviderError, match=message):
        prepare_repository_pack(
            repository_path,
            state / "invocation.pack",
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1024,
            timeout_seconds=1,
        )


def test_host_pack_preparation_wraps_ancestor_symlink_loop(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name, target_is_directory=True)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)

    with pytest.raises(GateProviderError, match="repository root cannot be inspected"):
        prepare_repository_pack(
            loop / "source",
            state / "invocation.pack",
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1024,
            timeout_seconds=1,
        )


def test_host_pack_preparation_rejects_unencodable_destination_before_staging(
    tmp_path: Path,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    target = state / "\ud800.pack"

    with pytest.raises(GateProviderError, match="repository pack path is invalid"):
        prepare_repository_pack(
            repository,
            target,
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    assert not target.exists()
    assert list(state.iterdir()) == []


def test_repository_pack_identity_rejects_unencodable_path(tmp_path: Path) -> None:
    with pytest.raises(GateProviderError, match="repository pack path must be a canonical absolute path"):
        RepositoryPack(
            path=str(tmp_path.resolve() / "\ud800.pack"),
            sha256="0" * 64,
            size=1,
            device=1,
            inode=1,
            mode=0o444,
            parent_device=1,
            parent_inode=1,
            parent_mode=0o700,
            object_format="sha1",
            base_oid="1" * 40,
            candidate_oid="2" * 40,
        )


def test_host_pack_revalidation_wraps_path_resolution_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )
    pack_path = Path(pack.path)
    original_resolve = Path.resolve

    def fail_pack_resolve(path: Path, *, strict: bool = False) -> Path:
        if path == pack_path:
            raise RuntimeError("injected path resolution failure")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_pack_resolve)

    with pytest.raises(GateProviderError, match="pathname cannot be inspected"):
        verify_repository_pack(pack)


def test_host_pack_path_policy_errors_keep_their_specific_diagnostic(tmp_path: Path) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    repository_link = tmp_path / "repository-link"
    repository_link.symlink_to(repository, target_is_directory=True)

    with pytest.raises(GateProviderError, match="repository root must not be a symbolic link"):
        prepare_repository_pack(
            repository_link,
            state / "linked.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    pack = prepare_repository_pack(
        repository,
        state / "invocation.pack",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        maximum_bytes=1024 * 1024,
        timeout_seconds=30,
    )
    path = Path(pack.path)
    displaced = state / "displaced.pack"
    path.rename(displaced)
    path.symlink_to(displaced)
    with pytest.raises(GateProviderError, match="pathname became a symbolic link"):
        verify_repository_pack(pack)


@pytest.mark.parametrize("argument", ["repository", "destination"])
@pytest.mark.parametrize("invalid_kind", ["unicode", "nul", "type"])
def test_prepare_repository_pack_wraps_invalid_paths_before_filesystem_or_git_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
    invalid_kind: str,
) -> None:
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    if invalid_kind == "unicode":
        invalid: object = f"{state}/\ud800.pack" if argument == "destination" else "\ud800"
    elif invalid_kind == "nul":
        invalid = f"{state}/embedded\0nul.pack" if argument == "destination" else "embedded\0nul"
    else:
        invalid = object()
    repository: object = invalid if argument == "repository" else tmp_path
    destination: object = invalid if argument == "destination" else state / "invocation.pack"
    monkeypatch.setattr(
        gate_pack_module,
        "_real_directory",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("filesystem work started")),
    )
    monkeypatch.setattr(
        gate_pack_module,
        "_git_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Git work started")),
    )

    with pytest.raises(GateProviderError, match="repository (root|pack path)"):
        prepare_repository_pack(
            repository,  # type: ignore[arg-type]
            destination,  # type: ignore[arg-type]
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1024,
            timeout_seconds=1,
        )

@pytest.mark.parametrize("failed_pipe", ["stdin", "stdout", "stderr"])
def test_repository_pack_stream_wraps_each_pipe_close_and_closes_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_pipe: str,
) -> None:
    process = _FakePackProcess(failed_pipe=failed_pipe)
    monkeypatch.setattr(gate_pack_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        gate_pack_module,
        "_copy_process_output",
        lambda *args, **kwargs: (4, b""),
    )

    with pytest.raises(GateProviderError):
        gate_pack_module._stream_pack(
            tmp_path,
            -1,
            hashlib.sha256(),
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1024,
            timeout=1,
        )

    assert process.stdin.close_attempts >= 1
    assert process.stdout.close_attempts == 1
    assert process.stderr.close_attempts == 1


def test_repository_pack_stream_preserves_gate_failure_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePackProcess(failed_pipe="stdout", poll_error=OSError("injected poll failure"))
    monkeypatch.setattr(gate_pack_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        gate_pack_module,
        "_copy_process_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(GateProviderError("primary gate failure")),
    )
    monkeypatch.setattr(
        gate_pack_module.os,
        "killpg",
        lambda *args, **kwargs: (_ for _ in ()).throw(ProcessLookupError()),
    )

    with pytest.raises(GateProviderError, match="primary gate failure"):
        gate_pack_module._stream_pack(
            tmp_path,
            -1,
            hashlib.sha256(),
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1024,
            timeout=1,
        )

    assert process.kill_attempts == 1
    assert process.stdin.close_attempts >= 1
    assert process.stdout.close_attempts == 1
    assert process.stderr.close_attempts == 1


def test_repository_pack_stream_closes_selector_after_second_registration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePackProcess()
    selector = _FakePackSelector(failed_registration=2)
    monkeypatch.setattr(gate_pack_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(gate_pack_module.selectors, "DefaultSelector", lambda: selector)

    with pytest.raises(GateProviderError, match="process failed"):
        gate_pack_module._stream_pack(
            tmp_path,
            -1,
            hashlib.sha256(),
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1024,
            timeout=1,
        )

    assert selector.registration_attempts == 2
    assert selector.close_attempts == 1


@pytest.mark.parametrize(
    ("primary_failure", "message"),
    [
        ("copy", "primary copy failure"),
        ("limit", "byte limit"),
        ("timeout", "timed out"),
    ],
)
def test_repository_pack_selector_close_does_not_mask_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: str,
    message: str,
) -> None:
    process = _FakePackProcess()
    selector = _FakePackSelector(fail_close=True)
    monkeypatch.setattr(gate_pack_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(gate_pack_module.selectors, "DefaultSelector", lambda: selector)
    if primary_failure != "timeout":
        monkeypatch.setattr(gate_pack_module.os, "read", lambda *args, **kwargs: b"pack")
    if primary_failure == "copy":
        monkeypatch.setattr(
            gate_pack_module,
            "_write_all",
            lambda *args, **kwargs: (_ for _ in ()).throw(GateProviderError("primary copy failure")),
        )

    with pytest.raises(GateProviderError, match=message):
        gate_pack_module._stream_pack(
            tmp_path,
            -1,
            hashlib.sha256(),
            base_oid="1" * 40,
            candidate_oid="2" * 40,
            maximum_bytes=1 if primary_failure == "limit" else 1024,
            timeout=-1 if primary_failure == "timeout" else 1,
        )

    assert selector.close_attempts == 1


@pytest.mark.parametrize("primary_failure", ["preparation", "publication", "none"])
def test_host_pack_cleanup_attempts_both_descriptors_with_stable_error_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: str,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    original_close_descriptor = gate_pack_module._close_descriptor
    close_attempts: list[str] = []

    def fail_close(descriptor: int, *, label: str) -> None:
        close_attempts.append(label)
        original_close_descriptor(descriptor, label=label)
        raise GateProviderError(f"injected {label} close failure")

    monkeypatch.setattr(gate_pack_module, "_close_descriptor", fail_close)
    if primary_failure == "preparation":
        monkeypatch.setattr(
            gate_pack_module,
            "_stream_pack",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                GateProviderError("primary preparation failure")
            ),
        )
    elif primary_failure == "publication":
        monkeypatch.setattr(
            gate_pack_module,
            "_publish_repository_pack",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                GateProviderError("primary publication failure")
            ),
        )

    expected = (
        f"primary {primary_failure} failure"
        if primary_failure != "none"
        else "injected repository pack close failure"
    )
    with pytest.raises(GateProviderError, match=expected):
        prepare_repository_pack(
            repository,
            state / "invocation.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    assert close_attempts == ["repository pack", "repository pack directory"]


def test_host_pack_staging_open_failure_precedes_parent_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    original_open = gate_pack_module.os.open
    original_close_descriptor = gate_pack_module._close_descriptor
    close_attempts: list[str] = []

    def fail_staging_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path.startswith(".autoform-gate-pack-"):
            raise OSError("primary staging open failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def fail_parent_close(descriptor: int, *, label: str) -> None:
        close_attempts.append(label)
        original_close_descriptor(descriptor, label=label)
        raise GateProviderError("secondary parent close failure")

    monkeypatch.setattr(gate_pack_module.os, "open", fail_staging_open)
    monkeypatch.setattr(gate_pack_module, "_close_descriptor", fail_parent_close)

    with pytest.raises(GateProviderError, match="destination cannot be created exclusively") as captured:
        prepare_repository_pack(
            repository,
            state / "invocation.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    assert "primary staging open failure" in str(captured.value.__cause__)
    assert close_attempts == ["repository pack directory"]


@pytest.mark.parametrize("primary_failure", ["fstat", "identity"])
def test_host_pack_bound_parent_failure_precedes_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: str,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    original_open = gate_pack_module.os.open
    original_fstat = gate_pack_module.os.fstat
    original_close_descriptor = gate_pack_module._close_descriptor
    parent_descriptor: int | None = None
    close_attempts = 0

    def capture_parent_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal parent_descriptor
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == state and dir_fd is None:
            parent_descriptor = descriptor
        return descriptor

    def fail_parent_validation(descriptor: int) -> os.stat_result:
        info = original_fstat(descriptor)
        if descriptor != parent_descriptor:
            return info
        if primary_failure == "fstat":
            raise OSError("primary bound-parent fstat failure")
        changed = list(info)
        changed[1] += 1
        return os.stat_result(changed)

    def fail_parent_close(descriptor: int, *, label: str) -> None:
        nonlocal close_attempts
        close_attempts += 1
        original_close_descriptor(descriptor, label=label)
        raise GateProviderError("secondary bound-parent close failure")

    monkeypatch.setattr(gate_pack_module.os, "open", capture_parent_open)
    monkeypatch.setattr(gate_pack_module.os, "fstat", fail_parent_validation)
    monkeypatch.setattr(gate_pack_module, "_close_descriptor", fail_parent_close)

    expected = "directory cannot be opened safely" if primary_failure == "fstat" else "changed while it was opened"
    with pytest.raises(GateProviderError, match=expected) as captured:
        prepare_repository_pack(
            repository,
            state / "invocation.pack",
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    if primary_failure == "fstat":
        assert "primary bound-parent fstat failure" in str(captured.value.__cause__)
    assert close_attempts == 1


def test_host_pack_publication_preserves_a_replaced_staging_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    target = state / "invocation.pack"
    displaced = state / "displaced.pack"
    original_rename = gate_pack_module._rename_noreplace

    def replace_before_rename(parent_descriptor: int, source: str, destination: str) -> None:
        staging = state / source
        staging.rename(displaced)
        staging.write_bytes(b"foreign")
        staging.chmod(0o444)
        original_rename(parent_descriptor, source, destination)

    monkeypatch.setattr(
        gate_pack_module,
        "_rename_noreplace",
        replace_before_rename,
    )
    with pytest.raises(GateProviderError, match="wrong identity"):
        prepare_repository_pack(
            repository,
            target,
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    assert target.read_bytes() == b"foreign"
    assert displaced.exists()


def test_host_pack_publication_preserves_ambiguous_post_rename_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, base_oid, candidate_oid = _repository(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    target = state / "invocation.pack"
    original_fsync = gate_pack_module._fsync_descriptor
    calls = 0

    def fail_second_fsync(descriptor: int, *, label: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GateProviderError("injected parent fsync failure")
        original_fsync(descriptor, label=label)

    monkeypatch.setattr(gate_pack_module, "_fsync_descriptor", fail_second_fsync)
    with pytest.raises(GateProviderError, match="injected parent fsync failure"):
        prepare_repository_pack(
            repository,
            target,
            base_oid=base_oid,
            candidate_oid=candidate_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )

    assert target.is_file()
    assert target.stat().st_nlink == 1
    assert not list(state.glob(".autoform-gate-pack-*.stage"))


def test_host_pack_publication_wraps_descriptor_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)
    staging_name = ".autoform-gate-pack-test.stage"
    target_name = "invocation.pack"
    staging = state / staging_name
    staging.write_bytes(b"pack")
    staging.chmod(0o444)
    parent_descriptor = os.open(state, os.O_RDONLY)
    descriptor = os.open(staging, os.O_RDONLY)
    prepared = os.fstat(descriptor)
    original_fstat = gate_pack_module.os.fstat

    def fail_pack_fstat(value: int) -> os.stat_result:
        if value == descriptor:
            raise OSError("injected")
        return original_fstat(value)

    monkeypatch.setattr(gate_pack_module.os, "fstat", fail_pack_fstat)
    try:
        with pytest.raises(GateProviderError, match="descriptor cannot be revalidated"):
            gate_pack_module._publish_repository_pack(
                parent_descriptor,
                staging_name,
                target_name,
                descriptor,
                prepared,
            )
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)
    assert staging.exists()
    assert not (state / target_name).exists()


def test_host_pack_rejects_zero_or_non_descendant_candidate(tmp_path: Path) -> None:
    pack, base_oid, unrelated_oid, repository = _unrelated_repository_pack(tmp_path)
    state = (tmp_path / "state").resolve()
    state.mkdir(mode=0o700)

    with pytest.raises(GateProviderError, match="all-zero"):
        prepare_repository_pack(
            repository,
            state / "zero.pack",
            base_oid="0" * len(base_oid),
            candidate_oid=unrelated_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )
    with pytest.raises(GateProviderError, match="Git input verification failed"):
        prepare_repository_pack(
            repository,
            state / "unrelated-output.pack",
            base_oid=base_oid,
            candidate_oid=unrelated_oid,
            maximum_bytes=1024 * 1024,
            timeout_seconds=30,
        )
    assert not (state / "zero.pack").exists()
    assert not (state / "unrelated-output.pack").exists()

    request = _request(pack, base_oid, unrelated_oid)
    work_root = (tmp_path / "work-unrelated").resolve()
    work_root.mkdir()
    with pytest.raises(GateProviderError, match="isolated Git command failed"):
        _materialize_worktrees(request, pack, work_root)


def _request(pack: Path, base_oid: str, candidate_oid: str, *, work_item_sha256: str = "c" * 64) -> GateInvocationRequest:
    content = pack.read_bytes()
    return GateInvocationRequest(
        invocation_id="7" * 64,
        run_id="run-1",
        attempt_id="attempt-1",
        base_oid=base_oid,
        candidate_oid=candidate_oid,
        node_id="chapter/result",
        article_id="af_0123456789abcdef01234567",
        phase="proof",
        attempt=1,
        source_revision="d" * 64,
        source_contract_sha256="a" * 64,
        protected_roadmap_sha256="b" * 64,
        work_item_sha256=work_item_sha256,
        repository_pack_sha256=hashlib.sha256(content).hexdigest(),
        repository_pack_bytes=len(content),
        result_bytes_limit=4 * 1024**2,
        provider_config_sha256="e" * 64,
    )


def test_repository_pack_is_copied_rehashed_and_materialized(tmp_path: Path) -> None:
    pack, base_oid, candidate_oid = _repository_pack(tmp_path)
    request = _request(pack, base_oid, candidate_oid)
    work_root = (tmp_path / "work").resolve()
    work_root.mkdir()

    copied, identity = _copy_verified_pack(request, pack, work_root)
    base, candidate = _materialize_worktrees(request, copied, work_root)

    assert identity[2] == len(pack.read_bytes())
    assert copied.read_bytes() == pack.read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o400
    assert (base / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert (candidate / "tracked.txt").read_text(encoding="utf-8") == "candidate\n"
    assert _git(base, "rev-parse", "HEAD") == f"{base_oid}\n".encode()
    assert _git(candidate, "rev-parse", "HEAD") == f"{candidate_oid}\n".encode()


@pytest.mark.parametrize("change", ["size", "digest", "hardlink", "mode", "symlink"])
def test_repository_pack_copy_rejects_identity_or_content_drift(
    tmp_path: Path,
    change: str,
) -> None:
    pack, base_oid, candidate_oid = _repository_pack(tmp_path)
    request = _request(pack, base_oid, candidate_oid)
    if change == "size":
        request = replace(request, repository_pack_bytes=request.repository_pack_bytes + 1)
    elif change == "digest":
        request = replace(request, repository_pack_sha256="0" * 64)
    elif change == "hardlink":
        os.link(pack, tmp_path / "other.pack")
    elif change == "mode":
        pack.chmod(0o400)
    else:
        target = tmp_path / "target.pack"
        pack.rename(target)
        pack.symlink_to(target)
    work_root = (tmp_path / "work").resolve()
    work_root.mkdir()

    with pytest.raises(GateProviderError):
        _copy_verified_pack(request, pack, work_root)


def test_repository_pack_copy_detects_a_mid_copy_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack, base_oid, candidate_oid = _repository_pack(tmp_path)
    request = _request(pack, base_oid, candidate_oid)
    work_root = (tmp_path / "work").resolve()
    work_root.mkdir()
    original_write = evaluator_module._write_all
    changed = False

    def mutate_source(descriptor: int, content: bytes) -> None:
        nonlocal changed
        original_write(descriptor, content)
        if not changed:
            changed = True
            pack.chmod(0o600)
            pack.write_bytes(b"x" * request.repository_pack_bytes)
            pack.chmod(0o444)

    monkeypatch.setattr(evaluator_module, "_write_all", mutate_source)

    with pytest.raises(GateProviderError, match="changed|digest"):
        _copy_verified_pack(request, pack, work_root)


@dataclass(frozen=True)
class _Node:
    id: str = "chapter/result"
    article_id: str = "af_0123456789abcdef01234567"

    def as_dict(self) -> dict[str, object]:
        return {"article_id": self.article_id, "id": self.id}


class _Runtime:
    source_revision = "d" * 64

    def __init__(self, node: _Node) -> None:
        self.node = node

    def get(self, node_id: str) -> _Node | None:
        return self.node if node_id == self.node.id else None


@dataclass(frozen=True)
class _ExecutionInput:
    runtime: _Runtime
    source_contract_sha256: str = "a" * 64


def test_gate_evaluator_binds_materialized_input_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack, base_oid, candidate_oid = _repository_pack(tmp_path)
    node = _Node()
    item = WorkItem(
        node=node,  # type: ignore[arg-type]
        phase=WorkPhase.PROOF,
        attempt=1,
        source_revision="d" * 64,
        source_contract_sha256="a" * 64,
        protected_roadmap_sha256="b" * 64,
    )
    request = _request(
        pack,
        base_oid,
        candidate_oid,
        work_item_sha256=_work_item_sha256(item),
    )
    monkeypatch.setattr(
        evaluator_module,
        "load_execution_input",
        lambda *_args, **_kwargs: _ExecutionInput(_Runtime(node)),
    )
    observed: list[tuple[Path, Path, WorkItem]] = []

    def gate_runner(base: str | Path, candidate: str | Path, received: WorkItem) -> CandidateGateResult:
        observed.append((Path(base), Path(candidate), received))
        return CandidateGateResult(
            passed=False,
            node_id=node.id,
            article_id=node.article_id,
            phase="proof",
            attempt=1,
            source_revision="d" * 64,
            source_contract_sha256="a" * 64,
            protected_roadmap_sha256="b" * 64,
            workspace_project_id=item.workspace_project_id,
            workspace_project_binding_sha256=item.workspace_project_binding_sha256,
            blueprint_path=item.blueprint_path,
            work_item_sha256=request.work_item_sha256,
            base_execution_input_sha256=None,
            candidate_execution_input_sha256=None,
            base_toolchain=None,
            candidate_toolchain=None,
            checks=(),
        )

    work_root = (tmp_path / "work").resolve()
    work_root.mkdir()
    result = evaluate_gate_request(
        request,
        input_pack=pack,
        work_root=work_root,
        gate_runner=gate_runner,
    )

    assert result.passed is False
    assert len(observed) == 1
    assert observed[0][2] == item
    assert (observed[0][0] / "tracked.txt").read_text() == "base\n"
    assert (observed[0][1] / "tracked.txt").read_text() == "candidate\n"


def test_gate_evaluator_rejects_wrong_work_item_before_running_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack, base_oid, candidate_oid = _repository_pack(tmp_path)
    request = _request(pack, base_oid, candidate_oid)
    monkeypatch.setattr(
        evaluator_module,
        "load_execution_input",
        lambda *_args, **_kwargs: _ExecutionInput(_Runtime(_Node())),
    )
    called = False

    def gate_runner(*_args: object) -> CandidateGateResult:
        nonlocal called
        called = True
        raise AssertionError("gate runner must not run")

    work_root = (tmp_path / "work").resolve()
    work_root.mkdir()
    with pytest.raises(GateProviderError, match="work item"):
        evaluate_gate_request(
            request,
            input_pack=pack,
            work_root=work_root,
            gate_runner=gate_runner,
        )
    assert called is False


def test_gate_request_argument_requires_canonical_strict_base64(tmp_path: Path) -> None:
    pack, base_oid, candidate_oid = _repository_pack(tmp_path)
    request = _request(pack, base_oid, candidate_oid)
    encoded = base64.urlsafe_b64encode(request.evidence_bytes()).decode("ascii")

    assert _decode_request(encoded) == request
    with pytest.raises(GateProviderError):
        _decode_request(encoded + "!")
    noncanonical = base64.urlsafe_b64encode(b" " + request.evidence_bytes()).decode("ascii")
    with pytest.raises(GateProviderError):
        _decode_request(noncanonical)
    assert encoded.endswith("0=")
    alternative = encoded[:-2] + "1="
    assert base64.urlsafe_b64decode(alternative) == request.evidence_bytes()
    with pytest.raises(GateProviderError, match="canonical URL-safe base64"):
        _decode_request(alternative)
