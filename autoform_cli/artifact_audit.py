"""Bind blueprint claims to the Lean and Mathlib artifacts checked by CI.

The verifier retains bounded snapshots across its build and kernel audit. For
Mathlib claims it verifies the canonical Git revision and source blob, asks
Lake to rehash the selected build, checks the trace's output descriptors, and
binds the declaration and loaded OLean path in the kernel. This is an integrity
check within a boundary that trusts pinned Lean/Lake/Git/GitHub/TLS and trusts
local Mathlib artifacts and traces against coordinated fabrication. It is not
cryptographic or hostile-cache attestation. Repository-owned Lake code and a
malicious same-user process capable of exact ABA restoration are also outside
the boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from ._directory_binding import RetainedDirectory, lexical_absolute_path, open_directory
from ._tree_snapshot import (
    BoundDirectoryTree,
    TreeCaptureLimits,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
)
from .graph import GraphValidationError, load_graph
from .lean import declaration_kind, declaration_names, mathlib_module_name

_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_TOOLCHAIN_BYTES = 64 * 1024
_MAX_ILEAN_BYTES = 16 * 1024 * 1024
_MAX_TRACE_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_OLEAN_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_CONTENT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_NAME_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ROOT_MODULES = 100_000
_MAX_TARGETS = 100_000
_MAX_MATHLIB_MODULES = 4_096
_MAX_MANIFEST_PACKAGES = 10_000
_MAX_DIRECTORY_ENTRIES = 100_000
_MAX_PROJECT_INPUT_BYTES = 256 * 1024 * 1024
_MAX_MATHLIB_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_REMOTE_REPOSITORY_BYTES = 256 * 1024 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_NAME_LENGTH = 1024
_DEFAULT_DEADLINE_SECONDS = 110 * 60
_QUERY_BATCH_SIZE = 128
_HASH_BATCH_SIZE = 64
_TOP_LEVEL_NAME = re.compile(r'^name\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?$')
_FULL_GIT_REVISION = re.compile(r"[0-9a-f]{40}")
_LAKE_HASH = re.compile(r"[0-9a-f]{16}")
_MANIFEST_VERSION = re.compile(r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)(?:-.+)?")
_CANONICAL_MATHLIB_URL = "https://github.com/leanprover-community/mathlib4.git"
_CANONICAL_MATHLIB_URLS = frozenset(
    {_CANONICAL_MATHLIB_URL, _CANONICAL_MATHLIB_URL.removesuffix(".git")}
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "AUX",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
)
_BLUEPRINT_LIMITS = TreeCaptureLimits(
    max_entries=_MAX_DIRECTORY_ENTRIES,
    max_depth=64,
    max_file_bytes=_MAX_CONFIG_BYTES,
    max_total_bytes=64 * 1024 * 1024,
)
_PROJECT_INPUT_LIMITS = TreeCaptureLimits(
    max_entries=_MAX_DIRECTORY_ENTRIES,
    max_depth=64,
    max_file_bytes=_MAX_SOURCE_BYTES,
    max_total_bytes=_MAX_PROJECT_INPUT_BYTES,
)
_PROJECT_IGNORED_DIRECTORIES = frozenset(
    {
        ".direnv",
        ".git",
        ".lake",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_PROJECT_CONTROL_FILES = frozenset(
    {"lake-manifest.json", "lakefile.lean", "lakefile.toml", "lean-toolchain"}
)


class AuditInputError(ValueError):
    """Evidence supplied to the artifact audit is invalid or unstable."""


@dataclass(frozen=True, slots=True)
class BlueprintTarget:
    """One declaration claim loaded from the captured blueprint."""

    article_path: str
    name: str
    expected_kind: str
    owner: str = "root"
    expected_module: str | None = None
    source_file: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactAuditSummary:
    """Stable, path-free summary of one completed audit."""

    root_package: str
    root_modules: tuple[str, ...]
    target_count: int
    mathlib_modules: tuple[str, ...] = ()

    def message(self) -> str:
        return (
            f"artifact audit clean: {len(self.root_modules)} root-package module(s), "
            f"{self.target_count} blueprint declaration claim(s), "
            f"{len(self.mathlib_modules)} Mathlib module(s)"
        )


@dataclass(frozen=True, slots=True)
class _FileSignature:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(slots=True)
class _BoundFile:
    path: Path
    label: str
    parent: RetainedDirectory
    descriptor: int
    signature: _FileSignature
    digest: str
    maximum_bytes: int
    data: bytes | None
    _closed: bool = False

    def verify(self) -> None:
        if self._closed:
            raise AuditInputError(f"{self.label} evidence is closed")
        try:
            self.parent.verify()
            named = os.stat(
                self.path.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(self.descriptor)
        except OSError as exc:
            raise AuditInputError(f"{self.label} changed after it was read") from exc
        if _signature(named) != self.signature or _signature(opened) != self.signature:
            raise AuditInputError(f"{self.label} changed after it was read")
        digest, _data = _read_descriptor(
            self.descriptor,
            self.label,
            self.maximum_bytes,
            collect=False,
        )
        if digest != self.digest:
            raise AuditInputError(f"{self.label} changed after it was read")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.descriptor)
        except OSError:
            pass
        self.parent.close()


@dataclass(frozen=True, slots=True)
class _ArchivedModule:
    module: str
    ilean_path: str
    ilean_digest: str
    olean_path: str
    olean_digest: str
    trace_path: str
    trace_digest: str
    trace: object


@dataclass(frozen=True, slots=True)
class _ArchiveSnapshot:
    modules: tuple[str, ...]
    records: Mapping[str, _ArchivedModule]


@dataclass(frozen=True, slots=True)
class _ProjectInputSnapshot:
    manifest: bytes
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _MathlibManifest:
    packages_dir: PurePosixPath
    revision: str


@dataclass(frozen=True, slots=True)
class _MathlibArtifactEvidence:
    olean_paths: tuple[tuple[str, str], ...]
    snapshot: _ArtifactSetSnapshot


@dataclass(frozen=True, slots=True)
class _ArtifactSetSnapshot:
    generation_revision: str
    digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(slots=True)
class _Deadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> _Deadline:
        if (
            not isinstance(seconds, (int, float))
            or isinstance(seconds, bool)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise AuditInputError("artifact audit deadline must be positive")
        return cls(time.monotonic() + float(seconds))

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise AuditInputError("artifact audit exceeded its aggregate subprocess deadline")
        return remaining


def root_package_from_config(config: Path) -> str:
    """Read the single top-level package name from evaluated Lake TOML."""

    evidence = _open_bound_file(config, "evaluated Lake configuration", _MAX_CONFIG_BYTES, collect=True)
    try:
        name = _root_package_from_bytes(evidence.data)
        evidence.verify()
        return name
    finally:
        evidence.close()


def modules_from_archive(archive: Path, root_package: str) -> tuple[str, ...]:
    """Return modules backed by complete root-package ILean/OLean/trace triples."""

    evidence = _open_bound_file(archive, "root-package build archive", _MAX_ARCHIVE_BYTES, collect=False)
    try:
        snapshot = _archive_snapshot(evidence, root_package)
        evidence.verify()
        return snapshot.modules
    finally:
        evidence.close()


def targets_from_blueprint(blueprint: Path) -> tuple[BlueprintTarget, ...]:
    """Load bounded local and Mathlib declaration claims from a valid graph."""

    try:
        graph = load_graph(blueprint)
    except GraphValidationError as exc:
        raise AuditInputError("blueprint is invalid: " + "; ".join(exc.issues)) from exc

    targets: list[BlueprintTarget] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        try:
            article_path = node.path.relative_to(graph.blueprint_dir).as_posix()
        except ValueError as exc:
            raise AuditInputError(f"{node_id}: article path escapes the blueprint") from exc

        local_names = declaration_names(node.lean or "")
        mathlib_names = declaration_names(node.mathlib_declaration or "") if node.mathlib else ()
        if (node.statement_formalized or node.proof_formalized) and not local_names and not node.mathlib:
            raise AuditInputError(
                f"{article_path}: formalized local work has no lean declaration target"
            )
        if node.mathlib and not mathlib_names:
            raise AuditInputError(
                f"{article_path}: mathlib is true but mathlib_declaration is missing"
            )
        mathlib_module = None
        if node.mathlib:
            mathlib_module = mathlib_module_name(node.mathlib_file or "")
            if mathlib_module is None:
                raise AuditInputError(
                    f"{article_path}: mathlib_file must be a canonical Mathlib/**/*.lean source path"
                )
        if not local_names and not mathlib_names:
            continue

        expected_kind = declaration_kind(node.declaration)
        if expected_kind is None:
            raise AuditInputError(
                f"{article_path}: declaration intent is missing or unsupported: {node.declaration or ''!r}"
            )
        for name in local_names:
            _validate_lean_name(name, article_path)
            targets.append(BlueprintTarget(article_path, name, expected_kind))
        for name in mathlib_names:
            _validate_lean_name(name, article_path)
            targets.append(
                BlueprintTarget(
                    article_path,
                    name,
                    expected_kind,
                    owner="mathlib",
                    expected_module=mathlib_module,
                    source_file=node.mathlib_file,
                )
            )
        if len(targets) > _MAX_TARGETS:
            raise AuditInputError(f"blueprint exceeds declaration target limit {_MAX_TARGETS}")

    return tuple(
        sorted(
            targets,
            key=lambda target: (
                target.article_path,
                target.owner,
                target.name,
                target.expected_kind,
                target.expected_module or "",
            ),
        )
    )


def preflight_blueprint(blueprint: Path) -> int:
    """Reject unsupported claims from one bounded blueprint snapshot."""

    with ExitStack() as stack:
        tree = _open_blueprint_tree(blueprint)
        stack.callback(tree.close)
        snapshot = _capture_blueprint(tree)
        with tempfile.TemporaryDirectory(prefix="autoform-artifact-preflight-") as temporary:
            private = Path(temporary).resolve(strict=True)
            private.chmod(0o700)
            captured = private / "blueprint"
            snapshot.materialize(captured)
            targets = targets_from_blueprint(captured)
        final = _capture_blueprint(tree)
        if final.generation_revision != snapshot.generation_revision:
            raise AuditInputError("blueprint changed during artifact preflight")
        return len(targets)


def render_probe(
    modules: tuple[str, ...],
    targets: tuple[BlueprintTarget, ...] = (),
    mathlib_oleans: Mapping[str, str] | None = None,
) -> str:
    """Render the Lean kernel probe for root declarations and roadmap claims."""

    if not modules:
        raise AuditInputError("refusing to render an empty kernel-trust audit")
    olean_paths = dict(mathlib_oleans or {})
    required_mathlib_modules = {
        target.expected_module
        for target in targets
        if target.owner == "mathlib" and target.expected_module is not None
    }
    missing_mathlib_modules = required_mathlib_modules - set(olean_paths)
    if missing_mathlib_modules:
        missing = ", ".join(sorted(missing_mathlib_modules))
        raise AuditInputError(
            "Mathlib blueprint modules lack validated build artifacts: " + missing
        )
    unexpected_mathlib_modules = set(olean_paths) - required_mathlib_modules
    if unexpected_mathlib_modules:
        unexpected = ", ".join(sorted(unexpected_mathlib_modules))
        raise AuditInputError("unexpected validated Mathlib build artifacts: " + unexpected)
    imports = "\n".join(
        f"import {module}" for module in sorted(set(modules) | required_mathlib_modules)
    )
    root_names = ", ".join(_lean_name(module) for module in modules)
    local_targets = ", ".join(
        f"({json.dumps(target.article_path, ensure_ascii=False)}, {_lean_name(target.name)}, "
        f"{json.dumps(target.expected_kind)})"
        for target in targets
        if target.owner == "root"
    )
    mathlib_targets = ", ".join(
        f"({json.dumps(target.article_path, ensure_ascii=False)}, {_lean_name(target.name)}, "
        f"{json.dumps(target.expected_kind)}, {_lean_name(target.expected_module or '')})"
        for target in targets
        if target.owner == "mathlib"
    )
    mathlib_artifacts = ", ".join(
        f"({_lean_name(module)}, {json.dumps(path, ensure_ascii=False)})"
        for module, path in sorted(olean_paths.items())
    )
    probe = f"""{imports}
