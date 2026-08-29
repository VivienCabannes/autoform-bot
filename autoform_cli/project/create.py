"""Create a complete Autoform Lean project and publish it atomically."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..graph import _parse_node
from ..provenance import normalize_git_source
from ..scaffold import DEFAULT_AUTOFORM_SOURCE, _ScaffoldFile, _scaffold_plan
from .catalog import load_release_catalog
from .inspect import _inspect_project_root
from .model import SupportedRelease

_PACKAGE_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_RESERVED_PACKAGE_NAMES = frozenset({"Prop", "Sort", "Type"})
# A generated lean_lib claims its name as a module prefix. Reusing a prefix
# from Lean, Lake, Mathlib, or Mathlib's transitive production libraries makes
# imports resolve into the new project's empty subtree and creates cycles.
# Keep this allowlist-indexed contract in lockstep with releases.json.
_RELEASE_MODULE_ROOTS = {
    "lean-v4.32.2-mathlib-v4.32.2": frozenset(
        {
            "Aesop",
            "Batteries",
            "Cache",
            "Cli",
            "ImportGraph",
            "Init",
            "Lake",
            "Lean",
            "LeanSearchClient",
            "Mathlib",
            "Plausible",
            "ProofWidgets",
            "Qq",
            "Std",
        }
    )
}
_STAGE_ATTEMPTS = 32
_COMMIT_UNCERTAIN_MESSAGE = "Project publication may have occurred; inspect the target before retrying."


class ProjectCreateError(ValueError):
    """A new project could not be created without risking existing data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        return {"error": {"code": self.code, "message": self.message}, "ok": False}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProjectCreateResult:
    package: str
    release: str
    target: str
    written: tuple[str, ...]
    workflows_pinned: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "package": self.package,
            "release": self.release,
            "target": self.target,
            "workflows_pinned": self.workflows_pinned,
            "written": list(self.written),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def create_project(
    target: str | Path | None,
    *,
    package: str | None,
    release_id: str | None,
    autoform_source: str = "",
    autoform_ref: str = "",
) -> ProjectCreateResult:
    """Create and atomically publish a new project at an absent *target*."""

    requested = _validate_target(target)
    package_name = _validate_package(package)
    release = _find_release(release_id)
    _validate_package_for_release(package_name, release)
    workflow_source, workflow_ref = _validate_workflow_pin(autoform_source, autoform_ref)
    try:
        plan, workflows_pinned = _build_project_plan(
            package_name,
            release,
            autoform_source=workflow_source,
            autoform_ref=workflow_ref,
        )
        _plan_tree(plan)
        _validate_roadmap_plan(plan)
    except (OSError, UnicodeError):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The generated project did not satisfy Autoform's project contracts.",
        ) from None
    parent = requested.parent
    parent_descriptor = _open_parent(parent)
    stage_name: str | None = None
    stage_descriptor: int | None = None
    publication_started = False
    published = False
    try:
        _lock_parent(parent_descriptor)
        _require_absent(parent_descriptor, requested.name)
        stage_name = _create_stage(parent_descriptor)
        stage_metadata = os.stat(stage_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(stage_metadata.st_mode):
            raise OSError(errno.ENOTDIR, "staging path is not a directory")
        stage_descriptor = _open_stage(parent_descriptor, stage_name)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        _materialize_project(stage_descriptor, plan)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        _validate_staged_project(stage_descriptor, plan, release)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        os.fchmod(stage_descriptor, 0o755)
        os.fsync(stage_descriptor)
        _verify_project_plan(stage_descriptor, plan, root_mode=0o755)
        _require_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        publication_started = True
        try:
            _rename_noreplace(
                parent_descriptor,
                stage_name,
                parent_descriptor,
                requested.name,
            )
        except FileExistsError:
            raise ProjectCreateError(
                "project-target-exists",
                "The target already exists; project new never overwrites it.",
            ) from None
        published = True
        _require_stage_identity(parent_descriptor, requested.name, stage_descriptor)
        os.fsync(parent_descriptor)
        return ProjectCreateResult(
            package=package_name,
            release=release.id,
            target=requested.name,
            written=tuple(item.relative for item in plan),
            workflows_pinned=workflows_pinned,
        )
    except ProjectCreateError as error:
        if published:
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _COMMIT_UNCERTAIN_MESSAGE,
            ) from None
        if stage_name is not None:
            raise ProjectCreateError(error.code, _with_preserved_stage(error.message)) from None
        raise
    except OSError:
        if published:
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _COMMIT_UNCERTAIN_MESSAGE,
            ) from None
        message = "Project creation failed; no project was created."
        if stage_name is not None:
            message = _with_preserved_stage(message)
        raise ProjectCreateError("project-create-failed", message) from None
    finally:
        publication_uncertain = (
            not published
            and publication_started
            and stage_descriptor is not None
            and stage_name is not None
            and (
                _entry_matches_descriptor(parent_descriptor, requested.name, stage_descriptor)
                or not _entry_matches_descriptor(parent_descriptor, stage_name, stage_descriptor)
            )
        )
        if publication_uncertain:
            published = True
        close_failed = False
        for descriptor in (stage_descriptor, parent_descriptor):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                close_failed = True
        if publication_uncertain:
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _COMMIT_UNCERTAIN_MESSAGE,
            )
        if close_failed and published:
            raise ProjectCreateError(
                "project-create-commit-uncertain",
                _COMMIT_UNCERTAIN_MESSAGE,
            )


