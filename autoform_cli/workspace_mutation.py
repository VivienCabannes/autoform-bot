"""Safe filesystem mutations for manifest-managed Autoform workspaces."""

from __future__ import annotations

import ctypes
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomlkit
from tomlkit.items import InlineTable, KeyType, SingleKey, Table

from .scaffold import ScaffoldError, scaffold_blueprint
from .project.create import ProjectCreateError, _rename_noreplace
from .workspace import (
    Workspace,
    _path_contains_symlink,
    _reject_case_collisions,
    _reject_existing_symlink_chain,
    _require_blueprint,
    discover_workspace,
)
from .workspace_manifest import (
    BLUEPRINT_CHANGE_SCHEMA,
    MAX_MANIFEST_BYTES,
    WORKSPACE_FILE,
    WORKSPACE_INIT_SCHEMA,
    WORKSPACE_SCHEMA,
    WorkspaceError,
    WorkspaceLocation,
    WorkspaceManifest,
    parse_workspace,
    path_keys_overlap,
    portable_name_key,
    portable_path_key,
    uses_reserved_repository_root,
    valid_blueprint_member,
    valid_identifier,
    valid_relative_path,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - unsupported mutation platform
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class _BlueprintBinding:
    workspace: Workspace
    location: WorkspaceLocation
    combined: str
    destination: Path


@dataclass(frozen=True, slots=True)
class _StagedFile:
    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class WorkspaceInitResult:
    root: Path
    manifest_path: str
    location_id: str
    blueprint_root: str

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_root": self.blueprint_root,
            "location_id": self.location_id,
            "manifest": self.manifest_path,
            "ok": True,
            "root": ".",
            "schema": WORKSPACE_INIT_SCHEMA,
        }


@dataclass(frozen=True, slots=True)
class BlueprintCreateResult:
    project_id: str
    blueprint_path: str
    written: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "blueprint_path": self.blueprint_path,
            "ok": True,
            "project": self.project_id,
            "schema": BLUEPRINT_CHANGE_SCHEMA,
            "workspace_root": ".",
            "written": list(self.written),
        }


def initialize_workspace(
    target: str | Path,
    *,
    blueprint_root: str,
    location_id: str = "blueprints",
) -> WorkspaceInitResult:
    """Create a root manifest and its blueprint collection without creating a vault."""

    _require_workspace_mutation_support()
    if not valid_identifier(location_id):
        raise WorkspaceError(["location id is not portable"])
    if not valid_relative_path(blueprint_root, allow_dot=False):
        raise WorkspaceError(["blueprint root must be a portable repository-relative path"])
    first_component = PurePosixPath(blueprint_root).parts[0]
    if uses_reserved_repository_root(blueprint_root):
        raise WorkspaceError(
            [f"blueprint root uses reserved repository path: {first_component}"]
        )
    try:
        root = Path(target).expanduser()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace target cannot be resolved"]) from None
    if _path_contains_symlink(root.absolute()) or not root.is_dir():
        raise WorkspaceError(["workspace target must be an existing non-symlink directory"])
    try:
        root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceError(["workspace target cannot be resolved"]) from None
    manifest_path = root / WORKSPACE_FILE
    _reject_case_collisions(root, PurePosixPath(WORKSPACE_FILE))
    if manifest_path.exists() or manifest_path.is_symlink():
        raise WorkspaceError([f"{WORKSPACE_FILE} already exists"])
    collection = root / PurePosixPath(blueprint_root)
    _reject_case_collisions(root, PurePosixPath(blueprint_root))
    _reject_existing_symlink_chain(collection, root)
    _preflight_directory_chain(root, PurePosixPath(blueprint_root))

    text = _initial_manifest(location_id=location_id, blueprint_root=blueprint_root)
    staged = _stage_new_file(manifest_path, text.encode("utf-8"))
    created_directories: tuple[Path, ...] = ()
    try:
        try:
            created_directories = _create_directory_chain(
                root,
                PurePosixPath(blueprint_root),
            )
        except WorkspaceError as error:
            detail = "; ".join(error.issues)
            raise WorkspaceError(
                [f"{detail}; retained complete staged manifest at {staged.path.name}"]
            ) from None
        try:
            _publish_staged_file(manifest_path, staged, mode=0o644)
        except WorkspaceError as error:
            retained = ", ".join(
                path.relative_to(root).as_posix() for path in created_directories
            )
            detail = "; ".join(error.issues)
            if retained:
                detail += f"; retained unregistered directories: {retained}"
            raise WorkspaceError([detail]) from None
    finally:
        try:
            os.close(staged.descriptor)
        except OSError:
            pass
    return WorkspaceInitResult(root, WORKSPACE_FILE, location_id, blueprint_root)


