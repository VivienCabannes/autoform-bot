"""Resolve and inspect manifest-managed Autoform blueprint workspaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class Workspace:
    """A validated manifest anchored to its repository root."""

    root: Path
    manifest: WorkspaceManifest

    @property
    def path(self) -> Path:
        return self.root / WORKSPACE_FILE

    def blueprint_path(self, project: WorkspaceProject) -> Path:
        return self.root / self.manifest.blueprint_relative(project)


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
        requested = Path(root).expanduser()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace root path cannot be resolved"]) from None
    if _path_contains_symlink(requested.absolute()):
        raise WorkspaceError(["workspace root path must not contain a symbolic link"])
    try:
        resolved = requested.resolve()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace root path cannot be resolved"]) from None
    manifest_path = resolved / WORKSPACE_FILE
    if manifest_path.is_symlink():
        raise WorkspaceError([f"{WORKSPACE_FILE} must not be a symbolic link"])
    if manifest_path.exists() and not manifest_path.is_file():
        raise WorkspaceError([f"{WORKSPACE_FILE} must be a regular file"])
    try:
        with manifest_path.open("rb") as stream:
            content = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise WorkspaceError([f"cannot read {WORKSPACE_FILE}"]) from None
    if len(content) > MAX_MANIFEST_BYTES:
        raise WorkspaceError([f"{WORKSPACE_FILE} exceeds the {MAX_MANIFEST_BYTES}-byte limit"])
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkspaceError([f"{WORKSPACE_FILE} is not valid UTF-8 TOML"]) from None
    manifest = parse_workspace(text)
    workspace = Workspace(resolved, manifest)
    _validate_workspace_paths(workspace)
    return workspace


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