def _with_preserved_stage(message: str) -> str:
    return f"{message} An .autoform-new-* stage may remain; inspect it before removal."


def _validate_package(package: str | None) -> str:
    if not isinstance(package, str) or _PACKAGE_NAME.fullmatch(package) is None or package in _RESERVED_PACKAGE_NAMES:
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name must be an UpperCamelCase Lean identifier.",
        )
    return package


def _find_release(release_id: str | None) -> SupportedRelease:
    catalog = load_release_catalog()
    release = next((item for item in catalog.releases if item.id == release_id), None)
    if release is None:
        raise ProjectCreateError(
            "project-release-unknown",
            "The requested release is not in the bundled release catalog.",
        )
    return release


def _validate_package_for_release(package: str, release: SupportedRelease) -> None:
    roots = _RELEASE_MODULE_ROOTS.get(release.id)
    if roots is None:
        raise ProjectCreateError(
            "project-release-unknown",
            "The requested release lacks a bundled module-collision contract.",
        )
    if package in roots:
        raise ProjectCreateError(
            "project-name-invalid",
            "Project name must not shadow a Lean module root used by the selected release.",
        )


def _validate_workflow_pin(source: str, ref: str) -> tuple[str, str]:
    """Validate explicit provenance before creating filesystem state."""

    if not isinstance(source, str) or not isinstance(ref, str):
        raise ProjectCreateError(
            "project-provenance-invalid",
            "Autoform workflow provenance must include a safe Git source and full commit SHA.",
        )
    if not source and not ref:
        return "", ""
    safe_source = normalize_git_source(source)
    normalized_ref = ref.strip().lower()
    if not source or not ref or safe_source is None or _FULL_SHA.fullmatch(normalized_ref) is None:
        raise ProjectCreateError(
            "project-provenance-invalid",
            "Autoform workflow provenance must include a safe Git source and full commit SHA.",
        )
    return safe_source, normalized_ref


def _validate_target(target: str | Path | None) -> Path:
    try:
        if target is None:
            raise ValueError
        encoded = os.fspath(target)
        if not isinstance(encoded, str) or "\0" in encoded:
            raise ValueError
        raw = Path(encoded).expanduser().absolute()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ProjectCreateError("project-target-invalid", "The project target cannot be resolved safely.") from None
    if raw.name in {"", ".", ".."}:
        raise ProjectCreateError("project-target-invalid", "The project target must name a new directory.")
    parent = raw.parent
    if not parent.exists():
        raise ProjectCreateError("project-parent-missing", "The target parent directory does not exist.")
    if not parent.is_dir():
        raise ProjectCreateError("project-parent-invalid", "The target parent is not a directory.")
    try:
        metadata = parent.stat()
    except OSError:
        raise ProjectCreateError("project-parent-invalid", "The target parent is not a directory.") from None
    writable_by_others = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if writable_by_others and not metadata.st_mode & stat.S_ISVTX:
        raise ProjectCreateError(
            "project-parent-unsafe",
            "The target parent must not be group- or world-writable unless it is sticky.",
        )
    try:
        canonical_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProjectCreateError("project-parent-invalid", "The target parent is not a directory.") from None
    return canonical_parent / raw.name


def _open_parent(parent: Path) -> int:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
        or any(function not in os.supports_dir_fd for function in (os.mkdir, os.open, os.stat))
        or os.stat not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
    ):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot create the project with the required path safety.",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    absolute = parent.absolute()
    try:
        descriptor = os.open(absolute.anchor, flags)
        try:
            for part in absolute.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
    except OSError:
        raise ProjectCreateError("project-path-is-symlink", "The target path contains a symbolic link.") from None
    return descriptor


