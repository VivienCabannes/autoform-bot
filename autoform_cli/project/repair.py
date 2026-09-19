"""Conservatively add unambiguous missing files to an existing project."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..scaffold import DEFAULT_AUTOFORM_SOURCE, _scaffold_plan
from .create import (
    ProjectCreateError,
    _OwnedDescriptor,
    _noreplace_function,
    _open_parent,
    _plan_tree,
    _rename_noreplace,
    _validate_workflow_pin,
)
from .inspect import inspect_project

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import compatibility
    fcntl = None  # type: ignore[assignment]

PROJECT_REPAIR_SCHEMA = "autoform-project-repair/v1"
_RENDER_REF = "0" * 40
_MAX_DIRECTORY_ENTRIES = 100_000
_MAX_INSPECTION_FILE_BYTES = 2 * 1024 * 1024
_MAX_MANAGED_FILE_BYTES = 32 * 1024 * 1024
_REQUIRED_INPUTS = {
    "README.md": ("title",),
    "blueprint/README.md": ("title",),
    "blueprint/roadmap/README.md": ("title",),
    "mkdocs.yml": ("title", "repository-url"),
    ".github/workflows/autoform-verify.yml": ("autoform-source", "autoform-ref"),
    ".github/workflows/blueprint-pages.yml": ("autoform-source", "autoform-ref"),
}
_WORKFLOW_PATHS = (
    ".github/workflows/autoform-verify.yml",
    ".github/workflows/blueprint-pages.yml",
)


@dataclass(frozen=True, order=True, slots=True)
class ProjectRepairConflict:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "path": self.path}


class ProjectRepairError(ValueError):
    """Repair would require changing or guessing existing project content."""

    def __init__(
        self,
        conflicts: tuple[ProjectRepairConflict, ...],
        *,
        code: str = "project-repair-conflict",
        written: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.conflicts = conflicts
        self.written = written
        self.message = "The project cannot be repaired without changing or guessing existing content."
        super().__init__(self.message)

    def as_dict(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "conflicts": [conflict.as_dict() for conflict in self.conflicts],
                "message": self.message,
            },
            "ok": False,
            "schema": PROJECT_REPAIR_SCHEMA,
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProjectRepairResult:
    dry_run: bool
    package: str
    release: str
    planned: tuple[str, ...]
    written: tuple[str, ...]
    preserved: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "ok": True,
            "package": self.package,
            "planned": list(self.planned),
            "preserved": list(self.preserved),
            "release": self.release,
            "schema": PROJECT_REPAIR_SCHEMA,
            "target": ".",
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    path: str
    content: bytes
    mode: int
    required_inputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ParentIdentity:
    name: str
    path: str
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _ObservedPath:
    path: str
    signature: tuple[int, ...] | None
    sha256: str | None


def repair_project(
    target: str | Path,
    *,
    dry_run: bool = False,
    title: str | None = None,
    repository_url: str | None = None,
    autoform_source: str | None = None,
    autoform_ref: str | None = None,
) -> ProjectRepairResult:
    """Add only absent, canonically generated Autoform overlay files."""

    root = _project_root(target)
    root_descriptor = _open_root(root)
    written: list[str] = []
    try:
        try:
            if fcntl is None:
                raise OSError(errno.ENOSYS, "advisory locks are unavailable")
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        except (AttributeError, OSError):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "The project root cannot be locked for conservative repair.",
                        ".",
                    ),
                ),
                code="project-repair-safety-unavailable",
            ) from None
        if not dry_run and _noreplace_function() is None:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "The project filesystem cannot publish repair files atomically.",
                        ".",
                    ),
                ),
                code="project-repair-safety-unavailable",
            )
        root_identity = _descriptor_identity(root_descriptor)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_private_directory(root_descriptor, ".")
        inspection = inspect_project(root)
        _require_root_identity(root_descriptor, root_identity, root)
        conflicts = _inspection_conflicts(inspection)
        if conflicts:
            raise ProjectRepairError(tuple(sorted(conflicts)))
        assert inspection.lake is not None
        assert inspection.lake.name is not None
        assert inspection.compatibility.release is not None
        _require_config_identity(root_descriptor, inspection)

        try:
            desired = _render_overlay(
                title=title,
                repository_url=repository_url,
                autoform_source=autoform_source,
                autoform_ref=autoform_ref,
            )
        except ProjectRepairError:
            raise
        except (OSError, ValueError):
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-render-failed",
                        "The canonical repair overlay could not be rendered.",
                    ),
                ),
                code="project-repair-failed",
            ) from None
        _require_root_identity(root_descriptor, root_identity, root)
        _require_config_identity(root_descriptor, inspection)
        provided_inputs = frozenset(
            name
            for name, value in (
                ("title", title),
                ("repository-url", repository_url),
                ("autoform-source", autoform_source),
                ("autoform-ref", autoform_ref),
            )
            if value is not None
        )
        recovery_conflicts = _find_recovery_conflicts(root_descriptor, desired)
        if recovery_conflicts:
            raise ProjectRepairError(tuple(sorted(recovery_conflicts)))
        desired, omitted_observations = _scope_workflow_files(
            root_descriptor, desired, provided_inputs
        )
        planned, preserved, preserved_observations, path_conflicts = _plan(
            root_descriptor, desired, provided_inputs
        )
        if path_conflicts:
            raise ProjectRepairError(tuple(sorted(path_conflicts)))
        planned_paths = tuple(item.path for item in planned)
        observations = {
            observation.path: observation
            for observation in preserved_observations
        }
        observations.update(
            {
                observation.path: observation
                for observation in omitted_observations
            }
        )
        for item in planned:
            _validate_parent_chain(root_descriptor, item.path)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_config_identity(root_descriptor, inspection)
        _require_observed_paths(root_descriptor, tuple(observations.values()))
        if dry_run or not planned:
            return ProjectRepairResult(
                dry_run=dry_run,
                package=inspection.lake.name,
                release=inspection.compatibility.release,
                planned=planned_paths,
                written=(),
                preserved=preserved,
            )
        for item in planned:
            try:
                _require_root_identity(root_descriptor, root_identity, root)
                _require_config_identity(root_descriptor, inspection)
                _require_observed_paths(root_descriptor, tuple(observations.values()))
                published_observation = _publish(
                    root,
                    root_descriptor,
                    root_identity,
                    item,
                    inspection,
                    tuple(
                        observation
                        for path, observation in observations.items()
                        if path != item.path
                    ),
                )
            except OSError:
                raise ProjectRepairError(
                    (
                        ProjectRepairConflict(
                            "project-repair-write-failed",
                            "A managed path could not be traversed or published safely.",
                            item.path,
                        ),
                    ),
                    code="project-repair-failed",
                    written=tuple(written),
                ) from None
            except ProjectRepairError as error:
                published = tuple((*written, *error.written))
                raise ProjectRepairError(
                    error.conflicts,
                    code=error.code,
                    written=published,
                ) from None
            written.append(item.path)
            observations[item.path] = published_observation
        try:
            _require_root_identity(root_descriptor, root_identity, root)
            _require_config_identity(root_descriptor, inspection)
            _require_observed_paths(root_descriptor, tuple(observations.values()))
        except ProjectRepairError as error:
            raise ProjectRepairError(
                error.conflicts,
                code=error.code,
                written=tuple(written),
            ) from None
        return ProjectRepairResult(
            dry_run=False,
            package=inspection.lake.name,
            release=inspection.compatibility.release,
            planned=planned_paths,
            written=tuple(written),
            preserved=preserved,
        )
    except OSError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-io-failed",
                    "A project path could not be inspected or repaired safely.",
                    ".",
                ),
            ),
            code="project-repair-failed",
            written=tuple(written),
        ) from None
    finally:
        pending_error = sys.exc_info()[1]
        try:
            os.close(root_descriptor)
        except OSError:
            conflict = ProjectRepairConflict(
                "project-repair-close-failed",
                "The project root descriptor could not be closed.",
                ".",
            )
            if isinstance(pending_error, ProjectRepairError):
                raise ProjectRepairError(
                    (*pending_error.conflicts, conflict),
                    code=pending_error.code,
                    written=pending_error.written,
                ) from None
            raise ProjectRepairError(
                (conflict,),
                code="project-repair-failed",
                written=tuple(written),
            ) from None


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _close_owner_preserving(owner: _OwnedDescriptor) -> None:
    pending_error = sys.exc_info()[1]
    try:
        owner.close()
    except OSError:
        if pending_error is None:
            raise


def _open_root(root: Path) -> int:
    try:
        return _open_parent(root)
    except ProjectCreateError as error:
        if error.code == "project-create-safety-unavailable":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "The platform cannot traverse the project with the required path safety.",
                        ".",
                    ),
                ),
                code="project-repair-safety-unavailable",
            ) from None
        if error.code == "project-path-is-symlink":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-target-invalid",
                        "The repair target path must not contain a symbolic link.",
                        ".",
                    ),
                )
            ) from None
        raise _race_conflict(".", "The project root changed during repair.") from None


def _require_root_identity(
    descriptor: int, expected: tuple[int, int], path: Path
) -> None:
    metadata = os.fstat(descriptor)
    try:
        named = path.stat(follow_symlinks=False)
    except OSError:
        raise _race_conflict(".", "The project root changed during repair.") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (metadata.st_dev, metadata.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise _race_conflict(".", "The project root changed during repair.")
    _require_private_directory(descriptor, ".")


def _project_root(target: str | Path) -> Path:
    try:
        requested = Path(target).expanduser()
        if requested.is_symlink():
            raise OSError
        root = requested.absolute()
    except (OSError, RuntimeError, ValueError):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-target-invalid",
                    "The repair target must be an existing project directory.",
                ),
            )
        ) from None
    if not root.is_dir() or not (root / "lakefile.toml").is_file():
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-target-invalid",
                    "The repair target must be the existing project root.",
                    "lakefile.toml",
                ),
            )
        )
    return root


def _require_config_identity(root_descriptor: int, inspection) -> None:
    assert inspection.lake is not None
    assert inspection.lean is not None
    expected = {
        inspection.lake.path: inspection.lake.sha256,
        inspection.lean.path: inspection.lean.sha256,
    }
    for relative, digest in expected.items():
        try:
            observation = _observe_regular_file(
                root_descriptor,
                relative,
                relative,
                max_bytes=_MAX_INSPECTION_FILE_BYTES,
            )
        except OSError:
            raise _race_conflict(relative, "Project configuration changed during repair.") from None
        if observation.sha256 != digest:
            raise _race_conflict(relative, "Project configuration changed during repair.")


def _inspection_conflicts(inspection) -> list[ProjectRepairConflict]:
    conflicts = [
        ProjectRepairConflict(
            "project-repair-inspection-failed",
            diagnostic.message,
            diagnostic.path,
        )
        for diagnostic in inspection.diagnostics
        if diagnostic.severity == "error"
    ]
    if inspection.lake is None or inspection.lake.name is None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-package-indeterminate",
                "The existing Lake package name is required for repair.",
                "lakefile.toml",
            )
        )
    if inspection.lean is None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-toolchain-indeterminate",
                "An existing lean-toolchain is required for repair.",
                "lean-toolchain",
            )
        )
    if inspection.compatibility.status != "supported" or inspection.compatibility.release is None:
        conflicts.append(
            ProjectRepairConflict(
                "project-repair-release-indeterminate",
                "Repair requires an existing Lean/Mathlib pair from the bundled release catalog.",
            )
        )
    return conflicts


def _render_overlay(
    *,
    title: str | None,
    repository_url: str | None,
    autoform_source: str | None,
    autoform_ref: str | None,
) -> tuple[_PlannedFile, ...]:
    if (autoform_source is None) != (autoform_ref is None):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-input-invalid",
                    "--autoform-source and --autoform-ref must be supplied together.",
                ),
            ),
            code="project-repair-input-invalid",
        )
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise _input_error("--title must be nonempty when supplied.")
    if repository_url is not None and not isinstance(repository_url, str):
        raise _input_error("--repository-url must be text when supplied.")
    if autoform_source is None:
        workflow_source, workflow_ref = DEFAULT_AUTOFORM_SOURCE, _RENDER_REF
    else:
        if (
            not isinstance(autoform_source, str)
            or not isinstance(autoform_ref, str)
            or not autoform_source.strip()
            or not autoform_ref.strip()
        ):
            raise _input_error("--autoform-source and --autoform-ref must both be nonempty.")
        try:
            workflow_source, workflow_ref = _validate_workflow_pin(
                autoform_source, autoform_ref
            )
        except ProjectCreateError as error:
            raise _input_error(error.message) from None
    try:
        plan, omitted = _scaffold_plan(
            title=title.strip() if title is not None else "Autoform repair placeholder",
            repository_url=repository_url.strip() if repository_url is not None else "",
            autoform_source=workflow_source,
            autoform_ref=workflow_ref,
        )
        _plan_tree(plan)
    except (OSError, UnicodeError, ValueError):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-render-failed",
                    "The canonical repair overlay could not be rendered.",
                ),
            ),
            code="project-repair-failed",
        ) from None
    if omitted:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-render-failed",
                    "The canonical repair overlay omitted required files.",
                ),
            ),
            code="project-repair-failed",
        )
    return tuple(
        _PlannedFile(
            item.relative,
            item.content,
            item.mode,
            _REQUIRED_INPUTS.get(item.relative, ()),
        )
        for item in sorted(plan, key=lambda planned: planned.relative)
    )


def _input_error(message: str) -> ProjectRepairError:
    return ProjectRepairError(
        (ProjectRepairConflict("project-repair-input-invalid", message),),
        code="project-repair-input-invalid",
    )


def _managed_path_state(root_descriptor: int, path: str) -> str:
    root_device = os.fstat(root_descriptor).st_dev
    owner = _OwnedDescriptor(os.dup(root_descriptor))
    try:
        for part in PurePosixPath(path).parts[:-1]:
            flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                child = _OwnedDescriptor(
                    os.open(part, flags, dir_fd=owner.descriptor)
                )
            except FileNotFoundError:
                return "absent"
            except OSError:
                return "unsafe"
            try:
                child_device = os.fstat(child.descriptor).st_dev
            except BaseException:
                _close_owner_preserving(child)
                raise
            if child_device != root_device:
                try:
                    child.close()
                except OSError:
                    pass
                return "unsafe"
            try:
                owner.replace(child)
            except OSError:
                return "unsafe"
        try:
            os.stat(
                PurePosixPath(path).name,
                dir_fd=owner.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unsafe"
        return "exists"
    finally:
        _close_owner_preserving(owner)


def _stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _observe_regular_file(
    parent_descriptor: int,
    name: str,
    path: str,
    *,
    max_bytes: int = _MAX_MANAGED_FILE_BYTES,
) -> _ObservedPath:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        named_before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(named_before.st_mode)
            or _stat_signature(named_before) != _stat_signature(opened_before)
        ):
            raise OSError(errno.ESTALE, "managed file changed during repair")
        limit = max_bytes + 1
        content = bytearray()
        while len(content) < limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes:
            raise OSError(errno.EFBIG, "managed file is too large to snapshot")
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not (
            _stat_signature(named_before)
            == _stat_signature(opened_after)
            == _stat_signature(named_after)
        ):
            raise OSError(errno.ESTALE, "managed file changed during repair")
        if len(content) != opened_after.st_size:
            raise OSError(errno.ESTALE, "managed file changed during repair")
        return _ObservedPath(
            path,
            _stat_signature(opened_after),
            hashlib.sha256(content).hexdigest(),
        )
    finally:
        os.close(descriptor)


def _directory_entries(descriptor: int, path: str) -> tuple[str, ...]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fresh = os.open(".", flags, dir_fd=descriptor)
    try:
        if _descriptor_identity(fresh) != _descriptor_identity(descriptor):
            raise OSError(errno.ESTALE, "managed directory changed during repair")
        entries: list[str] = []
        with os.scandir(fresh) as iterator:
            for entry in iterator:
                if len(entries) >= _MAX_DIRECTORY_ENTRIES:
                    raise ProjectRepairError(
                        (
                            ProjectRepairConflict(
                                "project-repair-directory-too-large",
                                "A managed directory has too many entries to inspect safely.",
                                path,
                            ),
                        )
                    )
                entries.append(entry.name)
    finally:
        os.close(fresh)
    return tuple(entries)


def _scope_workflow_files(
    root_descriptor: int,
    desired: tuple[_PlannedFile, ...],
    provided_inputs: frozenset[str],
) -> tuple[
    tuple[_PlannedFile, ...],
    tuple[_ObservedPath, ...],
]:
    observations: list[_ObservedPath] = []
    for path in _WORKFLOW_PATHS:
        state = _managed_path_state(root_descriptor, path)
        if state == "unsafe":
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-destination-invalid",
                        "A workflow destination could not be inspected safely.",
                        path,
                    ),
                )
            )
        if state == "absent":
            observations.append(_ObservedPath(path, None, None))
        else:
            try:
                observations.append(_observe_path(root_descriptor, path))
            except OSError:
                raise _race_conflict(
                    path, "A workflow destination changed during repair."
                ) from None
    states = tuple(
        "absent" if observation.signature is None else "exists"
        for observation in observations
    )
    if {"autoform-source", "autoform-ref"} <= provided_inputs:
        if states.count("exists") == 1:
            rendered = {item.path: item.content for item in desired}
            existing = next(
                observation
                for observation in observations
                if observation.signature is not None
            )
            if existing.sha256 != hashlib.sha256(rendered[existing.path]).hexdigest():
                raise ProjectRepairError(
                    (
                        ProjectRepairConflict(
                            "project-repair-workflow-mismatch",
                            "An existing workflow does not match the supplied immutable provenance.",
                            existing.path,
                        ),
                    )
                )
        return desired, tuple(observations)
    if states == ("absent", "absent"):
        return (
            tuple(item for item in desired if not item.path.startswith(".github/")),
            tuple(observations),
        )
    return desired, tuple(observations)


def _observe_path(root_descriptor: int, path: str) -> _ObservedPath:
    root_device = os.fstat(root_descriptor).st_dev
    owner = _OwnedDescriptor(os.dup(root_descriptor))
    try:
        walked: list[str] = []
        for part in PurePosixPath(path).parts[:-1]:
            walked.append(part)
            _open_existing_directory(
                owner,
                part,
                "/".join(walked),
                expected_device=root_device,
            )
        return _observe_regular_file(
            owner.descriptor,
            PurePosixPath(path).name,
            path,
        )
    finally:
        _close_owner_preserving(owner)


def _find_recovery_conflicts(
    root_descriptor: int, desired: tuple[_PlannedFile, ...]
) -> list[ProjectRepairConflict]:
    conflicts: list[ProjectRepairConflict] = []
    root_device = os.fstat(root_descriptor).st_dev
    for item in desired:
        owner = _OwnedDescriptor(os.dup(root_descriptor))
        try:
            safe_parent = True
            walked: list[str] = []
            for part in PurePosixPath(item.path).parts[:-1]:
                walked.append(part)
                try:
                    _open_existing_directory(
                        owner,
                        part,
                        "/".join(walked),
                        expected_device=root_device,
                    )
                except ProjectRepairError as error:
                    conflicts.extend(
                        conflict
                        for conflict in error.conflicts
                        if conflict.code == "project-repair-close-failed"
                    )
                    safe_parent = False
                    break
            if not safe_parent:
                continue
            name = PurePosixPath(item.path).name
            try:
                entries = _directory_entries(
                    owner.descriptor, PurePosixPath(item.path).parent.as_posix()
                )
            except ProjectRepairError as error:
                conflicts.extend(error.conflicts)
                continue
            except OSError:
                continue
            parent = PurePosixPath(item.path).parent
            for entry in sorted(entries):
                if not re.fullmatch(
                    rf"\.{re.escape(name)}\.autoform-repair-[0-9a-f]{{16}}",
                    entry,
                ):
                    continue
                orphan_path = (
                    entry if parent == PurePosixPath(".") else f"{parent}/{entry}"
                )
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "An unverified repair temporary file requires manual recovery.",
                        orphan_path,
                    )
                )
        finally:
            pending_error = sys.exc_info()[1]
            try:
                owner.close()
            except OSError:
                if pending_error is None:
                    conflicts.append(
                        ProjectRepairConflict(
                            "project-repair-close-failed",
                            "A managed parent descriptor could not be closed.",
                            item.path,
                        )
                    )
    return conflicts


def _plan(
    root_descriptor: int,
    desired: tuple[_PlannedFile, ...],
    provided_inputs: frozenset[str],
) -> tuple[
    tuple[_PlannedFile, ...],
    tuple[str, ...],
    tuple[_ObservedPath, ...],
    list[ProjectRepairConflict],
]:
    planned: list[_PlannedFile] = []
    preserved: list[str] = []
    observations: list[_ObservedPath] = []
    conflicts: list[ProjectRepairConflict] = []
    root_device = os.fstat(root_descriptor).st_dev
    for item in desired:
        owner = _OwnedDescriptor(os.dup(root_descriptor))
        try:
            walked: list[str] = []
            blocked = False
            for part in PurePosixPath(item.path).parts[:-1]:
                walked.append(part)
                path = "/".join(walked)
                try:
                    _open_existing_directory(
                        owner,
                        part,
                        path,
                        expected_device=root_device,
                    )
                except ProjectRepairError as error:
                    conflicts.extend(error.conflicts)
                    blocked = True
                    break
            if blocked:
                continue
            name = PurePosixPath(item.path).name
            try:
                orphaned = sorted(
                    entry
                    for entry in _directory_entries(
                        owner.descriptor,
                        PurePosixPath(item.path).parent.as_posix(),
                    )
                    if re.fullmatch(
                        rf"\.{re.escape(name)}\.autoform-repair-[0-9a-f]{{16}}",
                        entry,
                    )
                )
            except OSError:
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-invalid",
                        "A managed destination could not be inspected safely.",
                        item.path,
                    )
                )
                continue
            for orphan in orphaned:
                parent = PurePosixPath(item.path).parent
                orphan_path = (
                    orphan if parent == PurePosixPath(".") else f"{parent}/{orphan}"
                )
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "An unverified repair temporary file requires manual recovery.",
                        orphan_path,
                    )
                )
            try:
                metadata = os.stat(
                    name,
                    dir_fd=owner.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                missing_inputs = tuple(
                    value for value in item.required_inputs if value not in provided_inputs
                )
                if missing_inputs:
                    flags = ", ".join(f"--{value}" for value in missing_inputs)
                    conflicts.append(
                        ProjectRepairConflict(
                            "project-repair-input-required",
                            f"Repair requires explicit {flags} input to reconstruct this file.",
                            item.path,
                        )
                    )
                else:
                    planned.append(item)
                    observations.append(_ObservedPath(item.path, None, None))
                continue
            except OSError:
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-invalid",
                        "A managed destination could not be inspected safely.",
                        item.path,
                    )
                )
                continue
            if stat.S_ISLNK(metadata.st_mode):
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-symlink",
                        "A managed destination is a symbolic link.",
                        item.path,
                    )
                )
            elif not stat.S_ISREG(metadata.st_mode):
                conflicts.append(
                    ProjectRepairConflict(
                        "project-repair-destination-not-file",
                        "A managed destination exists and is not a regular file.",
                        item.path,
                    )
                )
            else:
                try:
                    observation = _observe_regular_file(
                        owner.descriptor, name, item.path
                    )
                except OSError as error:
                    code = (
                        "project-repair-destination-too-large"
                        if error.errno == errno.EFBIG
                        else "project-repair-race-conflict"
                    )
                    message = (
                        "A managed destination is too large to snapshot safely."
                        if code == "project-repair-destination-too-large"
                        else "A managed destination changed during repair."
                    )
                    conflicts.append(ProjectRepairConflict(code, message, item.path))
                else:
                    preserved.append(item.path)
                    observations.append(observation)
        finally:
            _close_owner_preserving(owner)
    return (
        tuple(planned),
        tuple(sorted(preserved)),
        tuple(sorted(observations, key=lambda item: item.path)),
        conflicts,
    )


def _require_private_directory(descriptor: int, path: str) -> None:
    mode = os.fstat(descriptor).st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-parent-unsafe",
                    "A managed parent directory is group- or world-writable.",
                    path,
                ),
            )
        )


def _validate_parent_chain(root_descriptor: int, path: str) -> None:
    root_device = os.fstat(root_descriptor).st_dev
    owner = _OwnedDescriptor(os.dup(root_descriptor))
    try:
        walked: list[str] = []
        for part in PurePosixPath(path).parts[:-1]:
            walked.append(part)
            _open_existing_directory(
                owner,
                part,
                "/".join(walked),
                expected_device=root_device,
            )
    finally:
        _close_owner_preserving(owner)


def _require_observed_paths(
    root_descriptor: int, observations: tuple[_ObservedPath, ...]
) -> None:
    root_device = os.fstat(root_descriptor).st_dev
    for observation in observations:
        if observation.signature is None:
            if _managed_path_state(root_descriptor, observation.path) == "absent":
                continue
            raise _race_conflict(
                observation.path,
                "A workflow path changed during repair.",
            )
        owner = _OwnedDescriptor(os.dup(root_descriptor))
        try:
            walked: list[str] = []
            for part in PurePosixPath(observation.path).parts[:-1]:
                walked.append(part)
                _open_existing_directory(
                    owner,
                    part,
                    "/".join(walked),
                    expected_device=root_device,
                )
            name = PurePosixPath(observation.path).name
            try:
                current = _observe_regular_file(
                    owner.descriptor, name, observation.path
                )
            except OSError:
                raise _race_conflict(
                    observation.path,
                    "A preserved managed file changed during repair.",
                ) from None
            if (
                current.signature != observation.signature
                or current.sha256 != observation.sha256
            ):
                raise _race_conflict(
                    observation.path,
                    "A preserved managed file changed during repair.",
                )
        finally:
            _close_owner_preserving(owner)


def _publish(
    root: Path,
    root_descriptor: int,
    root_identity: tuple[int, int],
    item: _PlannedFile,
    inspection,
    observations: tuple[_ObservedPath, ...],
) -> _ObservedPath:
    root_device = os.fstat(root_descriptor).st_dev
    owner = _OwnedDescriptor(os.dup(root_descriptor))
    outcome: _ObservedPath | None = None
    try:
        parts = PurePosixPath(item.path).parts
        walked: list[str] = []
        parent_chain: list[_ParentIdentity] = []
        for part in parts[:-1]:
            walked.append(part)
            _open_existing_directory(
                owner,
                part,
                "/".join(walked),
                expected_device=root_device,
            )
            parent_chain.append(
                _ParentIdentity(
                    name=part,
                    path="/".join(walked),
                    identity=_descriptor_identity(owner.descriptor),
                )
            )
        outcome = _publish_file(
            root_descriptor,
            root,
            root_identity,
            owner.descriptor,
            tuple(parent_chain),
            parts[-1],
            item,
            inspection,
            observations,
        )
        return outcome
    finally:
        pending_error = sys.exc_info()[1]
        try:
            owner.close()
        except OSError:
            conflict = ProjectRepairConflict(
                "project-repair-close-failed",
                "A managed parent descriptor could not be closed.",
                item.path,
            )
            if isinstance(pending_error, ProjectRepairError):
                raise ProjectRepairError(
                    (*pending_error.conflicts, conflict),
                    code=pending_error.code,
                    written=pending_error.written,
                ) from None
            raise ProjectRepairError(
                (conflict,),
                code="project-repair-failed",
                written=(item.path,) if outcome is not None else (),
            ) from None


def _require_parent_chain(
    root_descriptor: int, expected: tuple[_ParentIdentity, ...]
) -> None:
    root_device = os.fstat(root_descriptor).st_dev
    owner = _OwnedDescriptor(os.dup(root_descriptor))
    try:
        for link in expected:
            _open_existing_directory(
                owner,
                link.name,
                link.path,
                expected_device=root_device,
            )
            if _descriptor_identity(owner.descriptor) != link.identity:
                raise _race_conflict(
                    link.path, "A managed parent directory changed during repair."
                )
    finally:
        _close_owner_preserving(owner)


def _open_existing_directory(
    owner: _OwnedDescriptor,
    name: str,
    path: str,
    *,
    expected_device: int,
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        child = _OwnedDescriptor(
            os.open(name, flags, dir_fd=owner.descriptor)
        )
    except FileNotFoundError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-parent-missing",
                    "A required managed parent directory is missing.",
                    path,
                ),
            )
        ) from None
    except OSError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-parent-not-directory",
                    "A required parent path is not a safe directory.",
                    path,
                ),
            )
        ) from None
    try:
        _require_private_directory(child.descriptor, path)
        if os.fstat(child.descriptor).st_dev != expected_device:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-parent-filesystem",
                        "A managed parent directory is on a different filesystem.",
                        path,
                    ),
                )
            )
    except BaseException:
        _close_owner_preserving(child)
        raise
    try:
        owner.replace(child)
    except OSError:
        raise ProjectRepairError(
            (
                ProjectRepairConflict(
                    "project-repair-close-failed",
                    "A managed parent descriptor could not be closed during traversal.",
                    path,
                ),
            ),
            code="project-repair-failed",
        ) from None


def _require_temporary_identity(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != expected
        or (named.st_dev, named.st_ino) != expected
    ):
        raise OSError(errno.ESTALE, "temporary file changed during repair")


def _require_file_manifest(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
    item: _PlannedFile,
) -> None:
    _require_temporary_identity(
        parent_descriptor, name, descriptor, expected_identity
    )
    metadata = os.fstat(descriptor)
    if metadata.st_size != len(item.content) or stat.S_IMODE(metadata.st_mode) != item.mode:
        raise OSError(errno.ESTALE, "repair file metadata changed")
    offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = len(item.content) + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.lseek(descriptor, offset, os.SEEK_SET)
    if b"".join(chunks) != item.content:
        raise OSError(errno.ESTALE, "repair file content changed")


def _publish_file(
    root_descriptor: int,
    root: Path,
    root_identity: tuple[int, int],
    parent_descriptor: int,
    parent_chain: tuple[_ParentIdentity, ...],
    name: str,
    item: _PlannedFile,
    inspection,
    observations: tuple[_ObservedPath, ...],
) -> _ObservedPath:
    temporary = f".{name}.autoform-repair-{secrets.token_hex(8)}"
    parent = PurePosixPath(item.path).parent
    temporary_path = (
        temporary if parent == PurePosixPath(".") else f"{parent}/{temporary}"
    )
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    temporary_created = False
    publication_started = False
    published = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
        temporary_created = True
        temporary_metadata = os.fstat(descriptor)
        temporary_identity = temporary_metadata.st_dev, temporary_metadata.st_ino
        view = memoryview(item.content)
        while view:
            count = os.write(descriptor, view)
            if count == 0:
                raise OSError(errno.EIO, "short write")
            view = view[count:]
        os.fchmod(descriptor, item.mode)
        os.fsync(descriptor)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_config_identity(root_descriptor, inspection)
        _require_parent_chain(root_descriptor, parent_chain)
        _require_observed_paths(root_descriptor, observations)
        _require_root_identity(root_descriptor, root_identity, root)
        _require_file_manifest(
            parent_descriptor, temporary, descriptor, temporary_identity, item
        )
        publication_started = True
        try:
            _rename_noreplace(parent_descriptor, temporary, parent_descriptor, name)
        except FileExistsError:
            winner_descriptor, winner_identity = _concurrent_result(
                parent_descriptor, name, item
            )
            try:
                _require_root_identity(root_descriptor, root_identity, root)
                _require_config_identity(root_descriptor, inspection)
                _require_parent_chain(root_descriptor, parent_chain)
                _require_observed_paths(root_descriptor, observations)
                _require_root_identity(root_descriptor, root_identity, root)
                _require_file_manifest(
                    parent_descriptor,
                    name,
                    winner_descriptor,
                    winner_identity,
                    item,
                )
            finally:
                pending_winner_error = sys.exc_info()[1]
                try:
                    os.close(winner_descriptor)
                except OSError:
                    conflict = ProjectRepairConflict(
                        "project-repair-close-failed",
                        "A concurrent destination descriptor could not be closed.",
                        item.path,
                    )
                    if isinstance(pending_winner_error, ProjectRepairError):
                        raise ProjectRepairError(
                            (*pending_winner_error.conflicts, conflict),
                            code=pending_winner_error.code,
                            written=pending_winner_error.written,
                        ) from None
                    if isinstance(pending_winner_error, OSError):
                        validation_error = _race_conflict(
                            item.path,
                            "A concurrent destination changed during repair.",
                        )
                        raise ProjectRepairError(
                            (*validation_error.conflicts, conflict),
                            code=validation_error.code,
                        ) from None
                    raise ProjectRepairError(
                        (conflict,),
                        code="project-repair-failed",
                    ) from None
            raise _temporary_recovery_error(temporary_path)
        except ProjectCreateError:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-safety-unavailable",
                        "Atomic no-replace publication is unavailable.",
                        item.path,
                    ),
                ),
                code="project-repair-safety-unavailable",
            ) from None
        published = True
        try:
            _require_file_manifest(
                parent_descriptor, name, descriptor, temporary_identity, item
            )
            _require_root_identity(root_descriptor, root_identity, root)
            _require_config_identity(root_descriptor, inspection)
            _require_parent_chain(root_descriptor, parent_chain)
            _require_observed_paths(root_descriptor, observations)
        except ProjectRepairError as error:
            raise ProjectRepairError(
                (
                    *error.conflicts,
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after it or its parent changed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        except OSError:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after it or its parent changed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        try:
            os.fsync(parent_descriptor)
        except OSError:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-durability-failed",
                        "A managed file was published but its directory could not be synchronized.",
                        item.path,
                    ),
                ),
                code="project-repair-failed",
                written=(item.path,),
            ) from None
        try:
            _require_root_identity(root_descriptor, root_identity, root)
            _require_config_identity(root_descriptor, inspection)
            _require_parent_chain(root_descriptor, parent_chain)
            _require_observed_paths(root_descriptor, observations)
            _require_root_identity(root_descriptor, root_identity, root)
            _require_file_manifest(
                parent_descriptor, name, descriptor, temporary_identity, item
            )
        except ProjectRepairError as error:
            raise ProjectRepairError(
                (
                    *error.conflicts,
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after the project changed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        except OSError:
            raise ProjectRepairError(
                (
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after the project changed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        return _ObservedPath(
            item.path,
            _stat_signature(os.fstat(descriptor)),
            hashlib.sha256(item.content).hexdigest(),
        )
    except ProjectRepairError as error:
        if temporary_created and not published:
            if error.code == "project-repair-recovery-required":
                raise
            raise _temporary_recovery_error(
                temporary_path,
                conflicts=error.conflicts,
                written=error.written,
            ) from None
        raise
    except OSError:
        conflicts = (
            ProjectRepairConflict(
                "project-repair-write-failed",
                "A managed file could not be published safely.",
                item.path,
            ),
        )
        if temporary_created and not published:
            raise _temporary_recovery_error(
                temporary_path,
                conflicts=conflicts,
            ) from None
        if published:
            raise ProjectRepairError(
                (
                    *conflicts,
                    ProjectRepairConflict(
                        "project-repair-recovery-required",
                        "A published file was retained after final verification failed; inspect it before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-recovery-required",
                written=(item.path,),
            ) from None
        raise ProjectRepairError(conflicts, code="project-repair-failed") from None
    finally:
        pending_error = sys.exc_info()[1]
        commit_uncertain = False
        if (
            descriptor is not None
            and temporary_identity is not None
            and publication_started
            and not published
        ):
            temporary_binding = _binding_state(
                parent_descriptor, temporary, temporary_identity
            )
            destination_binding = _binding_state(
                parent_descriptor, name, temporary_identity
            )
            commit_uncertain = not (
                temporary_binding == "expected"
                and destination_binding != "expected"
            )
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                conflict = ProjectRepairConflict(
                    "project-repair-close-failed",
                    "A staged file descriptor could not be closed.",
                    item.path,
                )
                if commit_uncertain:
                    earlier = (
                        _without_stale_temporary_conflict(
                            pending_error.conflicts, temporary_path
                        )
                        if isinstance(pending_error, ProjectRepairError)
                        else ()
                    )
                    raise ProjectRepairError(
                        (
                            *earlier,
                            conflict,
                            ProjectRepairConflict(
                                "project-repair-commit-uncertain",
                                f"File publication may have occurred; inspect the destination and {temporary_path} before retrying.",
                                item.path,
                            ),
                        ),
                        code="project-repair-commit-uncertain",
                        written=(item.path,),
                    ) from None
                if isinstance(pending_error, ProjectRepairError):
                    raise ProjectRepairError(
                        (*pending_error.conflicts, conflict),
                        code=pending_error.code,
                        written=pending_error.written,
                    ) from None
                if temporary_created and not published:
                    raise _temporary_recovery_error(
                        temporary_path,
                        conflicts=(conflict,),
                    ) from None
                raise ProjectRepairError(
                    (conflict,),
                    code="project-repair-failed",
                    written=(item.path,) if published else (),
                ) from None
        if commit_uncertain:
            conflicts = (
                _without_stale_temporary_conflict(
                    pending_error.conflicts, temporary_path
                )
                if isinstance(pending_error, ProjectRepairError)
                else ()
            )
            raise ProjectRepairError(
                (
                    *conflicts,
                    ProjectRepairConflict(
                        "project-repair-commit-uncertain",
                        f"File publication may have occurred; inspect the destination and {temporary_path} before retrying.",
                        item.path,
                    ),
                ),
                code="project-repair-commit-uncertain",
                written=(item.path,),
            ) from None


def _binding_state(
    parent_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
) -> str:
    try:
        metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    if (
        stat.S_ISREG(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == expected_identity
    ):
        return "expected"
    return "other"


def _without_stale_temporary_conflict(
    conflicts: tuple[ProjectRepairConflict, ...], temporary_path: str
) -> tuple[ProjectRepairConflict, ...]:
    return tuple(
        conflict
        for conflict in conflicts
        if not (
            conflict.code == "project-repair-recovery-required"
            and conflict.path == temporary_path
        )
    )


def _temporary_recovery_error(
    path: str,
    *,
    conflicts: tuple[ProjectRepairConflict, ...] = (),
    written: tuple[str, ...] = (),
) -> ProjectRepairError:
    recovery = ProjectRepairConflict(
        "project-repair-recovery-required",
        "A repair temporary was retained after publication did not complete; inspect it before retrying.",
        path,
    )
    return ProjectRepairError(
        (*conflicts, recovery),
        code="project-repair-recovery-required",
        written=written,
    )


def _concurrent_result(
    parent_descriptor: int, name: str, item: _PlannedFile
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "managed destination is not a regular file")
        content = os.read(descriptor, len(item.content) + 1)
    except OSError:
        error = _race_conflict(item.path, "A managed destination changed during repair.")
        if descriptor is not None:
            error = _close_concurrent_descriptor(descriptor, item, error)
        raise error from None
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    if content == item.content:
        assert descriptor is not None
        return descriptor, (metadata.st_dev, metadata.st_ino)
    assert descriptor is not None
    error = _race_conflict(item.path, "A different managed file appeared during repair.")
    raise _close_concurrent_descriptor(descriptor, item, error)


def _close_concurrent_descriptor(
    descriptor: int,
    item: _PlannedFile,
    error: ProjectRepairError,
) -> ProjectRepairError:
    try:
        os.close(descriptor)
    except OSError:
        conflict = ProjectRepairConflict(
            "project-repair-close-failed",
            "A concurrent destination descriptor could not be closed.",
            item.path,
        )
        return ProjectRepairError(
            (*error.conflicts, conflict),
            code=error.code,
            written=error.written,
        )
    return error


def _race_conflict(path: str, message: str) -> ProjectRepairError:
    return ProjectRepairError(
        (ProjectRepairConflict("project-repair-race-conflict", message, path),),
        code="project-repair-race-conflict",
    )


__all__ = [
    "PROJECT_REPAIR_SCHEMA",
    "ProjectRepairConflict",
    "ProjectRepairError",
    "ProjectRepairResult",
    "repair_project",
]