def create_blueprint_project(
    start: str | Path,
    *,
    project_id: str,
    title: str,
    path: str | None = None,
    location_id: str | None = None,
) -> BlueprintCreateResult:
    """Create one vault and register it in the root manifest."""

    if not title.strip():
        raise WorkspaceError(["project title must not be empty"])
    member = project_id if path is None else path
    binding = _prepare_blueprint_binding(
        start,
        project_id=project_id,
        member=member,
        location_id=location_id,
    )
    _reject_case_collisions(
        binding.workspace.root,
        PurePosixPath(binding.combined),
    )
    if binding.destination.exists() or binding.destination.is_symlink():
        raise WorkspaceError([f"blueprint destination already exists: {binding.combined}"])

    try:
        binding.destination.mkdir(mode=0o755)
    except FileExistsError:
        raise WorkspaceError(
            [f"blueprint destination already exists: {binding.combined}"]
        ) from None
    except OSError:
        raise WorkspaceError(
            [f"blueprint destination could not be created: {binding.combined}"]
        ) from None
    try:
        written = scaffold_blueprint(binding.destination, title=title)
        _append_project(
            binding.workspace.path,
            project_id=project_id,
            title=title.strip(),
            location_id=binding.location.id,
            path=member,
            expected_blueprint_path=binding.combined,
        )
    except (OSError, ScaffoldError, WorkspaceError) as error:
        detail = (
            "; ".join(error.issues)
            if isinstance(error, (ScaffoldError, WorkspaceError))
            else "filesystem operation failed"
        )
        raise WorkspaceError(
            [
                f"blueprint creation stopped after reserving {binding.combined!r}: {detail}; "
                "inspect the unregistered directory before retrying"
            ]
        ) from None

    relative_written = tuple(
        sorted(
            str(PurePosixPath(binding.combined) / PurePosixPath(item))
            for item in written
        )
    )
    return BlueprintCreateResult(
        project_id,
        binding.combined,
        relative_written,
    )


def register_blueprint_project(
    start: str | Path,
    *,
    project_id: str,
    title: str | None,
    path: str,
    location_id: str | None = None,
) -> BlueprintCreateResult:
    """Register an existing vault without changing any file inside it."""

    if title is not None and not title.strip():
        raise WorkspaceError(["project title must not be empty"])
    binding = _prepare_blueprint_binding(
        start,
        project_id=project_id,
        member=path,
        location_id=location_id,
    )
    _reject_case_collisions(
        binding.workspace.root,
        PurePosixPath(binding.combined),
    )
    _reject_existing_symlink_chain(binding.destination, binding.workspace.root)
    if not binding.destination.exists():
        raise WorkspaceError([f"blueprint directory does not exist: {binding.combined}"])
    _require_blueprint(binding.destination)
    _append_project(
        binding.workspace.path,
        project_id=project_id,
        title=title.strip() if title is not None else project_id,
        location_id=binding.location.id,
        path=path,
        expected_blueprint_path=binding.combined,
    )
    return BlueprintCreateResult(project_id, binding.combined, ())


