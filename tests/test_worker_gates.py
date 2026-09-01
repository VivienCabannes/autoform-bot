from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.artifact_audit import RootPackageAudit, prepare_root_package_audit
from autoform_cli.audit import AuditResult
from autoform_cli.execution_input import EXECUTION_INPUT_SCHEMA, ExecutionInput
from autoform_cli.runtime import (
    RUNTIME_AUTHORITY,
    RUNTIME_SCHEMA,
    RuntimeAssertions,
    RuntimeGraph,
    RuntimeLeanTarget,
    RuntimeNode,
    RuntimeStatus,
)
from autoform_worker.executor import _protected_roadmap_sha256
from autoform_worker.gates import (
    CANDIDATE_GATE_EVIDENCE_SCHEMA,
    CandidateGateError,
    _StreamCapture,
    _command_evidence,
    _invoke,
    fingerprint_toolchain,
    run_candidate_gates,
    scrubbed_subprocess_environment,
)
from autoform_worker.scheduler import WorkItem, WorkPhase
from servers.prover.verify import Baseline, VerifyResult, verify_candidate_trust
from servers.prover.verify import (
    _declaration_contexts,
    _relevant_files,
    verify_candidate_static,
)


def _node(*, proved: bool = False) -> RuntimeNode:
    return RuntimeNode(
        id="result",
        article_id="af_0123456789abcdef01234567",
        title="Result",
        article_path="blueprint/roadmap/result.md",
        parent=None,
        depth=0,
        declaration="theorem",
        formalizable=True,
        dispatchable=True,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(True, proved, False),
        status=RuntimeStatus(
            "proved" if proved else "can_prove",
            False,
            not proved,
            True,
            proved,
            proved,
            False,
        ),
        origin="derived",
        source_targets=(),
        lean_targets=(RuntimeLeanTarget("result", "Main.lean"),),
        mathlib=False,
        mathlib_declarations=(),
        mathlib_file=None,
        source_sha256="1" * 64 if not proved else "2" * 64,
    )


def _execution_input(node: RuntimeNode, revision: str) -> ExecutionInput:
    runtime = RuntimeGraph(
        RUNTIME_SCHEMA,
        RUNTIME_AUTHORITY,
        revision,
        "blueprint",
        (node,),
        1,
        1,
        1,
        0,
        0,
    )
    return ExecutionInput(
        schema=EXECUTION_INPUT_SCHEMA,
        runtime=runtime,
        runtime_sha256="3" * 64,
        coverage_schema="autoform-source-coverage/v2",
        coverage_path="blueprint/coverage/coverage.json",
        coverage_sha256="4" * 64,
        artifact_path="blueprint/sources/source.txt",
        artifact_sha256="5" * 64,
        units=(),
        node_bindings=(),
        authority_sha256="6" * 64,
        lean_source_revision="7" * 64,
    )


def _project(path: Path, *, package: str = "Fixture") -> None:
    (path / "blueprint/roadmap").mkdir(parents=True)
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
    (path / "lakefile.toml").write_text(
        f'name = "{package}"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "{package}"\n',
        encoding="utf-8",
    )
    (path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "lakeDir": ".lake",
                "name": package,
                "packages": [],
                "packagesDir": ".lake/packages",
                "version": "1.2.0",
            }
        ),
        encoding="utf-8",
    )


class FakeRunner:
    def __init__(self, *, fail: tuple[str, ...] | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv, **kwargs):
        command = list(argv)
        self.calls.append((command, kwargs))
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert "ELAN_TOOLCHAIN" not in kwargs["env"]
        assert "LEAN_PATH" not in kwargs["env"]
        assert "SSH_AUTH_SOCK" not in kwargs["env"]
        if command == ["lean", "--version"]:
            return subprocess.CompletedProcess(command, 0, "Lean (version 4.32.2)\n", "")
        if command == ["lake", "--version"]:
            return subprocess.CompletedProcess(command, 0, "Lake version 5.0.0\n", "")
        if command[:3] == ["lake", "translate-config", "toml"]:
            Path(command[3]).write_text('name = "Fixture"\n', encoding="utf-8")
        if command[:2] == ["lake", "pack"]:
            Path(command[2]).write_bytes(b"archive")
        if self.fail is not None and tuple(command) == self.fail:
            return subprocess.CompletedProcess(command, 9, "", "rejected\n")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")


