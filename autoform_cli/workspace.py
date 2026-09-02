"""Resolve and inspect manifest-managed Autoform blueprint workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .workspace_manifest import (
    MAX_MANIFEST_BYTES,
    WORKSPACE_FILE,
    WORKSPACE_INSPECTION_SCHEMA,
    WorkspaceError,
    WorkspaceManifest,
    WorkspaceProject,
    parse_workspace,
    portable_name_key,
)


_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(slots=True)
class _WorkspaceRootBinding:
    """A lexical absolute directory chain retained for a workspace lifetime."""

    path: Path
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int], ...]
    _closed: bool = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise OSError("workspace root binding is closed")
        return self.descriptors[-1]

    @property
    def identity(self) -> tuple[int, int]:
        return self.identities[-1]

    def verify(self) -> None:
        """Verify every retained component name still names its bound inode."""

        if self._closed or len(self.descriptors) != len(self.identities):
            raise OSError("workspace root binding is incomplete")
        anchor = self.path.anchor
        if not anchor:
            raise OSError("workspace root is not absolute")
        opened = os.fstat(self.descriptors[0])
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != self.identities[0]
            or (named.st_dev, named.st_ino) != self.identities[0]
        ):
            raise OSError("workspace root anchor changed")
        for index, part in enumerate(self.path.parts[1:], start=1):
            opened = os.fstat(self.descriptors[index])
            named = os.stat(
                part,
                dir_fd=self.descriptors[index - 1],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (opened.st_dev, opened.st_ino) != self.identities[index]
                or (named.st_dev, named.st_ino) != self.identities[index]
            ):
                raise OSError("workspace root component changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()


def _open_workspace_root(path: Path) -> _WorkspaceRootBinding:
    """Bind every component of a lexical absolute root without following links."""

    absolute = Path(os.path.abspath(path))
    descriptors: list[int] = []
    identities: list[tuple[int, int]] = []
    try:
        anchor = absolute.anchor
        if not anchor:
            raise OSError("workspace root is not absolute")
        descriptor = os.open(anchor, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        named = os.stat(anchor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("workspace root anchor changed")
        identities.append((opened.st_dev, opened.st_ino))
        for part in absolute.parts[1:]:
            expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError("workspace root component is not a directory")
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            opened = os.fstat(child)
            named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or identity != (expected.st_dev, expected.st_ino)
                or identity != (named.st_dev, named.st_ino)
            ):
                raise OSError("workspace root component changed")
            identities.append(identity)
            descriptor = child
        binding = _WorkspaceRootBinding(absolute, tuple(descriptors), tuple(identities))
        binding.verify()
        return binding
    except OSError:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise WorkspaceError(
            ["workspace root path must not contain a symbolic link or change while opening"]
        ) from None


def _workspace_read_checkpoint(_event: str, _binding: _WorkspaceRootBinding) -> None:
    """Deterministic root-substitution boundary used by adversarial tests."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """A validated manifest anchored to its repository root."""

    root: Path
    manifest: WorkspaceManifest
    manifest_sha256: str
    _root_binding: _WorkspaceRootBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._root_binding is not None:
            return
        normalized = Path(os.path.abspath(self.root))
        object.__setattr__(self, "root", normalized)
        object.__setattr__(self, "_root_binding", _open_workspace_root(normalized))

    @property
    def root_descriptor(self) -> int:
        assert self._root_binding is not None
        return self._root_binding.descriptor

    @property
    def root_identity(self) -> tuple[int, int]:
        assert self._root_binding is not None
        return self._root_binding.identity

    def verify_root_binding(self) -> None:
        assert self._root_binding is not None
        try:
            self._root_binding.verify()
        except OSError:
            raise WorkspaceError(["workspace root changed during use"]) from None

    def close(self) -> None:
        """Release retained root descriptors when the workspace is no longer used."""

        assert self._root_binding is not None
        self._root_binding.close()

    def duplicate_root_descriptor(self) -> int:
        """Return a checked duplicate suitable for one bounded mutation."""

        self.verify_root_binding()
        descriptor: int | None = None
        try:
            descriptor = os.dup(self.root_descriptor)
            opened = os.fstat(descriptor)
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise WorkspaceError(["workspace root changed during use"]) from None
        if (opened.st_dev, opened.st_ino) != self.root_identity:
            os.close(descriptor)
            raise WorkspaceError(["workspace root changed during use"])
        try:
            self.verify_root_binding()
        except WorkspaceError:
            os.close(descriptor)
            raise
        return descriptor

    @property
    def path(self) -> Path:
        return self.root / WORKSPACE_FILE

    def blueprint_path(self, project: WorkspaceProject) -> Path:
        return self.root / self.manifest.blueprint_relative(project)

    def project_binding_sha256(self, project: WorkspaceProject) -> str:
        """Digest only the selected project entry and its referenced location."""

        location = self.manifest.location(project.blueprint_location)
        payload = {
            "blueprint_path": self.manifest.blueprint_relative(project).as_posix(),
            "location": location.as_dict(),
            "project": project.as_dict(),
            "schema": "autoform-workspace-project-binding/v1",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceDiagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceInspection:
    workspace: Workspace
    diagnostics: tuple[WorkspaceDiagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def as_dict(self) -> dict[str, object]:
        return {
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "locations": [item.as_dict() for item in self.workspace.manifest.locations],
            "manifest": WORKSPACE_FILE,
            "ok": self.ok,
            "projects": [
                {
                    **item.as_dict(),
                    "resolved_blueprint_path": self.workspace.blueprint_path(item)
                    .relative_to(self.workspace.root)
                    .as_posix(),
                }
                for item in self.workspace.manifest.projects
            ],
            "root": ".",
            "schema": WORKSPACE_INSPECTION_SCHEMA,
            "workspace_schema": self.workspace.manifest.schema,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def discover_workspace(start: str | Path = ".") -> Workspace:
    """Find and load the nearest enclosing ``.autoform.toml``."""

    try:
        candidate = Path(start).expanduser().absolute()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace search path cannot be resolved"]) from None
    if _path_contains_symlink(candidate):
        raise WorkspaceError(["workspace search path contains a symbolic link"])
    if candidate.is_file():
        candidate = candidate.parent
    for root in (candidate, *candidate.parents):
        _reject_case_collisions(root, PurePosixPath(WORKSPACE_FILE))
        manifest_path = root / WORKSPACE_FILE
        if manifest_path.is_symlink():
            raise WorkspaceError([f"{WORKSPACE_FILE} must not be a symbolic link"])
        if manifest_path.exists():
            if not manifest_path.is_file():
                raise WorkspaceError([f"{WORKSPACE_FILE} must be a regular file"])
            return load_workspace(root)
    raise WorkspaceError([f"no enclosing {WORKSPACE_FILE} was found"])


def load_workspace(root: str | Path) -> Workspace:
    """Load a workspace rooted at *root* without searching its parents."""

    try:
        requested = Path(os.path.abspath(Path(root).expanduser()))
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace root path cannot be resolved"]) from None
    binding = _open_workspace_root(requested)
    try:
        binding.verify()
        _reject_case_collisions(requested, PurePosixPath(WORKSPACE_FILE))
        binding.verify()
        manifest_path = requested / WORKSPACE_FILE
        content = _read_workspace_manifest(binding, manifest_path)
        if len(content) > MAX_MANIFEST_BYTES:
            raise WorkspaceError(
                [f"{WORKSPACE_FILE} exceeds the {MAX_MANIFEST_BYTES}-byte limit"]
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError([f"{WORKSPACE_FILE} is not valid UTF-8 TOML"]) from None
        manifest = parse_workspace(text)
        workspace = Workspace(
            requested,
            manifest,
            hashlib.sha256(content).hexdigest(),
            binding,
        )
        workspace.verify_root_binding()
        _validate_workspace_paths(workspace)
        workspace.verify_root_binding()
        return workspace
    except BaseException:
        binding.close()
        raise


def _read_workspace_manifest(
    root_binding: _WorkspaceRootBinding,
    manifest_path: Path,
) -> bytes:
    """Read one exact regular manifest generation through a bound root dirfd."""

    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        root_binding.verify()
        _workspace_read_checkpoint("before-manifest-open", root_binding)
        root_binding.verify()
        root_descriptor = root_binding.descriptor
        descriptor = os.open(WORKSPACE_FILE, file_flags, dir_fd=root_descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("workspace manifest is not regular")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        named = os.stat(WORKSPACE_FILE, dir_fd=root_descriptor, follow_symlinks=False)
        if _file_signature(before) != _file_signature(after) or _file_signature(
            after
        ) != _file_signature(named):
            raise OSError("workspace manifest changed")
        _workspace_read_checkpoint("after-manifest-read", root_binding)
        root_binding.verify()
        return b"".join(chunks)
    except OSError:
        raise WorkspaceError([f"cannot read {manifest_path.name} safely"]) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def inspect_workspace(start: str | Path = ".") -> WorkspaceInspection:
    """Inspect registered paths without treating unregistered directories as managed."""

    workspace = discover_workspace(start)
    diagnostics: list[WorkspaceDiagnostic] = []
    for location in workspace.manifest.locations:
        path = workspace.root / PurePosixPath(location.path)
        relative = path.relative_to(workspace.root).as_posix()
        if not path.exists():
            diagnostics.append(
                WorkspaceDiagnostic(
                    "warning", "location-missing", "Declared location does not exist.", relative
                )
            )
        elif not path.is_dir():
            diagnostics.append(
                WorkspaceDiagnostic(
                    "error",
                    "location-not-directory",
                    "Declared location is not a directory.",
                    relative,
                )
            )
    for project in workspace.manifest.projects:
        path = workspace.blueprint_path(project)
        relative = path.relative_to(workspace.root).as_posix()
        if not path.exists():
            diagnostics.append(
                WorkspaceDiagnostic(
                    "error", "blueprint-missing", "Registered blueprint does not exist.", relative
                )
            )
        elif not path.is_dir():
            diagnostics.append(
                WorkspaceDiagnostic(
                    "error",
                    "blueprint-not-directory",
                    "Registered blueprint is not a directory.",
                    relative,
                )
            )
        elif not (path / "roadmap").is_dir():
            diagnostics.append(
                WorkspaceDiagnostic(
                    "error",
                    "roadmap-missing",
                    "Registered blueprint has no roadmap directory.",
                    relative,
                )
            )
    return WorkspaceInspection(workspace, tuple(diagnostics))


def resolve_blueprint(
    start: str | Path = ".",
    *,
    project_id: str | None = None,
) -> tuple[Workspace, WorkspaceProject, Path]:
    """Resolve one registered blueprint from a path and optional project id."""

    workspace = discover_workspace(start)
    if project_id is not None:
        project = workspace.manifest.project(project_id)
        path = workspace.blueprint_path(project)
        _require_blueprint(path)
        return workspace, project, path

    try:
        candidate = Path(start).expanduser().absolute()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace project path cannot be resolved"]) from None
    if candidate.is_file():
        candidate = candidate.parent
    matches = [
        project
        for project in workspace.manifest.projects
        if _is_within(candidate, workspace.blueprint_path(project))
    ]
    if len(matches) == 1:
        project = matches[0]
    elif (
        not matches
        and len(workspace.manifest.projects) == 1
        and _same_path(candidate, workspace.root)
    ):
        project = workspace.manifest.projects[0]
    else:
        choices = ", ".join(project.id for project in workspace.manifest.projects) or "none"
        raise WorkspaceError([f"cannot choose an Autoform project; pass --project from: {choices}"])
    path = workspace.blueprint_path(project)
    _require_blueprint(path)
    return workspace, project, path


def _validate_workspace_paths(workspace: Workspace) -> None:
    issues: list[str] = []
    try:
        _reject_case_collisions(workspace.root, PurePosixPath(WORKSPACE_FILE))
    except WorkspaceError as error:
        issues.extend(error.issues)
    for location in workspace.manifest.locations:
        path = workspace.root / PurePosixPath(location.path)
        try:
            _reject_case_collisions(workspace.root, PurePosixPath(location.path))
            _reject_existing_symlink_chain(path, workspace.root)
        except WorkspaceError as error:
            issues.extend(f"locations.{location.id}: {item}" for item in error.issues)
    for project in workspace.manifest.projects:
        path = workspace.blueprint_path(project)
        try:
            relative = workspace.manifest.blueprint_relative(project)
            _reject_case_collisions(workspace.root, relative)
            _reject_existing_symlink_chain(path, workspace.root)
        except WorkspaceError as error:
            issues.extend(f"projects.{project.id}: {item}" for item in error.issues)
    if issues:
        raise WorkspaceError(issues)


def _reject_existing_symlink_chain(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise WorkspaceError(["managed path escapes the workspace root"]) from None
    try:
        probe = root
        for part in relative.parts:
            probe /= part
            if probe.is_symlink():
                raise WorkspaceError(
                    [f"managed path contains a symbolic link: {relative.as_posix()}"]
                )
            if not probe.exists():
                break
    except OSError:
        raise WorkspaceError([f"managed path cannot be inspected: {relative.as_posix()}"]) from None


def _reject_case_collisions(root: Path, relative: PurePosixPath) -> None:
    parent = root
    for part in relative.parts:
        if not parent.is_dir():
            return
        for sibling in _directory_entries(parent, "managed path parent"):
            if portable_name_key(sibling.name) == portable_name_key(part) and sibling.name != part:
                raise WorkspaceError(
                    [f"managed path is not portable beside existing path: {sibling.name}"]
                )
        parent /= part


def _path_contains_symlink(path: Path) -> bool:
    try:
        probe = Path(path.anchor)
        for part in path.parts[1:]:
            probe /= part
            if probe.is_symlink():
                return True
            if not probe.exists():
                return False
        return False
    except OSError:
        raise WorkspaceError(["workspace path cannot be inspected safely"]) from None


def _require_blueprint(path: Path) -> None:
    if not path.is_dir():
        raise WorkspaceError(["registered blueprint directory does not exist"])
    if not (path / "roadmap").is_dir():
        raise WorkspaceError(["registered blueprint has no roadmap directory"])


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return False


def _directory_entries(path: Path, label: str) -> tuple[Path, ...]:
    try:
        return tuple(path.iterdir())
    except OSError:
        raise WorkspaceError([f"cannot inspect {label}: {path.name or '.'}"]) from None


__all__ = [
    "Workspace",
    "WorkspaceDiagnostic",
    "WorkspaceInspection",
    "discover_workspace",
    "inspect_workspace",
    "load_workspace",
    "resolve_blueprint",
]