def _prepare_blueprint_binding(
    start: str | Path,
    *,
    project_id: str,
    member: str,
    location_id: str | None,
) -> _BlueprintBinding:
    _require_workspace_mutation_support()
    if not valid_identifier(project_id):
        raise WorkspaceError(["project id is not portable"])
    if not valid_blueprint_member(member):
        raise WorkspaceError(["blueprint path must name one portable immediate child directory"])

    workspace = discover_workspace(start)
    candidates = tuple(
        location for location in workspace.manifest.locations if "blueprints" in location.provides
    )
    if location_id is None:
        if len(candidates) != 1:
            choices = ", ".join(item.id for item in candidates) or "none"
            raise WorkspaceError([f"choose a blueprint location with --location from: {choices}"])
        selected_location_id = candidates[0].id
    else:
        selected_location_id = location_id
    location, combined = _validate_project_registration(
        workspace.manifest,
        project_id=project_id,
        location_id=selected_location_id,
        path=member,
    )

    collection = workspace.root / PurePosixPath(location.path)
    _reject_existing_symlink_chain(collection, workspace.root)
    if not collection.is_dir():
        raise WorkspaceError([f"blueprint location does not exist: {location.path}"])
    destination = collection / member
    return _BlueprintBinding(workspace, location, combined, destination)


def _validate_project_registration(
    manifest: WorkspaceManifest,
    *,
    project_id: str,
    location_id: str,
    path: str,
    expected_blueprint_path: str | None = None,
) -> tuple[WorkspaceLocation, str]:
    """Validate one new registry entry against the current manifest."""

    if any(
        portable_name_key(item.id) == portable_name_key(project_id)
        for item in manifest.projects
    ):
        raise WorkspaceError([f"Autoform project {project_id!r} is already registered"])
    location = manifest.location(location_id)
    if "blueprints" not in location.provides:
        raise WorkspaceError([f"workspace location {location_id!r} does not provide blueprints"])

    combined = PurePosixPath(location.path, path).as_posix()
    if uses_reserved_repository_root(combined):
        raise WorkspaceError([f"blueprint path uses reserved repository path: {combined}"])
    if expected_blueprint_path is not None and combined != expected_blueprint_path:
        raise WorkspaceError(["blueprint location changed during registration"])
    combined_key = portable_path_key(combined)
    for project in manifest.projects:
        existing = manifest.blueprint_relative(project).as_posix()
        existing_key = portable_path_key(existing)
        if not path_keys_overlap(combined_key, existing_key):
            continue
        if combined_key == existing_key:
            raise WorkspaceError([f"blueprint path {combined!r} is already registered"])
        raise WorkspaceError(
            [f"blueprint path {combined!r} overlaps registered project {project.id!r}"]
        )
    return location, combined


def _initial_manifest(*, location_id: str, blueprint_root: str) -> str:
    document = tomlkit.document()
    document.add("schema", WORKSPACE_SCHEMA)
    locations = tomlkit.table()
    location = tomlkit.table()
    location.add("path", PurePosixPath(blueprint_root).as_posix())
    location.add("provides", ["blueprints"])
    locations.add(SingleKey(location_id, KeyType.Basic), location)
    document.add("locations", locations)
    document.add("projects", tomlkit.table())
    return tomlkit.dumps(document)


def _preflight_directory_chain(root: Path, relative: PurePosixPath) -> None:
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise WorkspaceError(["blueprint root could not be inspected safely"]) from None
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceError(["blueprint root exists and is not a directory"])


def _manifest_with_project(
    text: str,
    *,
    project_id: str,
    title: str,
    location_id: str,
    path: str,
) -> str:
    """Add a project through a comment-preserving TOML syntax tree."""

    try:
        document = tomlkit.parse(text)
    except (ValueError, RecursionError, MemoryError):
        raise WorkspaceError([f"{WORKSPACE_FILE} is not valid TOML"]) from None
    projects = document.get("projects")
    if projects is None:
        projects = tomlkit.table()
        document.add("projects", projects)
    if not isinstance(projects, (Table, InlineTable)):
        raise WorkspaceError(["projects must be a TOML table"])

    project = tomlkit.inline_table() if isinstance(projects, InlineTable) else tomlkit.table()
    project.add("title", title)
    blueprint = tomlkit.inline_table()
    blueprint.add("location", location_id)
    blueprint.add("path", path)
    project.add("blueprint", blueprint)
    try:
        projects.add(SingleKey(project_id, KeyType.Basic), project)
        return tomlkit.dumps(document)
    except (KeyError, TypeError, ValueError):
        raise WorkspaceError([f"cannot add projects.{project_id} to {WORKSPACE_FILE}"]) from None