import Lean.Util.CollectAxioms
import Lean.Elab.Command
import Lean.Meta.Instances
import Lean.OriginalConstKind
import Lean.Structure
import Lean.Class
import Lean.Util.Path

open Lean Elab Command

private def declaringModule? (env : Environment) (declName : Name) : Option Name := do
  let moduleIdx ← env.getModuleIdxFor? declName
  env.header.moduleNames[moduleIdx.toNat]?

private def matchesDeclarationKind
    (env : Environment) (declName : Name) (expected : String) : Bool :=
  match expected with
  | "theorem" => getOriginalConstKind? env declName == some .thm
  | "axiom" => getOriginalConstKind? env declName == some .axiom
  | "opaque" => getOriginalConstKind? env declName == some .opaque
  | "abbrev" =>
      match env.find? declName with
      | some (.defnInfo info) => info.hints == .abbrev
      | _ => false
  | "def" =>
      match env.find? declName with
      | some (.defnInfo info) => info.hints != .abbrev
      | _ => false
  | "instance" => Meta.isInstanceCore env declName
  | "class" => isClass env declName
  | "structure" => isStructure env declName && !isClass env declName
  | "inductive" =>
      getOriginalConstKind? env declName == some .induct && !isStructure env declName
  | _ => false

run_cmd do
  let rootModules : List Name := [{root_names}]
  let localTargets : List (String × Name × String) := [{local_targets}]
  let mathlibTargets : List (String × Name × String × Name) := [{mathlib_targets}]
  let mathlibArtifacts : List (Name × String) := [{mathlib_artifacts}]
  let allowed : List Name := [``propext, ``Classical.choice, ``Quot.sound]
  let env ← getEnv
  let mut badTargets := false
  for (article, declName, expectedKind) in localTargets do
    if env.find? declName |>.isNone then
      badTargets := true
      logError m!"{{article}}: local declaration does not exist: {{declName}}"
    else
      match declaringModule? env declName with
      | none =>
          badTargets := true
          logError m!"{{article}}: local declaration has no declaring module: {{declName}}"
      | some moduleName =>
          unless rootModules.contains moduleName do
            badTargets := true
            logError m!"{{article}}: local declaration {{declName}} belongs to non-root module {{moduleName}}"
      unless matchesDeclarationKind env declName expectedKind do
        badTargets := true
        logError m!"{{article}}: declaration {{declName}} does not have expected kind {{expectedKind}}"
  for (article, declName, expectedKind, expectedModule) in mathlibTargets do
    if env.find? declName |>.isNone then
      badTargets := true
      logError m!"{{article}}: Mathlib declaration does not exist: {{declName}}"
    else
      match declaringModule? env declName with
      | none =>
          badTargets := true
          logError m!"{{article}}: Mathlib declaration has no declaring module: {{declName}}"
      | some moduleName =>
          if rootModules.contains moduleName then
            badTargets := true
            logError m!"{{article}}: Mathlib declaration {{declName}} is owned by root module {{moduleName}}"
          if moduleName != expectedModule then
            badTargets := true
            logError m!"{{article}}: Mathlib declaration {{declName}} belongs to {{moduleName}}, not {{expectedModule}}"
      unless matchesDeclarationKind env declName expectedKind do
        badTargets := true
        logError m!"{{article}}: declaration {{declName}} does not have expected kind {{expectedKind}}"
  for (moduleName, expectedPath) in mathlibArtifacts do
    let actualPath ← IO.FS.realPath (← Lean.findOLean moduleName)
    let expectedPath ← IO.FS.realPath expectedPath
    unless actualPath.normalize == expectedPath.normalize do
      badTargets := true
      logError m!"Mathlib module {{moduleName}} loaded from {{actualPath}}, not validated artifact {{expectedPath}}"
  let mut checked : Nat := 0
  let mut badSafety : Array Name := #[]
  let mut badAxioms : Array (Name × Name) := #[]
  for (declName, info) in env.constants do
    if let some moduleIdx := env.getModuleIdxFor? declName then
      if let some moduleName := env.header.moduleNames[moduleIdx.toNat]? then
        if rootModules.contains moduleName then
          checked := checked + 1
          if info.isUnsafe || info.isPartial then
            badSafety := badSafety.push declName
          for usedAxiom in (← Lean.collectAxioms declName) do
            unless allowed.contains usedAxiom do
              badAxioms := badAxioms.push (declName, usedAxiom)
  for declName in badSafety do
    logError m!"unsafe or partial declaration: {{declName}}"
  for (declName, usedAxiom) in badAxioms do
    logError m!"{{declName}} depends on unexpected axiom {{usedAxiom}}"
  if checked == 0 then
    throwError "kernel-trust audit found no root-package declarations"
  unless !badTargets && badSafety.isEmpty && badAxioms.isEmpty do
    throwError "blueprint or root-package declarations failed the artifact audit"
  logInfo m!"artifact audit clean ({{checked}} root-package declaration(s) audited)"
