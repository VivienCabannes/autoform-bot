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
from autoform_worker.gate_pack import prepare_repository_pack, verify_repository_pack
from autoform_worker.gate_provider import GateInvocationRequest, GateProviderError
from autoform_worker.gates import CandidateGateResult, _work_item_sha256
from autoform_worker.scheduler import WorkItem, WorkPhase


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