def _append_project(
    manifest_path: Path,
    *,
    project_id: str,
    title: str,
    location_id: str,
    path: str,
    expected_blueprint_path: str,
) -> None:
    descriptor = _open_locked_manifest(manifest_path)
    temporary: Path | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceError([f"{WORKSPACE_FILE} is not a regular file"])
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            original = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(original) > MAX_MANIFEST_BYTES:
            raise WorkspaceError([f"{WORKSPACE_FILE} exceeds the {MAX_MANIFEST_BYTES}-byte limit"])
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError([f"{WORKSPACE_FILE} is not valid UTF-8 TOML"]) from None
        current = parse_workspace(text)
        _validate_project_registration(
            current,
            project_id=project_id,
            location_id=location_id,
            path=path,
            expected_blueprint_path=expected_blueprint_path,
        )

        updated_text = _manifest_with_project(
            text,
            project_id=project_id,
            title=title,
            location_id=location_id,
            path=path,
        )
        parse_workspace(updated_text)
        updated = updated_text.encode("utf-8")
        if len(updated) > MAX_MANIFEST_BYTES:
            raise WorkspaceError(
                [f"updated {WORKSPACE_FILE} would exceed the {MAX_MANIFEST_BYTES}-byte limit"]
            )
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            dir=manifest_path.parent,
            prefix=f".{manifest_path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(temporary_descriptor, "wb") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(stat.S_IMODE(metadata.st_mode))
        current_metadata = os.fstat(descriptor)
        try:
            named_metadata = os.stat(manifest_path, follow_symlinks=False)
        except OSError:
            raise WorkspaceError([f"{WORKSPACE_FILE} changed during update"]) from None
        if _file_signature(current_metadata) != _file_signature(metadata) or (
            current_metadata.st_dev,
            current_metadata.st_ino,
        ) != (named_metadata.st_dev, named_metadata.st_ino):
            raise WorkspaceError([f"{WORKSPACE_FILE} changed during update"])
        os.replace(temporary, manifest_path)
        temporary = None
        _fsync_directory(manifest_path.parent)
    except WorkspaceError:
        raise
    except OSError:
        raise WorkspaceError([f"cannot update {WORKSPACE_FILE}"]) from None
    finally:
        os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _open_locked_manifest(manifest_path: Path) -> int:
    """Lock the inode currently named by the manifest, retrying across replacement."""

    _require_workspace_mutation_support()
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(8):
        try:
            descriptor = os.open(manifest_path, flags)
        except OSError:
            raise WorkspaceError([f"cannot update {WORKSPACE_FILE}"]) from None
        try:
            assert fcntl is not None
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            opened = os.fstat(descriptor)
            named = os.stat(manifest_path, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino):
                return descriptor
        except OSError:
            os.close(descriptor)
            raise WorkspaceError([f"cannot update {WORKSPACE_FILE}"]) from None
        os.close(descriptor)
    raise WorkspaceError([f"{WORKSPACE_FILE} changed repeatedly during update"])


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _require_workspace_mutation_support() -> None:
    required = (
        fcntl is not None,
        hasattr(os, "O_NOFOLLOW"),
        hasattr(os, "O_DIRECTORY"),
        _atomic_noreplace_available(),
        os.mkdir in os.supports_dir_fd,
        os.open in os.supports_dir_fd,
        os.stat in os.supports_dir_fd,
    )
    if not all(required):
        raise WorkspaceError(
            ["this platform cannot update a workspace with the required path safety"]
        )


def _atomic_noreplace_available() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return False
    return hasattr(libc, "renameatx_np") or hasattr(libc, "renameat2")