"""
    if len(probe.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise AuditInputError("generated artifact audit probe is unexpectedly large")
    return probe


def run_artifact_audit(
    blueprint: Path,
    lean_root: Path,
    *,
    timeout_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> ArtifactAuditSummary:
    """Run one fail-closed artifact audit under a shared absolute deadline."""

    try:
        return _run_artifact_audit(
            blueprint,
            lean_root,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
    except AuditInputError:
        raise
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        tarfile.TarError,
        subprocess.SubprocessError,
        TreeSnapshotError,
    ) as exc:
        raise AuditInputError(f"artifact audit could not validate its evidence: {exc}") from exc


def _run_artifact_audit(
    blueprint: Path,
    lean_root: Path,
    *,
    timeout_seconds: float,
    environment: Mapping[str, str] | None,
) -> ArtifactAuditSummary:

    deadline = _Deadline.after(timeout_seconds)
    with ExitStack() as stack:
        blueprint_tree = _open_blueprint_tree(blueprint)
        stack.callback(blueprint_tree.close)
        blueprint_snapshot = _capture_blueprint(blueprint_tree)

        with tempfile.TemporaryDirectory(prefix="autoform-artifact-audit-") as temporary:
            private = Path(temporary).resolve(strict=True)
            private.chmod(0o700)
            captured_blueprint = private / "blueprint"
            blueprint_snapshot.materialize(captured_blueprint)
            targets = targets_from_blueprint(captured_blueprint)

            project = _open_project_tree(lean_root)
            stack.callback(project.close)
            project_inputs = _capture_project_inputs(project)
            mathlib_targets = tuple(target for target in targets if target.owner == "mathlib")
            mathlib_modules = tuple(
                sorted(
                    {
                        target.expected_module
                        for target in mathlib_targets
                        if target.expected_module is not None
                    }
                )
            )
            if len(mathlib_modules) > _MAX_MATHLIB_MODULES:
                raise AuditInputError(
                    f"blueprint exceeds Mathlib module limit {_MAX_MATHLIB_MODULES}"
                )
            mathlib_manifest: _MathlibManifest | None = None
            mathlib_checkout: BoundDirectoryTree | None = None
            mathlib_sources: _ArtifactSetSnapshot | None = None
            mathlib_build_roots: tuple[PurePosixPath, ...] = ()
            if mathlib_modules:
                mathlib_manifest = _mathlib_manifest_from_bytes(project_inputs.manifest)
                mathlib_checkout = _open_mathlib_checkout(project, mathlib_manifest)
                stack.callback(mathlib_checkout.close)
                _validate_mathlib_checkout(
                    mathlib_checkout,
                    mathlib_manifest,
                    deadline,
                    environment=environment,
                )
                _verify_canonical_mathlib_revision(
                    private,
                    mathlib_manifest.revision,
                    deadline,
                    environment=environment,
                )
                mathlib_sources = _capture_mathlib_sources(
                    mathlib_checkout,
                    tuple(
                        sorted(
                            {
                                target.source_file
                                for target in mathlib_targets
                                if target.source_file is not None
                            }
                        )
                    ),
                    mathlib_manifest.revision,
                    deadline,
                    environment=environment,
                )

            config_path = private / "lake-config.toml"
            _checked_command(
                ["lake", "translate-config", "toml", str(config_path)],
                cwd=project.root,
                deadline=deadline,
                label="evaluated Lake configuration",
                environment=environment,
            )
            config = _open_bound_file(
                config_path,
                "evaluated Lake configuration",
                _MAX_CONFIG_BYTES,
                collect=True,
            )
            stack.callback(config.close)
            root_package = _root_package_from_bytes(config.data)

            _checked_command(
                ["lake", "check-build"],
                cwd=project.root,
                deadline=deadline,
                label="Lake build-directory check",
                environment=environment,
            )
            _checked_command(
                ["lake", "clean", root_package],
                cwd=project.root,
                deadline=deadline,
                label="root-package clean",
                environment=environment,
            )
            _checked_command(
                ["lake", "build"],
                cwd=project.root,
                deadline=deadline,
                label="root-package build",
                environment=environment,
            )
            archive_path = private / "root-build.tar.gz"
            _checked_command(
                ["lake", "pack", str(archive_path)],
                cwd=project.root,
                deadline=deadline,
                label="root-package archive",
                environment=environment,
            )
            packed = _open_bound_file(
                archive_path,
                "root-package build archive",
                _MAX_ARCHIVE_BYTES,
                collect=False,
            )
            stack.callback(packed.close)
            archive_snapshot = _archive_snapshot(packed, root_package)

            root_evidence = _inspect_root_artifacts(
                project,
                project_inputs,
                archive_snapshot,
                deadline,
                root_package,
                environment=environment,
            )
            mathlib_evidence: _MathlibArtifactEvidence | None = None
            if mathlib_checkout is not None:
                mathlib_paths = _query_mathlib_artifact_paths(
                    project.root,
                    mathlib_checkout.root,
                    mathlib_modules,
                    deadline,
                    environment=environment,
                )
                mathlib_build_roots = _mathlib_build_roots(
                    mathlib_checkout.root,
                    mathlib_paths,
                )
                _validate_mathlib_checkout(
                    mathlib_checkout,
                    mathlib_manifest,
                    deadline,
                    environment=environment,
                    allowed_build_roots=mathlib_build_roots,
                )
                mathlib_evidence = _inspect_mathlib_artifacts(
                    mathlib_checkout,
                    mathlib_paths,
                    mathlib_targets,
                    private,
                    project.root,
                    deadline,
                    environment=environment,
                )

            probe = private / "probe.lean"
            _write_private_file(
                probe,
                render_probe(
                    archive_snapshot.modules,
                    targets,
                    dict(mathlib_evidence.olean_paths) if mathlib_evidence else {},
                ).encode("utf-8"),
            )
            probe_evidence = _open_bound_file(
                probe,
                "generated artifact audit probe",
                _MAX_SOURCE_BYTES,
                collect=False,
            )
            try:
                _checked_command(
                    ["lake", "env", "lean", "--trust=0", str(probe)],
                    cwd=project.root,
                    deadline=deadline,
                    label="Lean artifact probe",
                    environment=environment,
                )

                config.verify()
                packed.verify()
                probe_evidence.verify()
                final_inputs = _capture_project_inputs(project)
                if final_inputs != project_inputs:
                    raise AuditInputError("Lean project inputs changed during build or artifact validation")
                final_artifacts = _capture_artifact_set(
                    project,
                    tuple(path for path, _digest in root_evidence.digests),
                    expected_digests=dict(root_evidence.digests),
                )
                if final_artifacts != root_evidence:
                    raise AuditInputError("root-package artifacts changed during artifact validation")
                if mathlib_checkout is not None:
                    assert mathlib_manifest is not None
                    assert mathlib_sources is not None
                    assert mathlib_evidence is not None
                    _validate_mathlib_checkout(
                        mathlib_checkout,
                        mathlib_manifest,
                        deadline,
                        environment=environment,
                        allowed_build_roots=mathlib_build_roots,
                    )
                    final_mathlib_sources = _capture_mathlib_sources(
                        mathlib_checkout,
                        tuple(path for path, _digest in mathlib_sources.digests),
                        mathlib_manifest.revision,
                        deadline,
                        environment=environment,
                    )
                    if final_mathlib_sources != mathlib_sources:
                        raise AuditInputError(
                            "Mathlib source modules changed during artifact validation"
                        )
                    final_mathlib_artifacts = _capture_artifact_set(
                        mathlib_checkout,
                        tuple(path for path, _digest in mathlib_evidence.snapshot.digests),
                        expected_digests=dict(mathlib_evidence.snapshot.digests),
                        label="Mathlib",
                        maximum_total_bytes=_MAX_MATHLIB_ARTIFACT_BYTES,
                    )
                    if final_mathlib_artifacts != mathlib_evidence.snapshot:
                        raise AuditInputError(
                            "Mathlib artifacts changed during artifact validation"
                        )
                final_blueprint = _capture_blueprint(blueprint_tree)
                if final_blueprint.generation_revision != blueprint_snapshot.generation_revision:
                    raise AuditInputError("blueprint changed during artifact validation")
            finally:
                probe_evidence.close()

        return ArtifactAuditSummary(
            root_package=root_package,
            root_modules=archive_snapshot.modules,
            target_count=len(targets),
            mathlib_modules=mathlib_modules,
        )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) == 2 and arguments[0] == "--root-package":
            print(root_package_from_config(Path(arguments[1])))
            return 0
        if len(arguments) == 2 and arguments[0] == "preflight":
            count = preflight_blueprint(Path(arguments[1]))
            print(f"artifact preflight clean: {count} blueprint declaration claim(s)")
            return 0
        if len(arguments) == 3 and arguments[0] == "verify":
            summary = run_artifact_audit(*(Path(value) for value in arguments[1:]))
            print(summary.message())
            return 0
    except AuditInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        "usage: autoform_audit.py --root-package EVALUATED_CONFIG\n"
        "   or: autoform_audit.py preflight BLUEPRINT\n"
        "   or: autoform_audit.py verify BLUEPRINT LEAN_ROOT",
        file=sys.stderr,
    )
    return 2


def _root_package_from_bytes(data: bytes | None) -> str:
    if data is None:
        raise AuditInputError("evaluated Lake configuration was not captured")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise AuditInputError("evaluated Lake configuration is not valid UTF-8") from exc
    if "skipkerneltc" in unicodedata.normalize("NFC", text).casefold():
        raise AuditInputError("evaluated Lake configuration enables debug.skipKernelTC")
    lines = text.splitlines()
    names: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = _TOP_LEVEL_NAME.fullmatch(stripped)
        if match is not None:
            try:
                value = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise AuditInputError("evaluated Lake configuration has an invalid package name") from exc
            if not _valid_package_name(value):
                raise AuditInputError("evaluated Lake configuration has an invalid package name")
            names.append(value)
    if len(names) != 1:
        raise AuditInputError("evaluated Lake configuration must define exactly one root package name")
    return names[0]


def _valid_package_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _module_parts(value, "evaluated Lake package name")
    except AuditInputError:
        return False
    return True


def _package_trace_name(package: str) -> str:
    """Return Lake trace spelling for a validated package ``Name``."""

    return ".".join(_module_parts(package, "evaluated Lake package name"))


def _is_nfc_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return unicodedata.normalize("NFC", value) == value


def _open_bound_file(
    path: Path,
    label: str,
    maximum_bytes: int,
    *,
    collect: bool,
    require_canonical: bool = True,
) -> _BoundFile:
    absolute = lexical_absolute_path(path)
    try:
        parent = open_directory(absolute.parent)
    except OSError as exc:
        raise AuditInputError(f"cannot retain {label} parent directory") from exc
    descriptor = -1
    try:
        if require_canonical:
            _require_canonical_child(parent, absolute.name, label)
        named = os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or _is_reparse_point(named):
            raise AuditInputError(f"{label} is not a regular file")
        descriptor = os.open(absolute.name, _FILE_FLAGS, dir_fd=parent.descriptor)
        opened = os.fstat(descriptor)
        if _signature(opened) != _signature(named):
            raise AuditInputError(f"{label} changed while it was opened")
        digest, data = _read_descriptor(descriptor, label, maximum_bytes, collect=collect)
        final = os.fstat(descriptor)
        if _signature(final) != _signature(opened):
            raise AuditInputError(f"{label} changed while it was read")
        return _BoundFile(
            absolute,
            label,
            parent,
            descriptor,
            _signature(opened),
            digest,
            maximum_bytes,
            data,
        )
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        parent.close()
        raise


def _read_descriptor(
    descriptor: int,
    label: str,
    maximum_bytes: int,
    *,
    collect: bool,
) -> tuple[str, bytes | None]:
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > maximum_bytes:
            raise AuditInputError(f"{label} exceeds {maximum_bytes} bytes")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if collect else None
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise AuditInputError(f"{label} exceeds {maximum_bytes} bytes")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        return digest.hexdigest(), b"".join(chunks) if chunks is not None else None
    except AuditInputError:
        raise
    except OSError as exc:
        raise AuditInputError(f"cannot read {label}") from exc


def _signature(metadata: os.stat_result) -> _FileSignature:
    return _FileSignature(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_canonical_child(parent: RetainedDirectory, requested: str, label: str) -> None:
    _require_canonical_name(parent.descriptor, requested, label)


def _require_canonical_name(descriptor: int, requested: str, label: str) -> None:
    if not requested or requested in {".", ".."} or unicodedata.normalize("NFC", requested) != requested:
        raise AuditInputError(f"{label} has a noncanonical filename")
    folded = unicodedata.normalize("NFC", requested).casefold()
    matches: list[str] = []
    try:
        with os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > _MAX_DIRECTORY_ENTRIES:
                    raise AuditInputError(
                        f"{label} parent exceeds {_MAX_DIRECTORY_ENTRIES} directory entries"
                    )
                if unicodedata.normalize("NFC", entry.name).casefold() == folded:
                    matches.append(entry.name)
    except AuditInputError:
        raise
    except OSError as exc:
        raise AuditInputError(f"cannot inspect {label} parent directory") from exc
    if matches != [requested]:
        raise AuditInputError(f"{label} filename is missing, aliased, or noncanonical")


def _archive_snapshot(evidence: _BoundFile, root_package: str) -> _ArchiveSnapshot:
    if not _valid_package_name(root_package):
        raise AuditInputError("root package name is empty or malformed")
    names: dict[str, tarfile.TarInfo] = {}
    aliases: dict[tuple[str, ...], str] = {}
    kinds: dict[str, str] = {}
    content_bytes = 0
    name_bytes = 0
    try:
        os.lseek(evidence.descriptor, 0, os.SEEK_SET)
        stream = os.fdopen(os.dup(evidence.descriptor), "rb", closefd=True)
        with stream, tarfile.open(fileobj=stream, mode="r:*") as packed:
            for count, member in enumerate(packed, start=1):
                if count > _MAX_ARCHIVE_MEMBERS:
                    raise AuditInputError(
                        f"root-package build archive exceeds {_MAX_ARCHIVE_MEMBERS} members"
                    )
                try:
                    name_bytes += len(member.name.encode("utf-8"))
                except UnicodeError as exc:
                    raise AuditInputError("root-package build archive has a non-UTF-8 member name") from exc
                if name_bytes > _MAX_ARCHIVE_NAME_BYTES:
                    raise AuditInputError(
                        "root-package build archive exceeds aggregate member-name byte limit "
                        f"{_MAX_ARCHIVE_NAME_BYTES}"
                    )
                if member.name in {".", "./"} and member.isdir():
                    continue
                path = _safe_archive_path(member.name)
                display = "/".join(path)
                key = tuple(unicodedata.normalize("NFC", part).casefold() for part in path)
                previous = aliases.get(key)
                if previous is not None:
                    if previous == display:
                        raise AuditInputError(f"duplicate build archive member: {display}")
                    raise AuditInputError(
                        f"case- or Unicode-aliased build archive members: {previous} and {display}"
                    )
                aliases[key] = display
                if member.isdir():
                    kinds[display] = "directory"
                    continue
                if not member.isfile():
                    raise AuditInputError(f"build archive member is not a regular file: {display}")
                if member.size < 0:
                    raise AuditInputError(f"build archive member has a negative size: {display}")
                content_bytes += member.size
                if content_bytes > _MAX_ARCHIVE_CONTENT_BYTES:
                    raise AuditInputError(
                        "root-package build archive exceeds aggregate decompressed byte limit "
                        f"{_MAX_ARCHIVE_CONTENT_BYTES}"
                    )
                kinds[display] = "file"
                if display.endswith((".ilean", ".olean")) or (
                    display.startswith("lib/lean/") and display.endswith(".trace")
                ):
                    names[display] = member
            _validate_archive_layout(kinds)

            records: dict[str, _ArchivedModule] = {}
            consumed: set[str] = set()
            for display, member in sorted(names.items()):
                if not display.endswith(".ilean"):
                    continue
                if len(records) >= _MAX_ROOT_MODULES:
                    raise AuditInputError(
                        f"root-package build archive exceeds {_MAX_ROOT_MODULES} modules"
                    )
                metadata_bytes, ilean_digest = _read_archive_member(
                    packed,
                    member,
                    "ILean",
                    display,
                    _MAX_ILEAN_BYTES,
                    collect=True,
                )
                assert metadata_bytes is not None
                metadata = _decode_json(metadata_bytes, "ILean", display)
                module = _module_from_metadata(metadata, tuple(display.split("/")), display)
                module_parts = _module_parts(module, display)
                expected_path = (
                    "lib",
                    "lean",
                    *module_parts[:-1],
                    f"{module_parts[-1]}.ilean",
                )
                if tuple(display.split("/")) != expected_path:
                    raise AuditInputError(
                        f"root-package ILean artifact is outside lib/lean: {display}"
                    )
                stem = display[: -len(".ilean")]
                olean_path = f"{stem}.olean"
                trace_path = f"{stem}.trace"
                olean = names.get(olean_path)
                trace_member = names.get(trace_path)
                if olean is None:
                    raise AuditInputError(f"ILean artifact has no matching OLean: {display}")
                if trace_member is None:
                    raise AuditInputError(f"ILean artifact has no matching Lake trace: {display}")
                _olean_bytes, olean_digest = _read_archive_member(
                    packed,
                    olean,
                    "OLean",
                    olean_path,
                    _MAX_OLEAN_BYTES,
                    collect=False,
                )
                trace_bytes, trace_digest = _read_archive_member(
                    packed,
                    trace_member,
                    "Lake trace",
                    trace_path,
                    _MAX_TRACE_BYTES,
                    collect=True,
                )
                assert trace_bytes is not None
                trace = _decode_json(trace_bytes, "Lake trace", trace_path)
                _validate_root_trace(trace, module, root_package, trace_path)
                if module in records:
                    raise AuditInputError(f"module {module!r} has duplicate ILean artifacts")
                records[module] = _ArchivedModule(
                    module,
                    display,
                    ilean_digest,
                    olean_path,
                    olean_digest,
                    trace_path,
                    trace_digest,
                    trace,
                )
                consumed.update((display, olean_path, trace_path))
    except AuditInputError:
        raise
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise AuditInputError(f"cannot read root-package build archive: {exc}") from exc
    if not records:
        raise AuditInputError("root-package build archive contains no ILean artifacts")
    orphaned = sorted(set(names) - consumed)
    if orphaned:
        raise AuditInputError(
            "root-package build archive contains orphan artifact: " + orphaned[0]
        )
    evidence.verify()
    return _ArchiveSnapshot(tuple(sorted(records)), records)


def _safe_archive_path(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name or len(name) > 16 * 1024:
        raise AuditInputError(f"unsafe ILean archive member path: {name!r}")
    while name.startswith("./"):
        name = name[2:]
    raw = name.split("/")
    if name.startswith("/") or not raw or any(part in {"", ".", ".."} for part in raw):
        raise AuditInputError(f"unsafe ILean archive member path: {name!r}")
    if any(
        not _is_nfc_text(part) or not _portable_component(part)
        for part in raw
    ):
        raise AuditInputError(f"noncanonical ILean archive member path: {name!r}")
    return tuple(raw)


def _portable_component(part: str) -> bool:
    if (
        part.endswith((".", " "))
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in part)
    ):
        return False
    return part.split(".", 1)[0].rstrip(" ").upper() not in _WINDOWS_DEVICE_NAMES


def _validate_archive_layout(kinds: Mapping[str, str]) -> None:
    for path in kinds:
        parts = path.split("/")
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if parent in kinds and kinds[parent] != "directory":
                raise AuditInputError(f"build archive member has a non-directory parent: {path}")


def _read_archive_member(
    packed: tarfile.TarFile,
    member: tarfile.TarInfo,
    kind: str,
    display: str,
    maximum_bytes: int,
    *,
    collect: bool,
) -> tuple[bytes | None, str]:
    if member.size < 0 or member.size > maximum_bytes:
        raise AuditInputError(f"{kind} archive member exceeds {maximum_bytes} bytes: {display}")
    source = packed.extractfile(member)
    if source is None:
        raise AuditInputError(f"cannot read {kind} archive member: {display}")
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if collect else None
    total = 0
    with source:
        while True:
            chunk = source.read(min(1024 * 1024, maximum_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise AuditInputError(f"{kind} archive member exceeds {maximum_bytes} bytes: {display}")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
    if total != member.size:
        raise AuditInputError(f"{kind} archive member was truncated: {display}")
    return (b"".join(chunks) if chunks is not None else None), digest.hexdigest()


def _decode_json(data: bytes, kind: str, display: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AuditInputError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise AuditInputError(f"nonstandard JSON constant {value!r}")

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except AuditInputError as exc:
        raise AuditInputError(f"malformed {kind} metadata in {display}: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise AuditInputError(f"malformed {kind} metadata in {display}: {exc}") from exc


def _module_from_metadata(metadata: object, parts: tuple[str, ...], display: str) -> str:
    if not isinstance(metadata, dict):
        raise AuditInputError(f"ILean metadata is not an object: {display}")
    module = metadata.get("module")
    if not isinstance(module, str):
        raise AuditInputError(f"ILean metadata has an invalid module name: {display}")
    module_parts = _module_parts(module, display)
    if isinstance(metadata.get("version"), bool) or not isinstance(metadata.get("version"), int):
        raise AuditInputError(f"ILean metadata has no integer version: {display}")
    for field in ("decls", "references"):
        if not isinstance(metadata.get(field), dict):
            raise AuditInputError(f"ILean metadata has an invalid {field} field: {display}")
    if not isinstance(metadata.get("directImports"), list):
        raise AuditInputError(f"ILean metadata has an invalid directImports field: {display}")
    suffix = (*module_parts[:-1], f"{module_parts[-1]}.ilean")
    if len(parts) < len(suffix) or parts[-len(suffix) :] != suffix:
        raise AuditInputError(f"ILean module {module!r} does not match its archive path: {display}")
    return module


def _module_parts(module: str, display: str) -> tuple[str, ...]:
    if not module or len(module) > _MAX_NAME_LENGTH or not _is_nfc_text(module):
        raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
    parts: list[str] = []
    index = 0
    while index < len(module):
        if module[index] == "«":
            end = module.find("»", index + 1)
            if end < 0:
                raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
            part = module[index + 1 : end]
            index = end + 1
        else:
            end = module.find(".", index)
            if end < 0:
                end = len(module)
            part = module[index:end]
            index = end
            if not part or not (part[0].isalpha() or part[0] == "_"):
                raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
            if any(not (character.isalnum() or character in "_'") for character in part):
                raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
        if not part or part in {".", ".."} or any(
            ord(character) < 32 or character in "/\\«»" for character in part
        ):
            raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
        parts.append(part)
        if index == len(module):
            break
        if module[index] != ".":
            raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
        index += 1
        if index == len(module):
            raise AuditInputError(f"invalid Lean name in {display}: {module!r}")
    return tuple(parts)


def _validate_lean_name(name: str, article_path: str) -> None:
    try:
        _module_parts(name, article_path)
    except AuditInputError as exc:
        raise AuditInputError(
            f"{article_path}: invalid Lean declaration name in blueprint: {name!r}"
        ) from exc


def _validate_root_trace(trace: object, module: str, package: str, display: str) -> None:
    if not isinstance(trace, dict) or trace.get("synthetic") is not False:
        raise AuditInputError(f"invalid Lake trace metadata: {display}")
    strings = set(_json_strings(trace))
    if f"Module.name: {module}" not in strings:
        raise AuditInputError(f"Lake trace does not identify module {module!r}: {display}")
    trace_package = _package_trace_name(package)
    if f"Package.id?: (some {trace_package})" not in strings:
        raise AuditInputError(f"Lake trace does not identify root package {package!r}: {display}")


def _json_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _json_strings(key)
            yield from _json_strings(item)


def _open_blueprint_tree(blueprint: Path) -> BoundDirectoryTree:
    absolute = lexical_absolute_path(blueprint)
    selection = TreeSelection(
        include=lambda path, mode: (
            bool(path.parts)
            and path.parts[0] == "roadmap"
            and (stat.S_ISDIR(mode) or stat.S_ISLNK(mode) or path.suffix == ".md")
        ),
        descend=lambda path: path == PurePosixPath("roadmap") or (
            bool(path.parts) and path.parts[0] == "roadmap"
        ),
        record_omitted=False,
        limits=_BLUEPRINT_LIMITS,
    )
    try:
        return BoundDirectoryTree(absolute, selection=selection)
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot retain blueprint: {exc}") from exc


def _capture_blueprint(tree: BoundDirectoryTree) -> TreeSnapshot:
    try:
        snapshot = tree.capture()
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot capture blueprint: {exc}") from exc
    if "roadmap" not in snapshot.directories:
        raise AuditInputError("blueprint has no roadmap directory")
    issues = snapshot.unsupported_entries()
    if issues:
        path, reason = issues[0]
        raise AuditInputError(f"unsafe blueprint entry {path}: {reason}")
    return snapshot


def _open_project_tree(lean_root: Path) -> BoundDirectoryTree:
    try:
        return BoundDirectoryTree(lexical_absolute_path(lean_root))
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot retain Lean project root: {exc}") from exc


def _capture_project_inputs(tree: BoundDirectoryTree) -> _ProjectInputSnapshot:
    manifest_evidence = _open_bound_file(
        tree.root / "lake-manifest.json",
        "Lean project lake-manifest.json",
        _MAX_MANIFEST_BYTES,
        collect=True,
    )
    try:
        if manifest_evidence.data is None:
            raise AuditInputError("Lean project lake-manifest.json was not captured")
        manifest_bytes = manifest_evidence.data
        packages_dir = _manifest_packages_dir(manifest_bytes)

        def inside_packages(path: PurePosixPath) -> bool:
            return path == packages_dir or packages_dir in path.parents

        snapshot = _capture_project_tree(tree, inside_packages=inside_packages)
        manifest_evidence.verify()
    finally:
        manifest_evidence.close()
    files = dict(snapshot.files)
    if files.get("lake-manifest.json") != manifest_bytes:
        raise AuditInputError("Lean project lake-manifest.json changed while inputs were captured")
    aliases = _snapshot_aliases(snapshot)
    for path in files:
        _require_canonical_snapshot_path(aliases, path, f"Lean project input {path}")
    control_limits = {
        "lean-toolchain": _MAX_TOOLCHAIN_BYTES,
        "lake-manifest.json": _MAX_MANIFEST_BYTES,
        "lakefile.lean": _MAX_CONFIG_BYTES,
        "lakefile.toml": _MAX_CONFIG_BYTES,
    }
    for name, maximum in control_limits.items():
        if name in files and len(files[name]) > maximum:
            raise AuditInputError(f"Lean project {name} exceeds {maximum} bytes")
    for name in ("lean-toolchain", "lake-manifest.json"):
        _require_snapshot_file(snapshot, aliases, name, f"Lean project {name}")
    lakefiles = [name for name in ("lakefile.lean", "lakefile.toml") if name in files]
    if len(lakefiles) != 1:
        raise AuditInputError("Lean project must contain exactly one regular lakefile.lean or lakefile.toml")
    _require_snapshot_file(snapshot, aliases, lakefiles[0], "Lean project lakefile")
    if not files["lean-toolchain"].strip():
        raise AuditInputError("Lean project lean-toolchain is empty")
    _reject_file_aliases(tuple(files))
    return _ProjectInputSnapshot(
        manifest=manifest_bytes,
        files=tuple(
            (path, hashlib.sha256(data).hexdigest())
            for path, data in sorted(snapshot.files)
        ),
    )


def _capture_project_tree(
    tree: BoundDirectoryTree,
    *,
    inside_packages: Callable[[PurePosixPath], bool],
) -> TreeSnapshot:
    def included(path: PurePosixPath, _mode: int) -> bool:
        return (
            len(path.parts) == 1 and path.name in _PROJECT_CONTROL_FILES
        ) or path.suffix == ".lean"

    def descended(path: PurePosixPath) -> bool:
        return (
            bool(path.parts)
            and path.parts[0] not in _PROJECT_IGNORED_DIRECTORIES
            and not inside_packages(path)
        )

    def byte_limit(path: PurePosixPath) -> int:
        if path.name == "lean-toolchain" and len(path.parts) == 1:
            return _MAX_TOOLCHAIN_BYTES
        if path.name == "lake-manifest.json" and len(path.parts) == 1:
            return _MAX_MANIFEST_BYTES
        if path.name in {"lakefile.lean", "lakefile.toml"} and len(path.parts) == 1:
            return _MAX_CONFIG_BYTES
        return _MAX_SOURCE_BYTES

    selection = TreeSelection(
        include=included,
        descend=descended,
        byte_limit=byte_limit,
        record_omitted=True,
        limits=_PROJECT_INPUT_LIMITS,
    )
    try:
        snapshot = tree.capture(selection=selection)
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot capture Lean project inputs: {exc}") from exc
    issues = snapshot.unsupported_entries()
    if issues:
        path, reason = issues[0]
        raise AuditInputError(f"unsafe Lean project input {path}: {reason}")
    return snapshot


def _manifest_packages_dir(data: bytes) -> PurePosixPath:
    manifest = _decode_json(data, "Lake manifest", "lake-manifest.json")
    if not isinstance(manifest, dict):
        raise AuditInputError("Lake manifest must be a JSON object")
    return _safe_manifest_path(manifest.get("packagesDir", ".lake/packages"), "Lake packagesDir")


def _mathlib_manifest_from_bytes(data: bytes) -> _MathlibManifest:
    manifest = _decode_json(data, "Lake manifest", "lake-manifest.json")
    if not isinstance(manifest, dict):
        raise AuditInputError("Lake manifest must be a JSON object")
    version_fields = [
        (field, manifest[field])
        for field in ("version", "schemaVersion")
        if field in manifest
    ]
    if len(version_fields) != 1:
        raise AuditInputError(
            "Lake manifest must contain exactly one version or schemaVersion field"
        )
    _field, version = version_fields[0]
    if not isinstance(version, str):
        raise AuditInputError("Lake manifest schema version must be a semantic version")
    match = _MANIFEST_VERSION.fullmatch(version)
    if match is None:
        raise AuditInputError("Lake manifest schema version must be a semantic version")
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    if not ((major == 0 and minor >= 7) or major == 1):
        raise AuditInputError(f"unsupported Lake manifest schema version {version!r}")

    packages = manifest.get("packages")
    if not isinstance(packages, list):
        raise AuditInputError("Lake manifest must contain a package list")
    if len(packages) > _MAX_MANIFEST_PACKAGES:
        raise AuditInputError(
            f"Lake manifest exceeds package limit {_MAX_MANIFEST_PACKAGES}"
        )
    if any(not isinstance(entry, dict) for entry in packages):
        raise AuditInputError("Lake manifest package entries must be JSON objects")
    entries = [entry for entry in packages if entry.get("name") == "mathlib"]
    if len(entries) != 1:
        raise AuditInputError("Lake manifest must contain exactly one mathlib package entry")
    entry = entries[0]
    if entry.get("type") != "git":
        raise AuditInputError("Lake manifest Mathlib entry must be a Git dependency")
    if entry.get("scope") not in {None, "", "leanprover-community"}:
        raise AuditInputError("Lake manifest Mathlib entry has an unsupported package scope")
    if entry.get("url") not in _CANONICAL_MATHLIB_URLS:
        raise AuditInputError(
            "Lake manifest Mathlib URL must identify the canonical mathlib4 repository"
        )
    revision = entry.get("rev")
    if not isinstance(revision, str) or _FULL_GIT_REVISION.fullmatch(revision) is None:
        raise AuditInputError("Lake manifest Mathlib revision must be a full lowercase 40-hex commit")
    if entry.get("subDir") is not None:
        raise AuditInputError("Lake manifest Mathlib dependency must not select a subdirectory")
    packages_dir = _manifest_packages_dir(data)
    return _MathlibManifest(packages_dir=packages_dir, revision=revision)


def _safe_manifest_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AuditInputError(f"{label} must be a nonempty confined relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(
            part in {"", ".", ".."}
            or not _is_nfc_text(part)
            or not _portable_component(part)
            for part in path.parts
        )
    ):
        raise AuditInputError(f"{label} must be a canonical confined relative path")
    return path


def _open_mathlib_checkout(
    project: BoundDirectoryTree,
    manifest: _MathlibManifest,
) -> BoundDirectoryTree:
    project.verify()
    checkout_path = project.root.joinpath(*manifest.packages_dir.parts, "mathlib")
    try:
        binding = open_directory(checkout_path)
    except OSError as exc:
        raise AuditInputError("cannot retain the manifest-selected Mathlib checkout") from exc
    try:
        project_parts = project.root.parts
        checkout_parts = checkout_path.parts
        if checkout_parts[: len(project_parts)] != project_parts:
            raise AuditInputError("Mathlib checkout escapes the Lean project")
        root_index = len(project_parts) - 1
        if binding.identities[root_index] != project.identity:
            raise AuditInputError("Mathlib checkout is not below the retained Lean project")
        for index in range(len(project_parts), len(checkout_parts)):
            _require_canonical_name(
                binding.descriptors[index - 1],
                checkout_parts[index],
                "Mathlib checkout",
            )
        identity = binding.identity
    finally:
        binding.close()
    try:
        checkout = BoundDirectoryTree(checkout_path, expected_identity=identity)
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot retain Mathlib checkout: {exc}") from exc
    project.verify()
    return checkout


def _validate_mathlib_checkout(
    checkout: BoundDirectoryTree,
    manifest: _MathlibManifest,
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
    allowed_build_roots: tuple[PurePosixPath, ...] | None = None,
) -> None:
    checkout.verify()
    try:
        git_directory = open_directory(checkout.root / ".git")
    except OSError as exc:
        raise AuditInputError("Mathlib checkout .git must be a real directory") from exc
    git_directory.close()

    config = _git_command(
        checkout.root,
        ("config", "--local", "--name-only", "--list", "-z"),
        deadline,
        "Mathlib local Git configuration",
        environment=environment,
    ).stdout
    try:
        config_keys = [value.decode("utf-8").casefold() for value in config.split(b"\0") if value]
    except UnicodeError as exc:
        raise AuditInputError("Mathlib local Git configuration is not UTF-8") from exc
    unsafe_config = sorted(
        key
        for key in config_keys
        if key.startswith(("filter.", "include.", "includeif."))
        or key
        in {
            "core.attributesfile",
            "core.fsmonitor",
            "core.fsmonitorhookversion",
            "core.hookspath",
            "extensions.worktreeconfig",
        }
    )
    if unsafe_config:
        raise AuditInputError(
            "Mathlib checkout has unsafe local Git configuration: "
            + ", ".join(unsafe_config)
        )
    attributes = checkout.root / ".git/info/attributes"
    try:
        attributes_status = os.lstat(attributes)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AuditInputError("cannot inspect Mathlib private attributes file") from exc
    else:
        if not stat.S_ISREG(attributes_status.st_mode) or attributes_status.st_size:
            raise AuditInputError("Mathlib checkout has a private attributes file")

    top = _git_text(
        checkout.root,
        ("rev-parse", "--show-toplevel"),
        deadline,
        "Mathlib Git top level",
        environment=environment,
    )
    if lexical_absolute_path(top) != checkout.root:
        raise AuditInputError("Mathlib package directory is not the Git checkout root")
    head = _git_text(
        checkout.root,
        ("rev-parse", "--verify", "HEAD^{commit}"),
        deadline,
        "Mathlib checkout HEAD",
        environment=environment,
    )
    if head != manifest.revision:
        raise AuditInputError(
            f"Mathlib checkout HEAD {head!r} does not match manifest revision {manifest.revision!r}"
        )
    status = _git_command(
        checkout.root,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        deadline,
        "Mathlib checkout status",
        environment=environment,
    ).stdout
    if status:
        raise AuditInputError("Mathlib checkout has tracked or visible untracked changes")
    if allowed_build_roots is not None:
        untracked = _git_command(
            checkout.root,
            ("ls-files", "--others", "-z"),
            deadline,
            "Mathlib untracked-file inventory",
            environment=environment,
        ).stdout
        try:
            untracked_paths = tuple(
                PurePosixPath(value.decode("utf-8"))
                for value in untracked.split(b"\0")
                if value
            )
        except UnicodeError as exc:
            raise AuditInputError("Mathlib untracked-file inventory is not UTF-8") from exc
        allowed_roots = (PurePosixPath(".lake"), *allowed_build_roots)
        for path in untracked_paths:
            if not any(path == root or root in path.parents for root in allowed_roots):
                raise AuditInputError("Mathlib checkout has untracked files outside build roots")
    flags = _git_command(
        checkout.root,
        ("ls-files", "-v", "-z"),
        deadline,
        "Mathlib index flags",
        environment=environment,
    ).stdout
    if any(not record.startswith(b"H ") for record in flags.split(b"\0") if record):
        raise AuditInputError("Mathlib checkout uses nonstandard Git index flags")
    checkout.verify()


def _git_command(
    checkout: Path,
    arguments: Sequence[str],
    deadline: _Deadline,
    label: str,
    *,
    environment: Mapping[str, str] | None,
) -> _CommandResult:
    return _checked_command(
        ["git", *arguments],
        cwd=checkout,
        deadline=deadline,
        label=label,
        environment=environment,
    )


def _git_text(
    checkout: Path,
    arguments: Sequence[str],
    deadline: _Deadline,
    label: str,
    *,
    environment: Mapping[str, str] | None,
) -> str:
    value = _git_command(
        checkout,
        arguments,
        deadline,
        label,
        environment=environment,
    ).stdout
    try:
        text = value.decode("utf-8").strip()
    except UnicodeError as exc:
        raise AuditInputError(f"{label} is not UTF-8") from exc
    if "\x00" in text:
        raise AuditInputError(f"{label} contains a null byte")
    return text


def _verify_canonical_mathlib_revision(
    private: Path,
    revision: str,
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
) -> None:
    if _FULL_GIT_REVISION.fullmatch(revision) is None:
        raise AuditInputError("canonical Mathlib revision is not a full lowercase commit")
    repository = private / "canonical-mathlib.git"
    _checked_command(
        ["git", "init", "--bare", "--quiet", str(repository)],
        cwd=private,
        deadline=deadline,
        label="private canonical Mathlib repository initialization",
        environment=environment,
    )
    _git_command(
        repository,
        (
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=1",
            "--filter=blob:none",
            _CANONICAL_MATHLIB_URL,
            revision,
        ),
        deadline,
        "canonical Mathlib revision fetch",
        environment=environment,
    )
    fetched = _git_text(
        repository,
        ("rev-parse", "--verify", "FETCH_HEAD^{commit}"),
        deadline,
        "canonical Mathlib fetched revision",
        environment=environment,
    )
    if fetched != revision:
        raise AuditInputError("canonical Mathlib fetch did not resolve the manifest revision")
    _validate_private_repository_size(repository)


def _validate_private_repository_size(repository: Path) -> None:
    selection = TreeSelection(
        include=lambda _path, mode: not stat.S_ISREG(mode),
        descend=lambda _path: True,
        placeholder=lambda _path, mode: stat.S_ISREG(mode),
        record_omitted=False,
        limits=_PROJECT_INPUT_LIMITS,
    )
    try:
        tree = BoundDirectoryTree(repository, selection=selection)
        before = tree.capture()
    except TreeSnapshotError as exc:
        raise AuditInputError(f"canonical Mathlib fetch exceeds repository limits: {exc}") from exc
    try:
        issues = before.unsupported_entries()
        if issues:
            path, reason = issues[0]
            raise AuditInputError(f"unsafe canonical Mathlib repository entry {path}: {reason}")
        total = 0
        for relative in before.placeholders:
            try:
                metadata = os.lstat(repository.joinpath(*PurePosixPath(relative).parts))
            except OSError as exc:
                raise AuditInputError("canonical Mathlib repository changed during inspection") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise AuditInputError("canonical Mathlib repository changed during inspection")
            total += metadata.st_size
            if total > _MAX_REMOTE_REPOSITORY_BYTES:
                raise AuditInputError(
                    "canonical Mathlib repository exceeds "
                    f"{_MAX_REMOTE_REPOSITORY_BYTES} bytes"
                )
        after = tree.capture()
        if after.generation_revision != before.generation_revision:
            raise AuditInputError("canonical Mathlib repository changed during inspection")
    finally:
        tree.close()


def _capture_mathlib_sources(
    checkout: BoundDirectoryTree,
    source_files: tuple[str, ...],
    revision: str,
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
) -> _ArtifactSetSnapshot:
    targets = {PurePosixPath(path) for path in source_files}
    prefixes = {
        PurePosixPath(*path.parts[:length])
        for path in targets
        for length in range(1, len(path.parts))
    }
    target_keys = {
        tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)
        for path in targets
    }
    prefix_keys = {
        tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)
        for path in prefixes
    }
    selected_keys = target_keys | prefix_keys

    def normalized(path: PurePosixPath) -> tuple[str, ...]:
        return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)

    selection = TreeSelection(
        include=lambda path, _mode: normalized(path) in selected_keys,
        descend=lambda path: normalized(path) in prefix_keys,
        byte_limit=lambda _path: _MAX_SOURCE_BYTES,
        record_omitted=False,
        limits=_PROJECT_INPUT_LIMITS,
    )
    try:
        snapshot = checkout.capture(selection=selection)
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot capture Mathlib source modules: {exc}") from exc
    issues = snapshot.unsupported_entries()
    if issues:
        path, reason = issues[0]
        raise AuditInputError(f"unsafe Mathlib source entry {path}: {reason}")
    aliases = _snapshot_aliases(snapshot)
    files = dict(snapshot.files)
    for path in sorted(source_files):
        _require_snapshot_file(snapshot, aliases, path, f"Mathlib source {path}")
    blobs = _mathlib_tree_blobs(
        checkout.root,
        source_files,
        revision,
        deadline,
        environment=environment,
    )
    digests: list[tuple[str, str]] = []
    for path in sorted(source_files):
        data = files[path]
        git_blob = hashlib.sha1(
            b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        ).hexdigest()
        if blobs.get(path) != git_blob:
            raise AuditInputError(
                f"Mathlib source {path} does not match its blob at revision {revision}"
            )
        digests.append((path, hashlib.sha256(data).hexdigest()))
    return _ArtifactSetSnapshot(snapshot.generation_revision, tuple(digests))


def _mathlib_tree_blobs(
    checkout: Path,
    source_files: tuple[str, ...],
    revision: str,
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for start in range(0, len(source_files), _QUERY_BATCH_SIZE):
        batch = source_files[start : start + _QUERY_BATCH_SIZE]
        output = _git_command(
            checkout,
            ("ls-tree", "-rz", "--full-tree", revision, "--", *batch),
            deadline,
            "Mathlib revision source inventory",
            environment=environment,
        ).stdout
        for record in output.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, kind, oid = header.decode("ascii").split(" ")
                path = raw_path.decode("utf-8")
            except (UnicodeError, ValueError) as exc:
                raise AuditInputError("Git returned malformed Mathlib source metadata") from exc
            if (
                mode not in {"100644", "100755"}
                or kind != "blob"
                or _FULL_GIT_REVISION.fullmatch(oid) is None
                or path not in batch
                or path in result
            ):
                raise AuditInputError("Git returned ambiguous Mathlib source metadata")
            result[path] = oid
        missing = set(batch) - set(result)
        if missing:
            raise AuditInputError(
                "manifest revision does not contain claimed Mathlib source: "
                + ", ".join(sorted(missing))
            )
    return result


def _snapshot_paths(snapshot: TreeSnapshot) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *snapshot.directories,
                *(path for path, _data in snapshot.files),
                *(path for path, _target in snapshot.symlinks),
                *(path for path, _mode in snapshot.special),
                *snapshot.placeholders,
                *(path for path, _kind in snapshot.omitted),
            }
        )
    )


def _snapshot_aliases(snapshot: TreeSnapshot) -> dict[tuple[str, ...], tuple[str, ...]]:
    grouped: dict[tuple[str, ...], list[str]] = {}
    for path in _snapshot_paths(snapshot):
        key = tuple(
            unicodedata.normalize("NFC", part).casefold()
            for part in PurePosixPath(path).parts
        )
        grouped.setdefault(key, []).append(path)
    return {key: tuple(values) for key, values in grouped.items()}


def _require_canonical_snapshot_path(
    aliases: Mapping[tuple[str, ...], tuple[str, ...]],
    relative: str,
    label: str,
) -> None:
    parts = PurePosixPath(relative).parts
    if not parts or any(not _is_nfc_text(part) or not _portable_component(part) for part in parts):
        raise AuditInputError(f"{label} is missing, aliased, or noncanonical")
    for length in range(1, len(parts) + 1):
        prefix = "/".join(parts[:length])
        folded = tuple(
            unicodedata.normalize("NFC", part).casefold()
            for part in parts[:length]
        )
        if aliases.get(folded, ()) != (prefix,):
            raise AuditInputError(f"{label} is missing, aliased, or noncanonical")


def _require_snapshot_file(
    snapshot: TreeSnapshot,
    aliases: Mapping[tuple[str, ...], tuple[str, ...]],
    relative: str,
    label: str,
) -> bytes:
    _require_canonical_snapshot_path(aliases, relative, label)
    files = dict(snapshot.files)
    if relative not in files:
        raise AuditInputError(f"{label} is not a regular file")
    return files[relative]


def _reject_file_aliases(paths: tuple[str, ...]) -> None:
    aliases: dict[tuple[str, ...], str] = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        key = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
        previous = aliases.setdefault(key, path)
        if previous != path:
            raise AuditInputError(f"Lean project inputs are case- or Unicode-aliased: {previous} and {path}")


def _inspect_root_artifacts(
    project: BoundDirectoryTree,
    project_inputs: _ProjectInputSnapshot,
    archive: _ArchiveSnapshot,
    deadline: _Deadline,
    root_package: str,
    *,
    environment: Mapping[str, str] | None,
) -> _ArtifactSetSnapshot:
    paths = _query_ilean_paths(
        project.root,
        archive.modules,
        deadline,
        package=root_package,
        environment=environment,
    )
    project_files = dict(project_inputs.files)
    expected: dict[str, str] = {}
    for module in archive.modules:
        record = archive.records[module]
        if _trace_package(record.trace) != _package_trace_name(root_package):
            raise AuditInputError(f"root-package trace ownership changed for module {module!r}")
        source = _root_source_from_trace(record.trace, project.root, module)
        source_relative = source.relative_to(project.root).as_posix()
        if source_relative not in project_files:
            raise AuditInputError(
                f"root source for {module!r} was not present in the retained pre-build inputs"
            )
        ilean = paths[module]
        _validate_live_artifact_path(project.root, ilean, module)
        for path, digest in zip(
            (ilean, ilean.with_suffix(".olean"), ilean.with_suffix(".trace")),
            (record.ilean_digest, record.olean_digest, record.trace_digest),
        ):
            relative = path.relative_to(project.root).as_posix()
            if relative in expected:
                raise AuditInputError(f"multiple root modules resolve to artifact {relative}")
            expected[relative] = digest
    return _capture_artifact_set(project, tuple(sorted(expected)), expected_digests=expected)


def _trace_package(trace: object) -> str:
    if not isinstance(trace, dict):
        raise AuditInputError("Lake trace metadata is not an object")
    prefix = "Package.id?: (some "
    values = {
        value[len(prefix) : -1]
        for value in _json_strings(trace)
        if value.startswith(prefix) and value.endswith(")")
    }
    if len(values) != 1:
        raise AuditInputError("Lake trace has ambiguous package ownership")
    return next(iter(values))


def _root_source_from_trace(trace: object, project: Path, module: str) -> Path:
    if not isinstance(trace, dict) or not isinstance(trace.get("inputs"), list):
        raise AuditInputError(f"Lake trace has no source evidence for module {module!r}")
    candidates = []
    for item in trace["inputs"]:
        if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and item[0].endswith(".lean"):
            candidates.append(item[0])
    if len(candidates) != 1:
        raise AuditInputError(f"Lake trace has ambiguous source evidence for module {module!r}")
    raw = Path(candidates[0])
    if any(part in {"", ".", ".."} for part in raw.parts):
        raise AuditInputError(f"root source path is not canonical for module {module!r}")
    source = raw if raw.is_absolute() else project / raw
    source = lexical_absolute_path(source)
    _require_within(source, project, f"root source for {module}")
    suffix = (*_module_parts(module, module)[:-1], f"{_module_parts(module, module)[-1]}.lean")
    if len(source.parts) < len(suffix) or source.parts[-len(suffix) :] != suffix:
        raise AuditInputError(f"root source path does not match module {module!r}")
    if ".lake" in source.relative_to(project).parts:
        raise AuditInputError(f"root source for {module!r} is inside the build directory")
    return source


def _validate_live_artifact_path(project: Path, ilean: Path, module: str) -> None:
    _require_within(ilean, project, "root-package ILean artifact")
    parts = _module_parts(module, module)
    suffix = ("lib", "lean", *parts[:-1], f"{parts[-1]}.ilean")
    if len(ilean.parts) < len(suffix) or ilean.parts[-len(suffix) :] != suffix:
        raise AuditInputError(f"root-package ILean path does not match module {module!r}")


def _capture_artifact_set(
    project: BoundDirectoryTree,
    paths: tuple[str, ...],
    *,
    expected_digests: Mapping[str, str] | None,
    label: str = "root-package",
    maximum_total_bytes: int | None = None,
) -> _ArtifactSetSnapshot:
    targets = {PurePosixPath(path) for path in paths}
    prefixes = {
        PurePosixPath(*path.parts[:length])
        for path in targets
        for length in range(1, len(path.parts))
    }
    selection = TreeSelection(
        include=lambda path, mode: path in targets and not stat.S_ISREG(mode),
        descend=lambda path: path in prefixes,
        placeholder=lambda path, mode: path in targets and stat.S_ISREG(mode),
        record_omitted=True,
        limits=TreeCaptureLimits(max_entries=_MAX_DIRECTORY_ENTRIES, max_depth=64),
    )
    try:
        before = project.capture(selection=selection)
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot capture {label} artifacts: {exc}") from exc
    issues = before.unsupported_entries()
    if issues:
        path, reason = issues[0]
        raise AuditInputError(f"unsafe {label} artifact {path}: {reason}")
    aliases = _snapshot_aliases(before)
    digests: list[tuple[str, str]] = []
    total_bytes = 0
    for relative in sorted(paths):
        _require_snapshot_placeholder(before, aliases, relative, label=label)
        suffix = PurePosixPath(relative).suffix
        maximum = {
            ".ilean": _MAX_ILEAN_BYTES,
            ".olean": _MAX_OLEAN_BYTES,
            ".trace": _MAX_TRACE_BYTES,
        }.get(suffix)
        if maximum is None:
            raise AuditInputError(f"unsupported {label} artifact extension: {relative}")
        evidence = _open_bound_file(
            project.root.joinpath(*PurePosixPath(relative).parts),
            f"{label} {suffix[1:].upper()} artifact {relative}",
            maximum,
            collect=False,
            require_canonical=False,
        )
        try:
            digest = evidence.digest
            total_bytes += evidence.signature.size
            if maximum_total_bytes is not None and total_bytes > maximum_total_bytes:
                raise AuditInputError(
                    f"{label} artifacts exceed {maximum_total_bytes} bytes"
                )
        finally:
            evidence.close()
        if expected_digests is not None:
            expected = expected_digests.get(relative)
            if expected is None or digest != expected:
                detail = "packed bytes" if label == "root-package" else "expected bytes"
                raise AuditInputError(
                    f"live {label} artifact does not match {detail}: {relative}"
                )
        digests.append((relative, digest))
    try:
        after = project.capture(selection=selection)
    except TreeSnapshotError as exc:
        raise AuditInputError(f"cannot revalidate {label} artifacts: {exc}") from exc
    if after.generation_revision != before.generation_revision:
        raise AuditInputError(f"{label} artifacts changed while they were inspected")
    return _ArtifactSetSnapshot(before.generation_revision, tuple(digests))


def _require_snapshot_placeholder(
    snapshot: TreeSnapshot,
    aliases: Mapping[tuple[str, ...], tuple[str, ...]],
    relative: str,
    *,
    label: str = "root-package",
) -> None:
    _require_canonical_snapshot_path(aliases, relative, f"{label} artifact")
    if relative not in snapshot.placeholders:
        raise AuditInputError(
            f"{label} artifact is missing, aliased, or not regular: {relative}"
        )


def _query_mathlib_artifact_paths(
    project: Path,
    checkout: Path,
    modules: tuple[str, ...],
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
) -> dict[str, tuple[Path, Path]]:
    if len(modules) > _MAX_MATHLIB_MODULES:
        raise AuditInputError(
            f"blueprint exceeds Mathlib module limit {_MAX_MATHLIB_MODULES}"
        )
    results: dict[str, tuple[Path, Path]] = {}
    for start in range(0, len(modules), _QUERY_BATCH_SIZE):
        batch = modules[start : start + _QUERY_BATCH_SIZE]
        targets = [
            f"@mathlib/+{module}:{extension}"
            for module in batch
            for extension in ("ilean", "olean")
        ]
        completed = _checked_command(
            ["lake", "--rehash", "--json", "query", *targets],
            cwd=project,
            deadline=deadline,
            label="Lake Mathlib artifact query",
            environment=environment,
        )
        try:
            lines = completed.stdout.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise AuditInputError("Lake returned non-UTF-8 Mathlib artifact metadata") from exc
        if len(lines) != len(targets):
            raise AuditInputError("Lake returned an unexpected number of Mathlib artifact paths")
        for index, module in enumerate(batch):
            paths: list[Path] = []
            for offset, extension in enumerate(("ilean", "olean")):
                value = _decode_json(
                    lines[index * 2 + offset].encode("utf-8"),
                    "Lake Mathlib artifact query",
                    module,
                )
                if not isinstance(value, str) or "\x00" in value:
                    raise AuditInputError(f"Lake returned no Mathlib {extension} for {module!r}")
                candidate = Path(value)
                if any(part in {"", ".", ".."} for part in candidate.parts):
                    raise AuditInputError(
                        f"Lake returned a noncanonical Mathlib artifact path for {module!r}"
                    )
                candidate = lexical_absolute_path(
                    candidate if candidate.is_absolute() else project / candidate
                )
                parts = _module_parts(module, module)
                _require_within(candidate, checkout, f"Mathlib {extension} artifact")
                suffix = ("lib", "lean", *parts[:-1], f"{parts[-1]}.{extension}")
                if len(candidate.parts) < len(suffix) or candidate.parts[-len(suffix) :] != suffix:
                    raise AuditInputError(
                        f"Mathlib {extension} path does not match module {module!r}"
                    )
                paths.append(candidate)
            if paths[1] != paths[0].with_suffix(".olean") or module in results:
                raise AuditInputError(f"Lake returned ambiguous Mathlib artifacts for {module!r}")
            results[module] = (paths[0], paths[1])
    return results


def _mathlib_build_roots(
    checkout: Path,
    artifact_paths: Mapping[str, tuple[Path, Path]],
) -> tuple[PurePosixPath, ...]:
    roots: set[PurePosixPath] = set()
    for module, (ilean, _olean) in artifact_paths.items():
        relative = PurePosixPath(ilean.relative_to(checkout).as_posix())
        parts = _module_parts(module, module)
        suffix = ("lib", "lean", *parts[:-1], f"{parts[-1]}.ilean")
        root_parts = relative.parts[: -len(suffix)]
        if not root_parts:
            raise AuditInputError(f"Mathlib build root is empty for module {module!r}")
        roots.add(PurePosixPath(*root_parts))
    if len(roots) != 1:
        raise AuditInputError("Mathlib modules resolve through multiple build roots")
    return tuple(sorted(roots, key=lambda path: path.as_posix()))


def _inspect_mathlib_artifacts(
    checkout: BoundDirectoryTree,
    artifact_paths: Mapping[str, tuple[Path, Path]],
    targets: tuple[BlueprintTarget, ...],
    private: Path,
    project: Path,
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
) -> _MathlibArtifactEvidence:
    claimed: dict[str, set[str]] = {}
    for target in targets:
        if target.owner != "mathlib" or target.expected_module is None:
            continue
        claimed.setdefault(target.expected_module, set()).add(target.name)
    if set(artifact_paths) != set(claimed):
        raise AuditInputError("Mathlib artifact set does not match blueprint modules")

    relative_paths: list[str] = []
    for module, (ilean, olean) in sorted(artifact_paths.items()):
        for artifact in (ilean, olean, ilean.with_suffix(".trace")):
            _require_within(artifact, checkout.root, "Mathlib build artifact")
            relative_paths.append(artifact.relative_to(checkout.root).as_posix())
    if len(set(relative_paths)) != len(relative_paths):
        raise AuditInputError("multiple Mathlib modules resolve to the same build artifact")
    initial = _capture_artifact_set(
        checkout,
        tuple(sorted(relative_paths)),
        expected_digests=None,
        label="Mathlib",
        maximum_total_bytes=_MAX_MATHLIB_ARTIFACT_BYTES,
    )

    lake_hashes: dict[Path, str] = {}
    olean_paths: list[tuple[str, str]] = []
    for module, (ilean, olean) in sorted(artifact_paths.items()):
        trace_path = ilean.with_suffix(".trace")
        ilean_evidence = _open_bound_file(
            ilean,
            f"Mathlib ILean artifact for {module}",
            _MAX_ILEAN_BYTES,
            collect=True,
            require_canonical=False,
        )
        trace_evidence = _open_bound_file(
            trace_path,
            f"Mathlib Lake trace for {module}",
            _MAX_TRACE_BYTES,
            collect=True,
            require_canonical=False,
        )
        try:
            metadata = _decode_json(ilean_evidence.data or b"", "Mathlib ILean", str(ilean))
            actual_module = _module_from_metadata(metadata, ilean.parts, str(ilean))
            if actual_module != module:
                raise AuditInputError(
                    f"Mathlib ILean identifies module {actual_module!r}, not {module!r}"
                )
            assert isinstance(metadata, dict)
            declarations = metadata["decls"]
            assert isinstance(declarations, dict)
            missing = claimed[module] - set(declarations)
            if missing:
                raise AuditInputError(
                    f"Mathlib ILean for {module!r} lacks claimed declaration(s): "
                    + ", ".join(sorted(missing))
                )
            trace = _decode_json(
                trace_evidence.data or b"",
                "Mathlib Lake trace",
                str(trace_path),
            )
            ilean_hash, olean_hash = _mathlib_trace_hashes(trace, module, str(trace_path))
            lake_hashes[ilean] = ilean_hash
            lake_hashes[olean] = olean_hash
        finally:
            trace_evidence.close()
            ilean_evidence.close()
        olean_paths.append((module, str(olean)))

    helper = private / "lake-content-hash.lean"
    _write_private_file(helper, _lake_hash_helper_source())
    observed_hashes = _lake_content_hashes(
        project,
        helper,
        tuple(sorted(lake_hashes, key=str)),
        deadline,
        environment=environment,
    )
    for path, expected in lake_hashes.items():
        if observed_hashes.get(path) != expected:
            raise AuditInputError(
                f"Mathlib artifact content does not match Lake trace descriptor: {path}"
            )
    final = _capture_artifact_set(
        checkout,
        tuple(sorted(relative_paths)),
        expected_digests=dict(initial.digests),
        label="Mathlib",
        maximum_total_bytes=_MAX_MATHLIB_ARTIFACT_BYTES,
    )
    if final != initial:
        raise AuditInputError("Mathlib artifacts changed during provenance validation")
    return _MathlibArtifactEvidence(
        olean_paths=tuple(olean_paths),
        snapshot=initial,
    )


def _mathlib_trace_hashes(trace: object, module: str, display: str) -> tuple[str, str]:
    if not isinstance(trace, dict):
        raise AuditInputError(f"invalid Mathlib Lake trace metadata: {display}")
    schema = trace.get("schemaVersion")
    dep_hash = trace.get("depHash")
    outputs = trace.get("outputs")
    if not isinstance(schema, str):
        raise AuditInputError(f"invalid Mathlib Lake trace metadata: {display}")
    try:
        date.fromisoformat(schema)
    except ValueError as exc:
        raise AuditInputError(f"invalid Mathlib Lake trace metadata: {display}") from exc
    if (
        not isinstance(dep_hash, str)
        or _LAKE_HASH.fullmatch(dep_hash) is None
        or not isinstance(outputs, dict)
    ):
        raise AuditInputError(f"invalid Mathlib Lake trace metadata: {display}")
    ilean_output = outputs.get("i")
    olean_outputs = outputs.get("o")
    if (
        not isinstance(ilean_output, str)
        or re.fullmatch(r"[0-9a-f]{16}\.ilean", ilean_output) is None
        or not isinstance(olean_outputs, list)
        or len(olean_outputs) > 64
        or any(not isinstance(value, str) for value in olean_outputs)
    ):
        raise AuditInputError(f"invalid Mathlib Lake trace outputs: {display}")
    primary_olean = [
        value
        for value in olean_outputs
        if re.fullmatch(r"[0-9a-f]{16}\.olean", value) is not None
    ]
    if len(primary_olean) != 1:
        raise AuditInputError(f"invalid Mathlib Lake trace outputs: {display}")
    if "synthetic" in trace and trace.get("synthetic") is not False:
        raise AuditInputError(f"invalid Mathlib Lake trace metadata: {display}")
    if "inputs" in trace:
        if not isinstance(trace["inputs"], list):
            raise AuditInputError(f"invalid Mathlib Lake trace inputs: {display}")
        strings = set(_json_strings(trace["inputs"]))
        if f"Module.name: {module}" not in strings:
            raise AuditInputError(f"Mathlib Lake trace does not identify module {module!r}")
        if "Package.id?: (some mathlib)" not in strings:
            raise AuditInputError("Mathlib Lake trace does not identify package id 'mathlib'")
    return ilean_output[:16], primary_olean[0][:16]


def _lake_hash_helper_source() -> bytes:
    return """import Lake.Build.Trace