def _require_absent(parent_descriptor: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.") from None
    raise ProjectCreateError("project-target-exists", "The target already exists; project new never overwrites it.")


def _create_stage(parent_descriptor: int) -> str:
    for _ in range(_STAGE_ATTEMPTS):
        name = f".autoform-new-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            return name
        except FileExistsError:
            continue
    raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.")


def _open_stage(parent_descriptor: int, stage_name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return os.open(stage_name, flags, dir_fd=parent_descriptor)


def _open_planned_file(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    return os.open(name, flags, dir_fd=parent_descriptor)


def _lock_parent(parent_descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(parent_descriptor, fcntl.LOCK_EX)
    except (ImportError, OSError):
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot serialize concurrent project creation safely.",
        ) from None


def _list_directory(directory_descriptor: int) -> list[str]:
    """List through a fresh descriptor so earlier scans cannot leave it at EOF."""

    fresh = _open_stage(directory_descriptor, ".")
    try:
        return os.listdir(fresh)
    finally:
        os.close(fresh)


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "staging path is not a directory")
    return metadata.st_dev, metadata.st_ino


def _require_stage_identity(workspace_descriptor: int, stage_name: str, stage_descriptor: int) -> None:
    expected = _descriptor_identity(stage_descriptor)
    metadata = os.stat(stage_name, dir_fd=workspace_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != expected:
        raise ProjectCreateError("project-create-failed", "Project creation failed; no project was created.")


def _entry_matches_descriptor(parent_descriptor: int, name: str, descriptor: int) -> bool:
    try:
        expected = _descriptor_identity(descriptor)
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == expected


def _build_project_plan(
    package: str,
    release: SupportedRelease,
    *,
    autoform_source: str,
    autoform_ref: str,
) -> tuple[tuple[_ScaffoldFile, ...], bool]:
    files = [
        _ScaffoldFile("lean-toolchain", f"{release.lean.toolchain}\n".encode(), 0o644),
        _ScaffoldFile(
            "lakefile.toml",
            (
                f'name = "{package}"\n'
                'version = "0.1.0"\n'
                f'defaultTargets = ["{package}"]\n\n'
                "[[require]]\n"
                'name = "mathlib"\n'
                f'git = "{release.mathlib.git}"\n'
                f'rev = "{release.mathlib.revision}"\n\n'
                "[[lean_lib]]\n"
                f'name = "{package}"\n'
                'srcDir = "src"\n'
            ).encode(),
            0o644,
        ),
        _ScaffoldFile(
            f"src/{package}.lean",
            (
                "import Mathlib\n\n"
                f"namespace {package}\n\n"
                "/-- Marker declaration for the initial project build. -/\n"
                "def autoformProjectInitialized : Bool := true\n\n"
                f"end {package}\n"
            ).encode(),
            0o644,
        ),
    ]
    scaffold_files, _ = _scaffold_plan(
        title=package,
        repository_url="",
        autoform_source=autoform_source or DEFAULT_AUTOFORM_SOURCE,
        autoform_ref=autoform_ref,
    )
    files.extend(scaffold_files)
    return tuple(sorted(files, key=lambda item: item.relative)), bool(autoform_ref)


def _plan_tree(plan: tuple[_ScaffoldFile, ...]) -> dict[str, object]:
    tree: dict[str, object] = {}
    for item in plan:
        path = PurePosixPath(item.relative)
        if (
            not item.relative
            or path.is_absolute()
            or path.as_posix() != item.relative
            or any(part in {"", ".", ".."} for part in path.parts)
            or item.mode & ~0o777
        ):
            raise ProjectCreateError(
                "project-create-validation-failed",
                "The generated project did not satisfy Autoform's project contracts.",
            )
        branch = tree
        for part in path.parts[:-1]:
            child = branch.setdefault(part, {})
            if not isinstance(child, dict):
                raise ProjectCreateError(
                    "project-create-validation-failed",
                    "The generated project did not satisfy Autoform's project contracts.",
                )
            branch = child
        if path.name in branch:
            raise ProjectCreateError(
                "project-create-validation-failed",
                "The generated project did not satisfy Autoform's project contracts.",
            )
        branch[path.name] = item
    return tree


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short project file write")
        offset += written


def _materialize_project(root_descriptor: int, plan: tuple[_ScaffoldFile, ...]) -> None:
    tree = _plan_tree(plan)

    def write_directory(descriptor: int, entries: dict[str, object]) -> None:
        for name, entry in sorted(entries.items()):
            if isinstance(entry, dict):
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child = _open_stage(descriptor, name)
                try:
                    if _descriptor_identity(child) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise OSError(errno.ESTALE, "project directory changed")
                    write_directory(child, entry)
                    os.fchmod(child, 0o755)
                    os.fsync(child)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise OSError(errno.ESTALE, "project directory changed")
                finally:
                    os.close(child)
                continue
            if not isinstance(entry, _ScaffoldFile):
                raise OSError(errno.EINVAL, "invalid project plan")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            child = os.open(name, flags, 0o600, dir_fd=descriptor)
            try:
                _write_all(child, entry.content)
                os.fchmod(child, entry.mode)
                os.fsync(child)
            finally:
                os.close(child)
        if set(_list_directory(descriptor)) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")

    write_directory(root_descriptor, tree)


def _verify_project_plan(
    root_descriptor: int,
    plan: tuple[_ScaffoldFile, ...],
    *,
    root_mode: int = 0o700,
) -> None:
    tree = _plan_tree(plan)
    root = os.fstat(root_descriptor)
    if not stat.S_ISDIR(root.st_mode) or stat.S_IMODE(root.st_mode) != root_mode:
        raise OSError(errno.ESTALE, "project root changed")

    def stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
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

    def verify_directory(descriptor: int, entries: dict[str, object]) -> None:
        directory_before = os.fstat(descriptor)
        if set(_list_directory(descriptor)) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")
        for name, entry in sorted(entries.items()):
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if isinstance(entry, dict):
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError(errno.ESTALE, "project directory changed")
                child = _open_stage(descriptor, name)
                try:
                    opened = os.fstat(child)
                    if stable_metadata(opened) != stable_metadata(metadata) or stat.S_IMODE(opened.st_mode) != 0o755:
                        raise OSError(errno.ESTALE, "project directory changed")
                    verify_directory(child, entry)
                    after = os.fstat(child)
                    current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if not (stable_metadata(opened) == stable_metadata(after) == stable_metadata(current)):
                        raise OSError(errno.ESTALE, "project directory changed")
                finally:
                    os.close(child)
                continue
            if not isinstance(entry, _ScaffoldFile) or not stat.S_ISREG(metadata.st_mode):
                raise OSError(errno.ESTALE, "project file changed")
            child = _open_planned_file(descriptor, name)
            try:
                opened = os.fstat(child)
                content = bytearray()
                while len(content) <= len(entry.content):
                    chunk = os.read(
                        child,
                        min(1024 * 1024, len(entry.content) + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                after = os.fstat(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stable_metadata(metadata) != stable_metadata(opened)
                    or stable_metadata(opened) != stable_metadata(after)
                    or stable_metadata(after) != stable_metadata(current)
                    or after.st_nlink != 1
                    or stat.S_IMODE(after.st_mode) != entry.mode
                    or bytes(content) != entry.content
                ):
                    raise OSError(errno.ESTALE, "project file changed")
            finally:
                os.close(child)
        directory_after = os.fstat(descriptor)
        if stable_metadata(directory_before) != stable_metadata(directory_after) or set(
            _list_directory(descriptor)
        ) != set(entries):
            raise OSError(errno.ESTALE, "project directory changed")

    verify_directory(root_descriptor, tree)


def _validate_staged_project(
    stage_descriptor: int,
    plan: tuple[_ScaffoldFile, ...],
    release: SupportedRelease,
) -> None:
    _verify_project_plan(stage_descriptor, plan)
    inspection = _inspect_project_root(stage_descriptor, load_release_catalog())
    if (
        not inspection.ok
        or inspection.compatibility.status != "supported"
        or inspection.compatibility.release != release.id
        or inspection.lake is None
    ):
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )
    _validate_roadmap_plan(plan)
    _verify_project_plan(stage_descriptor, plan)


def _validate_roadmap_plan(plan: tuple[_ScaffoldFile, ...]) -> None:
    roadmap = [
        item for item in plan if item.relative.startswith("blueprint/roadmap/") and item.relative.endswith(".md")
    ]
    if len(roadmap) != 1 or roadmap[0].relative != "blueprint/roadmap/README.md":
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )
    try:
        text = roadmap[0].content.decode("utf-8")
    except UnicodeError:
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        ) from None
    parsed, issues = _parse_node("roadmap", Path("roadmap/README.md"), text)
    if issues or parsed is None or parsed.statement_targets or parsed.proof_targets:
        raise ProjectCreateError(
            "project-create-validation-failed",
            "The staged project did not satisfy Autoform's project contracts.",
        )


def _rename_noreplace(
    source_parent_descriptor: int,
    source: str,
    target_parent_descriptor: int,
    target: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(
            source_parent_descriptor,
            source_bytes,
            target_parent_descriptor,
            target_bytes,
            0x00000004,
        )
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(
            source_parent_descriptor,
            source_bytes,
            target_parent_descriptor,
            target_bytes,
            1,
        )
    else:
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot atomically publish a new project without replacement.",
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), target)
    if error in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}:
        raise ProjectCreateError(
            "project-create-safety-unavailable",
            "This platform cannot atomically publish a new project without replacement.",
        )
    raise OSError(error, os.strerror(error), target)


__all__ = ["ProjectCreateError", "ProjectCreateResult", "create_project"]