def _item(base: ExecutionInput) -> WorkItem:
    return WorkItem(
        base.runtime.nodes[0],
        WorkPhase.PROOF,
        1,
        base.runtime.source_revision,
        base.source_contract_sha256,
        _protected_roadmap_sha256(base.runtime, base.runtime.nodes[0]),
    )


def _patch_gate_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    base_root: Path,
    candidate_root: Path,
    base: ExecutionInput,
    candidate: ExecutionInput,
) -> None:
    def verify_proof_candidate(node, *_args, **_kwargs):
        assert not node.assertions.proof_formalized
        return VerifyResult(True, checks={"axiom_audit": {"file": "random.lean"}})

    monkeypatch.setattr(
        "autoform_worker.gates.load_execution_input",
        lambda root, **_kwargs: base if Path(root) == base_root else candidate,
    )
    monkeypatch.setattr("autoform_worker.gates.audit_blueprint", lambda *_args, **_kwargs: AuditResult())
    monkeypatch.setattr(
        "autoform_worker.gates.capture_baseline",
        lambda *_args, **_kwargs: Baseline(base_root, files={"Main.lean": b"before"}),
    )
    monkeypatch.setattr(
        "autoform_worker.gates.verify_proof",
        verify_proof_candidate,
    )
    monkeypatch.setattr(
        "autoform_worker.gates.verify_candidate_static",
        lambda *_args, **_kwargs: VerifyResult(True, checks={"targets": []}),
    )


def _artifact_auditor(
    root_package: str,
    evaluated_config: Path,
    archive: Path,
    _blueprint: Path,
    _lean_root: Path,
    output_probe: Path,
    **_kwargs,
) -> RootPackageAudit:
    assert callable(_kwargs["runner"])
    assert "LEAN_PATH" not in _kwargs["environment"]
    output_probe.write_text("#check True\n", encoding="utf-8")
    return RootPackageAudit(
        root_package,
        ("Fixture",),
        1,
        (),
        hashlib.sha256(evaluated_config.read_bytes()).hexdigest(),
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        hashlib.sha256(output_probe.read_bytes()).hexdigest(),
    )


def test_candidate_gates_run_fixed_package_owned_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_root = tmp_path / "base"
    candidate_root = tmp_path / "candidate"
    _project(base_root)
    _project(candidate_root)
    base = _execution_input(_node(), "base-revision")
    candidate = _execution_input(_node(proved=True), "candidate-revision")
    _patch_gate_dependencies(monkeypatch, base_root, candidate_root, base, candidate)
    runner = FakeRunner()
    monkeypatch.setenv("ELAN_TOOLCHAIN", "attacker")
    monkeypatch.setenv("LEAN_PATH", str(candidate_root / "attacker"))

    result = run_candidate_gates(
        base_root,
        candidate_root,
        _item(base),
        runner=runner,
        artifact_auditor=_artifact_auditor,
    )

    assert result.passed
    assert [check.name for check in result.checks] == [
        "inputs",
        "toolchain",
        "execution-input",
        "transition",
        "static-trust-preflight",
        "blueprint-audit",
        "root-package-artifact",
        "target-trust",
        "stable-inputs",
    ]
    fixed_commands = [
        tuple(argv)
        for argv, _kwargs in runner.calls
        if argv not in (["lean", "--version"], ["lake", "--version"])
    ]
    assert [command[:2] for command in fixed_commands] == [
        ("lake", "translate-config"),
        ("lake", "check-build"),
        ("lake", "clean"),
        ("lake", "build"),
        ("lake", "pack"),
        ("lake", "env"),
    ]
    assert all(".github/autoform_audit.py" not in argument for call in fixed_commands for argument in call)
    assert json.loads(result.evidence_bytes())["schema"] == CANDIDATE_GATE_EVIDENCE_SCHEMA
    assert result.evidence_bytes() == result.evidence_bytes()
    assert "random.lean" not in result.evidence_bytes().decode("utf-8")


def test_candidate_gates_reject_missing_exact_source_contract_before_commands(
    tmp_path: Path,
) -> None:
    base_root = tmp_path / "base"
    candidate_root = tmp_path / "candidate"
    base_root.mkdir()
    candidate_root.mkdir()
    runner = FakeRunner()
    item = WorkItem(_node(), WorkPhase.PROOF, 1, "base-revision")

    result = run_candidate_gates(base_root, candidate_root, item, runner=runner)

    assert not result.passed
    assert result.checks[-1].name == "inputs"
    assert "source-contract" in result.checks[-1].detail
    assert runner.calls == []


