"""Deterministically inspect local Lean project configuration without executing it."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from ..bounded_toml import BoundedTomlError, loads_bounded_toml
from .catalog import load_release_catalog
from .model import (
    PROJECT_INSPECTION_SCHEMA,
    AutoformProject,
    LakeProject,
    LakeTarget,
    LeanProject,
    MathlibProject,
    ProjectCompatibility,
    ProjectDiagnostic,
    ProjectInspection,
    ReleaseCatalog,
)

_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_STRUCTURAL_DEPTH = 128
_PROJECT_MARKERS = ("lakefile.toml", "lakefile.lean", "lean-toolchain", "blueprint")
_TOOLCHAIN = re.compile(r"leanprover/lean4:(?P<version>v[0-9]+\.[0-9]+\.[0-9]+)")
# Lake's StdVer: a major.minor.patch triple with an optional `-` suffix that
# runs to the end of the string.
_LAKE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[^ \t\r\n]+)?")
_MANIFEST_VERSION = re.compile(
    r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)(?:-[^ \t\r\n]+)?"
)
_FULL_GIT_REVISION = re.compile(r"[0-9a-fA-F]{40}")
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_LEAN_ID_BEGIN_ESCAPE = "«"
_LEAN_ID_END_ESCAPE = "»"
_SNAPSHOT_ATTEMPTS = 2
_DECISION_FILES = {
    "lakefile.toml": ("lake-config", "error"),
    "lakefile.lean": ("lake-config", "error"),
    "lean-toolchain": ("lean-toolchain", "error"),
    "lake-manifest.json": ("lake-manifest", "error"),
    ".lake/package-overrides.json": ("package-overrides", "error"),
}
_DECISION_NODES = (
    *_DECISION_FILES,
    ".git",
    "blueprint",
    "mkdocs.yml",
    ".github/workflows/autoform-verify.yml",
    ".github/workflows/blueprint-pages.yml",
)
_CASE_SENSITIVE_NODES = tuple(
    dict.fromkeys(
        (
            *_DECISION_NODES,
            ".lake",
            ".github",
            ".github/workflows",
        )
    )
)


class _InvalidLakeField(ValueError):
    pass


class _NonportableLakePath(ValueError):
    pass


class _DuplicateMathlibRequirement(ValueError):
    pass


class _InvalidJson(ValueError):
    pass


class _UnsupportedManifest(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _SnapshotEntry:
    status: str
    content: bytes | None
    identity: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class _MathlibRequirement:
    name: str
    scope: str
    git: str | None
    revision: str | None
    source_kind: str
    subdirectory: str | None


@dataclass(frozen=True, slots=True)
class _MaterializedMathlib:
    scope: str
    package_type: str
    git: str | None
    resolved_revision: str | None
    input_revision: str | None
    subdirectory: str | None
    config_file: str
    manifest_file: str | None
    path: str | None


def inspect_project(target: str | Path, *, catalog: ReleaseCatalog | None = None) -> ProjectInspection:
    release_catalog = catalog or load_release_catalog()
    diagnostics: list[ProjectDiagnostic] = []
    try:
        root_descriptor = _discover_root(target, diagnostics)
    except (UnicodeError, ValueError):
        _issue(diagnostics, "error", "target-unreadable", "The inspection target cannot be resolved.")
        root_descriptor = None
    if root_descriptor is None:
        return _inspection(diagnostics, release_catalog)
    try:
        return _inspect_project_root(root_descriptor, release_catalog)
    finally:
        os.close(root_descriptor)


def _inspect_project_root(root_descriptor: int, release_catalog: ReleaseCatalog) -> ProjectInspection:
    """Inspect an already-bound project root without taking ownership of it."""

    diagnostics: list[ProjectDiagnostic] = []
    snapshot = _snapshot_project(root_descriptor, diagnostics)
    if snapshot is None:
        return _inspection(diagnostics, release_catalog)
    lake, declared_mathlib = _inspect_lake(snapshot, diagnostics)
    lean = _inspect_toolchain(snapshot, diagnostics)
    manifest_path, manifest_digest, manifest_valid, manifest_mathlib = _inspect_manifest(
        snapshot,
        declared_mathlib,
        diagnostics,
    )
    (
        overrides_path,
        overrides_digest,
        overrides_valid,
        override_mathlib,
    ) = _inspect_overrides(snapshot, declared_mathlib, diagnostics)
    mathlib = None
    if manifest_valid and overrides_valid:
        mathlib = override_mathlib if override_mathlib is not None else manifest_mathlib
        if override_mathlib is not None:
            _issue(
                diagnostics,
                "warning",
                "mathlib-overridden",
                "Lake overrides the manifest's Mathlib package for this workspace.",
                ".lake/package-overrides.json",
            )
    if mathlib is not None and declared_mathlib is None:
        if lake is not None and lake.format == "toml":
            _issue(
                diagnostics,
                "warning",
                "mathlib-manifest-unused",
                "The manifest contains Mathlib, but the active Lake configuration does not require it.",
                mathlib.source,
            )
        elif lake is not None and lake.format == "lean":
            _issue(
                diagnostics,
                "warning",
                "mathlib-config-unevaluated",
                "Offline inspection cannot confirm that lakefile.lean uses the manifest's Mathlib entry.",
                "lakefile.lean",
            )
        mathlib = None
    if any(
        diagnostic.code in {"lake-config-case-alias", "lake-state-case-alias"}
        for diagnostic in diagnostics
    ):
        lake = None
        mathlib = None
    autoform = _inspect_autoform(snapshot, diagnostics)
    git_path = _inspect_git(snapshot, diagnostics)
    compatibility = _compatibility(release_catalog, lean, mathlib, diagnostics)
    return ProjectInspection(
        schema=PROJECT_INSPECTION_SCHEMA,
        project_root=".",
        git_path=git_path,
        lake=lake,
        lake_manifest_path=manifest_path,
        lake_manifest_sha256=manifest_digest,
        package_overrides_path=overrides_path,
        package_overrides_sha256=overrides_digest,
        lean=lean,
        mathlib=mathlib,
        autoform=autoform,
        compatibility=compatibility,
        diagnostics=_ordered(diagnostics),
    )


def _inspection(
    diagnostics: list[ProjectDiagnostic],
    catalog: ReleaseCatalog,
) -> ProjectInspection:
    return ProjectInspection(
        schema=PROJECT_INSPECTION_SCHEMA,
        project_root=None,
        git_path=None,
        lake=None,
        lake_manifest_path=None,
        lake_manifest_sha256=None,
        package_overrides_path=None,
        package_overrides_sha256=None,
        lean=None,
        mathlib=None,
        autoform=AutoformProject(False, None, None, None, None),
        compatibility=ProjectCompatibility(
            catalog=catalog.schema,
            status="indeterminate",
            release=None,
            recommended_release=catalog.recommended.id,
        ),
        diagnostics=_ordered(diagnostics),
    )


def _discover_root(target: str | Path, diagnostics: list[ProjectDiagnostic]) -> int | None:
    """Return a no-follow descriptor for the nearest enclosing project root.

    Every ancestor is opened with O_NOFOLLOW as it is traversed and the chosen
    descriptor is retained, so no pathname is ever re-resolved after being
    checked. Replacing a directory with a symlink mid-walk fails the open
    instead of redirecting inspection to another project.
    """
    if not _secure_inspection_available(diagnostics):
        return None
    try:
        raw_target = os.fspath(target)
        if not isinstance(raw_target, str):
            raise ValueError("target must be text")
        if raw_target.startswith("~") and not (
            raw_target == "~"
            or raw_target.startswith("~/")
            or raw_target.startswith("~\\")
        ):
            raise ValueError("named-user home expansion is not inspected")
        candidate = Path(raw_target).expanduser().absolute()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        _issue(diagnostics, "error", "target-unreadable", "The inspection target cannot be resolved.")
        return None

    descriptors: list[int] = []
    chosen: int | None = None
    try:
        try:
            descriptors.append(_open_directory(candidate.anchor, None))
        except OSError:
            _issue(
                diagnostics,
                "error",
                "project-root-unreadable",
                "The project root cannot be opened safely.",
            )
            return None
        parts = candidate.parts[1:]
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if part == ".":
                continue
            if part == "..":
                if len(descriptors) > 1:
                    os.close(descriptors.pop())
                continue
            status = _entry_status(descriptors[-1], part)
            if status == "missing":
                _issue(
                    diagnostics,
                    "error",
                    "target-does-not-exist",
                    "The inspection target does not exist.",
                )
                return None
            if status == "symlink":
                if last:
                    _issue(
                        diagnostics, "error", "target-is-symlink", "The inspection target is a symlink."
                    )
                else:
                    _issue(
                        diagnostics,
                        "error",
                        "project-path-is-symlink",
                        "The target path contains a symlink.",
                    )
                return None
            if status == "directory":
                try:
                    descriptors.append(_open_directory(part, descriptors[-1]))
                except OSError:
                    _issue(
                        diagnostics,
                        "error",
                        "project-root-unreadable",
                        "The project root cannot be opened safely.",
                    )
                    return None
                continue
            if last and status == "file":
                break
            if last and status == "other":
                _issue(
                    diagnostics,
                    "error",
                    "target-not-file-or-directory",
                    "The inspection target is unsupported.",
                )
            else:
                _issue(
                    diagnostics,
                    "error",
                    "project-root-unreadable",
                    "The project root cannot be opened safely.",
                )
            return None

        for descriptor in reversed(descriptors):
            if _has_project_marker(descriptor):
                chosen = descriptor
                return chosen
        _issue(diagnostics, "error", "project-not-found", "No enclosing Lean or Autoform project was found.")
        return None
    finally:
        for descriptor in descriptors:
            if descriptor != chosen:
                os.close(descriptor)


def _secure_inspection_available(diagnostics: list[ProjectDiagnostic]) -> bool:
    if (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    ):
        return True
    _issue(
        diagnostics,
        "error",
        "secure-file-inspection-unavailable",
        "This platform cannot safely inspect project files without following links.",
    )
    return False


def _has_project_marker(descriptor: int) -> bool:
    for marker in _PROJECT_MARKERS:
        if _relative_status(descriptor, marker) != "missing":
            return True
        try:
            if _case_aliases(descriptor, marker):
                return True
        except OSError:
            return True
    return False


def _open_directory(name: str, parent_descriptor: int | None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if parent_descriptor is None:
        return os.open(name, flags)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _entry_status(parent_descriptor: int, name: str) -> str:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def _snapshot_project(
    root_descriptor: int, diagnostics: list[ProjectDiagnostic]
) -> dict[str, _SnapshotEntry] | None:
    """Capture every decision-bearing node from one stable filesystem generation."""
    for _attempt in range(_SNAPSHOT_ATTEMPTS):
        attempt_diagnostics: list[ProjectDiagnostic] = []
        snapshot: dict[str, _SnapshotEntry] = {}
        changed = False
        try:
            case_aliases = {
                relative: _case_aliases(root_descriptor, relative)
                for relative in _CASE_SENSITIVE_NODES
            }
        except OSError:
            _issue(
                diagnostics,
                "error",
                "project-root-unreadable",
                "The project root cannot be inspected safely.",
            )
            return None
        for expected, aliases in case_aliases.items():
            for alias in aliases:
                if expected in {"lakefile.lean", "lakefile.toml"}:
                    code = "lake-config-case-alias"
                elif (
                    expected == "lake-manifest.json"
                    or expected == ".lake"
                    or expected.startswith(".lake/")
                ):
                    code = "lake-state-case-alias"
                elif expected == "lean-toolchain":
                    code = "lean-toolchain-case-alias"
                else:
                    code = "project-path-case-alias"
                path_kind = "Lake path" if code.startswith("lake-") else "project path"
                _issue(
                    attempt_diagnostics,
                    "error",
                    code,
                    f"{alias} differs in case from the portable {path_kind} {expected}.",
                    alias,
                )
        lean_config_status, lean_config_metadata = _relative_info(
            root_descriptor, "lakefile.lean"
        )
        lean_config_generation = (
            lean_config_status,
            _metadata_identity(lean_config_metadata),
        )
        lean_config_present = (
            lean_config_status != "missing" or bool(case_aliases["lakefile.lean"])
        )
        for relative in _DECISION_NODES:
            kind, severity = _DECISION_FILES.get(relative, ("project-path", "error"))
            status, metadata = _relative_info(root_descriptor, relative)
            identity = _metadata_identity(metadata)
            ignored_toml = relative == "lakefile.toml" and lean_config_present
            if relative in _DECISION_FILES and status == "file" and not ignored_toml:
                entry, changed_while_reading = _capture_file(
                    root_descriptor,
                    relative,
                    kind,
                    attempt_diagnostics,
                    severity=severity,
                    expected_identity=identity,
                )
                snapshot[relative] = entry
                changed = changed or changed_while_reading
            else:
                if (
                    relative in _DECISION_FILES
                    and not ignored_toml
                    and status not in {"missing", "file"}
                ):
                    _file_status_issue(
                        attempt_diagnostics, kind, severity, relative, status
                    )
                snapshot[relative] = _SnapshotEntry(status, None, identity)

        lean_config_entry = snapshot["lakefile.lean"]
        if (lean_config_entry.status, lean_config_entry.identity) != lean_config_generation:
            changed = True
        try:
            if any(
                _case_aliases(root_descriptor, relative) != aliases
                for relative, aliases in case_aliases.items()
            ):
                changed = True
        except OSError:
            changed = True
        for relative, entry in snapshot.items():
            status, metadata = _relative_info(root_descriptor, relative)
            if (status, _metadata_identity(metadata)) != (entry.status, entry.identity):
                changed = True
                break
        if not changed:
            diagnostics.extend(attempt_diagnostics)
            return snapshot

    _issue(
        diagnostics,
        "error",
        "project-changed-during-inspection",
        "Project configuration changed while it was being inspected.",
    )
    return None


def _case_aliases(root_descriptor: int, expected: str) -> tuple[str, ...]:
    try:
        parent, name = _open_parent_descriptor(root_descriptor, expected)
    except FileNotFoundError:
        return ()
    except OSError:
        if "/" in expected:
            return ()
        raise
    try:
        parent_path = PurePosixPath(expected).parent
        prefix = "" if parent_path == PurePosixPath(".") else f"{parent_path}/"
        return tuple(
            sorted(
                f"{prefix}{entry}"
                for entry in os.listdir(parent)
                if entry != name and entry.casefold() == name.casefold()
            )
        )
    finally:
        os.close(parent)


def _capture_file(
    root_descriptor: int,
    relative: str,
    kind: str,
    diagnostics: list[ProjectDiagnostic],
    *,
    severity: str,
    expected_identity: tuple[int, ...] | None,
) -> tuple[_SnapshotEntry, bool]:
    """Read a pre-validated regular file and return its opened-file identity."""
    try:
        parent, name = _open_parent_descriptor(root_descriptor, relative)
    except OSError:
        _issue(
            diagnostics,
            severity,
            f"{kind}-is-symlink",
            "A decision-bearing project path cannot be traversed safely.",
            relative,
        )
        return _SnapshotEntry("file", None, expected_identity), False
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        exact = _exact_entry_exists(parent, name)
        if exact is None:
            _issue(
                diagnostics,
                severity,
                f"{kind}-unreadable",
                "A project configuration file cannot be read.",
                relative,
            )
            return _SnapshotEntry("file", None, expected_identity), False
        if not exact:
            return _SnapshotEntry("missing", None, None), True
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        code = (
            f"{kind}-is-symlink"
            if error.errno in {errno.ELOOP, errno.ENOTDIR}
            else f"{kind}-unreadable"
        )
        message = (
            "A decision-bearing project file cannot be opened without following links."
            if code.endswith("-is-symlink")
            else "A project configuration file cannot be read."
        )
        _issue(diagnostics, severity, code, message, relative)
        return _SnapshotEntry("file", None, expected_identity), False
    finally:
        os.close(parent)

    try:
        before = os.fstat(descriptor)
        before_identity = _metadata_identity(before)
        if not stat.S_ISREG(before.st_mode):
            _file_status_issue(diagnostics, kind, severity, relative, "other")
            return _SnapshotEntry("other", None, before_identity), False
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(_MAX_CONFIG_BYTES + 1)
        after_identity = _metadata_identity(os.fstat(descriptor))
        changed = before_identity != expected_identity or before_identity != after_identity
        if len(content) > _MAX_CONFIG_BYTES:
            _issue(
                diagnostics,
                severity,
                f"{kind}-too-large",
                "A project configuration file exceeds the inspection limit.",
                relative,
            )
            content = None
        return _SnapshotEntry("file", content, after_identity), changed
    except OSError:
        _issue(
            diagnostics,
            severity,
            f"{kind}-unreadable",
            "A project configuration file cannot be read.",
            relative,
        )
        return _SnapshotEntry("file", None, expected_identity), False
    finally:
        os.close(descriptor)


def _file_status_issue(
    diagnostics: list[ProjectDiagnostic],
    kind: str,
    severity: str,
    relative: str,
    status: str,
) -> None:
    if status == "symlink":
        _issue(
            diagnostics,
            severity,
            f"{kind}-is-symlink",
            "A decision-bearing project path cannot be inspected safely.",
            relative,
        )
    elif status == "unreadable":
        _issue(
            diagnostics,
            severity,
            f"{kind}-unreadable",
            "A project configuration file cannot be read.",
            relative,
        )
    else:
        _issue(
            diagnostics,
            severity,
            f"{kind}-not-regular",
            "A decision-bearing project path is not a regular file.",
            relative,
        )


def _metadata_identity(metadata: os.stat_result | None) -> tuple[int, ...] | None:
    if metadata is None:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _inspect_lake(
    snapshot: dict[str, _SnapshotEntry], diagnostics: list[ProjectDiagnostic]
) -> tuple[LakeProject | None, _MathlibRequirement | None]:
    toml_status = snapshot["lakefile.toml"].status
    lean_status = snapshot["lakefile.lean"].status
    if toml_status != "missing" and lean_status != "missing":
        _issue(
            diagnostics,
            "warning",
            "unused-lakefile-toml",
            "lakefile.toml is ignored because lakefile.lean takes precedence.",
            "lakefile.toml",
        )
    if lean_status != "missing":
        content = snapshot["lakefile.lean"].content
        if content is None:
            return None, None
        _issue(
            diagnostics,
            "warning",
            "lakefile-lean-not-evaluated",
            "lakefile.lean was detected but is not executed by offline inspection.",
            "lakefile.lean",
        )
        return LakeProject(
            format="lean",
            path="lakefile.lean",
            sha256=hashlib.sha256(content).hexdigest(),
            name=None,
            version=None,
            default_targets=(),
            package_src_dir=None,
            targets=(),
        ), None
    if toml_status != "missing":
        content = snapshot["lakefile.toml"].content
        if content is None:
            return None, None
        try:
            text = content.decode("utf-8")
            payload = loads_bounded_toml(text, max_depth=_MAX_STRUCTURAL_DEPTH)
        except (UnicodeError, BoundedTomlError):
            _issue(diagnostics, "error", "invalid-lake-toml", "lakefile.toml is not valid UTF-8 TOML.", "lakefile.toml")
            return None, None
        lake = _parse_lake_toml(payload, content, diagnostics)
        return lake, _parse_mathlib_requirement(payload, diagnostics) if lake is not None else None
    _issue(diagnostics, "error", "missing-lake-config", "The project has no Lake source configuration.")
    return None, None


def _parse_lake_toml(
    payload: dict[str, Any], content: bytes, diagnostics: list[ProjectDiagnostic]
) -> LakeProject | None:
    try:
        name = _required_string(payload.get("name"), "name")
        version = _lake_version(payload.get("version"))
        default_targets = _string_list(payload.get("defaultTargets", []), "defaultTargets")
        package_src_dir = _portable_path(
            payload.get("srcDir"), "srcDir", allow_empty=True
        )
        targets: list[LakeTarget] = []
        canonical_target_names: list[str] = []
        for kind in ("lean_lib", "lean_exe"):
            entries = payload.get(kind, [])
            if not isinstance(entries, list):
                raise _InvalidLakeField(kind)
            for entry in entries:
                if not isinstance(entry, dict):
                    raise _InvalidLakeField(kind)
                target_name = _required_string(entry.get("name"), f"{kind}.name")
                canonical_name = _canonical_target_name(target_name)
                if kind == "lean_lib" and "root" in entry:
                    raise _InvalidLakeField("lean_lib.root")
                if kind == "lean_exe" and "roots" in entry:
                    raise _InvalidLakeField("lean_exe.roots")
                if kind == "lean_lib":
                    roots = (
                        _module_list(entry["roots"], "lean_lib.roots")
                        if "roots" in entry
                        else (canonical_name,)
                    )
                else:
                    roots = ()
                targets.append(
                    LakeTarget(
                        kind=kind,
                        name=target_name,
                        root=(
                            (
                                _module(entry["root"], "lean_exe.root")
                                if "root" in entry
                                else canonical_name
                            )
                            if kind == "lean_exe"
                            else None
                        ),
                        roots=roots,
                        src_dir=_portable_path(
                            entry.get("srcDir"),
                            f"{kind}.srcDir",
                            allow_empty=True,
                        ),
                    )
                )
                canonical_target_names.append(canonical_name)
        if len(set(canonical_target_names)) != len(canonical_target_names):
            raise _InvalidLakeField("duplicate target name")
        exe_roots = [target.root for target in targets if target.kind == "lean_exe" and target.root]
        if len(set(exe_roots)) != len(exe_roots):
            raise _InvalidLakeField("duplicate executable root")
        _validate_mathlib_requirements(payload)
    except _NonportableLakePath:
        _issue(
            diagnostics,
            "error",
            "nonportable-lake-path",
            "lakefile.toml contains an absolute or parent-relative path.",
            "lakefile.toml",
        )
        return None
    except _DuplicateMathlibRequirement:
        _issue(
            diagnostics,
            "error",
            "duplicate-mathlib-requirement",
            "lakefile.toml contains multiple direct Mathlib requirements.",
            "lakefile.toml",
        )
        return None
    except _InvalidLakeField:
        _issue(
            diagnostics,
            "error",
            "invalid-lake-field",
            "lakefile.toml contains an invalid field used by Autoform.",
            "lakefile.toml",
        )
        return None
    return LakeProject(
        format="toml",
        path="lakefile.toml",
        sha256=hashlib.sha256(content).hexdigest(),
        name=name,
        version=version,
        default_targets=default_targets,
        package_src_dir=package_src_dir,
        targets=tuple(targets),
    )


def _inspect_toolchain(
    snapshot: dict[str, _SnapshotEntry], diagnostics: list[ProjectDiagnostic]
) -> LeanProject | None:
    if snapshot["lean-toolchain"].status == "missing":
        _issue(diagnostics, "error", "missing-lean-toolchain", "The project has no lean-toolchain file.")
        return None
    content = snapshot["lean-toolchain"].content
    if content is None:
        return None
    try:
        decoded = content.decode("utf-8")
    except UnicodeError:
        decoded = ""
    text = decoded.removesuffix("\n").removesuffix("\r")
    if (
        not text
        or decoded not in {text, f"{text}\n", f"{text}\r\n"}
        or _has_forbidden_unicode(text)
    ):
        _issue(diagnostics, "error", "invalid-lean-toolchain", "lean-toolchain must contain one UTF-8 value.", "lean-toolchain")
        return None
    match = _TOOLCHAIN.fullmatch(text)
    if match is None:
        _issue(
            diagnostics,
            "warning",
            "unrecognized-lean-toolchain",
            "The Lean toolchain is outside Autoform's recognized stable form.",
            "lean-toolchain",
        )
    return LeanProject(
        path="lean-toolchain",
        sha256=hashlib.sha256(content).hexdigest(),
        toolchain=text,
        version=match.group("version") if match is not None else None,
    )


def _open_parent_descriptor(root_descriptor: int, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError(errno.EINVAL, "invalid relative path")
    current = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            exact = _exact_entry_exists(current, part)
            if exact is None:
                raise OSError(errno.EACCES, "directory entries cannot be read")
            if not exact:
                raise FileNotFoundError(errno.ENOENT, "path component spelling differs")
            next_descriptor = _open_directory(part, current)
            os.close(current)
            current = next_descriptor
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _exact_entry_exists(parent_descriptor: int, name: str) -> bool | None:
    try:
        return name in os.listdir(parent_descriptor)
    except OSError:
        return None


def _relative_info(
    root_descriptor: int, relative: str
) -> tuple[str, os.stat_result | None]:
    try:
        parent, name = _open_parent_descriptor(root_descriptor, relative)
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeError, ValueError):
        return "unreadable", None
    try:
        exact = _exact_entry_exists(parent, name)
        if exact is None:
            return "unreadable", None
        if not exact:
            return "missing", None
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeError, ValueError):
        return "unreadable", None
    finally:
        os.close(parent)
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink", metadata
    if stat.S_ISDIR(metadata.st_mode):
        return "directory", metadata
    if stat.S_ISREG(metadata.st_mode):
        return "file", metadata
    return "other", metadata


def _relative_status(root_descriptor: int, relative: str) -> str:
    return _relative_info(root_descriptor, relative)[0]


def _inspect_git(
    snapshot: dict[str, _SnapshotEntry], diagnostics: list[ProjectDiagnostic]
) -> str | None:
    status = snapshot[".git"].status
    if status in {"symlink", "unreadable"}:
        _issue(
            diagnostics,
            "error",
            "git-path-is-symlink",
            "The project's .git metadata path cannot be inspected safely.",
            ".git",
        )
        return None
    return ".git" if status in {"file", "directory"} else None


def _inspect_autoform(
    snapshot: dict[str, _SnapshotEntry], diagnostics: list[ProjectDiagnostic]
) -> AutoformProject:
    paths = {
        "blueprint_path": ("blueprint", "directory"),
        "mkdocs_path": ("mkdocs.yml", "file"),
        "verification_workflow_path": (".github/workflows/autoform-verify.yml", "file"),
        "pages_workflow_path": (".github/workflows/blueprint-pages.yml", "file"),
    }
    values: dict[str, str | None] = {}
    for field, (relative, expected) in paths.items():
        status = snapshot[relative].status
        if status in {"symlink", "unreadable"}:
            _issue(
                diagnostics,
                "error",
                "scaffold-path-is-symlink",
                "An Autoform scaffold path cannot be inspected safely.",
                relative,
            )
            values[field] = None
        elif status == "missing":
            values[field] = None
        elif status != expected:
            _issue(
                diagnostics,
                "error",
                "scaffold-path-unexpected-type",
                "An Autoform scaffold path is not the expected file or directory.",
                relative,
            )
            values[field] = None
        else:
            values[field] = relative
    workflow_count = sum(
        values[field] is not None
        for field in ("verification_workflow_path", "pages_workflow_path")
    )
    if values["blueprint_path"] is not None and values["mkdocs_path"] is None:
        _issue(diagnostics, "warning", "autoform-mkdocs-missing", "The blueprint has no mkdocs.yml.")
    if workflow_count == 1:
        _issue(diagnostics, "warning", "autoform-workflows-partial", "Only one standard Autoform workflow exists.")
    return AutoformProject(
        detected=values["blueprint_path"] is not None,
        blueprint_path=values["blueprint_path"],
        mkdocs_path=values["mkdocs_path"],
        verification_workflow_path=values["verification_workflow_path"],
        pages_workflow_path=values["pages_workflow_path"],
    )


def _compatibility(
    catalog: ReleaseCatalog,
    lean: LeanProject | None,
    mathlib: MathlibProject | None,
    diagnostics: list[ProjectDiagnostic],
) -> ProjectCompatibility:
    matched = None
    resolved = (
        mathlib is not None
        and mathlib.package_type == "git"
        and mathlib.git is not None
        and mathlib.resolved_revision is not None
    )
    if lean is not None and resolved:
        assert mathlib is not None
        matched = next(
            (
                release
                for release in catalog.releases
                if release.lean.toolchain == lean.toolchain
                and release.mathlib.name == mathlib.name
                and release.mathlib.package_type == mathlib.package_type
                and release.mathlib.git == mathlib.git
                and release.mathlib.resolved_revision
                == _git_revision_identity(mathlib.resolved_revision)
                and release.mathlib.subdirectory == mathlib.subdirectory
                and release.mathlib.config_file == mathlib.config_file
                and release.mathlib.manifest_file == mathlib.manifest_file
            ),
            None,
        )
    if matched is not None:
        status = "supported"
        release_id = matched.id
        if mathlib is not None and mathlib.input_revision != matched.mathlib.input_revision:
            _issue(
                diagnostics,
                "warning",
                "mathlib-input-revision-alias",
                "Mathlib resolves to the catalog commit through a different input revision.",
                mathlib.source,
            )
        if mathlib is not None and mathlib.scope not in {"", matched.mathlib.scope}:
            _issue(
                diagnostics,
                "warning",
                "mathlib-scope-alias",
                "Mathlib resolves to the catalog source through a different package scope.",
                mathlib.source,
            )
    elif lean is not None and resolved:
        status = "unlisted"
        release_id = None
        _issue(
            diagnostics,
            "warning",
            "release-unlisted",
            "The resolved Lean and effective Mathlib source identity are not in the bundled release catalog.",
        )
    else:
        status = "indeterminate"
        release_id = None
        _issue(
            diagnostics,
            "warning",
            "release-indeterminate",
            "Offline inspection cannot determine a Lean and Mathlib release pair.",
        )
    return ProjectCompatibility(catalog.schema, status, release_id, catalog.recommended.id)


def _inspect_manifest(
    snapshot: dict[str, _SnapshotEntry],
    declared: _MathlibRequirement | None,
    diagnostics: list[ProjectDiagnostic],
) -> tuple[str | None, str | None, bool, MathlibProject | None]:
    relative = "lake-manifest.json"
    if snapshot[relative].status == "missing":
        _issue(
            diagnostics,
            "warning",
            "missing-lake-manifest",
            "The project has no resolved Lake manifest; dependency compatibility is indeterminate.",
            relative,
        )
        return None, None, False, None
    content = snapshot[relative].content
    if content is None:
        return relative, None, False, None
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
        if _json_nesting_exceeds(text, _MAX_STRUCTURAL_DEPTH):
            raise _InvalidJson
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
        mathlib = _resolved_mathlib(payload, declared, diagnostics)
    except _UnsupportedManifest:
        _issue(
            diagnostics,
            "warning",
            "unsupported-lake-manifest",
            "Lake accepts this legacy manifest, but offline compatibility inspection does not decode it.",
            relative,
        )
        return relative, digest, False, None
    except (
        UnicodeError,
        ValueError,
        RecursionError,
        MemoryError,
        DecimalException,
        _InvalidJson,
    ):
        _issue(
            diagnostics,
            "error",
            "invalid-lake-manifest",
            "lake-manifest.json is not a supported Lake manifest.",
            relative,
        )
        return relative, digest, False, None
    return relative, digest, True, mathlib


def _inspect_overrides(
    snapshot: dict[str, _SnapshotEntry],
    declared: _MathlibRequirement | None,
    diagnostics: list[ProjectDiagnostic],
) -> tuple[str | None, str | None, bool, MathlibProject | None]:
    relative = ".lake/package-overrides.json"
    if snapshot[relative].status == "missing":
        return None, None, True, None
    content = snapshot[relative].content
    if content is None:
        return relative, None, False, None
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
        if _json_nesting_exceeds(text, _MAX_STRUCTURAL_DEPTH):
            raise _InvalidJson
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
        mathlib = _resolved_mathlib(
            payload,
            declared,
            diagnostics,
            source=relative,
        )
    except _UnsupportedManifest:
        _issue(
            diagnostics,
            "warning",
            "unsupported-package-overrides",
            "Lake accepts this legacy override file, but offline compatibility inspection does not decode it.",
            relative,
        )
        return relative, digest, False, None
    except (
        UnicodeError,
        ValueError,
        RecursionError,
        MemoryError,
        DecimalException,
        _InvalidJson,
    ):
        _issue(
            diagnostics,
            "error",
            "invalid-package-overrides",
            "package-overrides.json is not a supported Lake override file.",
            relative,
        )
        return relative, digest, False, None
    return relative, digest, True, mathlib


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise _InvalidJson


def _manifest_version(value: object) -> tuple[int, int, int]:
    if type(value) is int:
        if value < 5:
            raise _InvalidJson
        if value < 7:
            raise _UnsupportedManifest
        return 0, value, 0
    if type(value) is not str:
        raise _InvalidJson
    match = _MANIFEST_VERSION.fullmatch(value)
    if match is None:
        raise _InvalidJson
    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    if version[0] > 1 or version < (0, 5, 0):
        raise _InvalidJson
    if version < (0, 7, 0):
        raise _UnsupportedManifest
    return version


def _manifest_string(value: object, *, empty: bool = False) -> str:
    if (
        type(value) is not str
        or (not empty and not value)
        or _has_forbidden_unicode(value)
    ):
        raise _InvalidJson
    return value


def _json_string(value: object) -> str:
    if type(value) is not str:
        raise _InvalidJson
    return value


def _git_revision_identity(value: str) -> str:
    return value.lower() if _FULL_GIT_REVISION.fullmatch(value) is not None else value


def _manifest_path(
    value: object,
    *,
    optional: bool = False,
    root_is_none: bool = False,
) -> str | None:
    if value is None:
        if optional:
            return None
        raise _InvalidJson
    text = _manifest_string(value)
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        "\\" in text
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
    ):
        raise _InvalidJson
    normalized = posix.as_posix()
    if normalized != text:
        raise _InvalidJson
    if normalized == ".":
        if root_is_none:
            return None
        raise _InvalidJson
    return normalized


def _resolved_mathlib(
    payload: object,
    declared: _MathlibRequirement | None,
    diagnostics: list[ProjectDiagnostic],
    *,
    source: str = "lake-manifest.json",
) -> MathlibProject | None:
    if type(payload) is not dict:
        raise _InvalidJson
    _manifest_version(payload.get("version", payload.get("schemaVersion")))
    if source == "lake-manifest.json":
        root_name_value = payload.get("name")
        if root_name_value is not None:
            root_name = _manifest_string(root_name_value)
            if (
                root_name != "[anonymous]"
                and _canonical_module_name(root_name) is None
            ):
                raise _InvalidJson
        lake_dir = payload.get("lakeDir")
        if lake_dir is not None:
            _manifest_path(lake_dir)
        fixed_toolchain = payload.get("fixedToolchain")
        if fixed_toolchain is not None and type(fixed_toolchain) is not bool:
            raise _InvalidJson
        packages_dir = payload.get("packagesDir")
        if packages_dir is not None:
            _manifest_path(packages_dir)
    packages = payload.get("packages")
    if packages is None:
        packages = []
    if type(packages) is not list:
        raise _InvalidJson
    matches: list[_MaterializedMathlib] = []
    for package in packages:
        if type(package) is not dict:
            raise _InvalidJson
        name = _canonical_module_name(_manifest_string(package.get("name")))
        if name is None:
            raise _InvalidJson
        scope_value = package.get("scope")
        scope = "" if scope_value is None else _json_string(scope_value)
        inherited = package.get("inherited")
        source_type = package.get("type")
        if type(inherited) is not bool or source_type not in {"git", "path"}:
            raise _InvalidJson
        config_file_value = package.get("configFile")
        config_file = _json_string(
            "lakefile" if config_file_value is None else config_file_value
        )
        manifest_file_value = package.get("manifestFile")
        manifest_file = _json_string(
            "lake-manifest.json"
            if manifest_file_value is None
            else manifest_file_value
        )
        if source_type == "path":
            directory = _json_string(package.get("dir"))
            if name == "mathlib":
                matches.append(
                    _MaterializedMathlib(
                        scope,
                        "path",
                        None,
                        None,
                        None,
                        None,
                        config_file,
                        manifest_file,
                        directory,
                    )
                )
            continue
        raw_git = _json_string(package.get("url"))
        revision = _json_string(package.get("rev"))
        input_revision = package.get("inputRev")
        if input_revision is not None:
            input_revision = _json_string(input_revision)
        subdirectory = package.get("subDir")
        if subdirectory is not None:
            subdirectory = _json_string(subdirectory)
        if name == "mathlib":
            matches.append(
                _MaterializedMathlib(
                    scope,
                    "git",
                    raw_git,
                    revision,
                    input_revision,
                    subdirectory,
                    config_file,
                    manifest_file,
                    None,
                )
            )
    if not matches:
        return None
    selected = matches[-1]
    scope = _manifest_string(selected.scope, empty=True)
    config_file = _manifest_path(selected.config_file)
    manifest_file = _manifest_path(selected.manifest_file)
    assert config_file is not None and manifest_file is not None
    if selected.package_type == "path":
        directory = _manifest_path(selected.path)
        assert directory is not None
        materialized = _MaterializedMathlib(
            scope,
            "path",
            None,
            None,
            None,
            None,
            config_file,
            manifest_file,
            directory,
        )
    else:
        assert selected.git is not None and selected.resolved_revision is not None
        git = _normalize_mathlib_git(selected.git, diagnostics, source)
        if git is None:
            raise _InvalidJson
        revision = _manifest_string(selected.resolved_revision, empty=True)
        input_revision = (
            None
            if selected.input_revision is None
            else _manifest_string(selected.input_revision, empty=True)
        )
        subdirectory = (
            None
            if selected.subdirectory in (None, "")
            else _manifest_path(selected.subdirectory, root_is_none=True)
        )
        materialized = _MaterializedMathlib(
            scope,
            "git",
            git,
            revision,
            input_revision,
            subdirectory,
            config_file,
            manifest_file,
            None,
        )
    if source == "lake-manifest.json" and declared is not None and (
        (
            declared.revision is not None
            and declared.revision != materialized.input_revision
        )
        or (declared.git is not None and declared.git != materialized.git)
        or (declared.scope and declared.scope != materialized.scope)
        or declared.source_kind == "path"
        or declared.subdirectory != materialized.subdirectory
    ):
        _issue(
        diagnostics,
            "warning",
            "mathlib-manifest-stale",
            "The resolved Mathlib manifest does not match the current Lake requirement.",
            "lake-manifest.json",
        )
    return MathlibProject(
        name="mathlib",
        scope=materialized.scope,
        package_type=materialized.package_type,
        git=materialized.git,
        input_revision=materialized.input_revision,
        resolved_revision=materialized.resolved_revision,
        declared_revision=declared.revision if declared is not None else None,
        subdirectory=materialized.subdirectory,
        config_file=materialized.config_file,
        manifest_file=materialized.manifest_file,
        path=materialized.path,
        source=source,
    )


def _validate_mathlib_requirements(payload: dict[str, Any]) -> None:
    requirements = payload.get("require", [])
    if not isinstance(requirements, list):
        raise _InvalidLakeField("require")
    canonical_names: list[str] = []
    for entry in requirements:
        if not isinstance(entry, dict):
            raise _InvalidLakeField("require")
        canonical_names.append(
            _canonical_target_name(_required_string(entry.get("name"), "require.name"))
        )
    matches = [
        entry
        for entry, canonical_name in zip(requirements, canonical_names, strict=True)
        if canonical_name == "mathlib"
    ]
    if len(matches) > 1:
        raise _DuplicateMathlibRequirement("duplicate mathlib")
    for entry in matches:
        if "scope" in entry and entry["scope"] != "":
            _required_string(entry["scope"], "mathlib.scope")
        if "rev" in entry:
            _required_string(entry["rev"], "mathlib.rev")
        if "subDir" in entry:
            _portable_path(entry["subDir"], "mathlib.subDir", allow_empty=True)
        if "path" in entry:
            _portable_path(entry["path"], "mathlib.path")
        elif "git" in entry:
            _git_url(entry["git"], "mathlib.git")
        elif "source" in entry:
            _validate_dependency_source(entry["source"])


def _parse_mathlib_requirement(
    payload: dict[str, Any], diagnostics: list[ProjectDiagnostic]
) -> _MathlibRequirement | None:
    requirements = payload.get("require", [])
    if not isinstance(requirements, list):
        return None
    matches = [
        entry
        for entry in requirements
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and _canonical_target_name(entry["name"]) == "mathlib"
    ]
    if len(matches) != 1:
        return None
    entry = matches[0]
    name = _canonical_target_name(entry["name"])
    scope = entry.get("scope", "")
    if not isinstance(scope, str):
        return None
    revision = entry.get("rev")
    if revision is not None and not isinstance(revision, str):
        return None
    raw_subdirectory = entry.get("subDir")
    subdirectory = (
        None
        if raw_subdirectory in (None, "", ".")
        else raw_subdirectory if isinstance(raw_subdirectory, str) else None
    )
    if "path" in entry:
        return _MathlibRequirement(name, scope, None, revision, "path", subdirectory)
    if "source" in entry:
        source = entry["source"]
        if not isinstance(source, dict):
            return None
        source_type = source.get("type")
        if source_type == "path":
            return _MathlibRequirement(name, scope, None, revision, "path", subdirectory)
        if source_type == "git":
            raw_git = source.get("url")
            if not isinstance(raw_git, str):
                return None
            source_revision = source.get("rev", revision)
            if source_revision is not None and not isinstance(source_revision, str):
                return None
            source_subdirectory = source.get("subDir", raw_subdirectory)
            subdirectory = (
                None
                if source_subdirectory in (None, "", ".")
                else source_subdirectory if isinstance(source_subdirectory, str) else None
            )
            git = _normalize_mathlib_git(raw_git, diagnostics, "lakefile.toml")
            return (
                None
                if git is None
                else _MathlibRequirement(
                    name,
                    scope,
                    git,
                    source_revision,
                    "git",
                    subdirectory,
                )
            )
        return None
    git = _mathlib_git_source(entry)
    if git is None:
        return _MathlibRequirement(
            name,
            scope,
            None,
            revision,
            "reservoir",
            subdirectory,
        )
    normalized_git = _normalize_mathlib_git(git, diagnostics, "lakefile.toml")
    if normalized_git is None:
        return None
    return _MathlibRequirement(
        name,
        scope,
        normalized_git,
        revision,
        "git",
        subdirectory,
    )


def _normalize_mathlib_git(
    git: str,
    diagnostics: list[ProjectDiagnostic],
    path: str,
) -> str | None:
    try:
        parsed = urlsplit(git)
        port = parsed.port
    except ValueError:
        _issue(
            diagnostics,
            "error",
            "invalid-mathlib-url",
            "The direct Mathlib Git URL is invalid.",
            path,
        )
        return None
    if parsed.username is not None or parsed.password is not None:
        _issue(
            diagnostics,
            "error",
            "credentialed-mathlib-url",
            "The direct Mathlib Git URL must not contain credentials.",
            path,
        )
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() != parsed.hostname.lower()
    ):
        _issue(
            diagnostics,
            "error",
            "invalid-mathlib-url",
            "The direct Mathlib Git URL must be credential-free HTTPS.",
            path,
        )
        return None
    normalized_path = parsed.path.rstrip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    if not normalized_path:
        _issue(
            diagnostics,
            "error",
            "invalid-mathlib-url",
            "The direct Mathlib Git URL must identify a repository.",
            path,
        )
        return None
    return f"https://{parsed.hostname.lower()}{normalized_path}"


def _validate_dependency_source(value: Any) -> None:
    if not isinstance(value, dict):
        raise _InvalidLakeField("mathlib.source")
    source_type = _required_string(value.get("type"), "mathlib.source.type")
    if source_type == "path":
        if set(value) != {"type", "dir"}:
            raise _InvalidLakeField("mathlib.source")
        _portable_path(value["dir"], "mathlib.source.dir")
    elif source_type == "git":
        if not set(value) <= {"type", "url", "rev", "subDir"} or "url" not in value:
            raise _InvalidLakeField("mathlib.source")
        _required_string(value["url"], "mathlib.source.url")
        if "rev" in value:
            _required_string(value["rev"], "mathlib.source.rev")
        if "subDir" in value:
            _portable_path(
                value["subDir"], "mathlib.source.subDir", allow_empty=True
            )
    else:
        raise _InvalidLakeField("mathlib.source.type")


def _mathlib_git_source(entry: dict[str, Any]) -> str | None:
    git = entry.get("git")
    if isinstance(git, dict):
        git = git.get("url")
    if isinstance(git, str):
        return git
    return None


def _git_url(value: Any, field: str) -> str:
    if isinstance(value, dict):
        if set(value) != {"url"}:
            raise _InvalidLakeField(field)
        value = value["url"]
    return _required_string(value, field)


def _required_string(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _has_forbidden_unicode(value)
    ):
        raise _InvalidLakeField(field)
    return value


def _has_forbidden_unicode(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _lake_version(value: Any) -> str | None:
    if value is None:
        return None
    text = _required_string(value, "version")
    if _LAKE_VERSION.fullmatch(text) is None:
        raise _InvalidLakeField("version")
    return text


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _InvalidLakeField(field)
    return tuple(_required_string(item, field) for item in value)


def _module_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _InvalidLakeField(field)
    return tuple(_module(item, field) for item in value)


def _module(value: Any, field: str) -> str:
    canonical = _canonical_module_name(_required_string(value, field))
    if canonical is None:
        raise _InvalidLakeField(field)
    return canonical


def _canonical_module_name(value: str) -> str | None:
    """Render a Lake name the way Lean's `String.toName` reads it.

    Numeric components are decoded as naturals, so `01` and `1` name the same
    module, and letter-like Unicode components such as `Ω` are accepted.
    Returns None when Lean would reject the string outright.
    """
    components = _split_lean_name(value)
    if components is None:
        return None
    root_kind, root_text = components[0]
    escape = not (
        root_kind == "str"
        and (root_text.startswith("#") or root_text.startswith("?"))
    )
    return ".".join(
        _render_lean_component(kind, text, escape=escape)
        for kind, text in components
    )


def _canonical_target_name(value: str) -> str:
    """Apply Lake's `stringToLegalOrSimpleName` fallback for target names."""
    canonical = _canonical_module_name(value)
    if canonical is not None:
        return canonical
    escape = not (value.startswith("#") or value.startswith("?"))
    return _render_lean_component("str", value, escape=escape)