def _stage_new_file(path: Path, content: bytes) -> _StagedFile:
    temporary: Path | None = None
    descriptor: int | None = None
    complete = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(os.dup(descriptor), "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        named = temporary.stat(follow_symlinks=False)
        if (named.st_dev, named.st_ino) != identity or not stat.S_ISREG(named.st_mode):
            raise WorkspaceError(
                [f"staged manifest changed before publication; inspect {temporary.name}"]
            )
        complete = True
        return _StagedFile(temporary, descriptor, identity)
    except WorkspaceError:
        raise
    except Exception:
        if temporary is not None:
            raise WorkspaceError(
                [f"could not stage {path.name}; retained staged file at {temporary.name}"]
            ) from None
        raise WorkspaceError([f"could not stage {path.name}"]) from None
    finally:
        if descriptor is not None and not complete:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish_staged_file(path: Path, staged: _StagedFile, *, mode: int) -> None:
    published = False
    try:
        os.fchmod(staged.descriptor, mode)
        os.fsync(staged.descriptor)
        named = staged.path.stat(follow_symlinks=False)
        if (
            (named.st_dev, named.st_ino) != staged.identity
            or not stat.S_ISREG(named.st_mode)
        ):
            raise WorkspaceError(
                [f"staged manifest changed before publication; inspect {staged.path.name}"]
            )
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            _rename_noreplace(
                parent_descriptor,
                staged.path.name,
                parent_descriptor,
                path.name,
            )
            published = True
        finally:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
        try:
            named = path.stat(follow_symlinks=False)
        except OSError:
            raise WorkspaceError(
                [f"published {path.name} changed before initialization could continue"]
            ) from None
        if (named.st_dev, named.st_ino) != staged.identity or not stat.S_ISREG(named.st_mode):
            raise WorkspaceError(
                [f"published {path.name} changed before initialization could continue"]
            )
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass
    except FileExistsError:
        _restrict_staged_file(staged.descriptor)
        raise WorkspaceError(
            [
                f"{path.name} already exists; retained complete staged manifest at "
                f"{staged.path.name}"
            ]
        ) from None
    except ProjectCreateError:
        _restrict_staged_file(staged.descriptor)
        raise WorkspaceError(
            [
                "atomic manifest publication failed; retained complete staged manifest at "
                f"{staged.path.name}"
            ]
        ) from None
    except WorkspaceError:
        if not published:
            _restrict_staged_file(staged.descriptor)
        raise
    except Exception:
        if published:
            raise WorkspaceError(
                [f"published {path.name} but could not confirm final state; inspect it before retrying"]
            ) from None
        _restrict_staged_file(staged.descriptor)
        raise WorkspaceError(
            [
                f"could not publish {path.name}; retained complete staged manifest at "
                f"{staged.path.name}"
            ]
        ) from None


def _restrict_staged_file(descriptor: int) -> None:
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError:
        pass


def _create_directory_chain(root: Path, relative: PurePosixPath) -> tuple[Path, ...]:
    """Create a confined directory chain and report only directories created here."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    created: list[Path] = []
    descriptor: int | None = None
    current = root
    try:
        descriptor = os.open(root, flags)
        for part in relative.parts:
            next_path = current / part
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            except OSError:
                raise WorkspaceError(["blueprint root could not be created"]) from None
            else:
                created.append(next_path)
            try:
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(expected.st_mode):
                    raise OSError("blueprint path component is not a directory")
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except OSError:
                raise WorkspaceError(["blueprint root could not be opened safely"]) from None
            opened = os.fstat(next_descriptor)
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                os.close(next_descriptor)
                raise WorkspaceError(["blueprint root changed while it was being opened"])
            os.close(descriptor)
            descriptor = next_descriptor
            current = next_path
    except WorkspaceError as error:
        retained = ", ".join(path.relative_to(root).as_posix() for path in created)
        detail = "; ".join(error.issues)
        if retained:
            detail += f"; retained created directories: {retained}"
        raise WorkspaceError([detail]) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return tuple(created)


def _fsync_directory(path: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | directory_flag | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # The file itself was already flushed and published. Directory fsync is
        # an extra durability measure, not grounds to report a failed mutation
        # after the user-visible path has appeared.
        return


__all__ = [
    "BlueprintCreateResult",
    "WorkspaceInitResult",
    "create_blueprint_project",
    "initialize_workspace",
    "register_blueprint_project",
]
