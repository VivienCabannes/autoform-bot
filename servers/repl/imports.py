"""Validate and resolve structured Lean imports for one Lake project."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
import weakref
import zlib
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from servers import (
    ProjectFingerprint,
    clean_lake_environment,
    lean_project_fingerprint,
)

_MAX_DISCOVERY_OUTPUT_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_PROCESS_KILL_WAIT_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_DESCRIPTOR_INSPECTION_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_NONBLOCK")
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_follow_symlinks", ())
    and os.listdir in getattr(os, "supports_fd", ())
)
_MODULE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_RESOLUTION_PROVENANCE = object()
_DESCRIPTOR_SESSION_LIMIT = 64
_VALIDATION_CACHE_LIMIT = 64
_validation_cache_lock = threading.Lock()
_project_validation_locks: weakref.WeakValueDictionary[Path, threading.Lock] = (
    weakref.WeakValueDictionary()
)
_resolved_import_cache: OrderedDict[
    tuple[Path, tuple[str, ...], ProjectFingerprint], ResolvedImports
] = OrderedDict()


class LeanImportError(ValueError):
    """Structured imports cannot be resolved safely for the project."""


class StaleResolvedImportsError(LeanImportError):
    """A resolved import descriptor no longer identifies the same project."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    path: Path
    root: Path
    metadata: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ModuleSelection:
    module: str
    artifact: _FileIdentity
    source: _FileIdentity | None


@dataclass(frozen=True, slots=True)
class _OleanDependency:
    module: str
    artifacts: tuple[_FileIdentity, ...]
    source: _FileIdentity | None


@dataclass(frozen=True, slots=True)
class _DependencySnapshot:
    digest: bytes
    file_count: int


@dataclass(frozen=True, slots=True)
class _ClosureBinding:
    module: str
    artifact_root: int
    source_root: int


@dataclass(frozen=True, slots=True)
class _PackageGeneration:
    root: Path
    revision: str


@dataclass(frozen=True, slots=True)
class _PackageSpec:
    root: Path
    revision: str | None
    config_file: str
    manifest_file: str | None


@dataclass(frozen=True, slots=True)
class _LakeRoots:
    bindings: tuple[tuple[Path, Path | None], ...]
    resolved: tuple[Path, ...]