def _split_lean_name(value: str) -> list[tuple[str, str]] | None:
    components: list[tuple[str, str]] = []
    index = 0
    while True:
        if index >= len(value):
            return None
        character = value[index]
        if character == _LEAN_ID_BEGIN_ESCAPE:
            end = value.find(_LEAN_ID_END_ESCAPE, index + 1)
            if end < 0:
                return None
            components.append(("str", value[index + 1 : end]))
            index = end + 1
        elif _lean_is_id_first(character):
            start = index
            index += 1
            while index < len(value) and _lean_is_id_rest(value[index]):
                index += 1
            components.append(("str", value[start:index]))
        elif _lean_is_digit(character):
            start = index
            while index < len(value) and _lean_is_digit(value[index]):
                index += 1
            digits = value[start:index]
            components.append(("num", digits.lstrip("0") or "0"))
        else:
            return None
        if index == len(value):
            return components
        if value[index] != ".":
            return None
        index += 1


def _render_lean_component(kind: str, text: str, *, escape: bool = True) -> str:
    if kind == "num":
        return text
    if not escape:
        return text
    # Lean's `Name.escapePart` cannot round-trip a closing guillemet, so it
    # leaves the complete simple component unescaped in that case.
    if _LEAN_ID_END_ESCAPE in text:
        return text
    if text and _lean_is_id_first(text[0]) and all(_lean_is_id_rest(c) for c in text[1:]):
        return text
    return f"{_LEAN_ID_BEGIN_ESCAPE}{text}{_LEAN_ID_END_ESCAPE}"