def test_candidate_gates_reject_toolchain_drift(
    tmp_path: Path,
) -> None:
    base_root = tmp_path / "base"
    candidate_root = tmp_path / "candidate"
    _project(base_root)
    _project(candidate_root, package="Other")
    base = _execution_input(_node(), "base-revision")
    runner = FakeRunner()

    result = run_candidate_gates(base_root, candidate_root, _item(base), runner=runner)

    assert not result.passed
    assert result.checks[-1].name == "toolchain"
    assert "differs from the base" in result.checks[-1].detail


def test_candidate_gates_preserve_failed_build_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_root = tmp_path / "base"
    candidate_root = tmp_path / "candidate"
    _project(base_root)
    _project(candidate_root)
    base = _execution_input(_node(), "base-revision")
    candidate = _execution_input(_node(proved=True), "candidate-revision")
    _patch_gate_dependencies(monkeypatch, base_root, candidate_root, base, candidate)
    runner = FakeRunner(fail=("lake", "build"))

    result = run_candidate_gates(
        base_root,
        candidate_root,
        _item(base),
        runner=runner,
        artifact_auditor=_artifact_auditor,
    )

    assert not result.passed
    failed = result.checks[-1]
    assert failed.name == "root-package-artifact"
    assert [command["name"] for command in failed.evidence["commands"]] == [
        "lake-translate-config",
        "lake-check-build",
        "lake-clean-root-package",
        "lake-build",
    ]
    assert failed.evidence["commands"][-1]["returncode"] == 9


def test_static_rejection_happens_before_any_candidate_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_root = tmp_path / "base"
    candidate_root = tmp_path / "candidate"
    _project(base_root)
    _project(candidate_root)
    base = _execution_input(_node(), "base-revision")
    candidate = _execution_input(_node(proved=True), "candidate-revision")
    _patch_gate_dependencies(monkeypatch, base_root, candidate_root, base, candidate)
    monkeypatch.setattr(
        "autoform_worker.gates.verify_candidate_static",
        lambda *_args, **_kwargs: VerifyResult(False, "unsafe elaboration"),
    )
    runner = FakeRunner()

    result = run_candidate_gates(
        base_root,
        candidate_root,
        _item(base),
        runner=runner,
        artifact_auditor=_artifact_auditor,
    )

    assert not result.passed
    assert result.checks[-1].name == "static-trust-preflight"
    assert all(argv in (["lean", "--version"], ["lake", "--version"]) for argv, _ in runner.calls)


def test_non_target_file_change_is_rejected_before_candidate_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_root = tmp_path / "base"
    candidate_root = tmp_path / "candidate"
    _project(base_root)
    _project(candidate_root)
    (candidate_root / "build-hook.sh").write_text("exit 0\n", encoding="utf-8")
    base = _execution_input(_node(), "base-revision")
    candidate = _execution_input(_node(proved=True), "candidate-revision")
    _patch_gate_dependencies(monkeypatch, base_root, candidate_root, base, candidate)
    runner = FakeRunner()

    result = run_candidate_gates(
        base_root,
        candidate_root,
        _item(base),
        runner=runner,
        artifact_auditor=_artifact_auditor,
    )

    assert not result.passed
    assert result.checks[-1].name == "static-trust-preflight"
    assert "build-hook.sh" in result.checks[-1].detail
    assert all(argv in (["lean", "--version"], ["lake", "--version"]) for argv, _ in runner.calls)


