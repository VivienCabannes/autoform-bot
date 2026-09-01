"""Fixed, package-owned admission gates for an authored candidate worktree."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

import psutil

from autoform_cli.artifact_audit import (
    RootPackageAudit,
    prepare_root_package_audit,
    root_package_from_config,
    verify_root_package_audit,
)
from autoform_cli.audit import audit_blueprint
from autoform_cli.execution_input import ExecutionInput, load_execution_input
from autoform_cli.lean import index_project
from autoform_cli.project.inspect import inspect_project
from autoform_cli.runtime import RuntimeNode
from servers.lean_client import LeanRuntimeClient
from servers.prover.verify import (
    Baseline,
    RuntimeClient,
    VerifyResult,
    capture_baseline,
    verify_candidate_static,
    verify_candidate_trust,
    verify_proof,
)

from .executor import (
    _capture_statement_baseline,
    _proof_transition_error,
    _protected_roadmap_sha256,
    _statement_transition_error,
    _verify_statement,
)
from .scheduler import WorkItem, WorkPhase


TOOLCHAIN_FINGERPRINT_SCHEMA = "autoform-toolchain-fingerprint/v1"
CANDIDATE_GATE_EVIDENCE_SCHEMA = "autoform-candidate-gates/v1"
CANDIDATE_GATE_POLICY = "fixed-gates/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_VERSION_OUTPUT = 16 * 1024
_MAX_EVIDENCE_TAIL = 4096
_MAX_CAPTURED_OUTPUT = 64 * 1024
_COMMAND_TIMEOUT_SECONDS = 2 * 60 * 60
_ENV_ALLOWLIST = frozenset(
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
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
    }
)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ArtifactAuditor(Protocol):
    def __call__(
        self,
        root_package: str,
        evaluated_config: Path,
        archive: Path,
        blueprint: Path,
        lean_root: Path,
        output_probe: Path,
        *,
        runner: CommandRunner,
        environment: Mapping[str, str],
    ) -> RootPackageAudit: ...


class CandidateGateError(RuntimeError):
    """One fixed gate rejected or could not safely inspect a candidate."""


class _CommandError(CandidateGateError):
    def __init__(self, message: str, commands: tuple[CommandEvidence, ...]) -> None:
        super().__init__(message)
        self.commands = commands


@dataclass(frozen=True, slots=True)
class ToolchainFingerprint:
    """Content and executable-version identity for one Lean worktree."""

    schema: str
    files: tuple[tuple[str, str], ...]
    lean_toolchain: str
    lean_version: str
    lake_version: str

    def as_dict(self) -> dict[str, object]:
        return {
            "files": {path: digest for path, digest in self.files},
            "lake_version": self.lake_version,
            "lean_toolchain": self.lean_toolchain,
            "lean_version": self.lean_version,
            "schema": self.schema,
        }

    def evidence_bytes(self) -> bytes:
        return _json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    """Bounded, path-scrubbed evidence from one fixed argv invocation."""

    name: str
    argv: tuple[str, ...]
    returncode: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_tail: str
    stderr_tail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "name": self.name,
            "returncode": self.returncode,
            "stderr_bytes": self.stderr_bytes,
            "stderr_sha256": self.stderr_sha256,
            "stderr_tail": self.stderr_tail,
            "stderr_truncated": self.stderr_truncated,
            "stdout_bytes": self.stdout_bytes,
            "stdout_sha256": self.stdout_sha256,
            "stdout_tail": self.stdout_tail,
            "stdout_truncated": self.stdout_truncated,
        }


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One ordered candidate admission decision."""

    name: str
    passed: bool
    detail: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze_json(self.evidence))

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": _json_value(self.evidence),
            "name": self.name,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class CandidateGateResult:
    """Canonical evidence for accepting or rejecting one exact work item."""

    passed: bool
    node_id: str
    article_id: str | None
    phase: str
    attempt: int
    source_revision: str
    source_contract_sha256: str | None
    protected_roadmap_sha256: str | None
    work_item_sha256: str
    base_execution_input_sha256: str | None
    candidate_execution_input_sha256: str | None
    base_toolchain: ToolchainFingerprint | None
    candidate_toolchain: ToolchainFingerprint | None
    checks: tuple[GateCheck, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "base_execution_input_sha256": self.base_execution_input_sha256,
            "base_toolchain": self.base_toolchain.as_dict() if self.base_toolchain else None,
            "candidate_execution_input_sha256": self.candidate_execution_input_sha256,
            "candidate_toolchain": (
                self.candidate_toolchain.as_dict() if self.candidate_toolchain else None
            ),
            "checks": [check.as_dict() for check in self.checks],
            "identity": {
                "article_id": self.article_id,
                "attempt": self.attempt,
                "node_id": self.node_id,
                "phase": self.phase,
                "protected_roadmap_sha256": self.protected_roadmap_sha256,
                "source_contract_sha256": self.source_contract_sha256,
                "source_revision": self.source_revision,
                "work_item_sha256": self.work_item_sha256,
            },
            "passed": self.passed,
            "policy": CANDIDATE_GATE_POLICY,
            "schema": CANDIDATE_GATE_EVIDENCE_SCHEMA,
        }

    def evidence_bytes(self) -> bytes:
        """Return canonical bytes suitable for the ledger artifact CAS."""

        return _json_bytes(self.as_dict())

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()