@dataclass(slots=True)
class _DirectoryBinding:
    """Retain one lexical directory chain while a child is inspected."""

    path: Path
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int, int], ...]
    _closed: bool = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise OSError("directory binding is closed")
        return self.descriptors[-1]

    def verify(self, deadline: float) -> None:
        if self._closed or len(self.descriptors) != len(self.identities):
            raise OSError("directory binding is incomplete")
        _remaining(deadline)
        anchor = self.path.anchor
        opened = os.fstat(self.descriptors[0])
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _directory_identity(opened) != self.identities[0]
            or _directory_identity(named) != self.identities[0]
        ):
            raise OSError("directory anchor changed")
        for index, part in enumerate(self.path.parts[1:], start=1):
            _remaining(deadline)
            opened = os.fstat(self.descriptors[index])
            named = os.stat(
                part,
                dir_fd=self.descriptors[index - 1],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or _directory_identity(opened) != self.identities[index]
                or _directory_identity(named) != self.identities[index]
            ):
                raise OSError("directory component changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(slots=True)
class _DescriptorSession:
    bindings: OrderedDict[Path, _DirectoryBinding] = field(default_factory=OrderedDict)

    def close(self) -> None:
        for binding in reversed(tuple(self.bindings.values())):
            binding.close()
        self.bindings.clear()


_descriptor_sessions = threading.local()


@contextmanager
def _descriptor_session():
    current = getattr(_descriptor_sessions, "current", None)
    if current is not None:
        yield current
        return
    session = _DescriptorSession()
    _descriptor_sessions.current = session
    try:
        yield session
    finally:
        session.close()
        del _descriptor_sessions.current


def _with_descriptor_session(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _descriptor_session():
            return function(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True, init=False)
class ResolvedImports:
    """Validated imports bound to one canonical Lake project root."""

    project_root: Path
    modules: tuple[str, ...]
    project_fingerprint: ProjectFingerprint
    _selections: tuple[_ModuleSelection, ...]
    _dependencies: tuple[_OleanDependency, ...]
    _artifact_roots: _LakeRoots
    _source_roots: _LakeRoots
    _toolchain_artifact_root: Path | None
    _dependency_bindings: bytes | None
    _package_generations: tuple[_PackageGeneration, ...]
    _manifest_config_snapshot: _DependencySnapshot | None
    _dependency_snapshot: _DependencySnapshot | None
    _provenance: object = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object) -> ResolvedImports:
        raise TypeError("ResolvedImports values must come from project import resolution")

    def _assert_complete_resolution(self) -> None:
        if self._provenance is not _RESOLUTION_PROVENANCE:
            raise LeanImportError("untrusted resolved-import descriptor")
        if validate_imports(list(self.modules)) != self.modules:
            raise LeanImportError("resolved imports contain invalid module names")
        unique_modules = tuple(dict.fromkeys(self.modules))
        if not self.modules:
            if (
                self._selections
                or self._dependencies
                or self._artifact_roots.bindings
                or self._source_roots.bindings
                or self._toolchain_artifact_root is not None
                or self._dependency_bindings is not None
                or self._package_generations
                or self._manifest_config_snapshot is not None
                or self._dependency_snapshot is not None
            ):
                raise LeanImportError("empty resolved imports contain discovery state")
            return
        if (
            not self._artifact_roots.bindings
            or not self._source_roots.bindings
            or not self._artifact_roots.resolved
            or not self._source_roots.resolved
            or any(
                resolved is not None
                and resolved not in self._artifact_roots.resolved
                for _, resolved in self._artifact_roots.bindings
            )
            or any(
                resolved is not None
                and resolved not in self._source_roots.resolved
                for _, resolved in self._source_roots.bindings
            )
            or tuple(selection.module for selection in self._selections)
            != unique_modules
            or not self._dependencies
            or self._dependency_bindings is None
            or self._manifest_config_snapshot is None
            or self._dependency_snapshot is None
            or len({dependency.module for dependency in self._dependencies})
            != len(self._dependencies)
            or len(
                {
                    artifact.path
                    for dependency in self._dependencies
                    for artifact in dependency.artifacts
                }
            )
            != sum(len(dependency.artifacts) for dependency in self._dependencies)
            or any(
                not dependency.artifacts
                for dependency in self._dependencies
            )
            or any(
                selection.artifact.path
                not in {
                    artifact.path
                    for dependency in self._dependencies
                    for artifact in dependency.artifacts
                }
                for selection in self._selections
            )
            or any(
                selection.artifact.root not in self._artifact_roots.resolved
                or (
                    selection.source is not None
                    and selection.source.root not in self._source_roots.resolved
                )
                for selection in self._selections
            )
        ):
            raise LeanImportError("resolved imports contain incomplete discovery state")

    @_with_descriptor_session
    def assert_generation_current(self, deadline: float) -> None:
        """Check the cheap project and direct-import generation guards."""
        try:
            self._assert_complete_resolution()
            _remaining(deadline)
            current_root = self.project_root.resolve(strict=True)
            if current_root != self.project_root or not current_root.is_dir():
                raise LeanImportError("the canonical project root changed")
            current = lean_project_fingerprint(current_root)
            if current != self.project_fingerprint:
                raise LeanImportError("project configuration changed")
            _remaining(deadline)
            if self.modules:
                _require_toolchain_pin(current_root / "lean-toolchain", deadline)
                _require_regular_manifest(
                    current_root / "lake-manifest.json",
                    deadline,
                )
                _require_project_configuration_files(current_root, deadline)
                _require_lake_roots_current(self._artifact_roots, deadline)
                _require_lake_roots_current(self._source_roots, deadline)
                current_selections = _resolve_module_selections(
                    tuple(dict.fromkeys(self.modules)),
                    self._artifact_roots.resolved,
                    self._source_roots.resolved,
                    deadline,
                )
                if current_selections != self._selections:
                    raise StaleResolvedImportsError(
                        "resolved Lean imports are stale: import artifacts changed"
                    )
                _require_dependencies_current(
                    self._dependencies,
                    deadline,
                )
                _require_package_generations_current(
                    self._package_generations,
                    deadline,
                )
                if _manifest_configuration_snapshot(
                    self.project_root,
                    self.project_root / "lake-manifest.json",
                    deadline,
                ) != self._manifest_config_snapshot:
                    raise LeanImportError("Lake dependency configuration changed")
            _require_project_fingerprint(
                current_root,
                self.project_fingerprint,
                deadline,
            )
        except TimeoutError:
            raise
        except StaleResolvedImportsError:
            _evict_resolved_imports(self)
            raise
        except (LeanImportError, OSError, RuntimeError) as error:
            _evict_resolved_imports(self)
            raise StaleResolvedImportsError(
                f"resolved Lean imports are stale: {error}"
            ) from error

    @_with_descriptor_session
    def assert_current(self, deadline: float) -> None:
        """Recheck the complete resolved closure before returning a result."""
        try:
            self._assert_complete_resolution()
            if self.modules and (
                _snapshot_dependency_closure(
                    _decode_dependency_bindings(self._dependency_bindings),
                    self._artifact_roots.resolved,
                    self._source_roots.resolved,
                    self._toolchain_artifact_root,
                    deadline,
                )
                != self._dependency_snapshot
            ):
                raise StaleResolvedImportsError(
                    "resolved Lean imports are stale: dependency files changed"
                )
            self.assert_generation_current(deadline)
        except TimeoutError:
            raise
        except StaleResolvedImportsError:
            _evict_resolved_imports(self)
            raise
        except (LeanImportError, OSError, RuntimeError) as error:
            _evict_resolved_imports(self)
            raise StaleResolvedImportsError(
                f"resolved Lean imports are stale: {error}"
            ) from error


def validate_imports(value: Any) -> tuple[str, ...] | None:
    """Validate an optional JSON import list while preserving its exact order."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise LeanImportError("imports must be an array of Lean module names or null")
    modules: list[str] = []
    for index, module in enumerate(value):
        if not isinstance(module, str) or not module:
            raise LeanImportError(f"imports[{index}] must be a non-empty Lean module name")
        parts = module.split(".")
        if any(_MODULE_PART.fullmatch(part) is None for part in parts):
            raise LeanImportError(f"imports[{index}] is not a conservative Lean module name: {module!r}")
        modules.append(module)
    return tuple(modules)


@_with_descriptor_session
def resolve_project_imports(
    project_root: Path,
    modules: tuple[str, ...],
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> ResolvedImports:
    """Require fresh, unambiguous OLean artifacts in Lake-derived roots."""
    def resolved(
        *,
        canonical_root: Path,
        validated_modules: tuple[str, ...],
        fingerprint: ProjectFingerprint,
        selections: tuple[_ModuleSelection, ...] = (),
        dependencies: tuple[_OleanDependency, ...] = (),
        artifact_roots: _LakeRoots | None = None,
        source_roots: _LakeRoots | None = None,
        toolchain_artifact_root: Path | None = None,
        dependency_bindings: bytes | None = None,
        package_generations: tuple[_PackageGeneration, ...] = (),
        manifest_config_snapshot: _DependencySnapshot | None = None,
        dependency_snapshot: _DependencySnapshot | None = None,
    ) -> ResolvedImports:
        artifact_roots = artifact_roots or _LakeRoots((), ())
        source_roots = source_roots or _LakeRoots((), ())
        result = object.__new__(ResolvedImports)
        object.__setattr__(result, "project_root", canonical_root)
        object.__setattr__(result, "modules", validated_modules)
        object.__setattr__(result, "project_fingerprint", fingerprint)
        object.__setattr__(result, "_selections", selections)
        object.__setattr__(result, "_dependencies", dependencies)
        object.__setattr__(result, "_artifact_roots", artifact_roots)
        object.__setattr__(result, "_source_roots", source_roots)
        object.__setattr__(result, "_toolchain_artifact_root", toolchain_artifact_root)
        object.__setattr__(result, "_dependency_bindings", dependency_bindings)
        object.__setattr__(result, "_package_generations", package_generations)
        object.__setattr__(result, "_manifest_config_snapshot", manifest_config_snapshot)
        object.__setattr__(result, "_dependency_snapshot", dependency_snapshot)
        object.__setattr__(result, "_provenance", _RESOLUTION_PROVENANCE)
        result._assert_complete_resolution()
        return result

    if deadline is None:
        if timeout is None:
            raise TypeError("resolve_project_imports requires timeout or deadline")
        deadline = time.monotonic() + timeout
    elif timeout is not None:
        raise TypeError("pass timeout or deadline, not both")
    _remaining(deadline)
    validated_modules = validate_imports(list(modules))
    assert validated_modules is not None
    modules = validated_modules
    try:
        project_root = project_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LeanImportError(f"invalid Lean project root: {project_root}") from error
    if not project_root.is_dir():
        raise LeanImportError(f"Lean project root is not a directory: {project_root}")
    try:
        fingerprint = lean_project_fingerprint(project_root)
    except OSError as error:
        raise LeanImportError("cannot fingerprint the Lean project") from error
    _remaining(deadline)
    if not modules:
        return resolved(
            canonical_root=project_root,
            validated_modules=modules,
            fingerprint=fingerprint,
        )
    with _project_validation(project_root, deadline):
        _require_project_fingerprint(project_root, fingerprint, deadline)
        cache_key = (project_root, modules, fingerprint)
        cached = _cache_get(_resolved_import_cache, cache_key)
        if cached is not None:
            try:
                cached.assert_current(deadline)
            except StaleResolvedImportsError:
                _cache_remove(_resolved_import_cache, cache_key)
                cached = None
            if cached is not None:
                return resolved(
                    canonical_root=cached.project_root,
                    validated_modules=cached.modules,
                    fingerprint=cached.project_fingerprint,
                    selections=cached._selections,
                    dependencies=cached._dependencies,
                    artifact_roots=cached._artifact_roots,
                    source_roots=cached._source_roots,
                    toolchain_artifact_root=cached._toolchain_artifact_root,
                    dependency_bindings=cached._dependency_bindings,
                    package_generations=cached._package_generations,
                    manifest_config_snapshot=cached._manifest_config_snapshot,
                    dependency_snapshot=cached._dependency_snapshot,
                )

        _require_toolchain_pin(project_root / "lean-toolchain", deadline)
        _require_project_configuration_files(project_root, deadline)
        manifest = project_root / "lake-manifest.json"
        _require_regular_manifest(manifest, deadline)
        manifest_config_snapshot = _manifest_configuration_snapshot(
            project_root,
            manifest,
            deadline,
        )
        package_generations = _resolve_package_generations(
            project_root,
            manifest,
            deadline,
        )
        environment = _lake_environment(project_root, deadline=deadline)
        _require_regular_manifest(manifest, deadline)
        _require_project_fingerprint(project_root, fingerprint, deadline)
        if _manifest_configuration_snapshot(
            project_root,
            manifest,
            deadline,
        ) != manifest_config_snapshot:
            raise LeanImportError(
                "Lake dependency configuration changed during import discovery; retry the request"
            )

        artifact_roots = _environment_roots(environment, "LEAN_PATH", deadline)
        source_roots = _environment_roots(environment, "LEAN_SRC_PATH", deadline)
        toolchain_artifact_root = _toolchain_artifact_root(
            environment,
            artifact_roots.resolved,
            deadline,
        )
        unique_modules = tuple(dict.fromkeys(modules))
        selections = _resolve_module_selections(
            unique_modules,
            artifact_roots.resolved,
            source_roots.resolved,
            deadline,
        )
        (
            dependency_bindings,
            dependency_snapshot,
            dependencies,
            package_generations,
        ) = _validate_dependency_state(
            project_root,
            manifest,
            unique_modules,
            selections,
            artifact_roots,
            source_roots,
            toolchain_artifact_root,
            fingerprint,
            manifest_config_snapshot,
            package_generations,
            deadline,
        )
        result = resolved(
            canonical_root=project_root,
            validated_modules=modules,
            fingerprint=fingerprint,
            selections=selections,
            dependencies=dependencies,
            artifact_roots=artifact_roots,
            source_roots=source_roots,
            toolchain_artifact_root=toolchain_artifact_root,
            dependency_bindings=dependency_bindings,
            package_generations=package_generations,
            manifest_config_snapshot=manifest_config_snapshot,
            dependency_snapshot=dependency_snapshot,
        )
        _cache_add(_resolved_import_cache, cache_key, result)
        return result


def _validate_dependency_state(
    project_root: Path,
    manifest: Path,
    unique_modules: tuple[str, ...],
    selections: tuple[_ModuleSelection, ...],
    artifact_roots: _LakeRoots,
    source_roots: _LakeRoots,
    toolchain_artifact_root: Path | None,
    fingerprint: ProjectFingerprint,
    manifest_config_snapshot: _DependencySnapshot,
    package_generations: tuple[_PackageGeneration, ...],
    deadline: float,
) -> tuple[
    bytes,
    _DependencySnapshot,
    tuple[_OleanDependency, ...],
    tuple[_PackageGeneration, ...],
]:
    workspace_modules = _modules_requiring_lake_check(
        selections,
        toolchain_artifact_root,
    )
    modules = _query_transitive_modules(
        project_root,
        workspace_modules,
        deadline,
    )
    dependency_bindings = _bind_dependency_modules(
        modules,
        artifact_roots.resolved,
        source_roots.resolved,
        deadline,
    )
    decoded_bindings = _decode_dependency_bindings(dependency_bindings)
    initial_runtime_snapshot = _snapshot_dependency_closure(
        decoded_bindings,
        artifact_roots.resolved,
        source_roots.resolved,
        None,
        deadline,
    )
    if workspace_modules:
        result = _run_lake(
            [
                "lake",
                "--rehash",
                "--no-build",
                "build",
                *(f"+{module}:olean" for module in workspace_modules),
            ],
            project_root,
            deadline=deadline,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
            raise LeanImportError(
                "requested Lean modules or their dependencies are stale; "
                f"run `lake build` first ({detail or 'Lake reported an out-of-date target'})"
            )
    _remaining(deadline)
    dependencies = tuple(
        _direct_dependency_identity(
            selection,
            deadline,
            require_trace=(
                toolchain_artifact_root is None
                or not _is_relative_to(
                    selection.artifact.path,
                    toolchain_artifact_root,
                )
            ),
        )
        for selection in selections
    )
    _require_regular_manifest(manifest, deadline)
    _require_project_fingerprint(project_root, fingerprint, deadline)
    if _manifest_configuration_snapshot(
        project_root,
        manifest,
        deadline,
    ) != manifest_config_snapshot:
        raise LeanImportError(
            "Lake dependency configuration changed during import discovery; retry the request"
        )
    _require_lake_roots_current(artifact_roots, deadline)
    _require_lake_roots_current(source_roots, deadline)
    if (
        _resolve_module_selections(
            unique_modules,
            artifact_roots.resolved,
            source_roots.resolved,
            deadline,
        )
        != selections
    ):
        raise LeanImportError(
            "Lean import artifacts changed during import discovery; retry the request"
        )
    _require_dependencies_current(dependencies, deadline)
    _require_package_generations_current(package_generations, deadline)
    if workspace_modules:
        confirmed_modules = _query_transitive_modules(
            project_root,
            workspace_modules,
            deadline,
        )
        if confirmed_modules != modules:
            raise LeanImportError(
                "Lean import closure changed during import discovery; retry the request"
            )
        if _bind_dependency_modules(
            confirmed_modules,
            artifact_roots.resolved,
            source_roots.resolved,
            deadline,
        ) != dependency_bindings:
            raise LeanImportError(
                "Lean import closure paths changed during import discovery; retry the request"
            )
    current_runtime_snapshot = _snapshot_dependency_closure(
        decoded_bindings,
        artifact_roots.resolved,
        source_roots.resolved,
        None,
        deadline,
    )
    if current_runtime_snapshot != initial_runtime_snapshot:
        raise LeanImportError(
            "Lean dependency sources changed, or dependency artifacts changed during "
            "import discovery; retry the request"
        )
    dependency_snapshot = _snapshot_dependency_closure(
        decoded_bindings,
        artifact_roots.resolved,
        source_roots.resolved,
        toolchain_artifact_root,
        deadline,
    )
    _require_toolchain_pin(project_root / "lean-toolchain", deadline)
    _require_regular_manifest(manifest, deadline)
    _require_project_configuration_files(project_root, deadline)
    _require_project_fingerprint(project_root, fingerprint, deadline)
    _require_package_generations_current(package_generations, deadline)
    if _manifest_configuration_snapshot(
        project_root,
        manifest,
        deadline,
    ) != manifest_config_snapshot:
        raise LeanImportError(
            "Lake dependency configuration changed during import discovery; retry the request"
        )
    return (
        dependency_bindings,
        dependency_snapshot,
        dependencies,
        package_generations,
    )


def _require_project_fingerprint(
    project_root: Path,
    expected: ProjectFingerprint,
    deadline: float,
) -> None:
    _remaining(deadline)
    try:
        current = lean_project_fingerprint(project_root)
    except OSError as error:
        raise LeanImportError(
            "Lean project changed during import discovery; retry the request"
        ) from error
    _remaining(deadline)
    if current != expected:
        raise LeanImportError(
            "Lean project changed during import discovery; retry the request"
        )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _file_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_binding(path: Path, deadline: float) -> _DirectoryBinding:
    """Open every directory component without following names between checks."""
    if (
        not _DESCRIPTOR_INSPECTION_SUPPORTED
        or not path.is_absolute()
        or not path.anchor
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise LeanImportError("cannot inspect a project file safely on this platform")
    descriptors: list[int] = []
    identities: list[tuple[int, int, int]] = []
    try:
        _remaining(deadline)
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        named = os.stat(path.anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _directory_identity(opened) != _directory_identity(named)
        ):
            raise OSError("directory anchor changed")
        identities.append(_directory_identity(opened))
        for part in path.parts[1:]:
            _remaining(deadline)
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise OSError("directory component is not a directory")
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            opened = os.fstat(child)
            after = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            identity = _directory_identity(opened)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or identity != _directory_identity(before)
                or identity != _directory_identity(after)
            ):
                raise OSError("directory component changed")
            identities.append(identity)
            descriptor = child
        binding = _DirectoryBinding(path, tuple(descriptors), tuple(identities))
        binding.verify(deadline)
        descriptors.clear()
        return binding
    except TimeoutError:
        raise
    except LeanImportError:
        raise
    except (OSError, ValueError, NotImplementedError) as error:
        raise LeanImportError(
            f"project directory changed or contains a symbolic link: {path}"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _root_binding(
    session: _DescriptorSession,
    root: Path,
    deadline: float,
) -> _DirectoryBinding:
    binding = session.bindings.get(root)
    if binding is None:
        binding = _open_directory_binding(root, deadline)
        session.bindings[root] = binding
        while len(session.bindings) > _DESCRIPTOR_SESSION_LIMIT:
            _, evicted = session.bindings.popitem(last=False)
            evicted.close()
    else:
        session.bindings.move_to_end(root)
    try:
        binding.verify(deadline)
    except OSError as error:
        raise LeanImportError(
            f"project changed during import discovery: {root}"
        ) from error
    return binding


@contextmanager
def _bound_relative_directory(
    root: Path,
    binding: _DirectoryBinding,
    parts: tuple[str, ...],
    deadline: float,
):
    """Retain a relative directory chain below an already bound root."""
    descriptors: list[int] = []
    identities: list[tuple[int, int, int]] = []
    try:
        binding.verify(deadline)
        descriptor = binding.descriptor
        missing = False
        missing_part: str | None = None
        for part in parts:
            _remaining(deadline)
            try:
                before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                missing = True
                missing_part = part
                break
            if not stat.S_ISDIR(before.st_mode):
                raise OSError("relative component is not a directory")
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            opened = os.fstat(child)
            after = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            identity = _directory_identity(opened)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or identity != _directory_identity(before)
                or identity != _directory_identity(after)
            ):
                raise OSError("relative directory component changed")
            identities.append(identity)
            descriptor = child

        def verify() -> None:
            current = binding.descriptor
            for part, child, expected in zip(parts, descriptors, identities):
                _remaining(deadline)
                opened = os.fstat(child)
                named = os.stat(part, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _directory_identity(opened) != expected
                    or _directory_identity(named) != expected
                ):
                    raise OSError("relative directory component changed")
                current = child
            if missing_part is not None:
                try:
                    os.stat(missing_part, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise OSError("relative directory component changed")
            binding.verify(deadline)

        verify()
        yield None if missing else descriptor
        verify()
    except TimeoutError:
        raise
    except LeanImportError:
        raise
    except (OSError, ValueError, NotImplementedError) as error:
        raise LeanImportError(
            f"project directory changed or contains a symbolic link: {root}"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _inspect_regular_at(
    parent_descriptor: int,
    name: str,
    path: Path,
    root: Path,
    deadline: float,
    *,
    description: str,
    max_bytes: int | None = None,
    read: bool = False,
) -> tuple[_FileIdentity, bytes | None] | None:
    """Inspect one named regular file through its retained parent descriptor."""
    _remaining(deadline)
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LeanImportError(f"cannot inspect {description}: {path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise LeanImportError(
            f"{description} escapes its root or is not a regular file: {path}"
        )
    if max_bytes is not None and before.st_size > max_bytes:
        raise LeanImportError(f"{description} exceeded the size limit")
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        expected = _file_metadata(before)
        if not stat.S_ISREG(opened.st_mode) or _file_metadata(opened) != expected:
            raise LeanImportError(f"{description} changed while it was inspected")
        data: bytes | None = None
        if read:
            if max_bytes is None:
                raise TypeError("bounded project file reads require max_bytes")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                _remaining(deadline)
                chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise LeanImportError(f"{description} exceeded the size limit")
        _remaining(deadline)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _file_metadata(after) != expected
            or _file_metadata(named) != expected
            or (data is not None and len(data) != expected[3])
        ):
            raise LeanImportError(f"{description} changed while it was inspected")
        return _FileIdentity(path, root, expected), data
    except TimeoutError:
        raise
    except LeanImportError:
        raise
    except OSError as error:
        raise LeanImportError(f"{description} changed while it was inspected") from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _inspect_regular_file(
    path: Path,
    root: Path,
    deadline: float,
    *,
    description: str,
    max_bytes: int | None = None,
    read: bool = False,
) -> tuple[_FileIdentity, bytes | None] | None:
    """Return one stable contained-file identity and optional bounded contents."""
    if not path.is_absolute() or not root.is_absolute():
        raise LeanImportError(f"{description} escapes its root: {path}")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise LeanImportError(f"{description} escapes its root: {path}") from error
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LeanImportError(f"{description} escapes its root: {path}")
    with _descriptor_session() as session:
        binding = _root_binding(session, root, deadline)
        with _bound_relative_directory(
            root,
            binding,
            parts[:-1],
            deadline,
        ) as parent_descriptor:
            if parent_descriptor is None:
                return None
            return _inspect_regular_at(
                parent_descriptor,
                parts[-1],
                root.joinpath(*parts),
                root,
                deadline,
                description=description,
                max_bytes=max_bytes,
                read=read,
            )


def _require_regular_manifest(path: Path, deadline: float) -> None:
    """Validate the manifest without copying attacker-sized contents."""
    inspected = _inspect_regular_file(
        path,
        path.parent,
        deadline,
        description="lake-manifest.json",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    if inspected is None:
        raise LeanImportError(
            "project imports require lake-manifest.json; run `lake update` and `lake build` first"
        )


def _require_toolchain_pin(path: Path, deadline: float) -> None:
    inspected = _inspect_regular_file(
        path,
        path.parent,
        deadline,
        description="lean-toolchain",
        max_bytes=4096,
        read=True,
    )
    if inspected is None:
        raise LeanImportError(
            "structured imports require a project-local lean-toolchain pin"
        )
    _, encoded = inspected
    assert encoded is not None
    try:
        value = encoded.decode("utf-8").strip()
    except UnicodeError as error:
        raise LeanImportError("cannot read lean-toolchain") from error
    if not value or "\n" in value or "\r" in value:
        raise LeanImportError("lean-toolchain must contain one pinned toolchain name")
    _remaining(deadline)


def _require_project_configuration_files(
    project_root: Path,
    deadline: float,
) -> None:
    found = False
    for name in ("lakefile.toml", "lakefile.lean"):
        path = project_root / name
        inspected = _inspect_regular_file(
            path,
            project_root,
            deadline,
            description="Lake project configuration",
        )
        if inspected is None:
            continue
        found = True
    if not found:
        raise LeanImportError("structured imports require a project-local Lake config")


def _modules_requiring_lake_check(
    selections: tuple[_ModuleSelection, ...],
    toolchain_artifact_root: Path | None,
) -> tuple[str, ...]:
    """Return direct modules owned by the Lake workspace, not the toolchain."""
    workspace_modules: list[str] = []
    for selection in selections:
        if toolchain_artifact_root is not None and _is_relative_to(
            selection.artifact.path,
            toolchain_artifact_root,
        ):
            continue
        workspace_modules.append(selection.module)

    return tuple(workspace_modules)


def _read_package_specs(
    project_root: Path, manifest: Path, deadline: float
) -> tuple[_PackageSpec, ...]:
    inspected = _inspect_regular_file(
        manifest,
        project_root,
        deadline,
        description="lake-manifest.json",
        max_bytes=_MAX_MANIFEST_BYTES,
        read=True,
    )
    if inspected is None:
        raise LeanImportError(
            "project imports require lake-manifest.json; run `lake update` and `lake build` first"
        )
    _, encoded = inspected
    assert encoded is not None
    try:
        data = json.loads(encoded.decode("utf-8"))
        if not isinstance(data, dict):
            raise LeanImportError("lake-manifest.json has malformed package metadata")
        packages_dir_raw = data.get("packagesDir", ".lake/packages")
        packages = data.get("packages", [])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LeanImportError("cannot read lake-manifest.json") from error
    _remaining(deadline)
    if not isinstance(packages_dir_raw, str) or not isinstance(packages, list):
        raise LeanImportError("lake-manifest.json has malformed package metadata")
    packages_dir = Path(packages_dir_raw)
    if not packages_dir.is_absolute():
        packages_dir = project_root / packages_dir

    package_specs: list[_PackageSpec] = []
    for package in packages:
        _remaining(deadline)
        if not isinstance(package, dict):
            raise LeanImportError("lake-manifest.json has malformed package metadata")
        kind = package.get("type")
        name = package.get("name")
        config_file = package.get("configFile", "lakefile.lean")
        manifest_file = package.get("manifestFile", "lake-manifest.json")
        if (
            kind not in {"git", "path"}
            or not isinstance(name, str)
            or not name
            or not isinstance(config_file, str)
            or not config_file
            or (manifest_file is not None and not isinstance(manifest_file, str))
        ):
            raise LeanImportError("lake-manifest.json has malformed package metadata")
        if kind == "git":
            revision = package.get("rev")
            subdir = package.get("subDir")
            if not isinstance(revision, str) or (
                subdir is not None and not isinstance(subdir, str)
            ):
                raise LeanImportError("lake-manifest.json has malformed Git package metadata")
            package_root = packages_dir / name
            if subdir:
                package_root /= subdir
        else:
            revision = None
            directory = package.get("dir")
            if not isinstance(directory, str) or not directory:
                raise LeanImportError("lake-manifest.json has malformed path package metadata")
            package_root = Path(directory)
            if not package_root.is_absolute():
                package_root = project_root / package_root
        try:
            package_root = package_root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise LeanImportError(f"Lake package is missing: {name}") from error
        _remaining(deadline)
        package_specs.append(
            _PackageSpec(package_root, revision, config_file, manifest_file)
        )
    return tuple(package_specs)


def _resolve_package_generations(
    project_root: Path,
    manifest: Path,
    deadline: float,
) -> tuple[_PackageGeneration, ...]:
    generations: list[_PackageGeneration] = []
    for package in _read_package_specs(project_root, manifest, deadline):
        if package.revision is None or not package.config_file.endswith(".lean"):
            continue
        generation = _PackageGeneration(package.root, package.revision)
        _require_package_generation_current(generation, deadline)
        generations.append(generation)
    return tuple(generations)


def _require_package_generation_current(
    generation: _PackageGeneration,
    deadline: float,
) -> None:
    result = _run_lake(
        ["git", "-C", str(generation.root), "rev-parse", "HEAD"],
        generation.root,
        deadline=deadline,
    )
    if (
        result.returncode != 0
        or result.stdout.decode("ascii", errors="replace").strip()
        != generation.revision
    ):
        raise LeanImportError(
            f"Lake package generation differs from its manifest: {generation.root.name}"
        )
    status = _run_lake(
        [
            "git",
            "-C",
            str(generation.root),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        generation.root,
        deadline=deadline,
    )
    if status.returncode != 0:
        raise LeanImportError(
            f"cannot inspect Lake package generation: {generation.root.name}"
        )
    if status.stdout:
        raise LeanImportError(
            f"executable Lake package configuration is dirty: {generation.root.name}"
        )


def _require_package_generations_current(
    generations: tuple[_PackageGeneration, ...],
    deadline: float,
) -> None:
    for generation in generations:
        _require_package_generation_current(generation, deadline)


def _manifest_configuration_snapshot(
    project_root: Path, manifest: Path, deadline: float
) -> _DependencySnapshot:
    digest = hashlib.sha256()
    file_count = 0
    for package in _read_package_specs(project_root, manifest, deadline):
        paths = [package.root / package.config_file]
        if package.manifest_file:
            package_manifest = package.root / package.manifest_file
            inspected = _inspect_regular_file(
                package_manifest,
                package.root,
                deadline,
                description="Lake package manifest",
                max_bytes=_MAX_MANIFEST_BYTES,
            )
            if inspected is not None:
                paths.append(package_manifest)
        for path in paths:
            inspected = _inspect_regular_file(
                path,
                package.root,
                deadline,
                description="Lake package configuration",
                max_bytes=(
                    _MAX_MANIFEST_BYTES
                    if package.manifest_file is not None
                    and path == package.root / package.manifest_file
                    else None
                ),
            )
            if inspected is None:
                raise LeanImportError(f"Lake package configuration is missing: {path}")
            identity, _ = inspected
            _update_identity_digest(digest, identity)
            file_count += 1
    return _DependencySnapshot(digest.digest(), file_count)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out discovering project imports")
    return remaining


def _lake_environment(project_root: Path, *, deadline: float) -> dict[str, str]:
    result = _run_lake(
        ["lake", "--no-build", "env"],
        project_root,
        deadline=deadline,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise LeanImportError(f"Lake import discovery failed: {detail or 'unknown error'}")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeError as error:
        raise LeanImportError("Lake import discovery returned non-UTF-8 output") from error
    environment: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if not separator or not name or name in environment:
            raise LeanImportError("Lake import discovery returned malformed environment data")
        environment[name] = value
    return environment


def _toolchain_artifact_root(
    environment: dict[str, str], artifact_roots: tuple[Path, ...], deadline: float
) -> Path | None:
    """Identify Lean's immutable builtin library separately from Lake modules."""
    raw_sysroot = environment.get("LEAN_SYSROOT")
    if not raw_sysroot:
        return None
    _remaining(deadline)
    candidate = Path(raw_sysroot) / "lib" / "lean"
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LeanImportError("cannot inspect Lean's builtin library root") from error
    if candidate not in artifact_roots:
        raise LeanImportError("Lean's builtin library is missing from LEAN_PATH")
    return candidate


@contextmanager
def _project_validation(project_root: Path, deadline: float):
    with _validation_cache_lock:
        lock = _project_validation_locks.setdefault(project_root, threading.Lock())
    if not lock.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("timed out waiting to validate project imports")
    try:
        yield
    finally:
        lock.release()


def _cache_get(cache: OrderedDict, key: object) -> object | None:
    with _validation_cache_lock:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value


def _cache_remove(cache: OrderedDict, key: object) -> None:
    with _validation_cache_lock:
        cache.pop(key, None)


def _evict_resolved_imports(descriptor: ResolvedImports) -> None:
    key = (
        descriptor.project_root,
        descriptor.modules,
        descriptor.project_fingerprint,
    )
    with _validation_cache_lock:
        cached = _resolved_import_cache.get(key)
        if cached == descriptor:
            _resolved_import_cache.pop(key, None)


def _cache_add(cache: OrderedDict, key: object, value: object) -> None:
    with _validation_cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _VALIDATION_CACHE_LIMIT:
            cache.popitem(last=False)


def _query_transitive_modules(
    project_root: Path,
    modules: tuple[str, ...],
    deadline: float,
) -> tuple[str, ...]:
    """Ask Lake for the exact source dependency closure of workspace modules."""
    if not modules:
        return ()
    result = _run_lake(
        [
            "lake",
            "--no-build",
            "query",
            *(f"+{module}:transImports" for module in modules),
            "--json",
        ],
        project_root,
        deadline=deadline,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise LeanImportError(
            "cannot resolve the transitive Lean import closure with Lake"
            f" ({detail or 'unknown error'})"
        )
    try:
        lines = result.stdout.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise LeanImportError("Lake returned a non-UTF-8 import closure") from error
    if len(lines) != len(modules):
        raise LeanImportError("Lake returned an incomplete import closure")
    closure: list[str] = list(modules)
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise LeanImportError("Lake returned malformed import-closure JSON") from error
        parsed = validate_imports(value)
        if parsed is None:
            raise LeanImportError("Lake returned an invalid import closure")
        closure.extend(parsed)
    return tuple(dict.fromkeys(closure))


def _bind_dependency_modules(
    modules: tuple[str, ...],
    artifact_roots: tuple[Path, ...],
    source_roots: tuple[Path, ...],
    deadline: float,
) -> bytes:
    selections = _resolve_module_selections(
        modules,
        artifact_roots,
        source_roots,
        deadline,
    )
    bindings: list[list[str | int]] = []
    for selection in selections:
        if selection.source is None:
            raise LeanImportError(
                f"Lake did not expose the source for module {selection.module!r}"
            )
        bindings.append(
            [
                selection.module,
                artifact_roots.index(selection.artifact.root),
                source_roots.index(selection.source.root),
            ]
        )
    encoded = json.dumps(bindings, separators=(",", ":")).encode("utf-8")
    return zlib.compress(encoded, level=9)


def _decode_dependency_bindings(encoded: bytes) -> tuple[_ClosureBinding, ...]:
    try:
        values = json.loads(zlib.decompress(encoded).decode("utf-8"))
    except (UnicodeError, ValueError, zlib.error) as error:
        raise LeanImportError("resolved import closure bindings are corrupt") from error
    if not isinstance(values, list):
        raise LeanImportError("resolved import closure bindings are invalid")
    bindings: list[_ClosureBinding] = []
    for value in values:
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not isinstance(value[0], str)
            or not isinstance(value[1], int)
            or isinstance(value[1], bool)
            or not isinstance(value[2], int)
            or isinstance(value[2], bool)
        ):
            raise LeanImportError("resolved import closure bindings are invalid")
        bindings.append(_ClosureBinding(value[0], value[1], value[2]))
    modules = tuple(binding.module for binding in bindings)
    if validate_imports(list(modules)) != modules or len(set(modules)) != len(modules):
        raise LeanImportError("resolved import closure bindings are invalid")
    return tuple(bindings)


def _direct_dependency_identity(
    selection: _ModuleSelection,
    deadline: float,
    *,
    require_trace: bool,
) -> _OleanDependency:
    """Bind a direct OLean, every companion part, and Lake's closure trace."""
    return _OleanDependency(
        module=selection.module,
        artifacts=_direct_artifact_identities(
            selection.artifact.path,
            selection.artifact.root,
            deadline,
            require_trace=require_trace,
        ),
        source=selection.source,
    )


def _direct_artifact_identities(
    main: Path,
    root: Path,
    deadline: float,
    *,
    require_trace: bool = True,
) -> tuple[_FileIdentity, ...]:
    if not main.is_absolute() or not main.name.endswith(".olean"):
        raise LeanImportError(f"Lean returned an invalid import artifact path: {main}")
    candidates = (
        main,
        Path(f"{main}.server"),
        Path(f"{main}.private"),
        main.with_suffix(".ir"),
        main.with_suffix(".ilean"),
    )
    artifacts: list[_FileIdentity] = []
    for path in candidates:
        inspected = _inspect_regular_file(
            path,
            root,
            deadline,
            description="Lean import artifact",
        )
        if inspected is None:
            if path == main:
                raise LeanImportError(f"Lean import artifact is missing: {path}")
            continue
        identity, _ = inspected
        artifacts.append(identity)
    trace = main.with_suffix(".trace")
    inspected_trace = _inspect_regular_file(
        trace,
        root,
        deadline,
        description="Lean import build trace",
    )
    if inspected_trace is None and require_trace:
        raise LeanImportError(
            f"Lean import build trace is missing; run `lake build`: {trace}"
        )
    if inspected_trace is not None:
        trace_identity, _ = inspected_trace
        artifacts.append(trace_identity)
    return tuple(artifacts)


def _require_dependencies_current(
    dependencies: tuple[_OleanDependency, ...],
    deadline: float,
) -> None:
    for expected in dependencies:
        main = next(
            (
                artifact.path
                for artifact in expected.artifacts
                if artifact.path.name.endswith(".olean")
            ),
            None,
        )
        require_trace = any(
            artifact.path.name.endswith(".trace") for artifact in expected.artifacts
        )
        if main is None or _direct_artifact_identities(
            main,
            expected.artifacts[0].root if expected.artifacts else main.parent,
            deadline,
            require_trace=require_trace,
        ) != expected.artifacts:
            raise LeanImportError("Lean import artifacts changed")
        if expected.source is not None:
            current = _require_regular_contained(
                expected.source.path,
                expected.source.root,
                deadline,
            )
            if current != expected.source:
                raise LeanImportError("direct Lean import sources changed")


def _snapshot_dependency_closure(
    bindings: tuple[_ClosureBinding, ...],
    artifact_roots: tuple[Path, ...],
    source_roots: tuple[Path, ...],
    toolchain_artifact_root: Path | None,
    deadline: float,
) -> _DependencySnapshot:
    """Bind only the Lake closure plus Lean's pinned builtin artifacts."""
    digest = hashlib.sha256()
    file_count = 0
    for binding in bindings:
        if not (
            0 <= binding.artifact_root < len(artifact_roots)
            and 0 <= binding.source_root < len(source_roots)
        ):
            raise LeanImportError("resolved import closure root binding is invalid")
        relative = Path(*binding.module.split("."))
        artifact = _require_unique_bound_file(
            artifact_roots,
            relative.with_suffix(".olean"),
            binding.artifact_root,
            deadline,
        )
        source = _require_unique_bound_file(
            source_roots,
            relative.with_suffix(".lean"),
            binding.source_root,
            deadline,
        )
        digest.update(b"module\0")
        digest.update(binding.module.encode("utf-8"))
        digest.update(b"\0source\0")
        _update_identity_digest(digest, source)
        file_count += 1
        for item in _known_artifact_identities(
            artifact.path,
            artifact.root,
            deadline,
        ):
            digest.update(b"\0artifact\0")
            _update_identity_digest(digest, item)
            file_count += 1
    if toolchain_artifact_root is not None:
        toolchain = _snapshot_dependency_roots(
            (toolchain_artifact_root,),
            deadline,
        )
        digest.update(b"\0toolchain\0")
        digest.update(toolchain.digest)
        file_count += toolchain.file_count
    _remaining(deadline)
    return _DependencySnapshot(digest.digest(), file_count)


def _require_unique_bound_file(
    roots: tuple[Path, ...],
    relative: Path,
    expected_root: int,
    deadline: float,
) -> _FileIdentity:
    match: _FileIdentity | None = None
    for index, root in enumerate(roots[: expected_root + 1]):
        path = root / relative
        inspected = _inspect_regular_file(
            path,
            root,
            deadline,
            description="Lean dependency file",
        )
        if inspected is None:
            continue
        if match is not None or index != expected_root:
            raise LeanImportError(f"Lean dependency path changed: {relative}")
        match, _ = inspected
    if match is None:
        raise LeanImportError(f"Lean dependency file is missing: {relative}")
    return match


def _known_artifact_identities(
    main: Path,
    root: Path,
    deadline: float,
) -> tuple[_FileIdentity, ...]:
    identities: list[_FileIdentity] = []
    candidates = [
        main,
        Path(f"{main}.server"),
        Path(f"{main}.private"),
        main.with_suffix(".ir"),
        main.with_suffix(".ilean"),
        main.with_suffix(".trace"),
    ]
    for path in candidates:
        inspected = _inspect_regular_file(
            path,
            root,
            deadline,
            description="Lean import artifact",
        )
        if inspected is None:
            if path.suffix == ".trace":
                raise LeanImportError(
                    f"Lean import build trace is missing; run `lake build`: {path}"
                )
            continue
        identity, _ = inspected
        identities.append(identity)
    return tuple(identities)


def _update_identity_digest(digest: Any, identity: _FileIdentity) -> None:
    encoded = os.fsencode(identity.path)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(repr(identity.metadata).encode("ascii"))


def _snapshot_dependency_roots(
    artifact_roots: tuple[Path, ...],
    deadline: float,
) -> _DependencySnapshot:
    """Bind a compact descriptor-relative snapshot of artifact trees."""
    digest = hashlib.sha256()
    file_count = 0
    suffixes = (
        ".olean",
        ".olean.server",
        ".olean.private",
        ".ir",
        ".ilean",
        ".trace",
    )

    def add_tree(root: Path) -> None:
        nonlocal file_count
        with _descriptor_session() as session:
            binding = _root_binding(session, root, deadline)
            scan(root, binding.descriptor, Path())
            binding.verify(deadline)

    def scan(root: Path, descriptor: int, relative: Path) -> None:
        nonlocal file_count
        _remaining(deadline)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise LeanImportError("Lean dependency root changed while it was inspected")
        directory_identity = _file_metadata(opened)
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as error:
            raise LeanImportError("cannot inspect Lean dependency root") from error
        expected_entries: dict[str, tuple[int, int, int, int, int, int]] = {}
        for name in names:
            _remaining(deadline)
            try:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                raise LeanImportError(
                    f"cannot inspect Lean dependency file: {root / relative / name}"
                ) from error
            expected_entries[name] = _file_metadata(metadata)
            child_relative = relative / name
            if stat.S_ISDIR(metadata.st_mode):
                child: int | None = None
                try:
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    child_metadata = os.fstat(child)
                    if _file_metadata(child_metadata) != _file_metadata(metadata):
                        raise LeanImportError(
                            "Lean dependency directory changed while it was inspected"
                        )
                    scan(root, child, child_relative)
                    final_child = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    if _file_metadata(final_child) != _file_metadata(metadata):
                        raise LeanImportError(
                            "Lean dependency directory changed while it was inspected"
                        )
                except LeanImportError:
                    raise
                except TimeoutError:
                    raise
                except OSError as error:
                    raise LeanImportError(
                        f"cannot inspect Lean dependency root: {root / child_relative}"
                    ) from error
                finally:
                    if child is not None:
                        try:
                            os.close(child)
                        except OSError:
                            pass
                continue
            if not name.endswith(suffixes):
                continue
            inspected = _inspect_regular_at(
                descriptor,
                name,
                root / child_relative,
                root,
                deadline,
                description="Lean dependency artifact",
            )
            if inspected is None:
                raise LeanImportError(
                    "Lean dependency artifact changed while it was inspected"
                )
            identity, _ = inspected
            encoded = os.fsencode(child_relative)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(repr(identity.metadata).encode("ascii"))
            file_count += 1
            if file_count % 256 == 0:
                _remaining(deadline)
        try:
            if (
                _file_metadata(os.fstat(descriptor)) != directory_identity
                or tuple(sorted(os.listdir(descriptor))) != names
            ):
                raise LeanImportError(
                    "Lean dependency directory changed while it was inspected"
                )
            for name, expected in expected_entries.items():
                current = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if _file_metadata(current) != expected:
                    raise LeanImportError(
                        "Lean dependency directory changed while it was inspected"
                    )
        except LeanImportError:
            raise
        except OSError as error:
            raise LeanImportError(
                "Lean dependency directory changed while it was inspected"
            ) from error

    digest.update(b"artifacts")
    for root in artifact_roots:
        encoded_root = os.fsencode(root)
        digest.update(len(encoded_root).to_bytes(8, "big"))
        digest.update(encoded_root)
        add_tree(root)
    _remaining(deadline)
    return _DependencySnapshot(digest.digest(), file_count)


def _run_lake(
    command: list[str],
    project_root: Path,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if deadline is not None and timeout is not None:
        raise TypeError("pass timeout or deadline, not both")
    if deadline is None:
        if timeout is None:
            raise TypeError("_run_lake requires timeout or deadline")
        if timeout <= 0:
            raise TimeoutError("no request time remains for Lake import discovery")
        deadline = time.monotonic() + timeout
    elif deadline - time.monotonic() <= 0:
        raise TimeoutError("no request time remains for Lake import discovery")
    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=_clean_resolver_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise LeanImportError(f"cannot run Lake import discovery: {error}") from error
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    total_bytes = 0
    selector = selectors.DefaultSelector()
    completed = False
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out discovering project imports")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), _READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(chunk)
                if total_bytes > _MAX_DISCOVERY_OUTPUT_BYTES:
                    raise LeanImportError("Lake import discovery output exceeded the size limit")
                streams[key.fileobj].extend(chunk)
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        completed = True
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("timed out discovering project imports") from error
    finally:
        selector.close()
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=_PROCESS_KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        command,
        returncode,
        bytes(streams[process.stdout]),
        bytes(streams[process.stderr]),
    )


def _clean_resolver_environment() -> dict[str, str]:
    """Remove ambient Git redirection and configuration from resolver commands."""
    environment = clean_lake_environment()
    for name in tuple(environment):
        if name.startswith("GIT_"):
            environment.pop(name)
    return environment


def _environment_roots(
    environment: dict[str, str], name: str, deadline: float
) -> _LakeRoots:
    value = environment.get(name)
    if value is None:
        raise LeanImportError(f"Lake import discovery did not provide {name}")
    bindings: list[tuple[Path, Path | None]] = []
    roots: list[Path] = []
    for raw in value.split(os.pathsep):
        _remaining(deadline)
        if not raw:
            raise LeanImportError(f"Lake import discovery returned an empty {name} entry")
        path = Path(raw)
        if not path.is_absolute():
            raise LeanImportError(f"Lake import discovery returned a relative {name} entry")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            bindings.append((path, None))
            continue
        except (OSError, RuntimeError) as error:
            raise LeanImportError(
                f"cannot inspect Lake import root: {path}"
            ) from error
        if not resolved.is_dir():
            raise LeanImportError(f"Lake import root is not a directory: {resolved}")
        bindings.append((path, resolved))
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise LeanImportError(f"Lake import discovery provided no existing {name} roots")
    return _LakeRoots(tuple(bindings), tuple(roots))


def _require_lake_roots_current(roots: _LakeRoots, deadline: float) -> None:
    for raw, expected in roots.bindings:
        _remaining(deadline)
        try:
            current = raw.resolve(strict=True)
        except FileNotFoundError as error:
            if expected is None:
                continue
            raise LeanImportError(f"Lake import root changed: {raw}") from error
        except (OSError, RuntimeError) as error:
            raise LeanImportError(f"Lake import root changed: {raw}") from error
        if expected is None or current != expected or not current.is_dir():
            raise LeanImportError(f"Lake import root changed: {raw}")


def _resolve_module_selections(
    modules: tuple[str, ...],
    artifact_roots: tuple[Path, ...],
    source_roots: tuple[Path, ...],
    deadline: float,
) -> tuple[_ModuleSelection, ...]:
    selections: list[_ModuleSelection] = []
    for module in modules:
        _remaining(deadline)
        relative = Path(*module.split("."))
        artifacts = _matching_files(
            artifact_roots,
            relative.with_suffix(".olean"),
            deadline,
        )
        if not artifacts:
            raise LeanImportError(
                f"Lean module {module!r} is not built for this project; "
                "run `lake build` first"
            )
        if len(artifacts) != 1:
            rendered = ", ".join(str(item.path) for item in artifacts)
            raise LeanImportError(
                f"Lean module {module!r} is ambiguous across Lake roots: {rendered}"
            )
        sources = _matching_files(
            source_roots,
            relative.with_suffix(".lean"),
            deadline,
        )
        if len(sources) > 1:
            rendered = ", ".join(str(item.path) for item in sources)
            raise LeanImportError(
                f"Lean module {module!r} has ambiguous sources: {rendered}"
            )
        selections.append(
            _ModuleSelection(
                module=module,
                artifact=artifacts[0],
                source=sources[0] if sources else None,
            )
        )
    return tuple(selections)


def _matching_files(
    roots: tuple[Path, ...], relative: Path, deadline: float
) -> tuple[_FileIdentity, ...]:
    matches: list[_FileIdentity] = []
    for root in roots:
        candidate = root / relative
        inspected = _inspect_regular_file(
            candidate,
            root,
            deadline,
            description="Lean import artifact",
        )
        if inspected is None:
            continue
        identity, _ = inspected
        matches.append(identity)
    return tuple(matches)


def _require_regular_contained(
    path: Path, root: Path, deadline: float
) -> _FileIdentity:
    inspected = _inspect_regular_file(
        path,
        root,
        deadline,
        description="Lean import artifact",
    )
    if inspected is None:
        raise LeanImportError(f"Lean import artifact is missing: {path}")
    identity, _ = inspected
    return identity