def test_toolchain_fingerprint_requires_checked_manifest(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    (tmp_path / "lake-manifest.json").unlink()
    runner = FakeRunner()

    with pytest.raises(CandidateGateError, match="lake-manifest"):
        fingerprint_toolchain(tmp_path, runner=runner)

    assert runner.calls == []


def test_statement_trust_rejects_new_forbidden_token(tmp_path: Path) -> None:
    _project(tmp_path)
    (tmp_path / "Main.lean").write_text(
        "theorem result : True := by sorry\n",
        encoding="utf-8",
    )
    baseline = Baseline(tmp_path, files={"Main.lean": b""})

    node = _node()
    blocked = replace(node, status=replace(node.status, can_prove=False, state="blocked"))

    result = verify_candidate_trust(blocked, str(tmp_path), baseline=baseline)

    assert not result.ok
    assert "forbidden token 'sorry'" in result.reason


def test_proof_static_preflight_rejects_moved_directive_outside_target(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    _project(base)
    _project(candidate)
    base_source = 'run_cmd logInfo "old"\n\ntheorem result : True := by\n  trivial\n'
    candidate_source = 'theorem result : True := by\n  trivial\n\nrun_cmd logInfo "new"\n'
    (base / "Main.lean").write_text(base_source, encoding="utf-8")
    (candidate / "Main.lean").write_text(candidate_source, encoding="utf-8")
    node = _node()
    baseline = Baseline(
        root=candidate,
        files=_relevant_files(base),
        targets=frozenset({"Main.lean"}),
        target_contexts=_declaration_contexts(base, node),
    )

    result = verify_candidate_static(
        node,
        str(candidate),
        baseline=baseline,
        confine_changes=True,
    )

    assert not result.ok
    assert "outside target declarations" in result.reason


def test_proof_static_preflight_rejects_builtin_initializer_after_target(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    _project(base)
    _project(candidate)
    source = "theorem result : True := by\n  trivial\n"
    (base / "Main.lean").write_text(source, encoding="utf-8")
    (candidate / "Main.lean").write_text(
        source + '\nbuiltin_initialize IO.println "executed"\n',
        encoding="utf-8",
    )
    node = _node()
    baseline = Baseline(
        root=candidate,
        files=_relevant_files(base),
        targets=frozenset({"Main.lean"}),
        target_contexts=_declaration_contexts(base, node),
    )

    result = verify_candidate_static(
        node,
        str(candidate),
        baseline=baseline,
        confine_changes=True,
    )

    assert not result.ok
    assert "outside target declarations" in result.reason or "builtin_initialize" in result.reason


def test_package_artifact_audit_reuses_shipped_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "probe.lean"
    evaluated = tmp_path / "evaluated.toml"
    archive = tmp_path / "archive.tgz"
    evaluated.write_bytes(b'name = "Fixture"\n')
    archive.write_bytes(b"archive")
    monkeypatch.setattr("autoform_cli.artifact_audit.modules_from_archive", lambda *_args: ("Fixture",))
    monkeypatch.setattr("autoform_cli.artifact_audit.targets_from_blueprint", lambda *_args: (object(),))
    captured: dict[str, object] = {}

    def mathlib_modules(*_args, **kwargs):
        captured.update(kwargs)
        return ()

    monkeypatch.setattr("autoform_cli.artifact_audit.mathlib_modules_from_lake", mathlib_modules)
    monkeypatch.setattr(
        "autoform_cli.artifact_audit.render_probe",
        lambda modules, targets, mathlib: f"-- {modules!r} {len(targets)} {mathlib!r}\n",
    )

    runner = FakeRunner()
    environment = {"PATH": "/trusted/bin"}
    result = prepare_root_package_audit(
        "Fixture",
        evaluated,
        archive,
        tmp_path / "blueprint",
        tmp_path,
        probe,
        runner=runner,
        environment=environment,
    )

    assert result.root_package == "Fixture"
    assert result.modules == ("Fixture",)
    assert result.target_count == 1
    assert result.mathlib_modules == ()
    assert result.evaluated_config_sha256 == hashlib.sha256(evaluated.read_bytes()).hexdigest()
    assert result.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert result.probe_sha256 == hashlib.sha256(probe.read_bytes()).hexdigest()
    assert captured == {"environment": environment, "runner": runner}
    assert probe.read_text(encoding="utf-8") == "-- ('Fixture',) 1 ()\n"


def test_package_artifact_probe_refuses_existing_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluated = tmp_path / "evaluated.toml"
    archive = tmp_path / "archive.tgz"
    outside = tmp_path / "outside.lean"
    probe = tmp_path / "probe.lean"
    evaluated.write_text('name = "Fixture"\n', encoding="utf-8")
    archive.write_bytes(b"archive")
    outside.write_text("preserve\n", encoding="utf-8")
    probe.symlink_to(outside)
    monkeypatch.setattr("autoform_cli.artifact_audit.modules_from_archive", lambda *_args: ("Fixture",))
    monkeypatch.setattr("autoform_cli.artifact_audit.targets_from_blueprint", lambda *_args: ())
    monkeypatch.setattr("autoform_cli.artifact_audit.mathlib_modules_from_lake", lambda *_args, **_kwargs: ())
    monkeypatch.setattr("autoform_cli.artifact_audit.render_probe", lambda *_args: "#check True\n")

    with pytest.raises(ValueError, match="cannot create root-package audit probe"):
        prepare_root_package_audit(
            "Fixture",
            evaluated,
            archive,
            tmp_path / "blueprint",
            tmp_path,
            probe,
        )

    assert outside.read_text(encoding="utf-8") == "preserve\n"
    assert probe.read_text(encoding="utf-8") == "preserve\n"


def test_default_gate_runner_bounds_retained_output_and_hashes_full_stream(
    tmp_path: Path,
) -> None:
    script = (
        "import os, sys; "
        "os.write(1, b'x' * 70000 + sys.argv[1].encode() + b'tail'); "
        "os.write(2, b'y' * 70001)"
    )
    completed = _invoke(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=tmp_path,
        env=scrubbed_subprocess_environment(),
        runner=subprocess.run,
        timeout=10,
        replacements=((tmp_path, "<project>"),),
    )
    evidence = _command_evidence(
        "large-output",
        [sys.executable, "-c", script, str(tmp_path)],
        completed,
        ((tmp_path, "<project>"),),
    )
    expected_stdout = "x" * 70000 + "<project>tail"
    expected_stderr = "y" * 70001

    assert completed.returncode == 0
    assert len(completed.stdout) == 64 * 1024
    assert len(completed.stderr) == 64 * 1024
    assert evidence.stdout_truncated
    assert evidence.stderr_truncated
    assert evidence.stdout_bytes == len(expected_stdout.encode("utf-8"))
    assert evidence.stderr_bytes == len(expected_stderr.encode("utf-8"))
    assert evidence.stdout_sha256 == hashlib.sha256(expected_stdout.encode("utf-8")).hexdigest()
    assert evidence.stderr_sha256 == hashlib.sha256(expected_stderr.encode("utf-8")).hexdigest()
    assert evidence.stdout_tail == expected_stdout[-4096:]
    assert evidence.stderr_tail == expected_stderr[-4096:]


def test_stream_capture_scrubs_paths_and_newlines_across_chunks(tmp_path: Path) -> None:
    source = str(tmp_path / "nested")
    payload = f"prefix\r\n{source} suffix\rend".encode("utf-8")
    split = payload.index(source.encode("utf-8")) + len(source.encode("utf-8")) // 2
    capture = _StreamCapture(((Path(source), "<project>"),))

    capture.feed(payload[:split])
    capture.feed(payload[split:])
    capture.finish()

    expected_text = f"prefix\n{source} suffix\nend"
    expected_evidence = "prefix\n<project> suffix\nend"
    assert capture.text == expected_text
    assert capture.scrubbed_tail == expected_evidence
    assert capture.scrubbed_bytes == len(expected_evidence.encode("utf-8"))
    assert capture.sha256 == hashlib.sha256(expected_evidence.encode("utf-8")).hexdigest()


def test_default_gate_runner_rejects_non_utf8_output(tmp_path: Path) -> None:
    with pytest.raises(CandidateGateError, match="could not read command output"):
        _invoke(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'\\xff' + b'x' * (1024 * 1024))",
            ],
            cwd=tmp_path,
            env=scrubbed_subprocess_environment(),
            runner=subprocess.run,
            timeout=2,
        )


@pytest.mark.skipif(os.name != "posix", reason="process-group behavior is POSIX-specific")
def test_default_gate_runner_timeout_stops_descendant_before_return(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    delayed = tmp_path / "delayed"
    child_script = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(3); pathlib.Path(sys.argv[1]).write_text('late')"
    )
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "pathlib.Path(sys.argv[3]).write_text('ready'); time.sleep(10)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _invoke(
            [
                sys.executable,
                "-c",
                parent_script,
                child_script,
                str(delayed),
                str(ready),
            ],
            cwd=tmp_path,
            env=scrubbed_subprocess_environment(),
            runner=subprocess.run,
            timeout=2,
        )

    assert ready.read_text(encoding="utf-8") == "ready"
    time.sleep(3.1)
    assert not delayed.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group behavior is POSIX-specific")
def test_default_gate_runner_normal_exit_stops_residual_descendant(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    delayed = tmp_path / "delayed"
    child_script = (
        "import pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(0.5); pathlib.Path(sys.argv[1]).write_text('late')"
    )
    parent_script = (
        "import pathlib, subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "pathlib.Path(sys.argv[3]).write_text('ready')"
    )

    completed = _invoke(
        [
            sys.executable,
            "-c",
            parent_script,
            child_script,
            str(delayed),
            str(ready),
        ],
        cwd=tmp_path,
        env=scrubbed_subprocess_environment(),
        runner=subprocess.run,
        timeout=10,
    )

    assert completed.returncode == 0
    assert ready.read_text(encoding="utf-8") == "ready"
    time.sleep(0.7)
    assert not delayed.exists()