def scrubbed_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Keep host essentials while removing toolchain and Git overrides."""

    supplied = os.environ if source is None else source
    environment = {
        key: value
        for key, value in supplied.items()
        if key in _ENV_ALLOWLIST or key.startswith("LC_")
    }
    path = environment.get("PATH", os.defpath)
    safe_path = [entry for entry in path.split(os.pathsep) if entry and Path(entry).is_absolute()]
    environment["PATH"] = os.pathsep.join(safe_path) or os.defpath
    environment.update(
        {
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def fingerprint_toolchain(
    project_root: str | Path,
    *,
    runner: CommandRunner = subprocess.run,
) -> ToolchainFingerprint:
    """Fingerprint checked project inputs and scrubbed Lean/Lake versions."""

    root = _existing_root(project_root, "project root")
    before = _toolchain_inputs(root)
    environment = scrubbed_subprocess_environment()
    lean_version = _version_output(
        "lean",
        _invoke(["lean", "--version"], cwd=root, env=environment, runner=runner, timeout=30),
        root,
    )
    lake_version = _version_output(
        "lake",
        _invoke(["lake", "--version"], cwd=root, env=environment, runner=runner, timeout=30),
        root,
    )
    after = _toolchain_inputs(root)
    if before != after:
        raise CandidateGateError("toolchain inputs changed while their versions were inspected")
    files, lean_toolchain, configured_version = before
    if configured_version is not None and configured_version.removeprefix("v") not in lean_version:
        raise CandidateGateError(
            "lean --version does not match the version selected by lean-toolchain"
        )
    return ToolchainFingerprint(
        schema=TOOLCHAIN_FINGERPRINT_SCHEMA,
        files=files,
        lean_toolchain=lean_toolchain,
        lean_version=lean_version,
        lake_version=lake_version,
    )


def run_candidate_gates(
    base_worktree: str | Path,
    candidate_worktree: str | Path,
    item: WorkItem,
    *,
    runner: CommandRunner = subprocess.run,
    runtime: RuntimeClient | None = None,
    artifact_auditor: ArtifactAuditor = prepare_root_package_audit,
) -> CandidateGateResult:
    """Run fixed gates against one exact candidate without repository helpers."""

    checks: list[GateCheck] = []
    base: Path | None = None
    candidate: Path | None = None
    base_input: ExecutionInput | None = None
    candidate_input: ExecutionInput | None = None
    base_toolchain: ToolchainFingerprint | None = None
    candidate_toolchain: ToolchainFingerprint | None = None

    def result(passed: bool) -> CandidateGateResult:
        return CandidateGateResult(
            passed=passed,
            node_id=item.node.id,
            article_id=item.node.article_id,
            phase=item.phase.value if isinstance(item.phase, WorkPhase) else str(item.phase),
            attempt=item.attempt,
            source_revision=item.source_revision,
            source_contract_sha256=item.source_contract_sha256,
            protected_roadmap_sha256=item.protected_roadmap_sha256,
            work_item_sha256=_work_item_sha256(item),
            base_execution_input_sha256=base_input.sha256 if base_input else None,
            candidate_execution_input_sha256=(
                candidate_input.sha256 if candidate_input else None
            ),
            base_toolchain=base_toolchain,
            candidate_toolchain=candidate_toolchain,
            checks=tuple(checks),
        )

    def fail(name: str, error: object, evidence: Mapping[str, object] | None = None) -> CandidateGateResult:
        checks.append(
            GateCheck(
                name,
                False,
                _stable_error(error, base, candidate),
                evidence or {},
            )
        )
        return result(False)

    try:
        base = _existing_root(base_worktree, "base worktree")
        candidate = _existing_root(candidate_worktree, "candidate worktree")
        if base == candidate or _contains(base, candidate) or _contains(candidate, base):
            raise CandidateGateError("base and candidate worktrees must be distinct, non-nested directories")
        _validate_work_item(item)
    except Exception as error:
        checks.append(GateCheck("inputs", False, _stable_error(error), {}))
        return result(False)
    checks.append(GateCheck("inputs", True, "exact worktree and work-item inputs accepted", {}))

    assert base is not None and candidate is not None

    try:
        base_toolchain = fingerprint_toolchain(base, runner=runner)
        candidate_toolchain = fingerprint_toolchain(candidate, runner=runner)
        if base_toolchain != candidate_toolchain:
            raise CandidateGateError("candidate toolchain fingerprint differs from the base")
    except Exception as error:
        return fail("toolchain", error)
    checks.append(
        GateCheck(
            "toolchain",
            True,
            "base and candidate use the same checked toolchain inputs",
            {"sha256": candidate_toolchain.sha256},
        )
    )

    try:
        base_input = load_execution_input(base, lean_root=base)
        base_node = _exact_base_node(base_input, item)
        candidate_input = load_execution_input(candidate, lean_root=candidate)
        candidate_node = _exact_candidate_node(candidate_input, item)
    except Exception as error:
        return fail("execution-input", error)
    checks.append(
        GateCheck(
            "execution-input",
            True,
            "source contract and durable article identity match the scheduled work item",
            {
                "base_sha256": base_input.sha256,
                "candidate_sha256": candidate_input.sha256,
            },
        )
    )

    client = runtime or LeanRuntimeClient()
    statement_baseline: Baseline | None = None
    proof_baseline: Baseline | None = None
    project_baseline: Baseline | None = None
    try:
        project_baseline = _capture_statement_baseline(base)
        if item.phase is WorkPhase.STATEMENT:
            statement_baseline = project_baseline
            transition_error = _statement_transition_error(
                base_node,
                candidate_node,
                statement_baseline,
                index_project(base),
                candidate,
            )
        else:
            transition_error = _proof_transition_error(base_node, candidate_node)
            proof_baseline = replace(
                capture_baseline(base_node, str(base), runtime=client),
                root=candidate,
            )
        if transition_error:
            raise CandidateGateError(transition_error)
    except Exception as error:
        return fail("transition", error)
    checks.append(
        GateCheck(
            "transition",
            True,
            f"candidate makes only the scheduled {item.phase.value} transition",
            {"article_id": candidate_node.article_id, "node_id": candidate_node.id},
        )
    )

    try:
        assert project_baseline is not None
        confinement_error = _candidate_tree_confinement_error(
            project_baseline,
            candidate,
            candidate_node,
        )
        if confinement_error:
            raise CandidateGateError(confinement_error)
        static_baseline = proof_baseline if item.phase is WorkPhase.PROOF else statement_baseline
        assert static_baseline is not None
        static = verify_candidate_static(
            candidate_node,
            str(candidate),
            baseline=static_baseline,
            confine_changes=item.phase is WorkPhase.PROOF,
        )
        if not static.ok:
            raise CandidateGateError(static.reason)
    except Exception as error:
        evidence = {"verification": _verification_evidence(static)} if "static" in locals() else {}
        return fail("static-trust-preflight", error, evidence)
    checks.append(
        GateCheck(
            "static-trust-preflight",
            True,
            "candidate text passed command-free confinement and forbidden-token checks",
            {"verification": _verification_evidence(static)},
        )
    )

    try:
        blueprint = _blueprint_path(candidate, candidate_input)
        audit = audit_blueprint(blueprint, lean_root=candidate)
        if not audit.clean:
            raise CandidateGateError("blueprint audit reported findings")
    except Exception as error:
        evidence = {"audit": audit.as_dict()} if "audit" in locals() else {}
        return fail("blueprint-audit", error, evidence)
    checks.append(
        GateCheck(
            "blueprint-audit",
            True,
            "blueprint and source coverage audit is clean",
            {"audit": audit.as_dict()},
        )
    )

    try:
        artifact, commands = _run_root_package_audit(
            candidate,
            blueprint,
            runner=runner,
            artifact_auditor=artifact_auditor,
        )
    except _CommandError as error:
        return fail(
            "root-package-artifact",
            error,
            {"commands": [command.as_dict() for command in error.commands]},
        )
    except Exception as error:
        return fail("root-package-artifact", error)
    checks.append(
        GateCheck(
            "root-package-artifact",
            True,
            "full build and package-owned root artifact audit passed",
            {
                "artifact": artifact.as_dict(),
                "commands": [command.as_dict() for command in commands],
            },
        )
    )

    try:
        if item.phase is WorkPhase.PROOF:
            assert proof_baseline is not None
            verified = verify_proof(
                base_node,
                str(candidate),
                baseline=proof_baseline,
                runtime=client,
            )
        else:
            assert statement_baseline is not None
            statement_error = _verify_statement(candidate_node, candidate, runtime=client)
            if statement_error:
                raise CandidateGateError(statement_error)
            verified = verify_candidate_trust(
                candidate_node,
                str(candidate),
                baseline=statement_baseline,
                runtime=client,
            )
        if not verified.ok:
            raise CandidateGateError(verified.reason)
    except Exception as error:
        evidence = (
            {"verification": _verification_evidence(verified)}
            if "verified" in locals()
            else {}
        )
        return fail("target-trust", error, evidence)
    checks.append(
        GateCheck(
            "target-trust",
            True,
            "target diagnostics, forbidden-token checks, and axiom audit passed",
            {"verification": _verification_evidence(verified)},
        )
    )

    try:
        final_base_toolchain = fingerprint_toolchain(base, runner=runner)
        final_candidate_toolchain = fingerprint_toolchain(candidate, runner=runner)
        final_base_input = load_execution_input(base, lean_root=base)
        final_candidate_input = load_execution_input(candidate, lean_root=candidate)
        if final_base_toolchain != base_toolchain or final_candidate_toolchain != candidate_toolchain:
            raise CandidateGateError("toolchain inputs changed while candidate gates ran")
        if final_base_input.sha256 != base_input.sha256:
            raise CandidateGateError("base execution input changed while candidate gates ran")
        if final_candidate_input.sha256 != candidate_input.sha256:
            raise CandidateGateError("candidate execution input changed while candidate gates ran")
    except Exception as error:
        return fail("stable-inputs", error)
    checks.append(
        GateCheck(
            "stable-inputs",
            True,
            "base and candidate decision inputs remained stable",
            {},
        )
    )
    return result(True)


def _toolchain_inputs(
    root: Path,
) -> tuple[tuple[tuple[str, str], ...], str, str | None]:
    inspection = inspect_project(root)
    errors = [diagnostic for diagnostic in inspection.diagnostics if diagnostic.severity == "error"]
    invalid_manifest = any(
        diagnostic.code == "invalid-lake-manifest" for diagnostic in inspection.diagnostics
    )
    if errors or invalid_manifest:
        details = ", ".join(diagnostic.code for diagnostic in (*errors,))
        if invalid_manifest:
            details = ", ".join(filter(None, (details, "invalid-lake-manifest")))
        raise CandidateGateError(f"project toolchain inputs are not safely inspectable: {details}")
    if inspection.lean is None or inspection.lake is None:
        raise CandidateGateError("project must have one checked lean-toolchain and Lake configuration")
    if inspection.lake_manifest_path is None or inspection.lake_manifest_sha256 is None:
        raise CandidateGateError("project must have a checked regular lake-manifest.json")
    files = tuple(
        sorted(
            (
                (inspection.lean.path, inspection.lean.sha256),
                (inspection.lake.path, inspection.lake.sha256),
                (inspection.lake_manifest_path, inspection.lake_manifest_sha256),
            )
        )
    )
    return files, inspection.lean.toolchain, inspection.lean.version


def _version_output(
    executable: str,
    result: subprocess.CompletedProcess[str],
    root: Path,
) -> str:
    if result.returncode != 0:
        detail = _scrub_text(result.stderr or result.stdout, ((root, "<project>"),))[
            -_MAX_EVIDENCE_TAIL:
        ]
        raise CandidateGateError(f"{executable} --version failed ({result.returncode}): {detail}")
    output = _scrub_text(
        "\n".join(part for part in (result.stdout, result.stderr) if part),
        ((root, "<project>"),),
    ).strip()
    if not output:
        raise CandidateGateError(f"{executable} --version returned no version text")
    if len(output.encode("utf-8")) > _MAX_VERSION_OUTPUT:
        raise CandidateGateError(f"{executable} --version output is unexpectedly large")
    if any(ord(character) < 32 and character not in "\n\t" for character in output):
        raise CandidateGateError(f"{executable} --version output contains control characters")
    return output


def _validate_work_item(item: WorkItem) -> None:
    if not isinstance(item.phase, WorkPhase):
        raise CandidateGateError("work item has an invalid phase")
    if item.node.article_id is None:
        raise CandidateGateError("work item has no durable article_id")
    if item.source_contract_sha256 is None or _SHA256.fullmatch(item.source_contract_sha256) is None:
        raise CandidateGateError("work item has no exact source-contract SHA-256")
    if item.protected_roadmap_sha256 is None or _SHA256.fullmatch(item.protected_roadmap_sha256) is None:
        raise CandidateGateError("work item has no protected-roadmap SHA-256")
    if not item.source_revision:
        raise CandidateGateError("work item has no source revision")


def _work_item_sha256(item: WorkItem) -> str:
    payload = {
        "attempt": item.attempt,
        "node": item.node.as_dict(),
        "phase": item.phase.value if isinstance(item.phase, WorkPhase) else str(item.phase),
        "protected_roadmap_sha256": item.protected_roadmap_sha256,
        "source_contract_sha256": item.source_contract_sha256,
        "source_revision": item.source_revision,
    }
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _exact_base_node(execution_input: ExecutionInput, item: WorkItem) -> RuntimeNode:
    if execution_input.source_contract_sha256 != item.source_contract_sha256:
        raise CandidateGateError("base source-coverage contract differs from the work item")
    if execution_input.runtime.source_revision != item.source_revision:
        raise CandidateGateError("base runtime source revision differs from the work item")
    node = execution_input.runtime.get(item.node.id)
    if node != item.node:
        raise CandidateGateError("base runtime node differs from the exact scheduled node")
    if _protected_roadmap_sha256(execution_input.runtime, node) != item.protected_roadmap_sha256:
        raise CandidateGateError("base protected-roadmap digest differs from the work item")
    return node


def _exact_candidate_node(execution_input: ExecutionInput, item: WorkItem) -> RuntimeNode:
    if execution_input.source_contract_sha256 != item.source_contract_sha256:
        raise CandidateGateError("candidate source-coverage contract differs from the work item")
    matches = [
        node for node in execution_input.runtime.nodes if node.article_id == item.node.article_id
    ]
    if len(matches) != 1:
        raise CandidateGateError("candidate does not contain exactly one scheduled article_id")
    node = matches[0]
    if _protected_roadmap_sha256(execution_input.runtime, node) != item.protected_roadmap_sha256:
        raise CandidateGateError("roadmap outside the selected article changed")
    return node


def _run_root_package_audit(
    candidate: Path,
    blueprint: Path,
    *,
    runner: CommandRunner,
    artifact_auditor: ArtifactAuditor,
) -> tuple[RootPackageAudit, tuple[CommandEvidence, ...]]:
    environment = scrubbed_subprocess_environment()
    commands: list[CommandEvidence] = []
    with tempfile.TemporaryDirectory(prefix="autoform-gates-") as raw_temporary:
        temporary = Path(raw_temporary)
        evaluated = temporary / "lake-config.toml"
        archive = temporary / "root-package.tgz"
        probe = temporary / "root-package-audit.lean"
        replacements = ((candidate, "<candidate>"), (temporary, "<temporary>"))
        _run_checked(
            "lake-translate-config",
            ["lake", "translate-config", "toml", str(evaluated)],
            candidate,
            environment,
            runner,
            commands,
            replacements,
        )
        try:
            root_package = root_package_from_config(evaluated)
        except Exception as error:
            raise _CommandError(_scrub_text(str(error), replacements), tuple(commands)) from error
        for name, argv in (
            ("lake-check-build", ["lake", "check-build"]),
            ("lake-clean-root-package", ["lake", "clean", root_package]),
            ("lake-build", ["lake", "build"]),
            ("lake-pack", ["lake", "pack", str(archive)]),
        ):
            _run_checked(
                name,
                argv,
                candidate,
                environment,
                runner,
                commands,
                replacements,
            )
        try:
            artifact = artifact_auditor(
                root_package,
                evaluated,
                archive,
                blueprint,
                candidate,
                probe,
                runner=_artifact_runner(runner),
                environment=environment,
            )
        except Exception as error:
            raise _CommandError(_scrub_text(str(error), replacements), tuple(commands)) from error
        _run_checked(
            "root-package-probe",
            ["lake", "env", "lean", str(probe)],
            candidate,
            environment,
            runner,
            commands,
            replacements,
        )
        try:
            verify_root_package_audit(artifact, evaluated, archive, probe)
        except Exception as error:
            raise _CommandError(_scrub_text(str(error), replacements), tuple(commands)) from error
        return artifact, tuple(commands)


def _candidate_tree_confinement_error(
    baseline: Baseline,
    candidate: Path,
    node: RuntimeNode,
) -> str:
    current = _capture_statement_baseline(candidate).files
    allowed = {
        node.article_path,
        *(target.source_file for target in node.lean_targets if target.source_file),
    }
    changed = sorted(
        relative
        for relative in current.keys() | baseline.files.keys()
        if relative not in allowed and current.get(relative) != baseline.files.get(relative)
    )
    if changed:
        return f"candidate changed files outside the scheduled article and Lean targets: {changed}"
    return ""


def _run_checked(
    name: str,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    runner: CommandRunner,
    commands: list[CommandEvidence],
    replacements: tuple[tuple[Path, str], ...],
) -> None:
    try:
        completed = _invoke(
            argv,
            cwd=cwd,
            env=environment,
            runner=runner,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            replacements=replacements,
        )
    except Exception as error:
        detail = _scrub_text(str(error), replacements)
        raise _CommandError(f"{name} could not run: {detail}", tuple(commands)) from error
    observation = _command_evidence(name, argv, completed, replacements)
    commands.append(observation)
    if completed.returncode != 0:
        detail = observation.stderr_tail or observation.stdout_tail
        raise _CommandError(
            f"{name} failed with exit code {completed.returncode}: {detail}",
            tuple(commands),
        )


class _StreamCapture:
    """Bounded text retention plus complete, scrubbed stream evidence."""

    def __init__(self, replacements: tuple[tuple[Path, str], ...]) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._pending_carriage_return = False
        self._captured = ""
        self._captured_characters = 0
        self._scrub_pending = ""
        configured = [(str(path), label) for path, label in replacements]
        home = os.environ.get("HOME")
        if home:
            configured.append((home, "<home>"))
        ordered = sorted(
            ((source, label) for source, label in configured if source),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        self._replacements: dict[str, str] = {}
        for source, label in ordered:
            self._replacements.setdefault(source, label)
        self._max_pattern = max(map(len, self._replacements), default=0)
        self._replacement_pattern = (
            re.compile("|".join(re.escape(source) for source in self._replacements))
            if self._replacements
            else None
        )
        self._digest = hashlib.sha256()
        self._scrubbed_bytes = 0
        self._scrubbed_tail = ""

    def feed(self, chunk: bytes) -> None:
        self._accept_decoded(self._decoder.decode(chunk), final=False)

    def finish(self) -> None:
        self._accept_decoded(self._decoder.decode(b"", final=True), final=True)
        self._flush_scrubbed(final=True)

    def _accept_decoded(self, value: str, *, final: bool) -> None:
        if self._pending_carriage_return:
            value = "\r" + value
            self._pending_carriage_return = False
        if not final and value.endswith("\r"):
            value = value[:-1]
            self._pending_carriage_return = True
        elif final and self._pending_carriage_return:
            value += "\r"
            self._pending_carriage_return = False
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        self._captured_characters += len(normalized)
        self._captured = (self._captured + normalized)[-_MAX_CAPTURED_OUTPUT:]
        self._scrub_pending += normalized
        self._flush_scrubbed(final=False)

    def _flush_scrubbed(self, *, final: bool) -> None:
        if not self._scrub_pending:
            return
        if self._replacement_pattern is None:
            self._emit(self._scrub_pending)
            self._scrub_pending = ""
            return
        limit = len(self._scrub_pending)
        if not final:
            limit = max(0, limit - self._max_pattern + 1)
        consumed = 0
        emitted: list[str] = []
        for match in self._replacement_pattern.finditer(self._scrub_pending):
            if match.start() >= limit:
                break
            emitted.append(self._scrub_pending[consumed : match.start()])
            emitted.append(self._replacements[match.group(0)])
            consumed = match.end()
        if consumed < limit:
            emitted.append(self._scrub_pending[consumed:limit])
            consumed = limit
        self._emit("".join(emitted))
        self._scrub_pending = self._scrub_pending[consumed:]

    def _emit(self, value: str) -> None:
        encoded = value.encode("utf-8")
        self._digest.update(encoded)
        self._scrubbed_bytes += len(encoded)
        self._scrubbed_tail = (self._scrubbed_tail + value)[-_MAX_EVIDENCE_TAIL:]

    @property
    def text(self) -> str:
        return self._captured

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def scrubbed_bytes(self) -> int:
        return self._scrubbed_bytes

    @property
    def scrubbed_tail(self) -> str:
        return self._scrubbed_tail

    @property
    def truncated(self) -> bool:
        return self._captured_characters > _MAX_CAPTURED_OUTPUT


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    earliest_creation_time: float,
) -> None:
    """Stop the command and ordinary descendants before returning to the caller."""

    if os.name == "posix" and process.poll() is None:
        try:
            process_group = os.getpgid(process.pid)
        except OSError:
            process_group = process.pid
        if process_group == os.getpgrp():  # pragma: no cover - start_new_session enforces this
            _terminate_process_tree(process, None, earliest_creation_time)
            process_group = -1
        if process_group >= 0:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                _terminate_process_tree(process, process_group, earliest_creation_time)
    elif os.name == "posix":
        _terminate_process_tree(process, process.pid, earliest_creation_time)
    else:  # pragma: no cover - exercised on Windows
        _terminate_process_tree(process, None, earliest_creation_time)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:  # pragma: no cover - OS failed to reap child
        process.kill()
        process.wait(timeout=1)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    process_group: int | None,
    earliest_creation_time: float,
) -> None:
    """Fallback for platforms that do not permit signalling the process group."""

    by_pid: dict[int, psutil.Process] = {}

    def remember(target: psutil.Process) -> None:
        try:
            if target.create_time() >= earliest_creation_time:
                by_pid[target.pid] = target
        except psutil.Error:
            pass

    try:
        root = psutil.Process(process.pid)
        for child in root.children(recursive=True):
            remember(child)
        remember(root)
    except psutil.Error:
        pass
    if process_group is not None:
        for target in psutil.process_iter():
            try:
                if os.getpgid(target.pid) == process_group:
                    remember(target)
            except (OSError, psutil.Error):
                pass
    processes = sorted(by_pid.values(), key=lambda target: target.pid != process.pid)
    for target in processes:
        try:
            target.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(processes, timeout=0.5)
    if process.poll() is None:
        process.kill()


def _bounded_subprocess_run(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
    text: bool = True,
    check: bool = False,
    shell: bool = False,
    timeout: int | float | None = None,
    replacements: tuple[tuple[Path, str], ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one fixed command without retaining an unbounded transcript in memory."""

    if not capture_output or not text or check or shell:
        raise CandidateGateError("bounded gate runner requires captured text and shell=False")
    command = list(argv)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        shell=False,
        start_new_session=True,
    )
    try:
        earliest_creation_time = psutil.Process(process.pid).create_time()
    except psutil.Error:  # pragma: no cover - process disappeared before inspection
        earliest_creation_time = time.time() - 1
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = _StreamCapture(replacements)
    stderr = _StreamCapture(replacements)
    reader_errors: list[BaseException] = []

    def read_stream(stream: Any, capture: _StreamCapture) -> None:
        failed = False
        try:
            while chunk := stream.read(64 * 1024):
                capture.feed(chunk)
        except BaseException as error:  # pragma: no cover - pipe failure is platform-specific
            failed = True
            reader_errors.append(error)
            try:
                while stream.read(64 * 1024):
                    pass
            except BaseException:
                pass
        finally:
            if not failed:
                try:
                    capture.finish()
                except BaseException as error:  # pragma: no cover - malformed final UTF-8 sequence
                    reader_errors.append(error)

    readers = (
        threading.Thread(target=read_stream, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_stream, args=(process.stderr, stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = -1
    finally:
        _terminate_process_group(process, earliest_creation_time)
        for reader in readers:
            reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
        for reader in readers:
            reader.join(timeout=0.5)
    if any(reader.is_alive() for reader in readers):
        raise CandidateGateError("command output pipes remained open after process-group shutdown")
    if reader_errors:
        raise CandidateGateError(f"could not read command output: {reader_errors[0]}")
    if timed_out:
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout.text,
            stderr=stderr.text,
        )
    completed = subprocess.CompletedProcess(command, returncode, stdout.text, stderr.text)
    completed._autoform_stdout_sha256 = stdout.sha256  # type: ignore[attr-defined]
    completed._autoform_stderr_sha256 = stderr.sha256  # type: ignore[attr-defined]
    completed._autoform_stdout_bytes = stdout.scrubbed_bytes  # type: ignore[attr-defined]
    completed._autoform_stderr_bytes = stderr.scrubbed_bytes  # type: ignore[attr-defined]
    completed._autoform_stdout_tail = stdout.scrubbed_tail  # type: ignore[attr-defined]
    completed._autoform_stderr_tail = stderr.scrubbed_tail  # type: ignore[attr-defined]
    completed._autoform_stdout_truncated = stdout.truncated  # type: ignore[attr-defined]
    completed._autoform_stderr_truncated = stderr.truncated  # type: ignore[attr-defined]
    return completed


def _artifact_runner(runner: CommandRunner) -> CommandRunner:
    if runner is not subprocess.run:
        return runner

    def bounded(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        completed = _bounded_subprocess_run(argv, **kwargs)
        if getattr(completed, "_autoform_stdout_truncated", False) or getattr(
            completed, "_autoform_stderr_truncated", False
        ):
            raise CandidateGateError("artifact-inspection command output exceeded the safe limit")
        return completed

    return bounded


def _invoke(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    runner: CommandRunner,
    timeout: int,
    replacements: tuple[tuple[Path, str], ...] = (),
) -> subprocess.CompletedProcess[str]:
    if runner is subprocess.run:
        completed = _bounded_subprocess_run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            replacements=replacements,
        )
    else:
        completed = runner(
            list(argv),
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    if not isinstance(completed.returncode, int):
        raise CandidateGateError("command runner returned an invalid exit code")
    if not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str):
        raise CandidateGateError("command runner must return text stdout and stderr")
    return completed


def _command_evidence(
    name: str,
    argv: Sequence[str],
    completed: subprocess.CompletedProcess[str],
    replacements: tuple[tuple[Path, str], ...],
) -> CommandEvidence:
    stdout = _scrub_text(completed.stdout, replacements)
    stderr = _scrub_text(completed.stderr, replacements)
    stdout_encoded = stdout.encode("utf-8")
    stderr_encoded = stderr.encode("utf-8")
    return CommandEvidence(
        name=name,
        argv=tuple(_scrub_text(argument, replacements) for argument in argv),
        returncode=completed.returncode,
        stdout_sha256=getattr(
            completed,
            "_autoform_stdout_sha256",
            hashlib.sha256(stdout_encoded).hexdigest(),
        ),
        stderr_sha256=getattr(
            completed,
            "_autoform_stderr_sha256",
            hashlib.sha256(stderr_encoded).hexdigest(),
        ),
        stdout_bytes=getattr(completed, "_autoform_stdout_bytes", len(stdout_encoded)),
        stderr_bytes=getattr(completed, "_autoform_stderr_bytes", len(stderr_encoded)),
        stdout_truncated=getattr(completed, "_autoform_stdout_truncated", False),
        stderr_truncated=getattr(completed, "_autoform_stderr_truncated", False),
        stdout_tail=getattr(
            completed,
            "_autoform_stdout_tail",
            stdout[-_MAX_EVIDENCE_TAIL:],
        ),
        stderr_tail=getattr(
            completed,
            "_autoform_stderr_tail",
            stderr[-_MAX_EVIDENCE_TAIL:],
        ),
    )


def _verification_evidence(result: VerifyResult) -> dict[str, object]:
    checks = _json_value(result.checks)
    if isinstance(checks, dict):
        axiom = checks.get("axiom_audit")
        if isinstance(axiom, dict):
            axiom.pop("file", None)
    return {"checks": checks, "ok": result.ok, "reason": result.reason}


def _blueprint_path(root: Path, execution_input: ExecutionInput) -> Path:
    relative = Path(execution_input.runtime.blueprint_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise CandidateGateError("runtime blueprint path is not confined")
    path = (root / relative).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise CandidateGateError("runtime blueprint path escapes the candidate") from error
    if not path.is_dir():
        raise CandidateGateError("runtime blueprint path is not a directory")
    return path


def _existing_root(value: str | Path, label: str) -> Path:
    supplied = Path(value).expanduser()
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    try:
        absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise CandidateGateError(f"{label} cannot be resolved: {error}") from error
    if absolute.is_symlink() or not absolute.is_dir() or resolved != absolute:
        raise CandidateGateError(f"{label} must be a real directory with no symbolic-link path")
    return resolved


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return child != parent


def _scrub_text(value: str, replacements: tuple[tuple[Path, str], ...]) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    for path, label in sorted(replacements, key=lambda item: len(str(item[0])), reverse=True):
        text = text.replace(str(path), label)
    home = os.environ.get("HOME")
    if home:
        text = text.replace(home, "<home>")
    return text


def _stable_error(
    error: object,
    base: Path | None = None,
    candidate: Path | None = None,
) -> str:
    replacements = tuple(
        (path, label)
        for path, label in ((base, "<base>"), (candidate, "<candidate>"))
        if path is not None
    )
    message = _scrub_text(str(error), replacements)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _json_value(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return repr(value)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CandidateGateError(f"gate evidence is not canonical JSON: {error}") from error


__all__ = [
    "CANDIDATE_GATE_EVIDENCE_SCHEMA",
    "CANDIDATE_GATE_POLICY",
    "CandidateGateError",
    "CandidateGateResult",
    "CommandEvidence",
    "GateCheck",
    "TOOLCHAIN_FINGERPRINT_SCHEMA",
    "ToolchainFingerprint",
    "fingerprint_toolchain",
    "run_candidate_gates",
    "scrubbed_subprocess_environment",
]