open Lake

def main (args : List String) : IO UInt32 := do
  for path in args do
    IO.println (toString (← computeFileHash path))
  return 0
""".encode("utf-8")


def _lake_content_hashes(
    project: Path,
    helper: Path,
    paths: tuple[Path, ...],
    deadline: _Deadline,
    *,
    environment: Mapping[str, str] | None,
) -> dict[Path, str]:
    hashes: dict[Path, str] = {}
    for start in range(0, len(paths), _HASH_BATCH_SIZE):
        batch = paths[start : start + _HASH_BATCH_SIZE]
        result = _checked_command(
            [
                "lake",
                "env",
                "lean",
                "--trust=0",
                "--run",
                str(helper),
                *(str(path) for path in batch),
            ],
            cwd=project,
            deadline=deadline,
            label="independent Lake content-hash verification",
            environment=environment,
        )
        try:
            lines = result.stdout.decode("ascii").splitlines()
        except UnicodeError as exc:
            raise AuditInputError("Lake content-hash helper returned non-ASCII output") from exc
        if len(lines) != len(batch) or any(_LAKE_HASH.fullmatch(line) is None for line in lines):
            raise AuditInputError("Lake content-hash helper returned malformed output")
        hashes.update(zip(batch, lines))
    return hashes


def _query_ilean_paths(
    project: Path,
    modules: tuple[str, ...],
    deadline: _Deadline,
    *,
    package: str | None,
    environment: Mapping[str, str] | None,
) -> dict[str, Path]:
    results: dict[str, Path] = {}
    for start in range(0, len(modules), _QUERY_BATCH_SIZE):
        batch = modules[start : start + _QUERY_BATCH_SIZE]
        prefix = f"@{package}/+" if package is not None else "+"
        command = ["lake", "query", "--json", *(f"{prefix}{module}:ilean" for module in batch)]
        completed = _checked_command(
            command,
            cwd=project,
            deadline=deadline,
            label=f"Lake {package or 'root-package'} artifact query",
            environment=environment,
        )
        try:
            lines = completed.stdout.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise AuditInputError("Lake returned non-UTF-8 artifact metadata") from exc
        if len(lines) != len(batch):
            raise AuditInputError("Lake returned an unexpected number of artifact paths")
        for module, line in zip(batch, lines):
            value = _decode_json(
                line.encode("utf-8"),
                "Lake artifact query",
                module,
            )
            if not isinstance(value, str) or not value.endswith(".ilean") or "\x00" in value:
                raise AuditInputError(f"Lake returned no ILean artifact for {module!r}")
            candidate = Path(value)
            if any(part == ".." for part in candidate.parts):
                raise AuditInputError(f"Lake returned a traversing artifact path for {module!r}")
            results[module] = lexical_absolute_path(candidate if candidate.is_absolute() else project / candidate)
    return results


def _checked_command(
    arguments: Sequence[str],
    *,
    cwd: Path,
    deadline: _Deadline,
    label: str,
    environment: Mapping[str, str] | None,
) -> _CommandResult:
    result = _run_bounded_command(
        arguments,
        cwd=cwd,
        deadline=deadline,
        environment=_audit_environment(environment),
    )
    if result.returncode != 0:
        detail = _last_output_line(result.stderr) or _last_output_line(result.stdout)
        suffix = f": {detail}" if detail else ""
        raise AuditInputError(f"{label} failed with exit code {result.returncode}{suffix}")
    return result


def _run_bounded_command(
    arguments: Sequence[str],
    *,
    cwd: Path,
    deadline: _Deadline,
    environment: Mapping[str, str],
) -> _CommandResult:
    command = tuple(str(value) for value in arguments)
    if not command or any("\x00" in value for value in command):
        raise AuditInputError("artifact audit subprocess command is malformed")
    creationflags = 0
    start_new_session = os.name == "posix"
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
            shell=False,
            start_new_session=start_new_session,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise AuditInputError(f"cannot start artifact audit subprocess {command[0]!r}: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    outputs = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream, destination: bytearray) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                if len(destination) < _MAX_OUTPUT_BYTES + 1:
                    remaining = _MAX_OUTPUT_BYTES + 1 - len(destination)
                    destination.extend(chunk[:remaining])
                if len(destination) > _MAX_OUTPUT_BYTES:
                    overflow.set()
        except OSError:
            overflow.set()

    threads = (
        threading.Thread(target=drain, args=(process.stdout, outputs[0]), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, outputs[1]), daemon=True),
    )
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                break
            try:
                remaining = deadline.remaining()
            except AuditInputError:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
        if overflow.is_set() or timed_out:
            _terminate_process_group(process)
        elif process.poll() is not None:
            _terminate_surviving_descendants(process)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        for thread in threads:
            thread.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    if timed_out:
        raise AuditInputError("artifact audit exceeded its aggregate subprocess deadline")
    if overflow.is_set():
        raise AuditInputError(f"artifact audit subprocess output exceeds {_MAX_OUTPUT_BYTES} bytes per stream")
    return _CommandResult(command, int(process.returncode or 0), bytes(outputs[0]), bytes(outputs[1]))


def _terminate_surviving_descendants(process: subprocess.Popen[bytes]) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return
    time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
    else:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    _terminate_surviving_descendants(process)


def _audit_environment(supplied: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if supplied is None else supplied
    forbidden = {
        "BASH_ENV",
        "CDPATH",
        "ELAN_TOOLCHAIN",
        "ENV",
        "LAKE_OPTS",
        "LD_PRELOAD",
        "LEAN_OPTS",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LAKE_HOME",
        "LAKE_PKG_URL_MAP",
        "PYTHONHOME",
        "PYTHONPATH",
        "SHELLOPTS",
    }
    environment = {
        key: value
        for key, value in source.items()
        if not key.startswith(("GIT_", "DYLD_")) and key not in forbidden
    }
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


def _last_output_line(value: bytes) -> str:
    lines = value.decode("utf-8", errors="replace").strip().splitlines()
    return lines[-1] if lines else ""


def _require_within(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AuditInputError(f"{label} escapes its owning directory") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise AuditInputError(f"{label} escapes its owning directory")


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise AuditInputError("could not finish writing the private Lean probe")
            offset += written
        os.fsync(descriptor)
    except AuditInputError:
        raise
    except OSError as exc:
        raise AuditInputError(f"cannot create private Lean probe: {exc}") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _lean_name(name: str) -> str:
    result = "Name.anonymous"
    for part in _module_parts(name, name):
        result = f"Name.str ({result}) {json.dumps(part, ensure_ascii=False)}"
    return result


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