def _lean_is_digit(character: str) -> bool:
    return "0" <= character <= "9"


def _lean_is_alpha(character: str) -> bool:
    return "a" <= character <= "z" or "A" <= character <= "Z"


def _lean_is_id_first(character: str) -> bool:
    return _lean_is_alpha(character) or character == "_" or _lean_is_letter_like(character)


def _lean_is_id_rest(character: str) -> bool:
    return (
        _lean_is_alpha(character)
        or _lean_is_digit(character)
        or character in "_'!?"
        or _lean_is_letter_like(character)
        or _lean_is_subscript_alnum(character)
    )


def _lean_is_letter_like(character: str) -> bool:
    code = ord(character)
    return (
        (0x3B1 <= code <= 0x3C9 and code != 0x3BB)
        or (0x391 <= code <= 0x3A9 and code not in {0x3A0, 0x3A3})
        or 0x3CA <= code <= 0x3FB
        or 0x1F00 <= code <= 0x1FFE
        or 0x2100 <= code <= 0x214F
        or 0x1D49C <= code <= 0x1D59F
        or (0xC0 <= code <= 0xFF and code not in {0xD7, 0xF7})
        or 0x100 <= code <= 0x17F
    )


def _lean_is_subscript_alnum(character: str) -> bool:
    code = ord(character)
    return (
        0x2080 <= code <= 0x2089
        or 0x2090 <= code <= 0x209C
        or 0x1D62 <= code <= 0x1D6A
        or code == 0x2C7C
    )


def _portable_path(
    value: Any, field: str, *, allow_empty: bool = False
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in {"", "."} and allow_empty:
        return "."
    text = _required_string(value, field)
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        posix.is_absolute()
        or "\\" in text
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or "." in posix.parts
        or ".." in windows.parts
        or "." in windows.parts
    ):
        raise _NonportableLakePath(field)
    return posix.as_posix()


def _json_nesting_exceeds(text: str, limit: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > limit:
                return True
        elif character in "]}":
            depth = max(0, depth - 1)
    return False


def _issue(
    diagnostics: list[ProjectDiagnostic],
    severity: str,
    code: str,
    message: str,
    path: str | None = None,
) -> None:
    diagnostics.append(ProjectDiagnostic(severity, code, message, path))


def _ordered(diagnostics: list[ProjectDiagnostic]) -> tuple[ProjectDiagnostic, ...]:
    unique = set(diagnostics)
    return tuple(
        sorted(
            unique,
            key=lambda diagnostic: (
                _SEVERITY_ORDER[diagnostic.severity],
                diagnostic.code,
                diagnostic.path or "",
                diagnostic.message,
            ),
        )
    )
