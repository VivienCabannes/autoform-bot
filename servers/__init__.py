"""Shared path validation for Autoform's LSP and REPL servers.

Lean tools always name an absolute Lake project and never infer one from the
server process's working directory.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LAKE_PROJECT_MARKERS = ("lakefile.lean", "lakefile.toml", "lake-manifest.json")
MAX_LEAN_TOOLCHAIN_BYTES = 4096
LEAN_PROJECT_CONFIG_FILES = (
    "lake-manifest.json",
    "lakefile.toml",
    "lakefile.lean",
)
_SCRUBBED_LAKE_ENVIRONMENT = frozenset(
    {
        "ELAN",
        "ELAN_TOOLCHAIN",
        "LAKE",
        "LAKE_ARTIFACT_CACHE",
        "LAKE_CACHE_ARTIFACT_ENDPOINT",
        "LAKE_CACHE_DIR",
        "LAKE_CACHE_KEY",
        "LAKE_CACHE_REVISION_ENDPOINT",
        "LAKE_CACHE_SERVICE",
        "LAKE_CONFIG",
        "LAKE_HOME",
        "LAKE_NO_CACHE",
        "LAKE_OVERRIDE_LEAN",
        "LAKE_PKG_URL_MAP",
        "LAKE_RESTORE_ARTIFACTS",
        "LEAN",
        "LEAN_AR",
        "LEAN_CC",
        "LEAN_GITHASH",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "LEAN_WORKER_PATH",
        "PYTHONPATH",
        "RESERVOIR_API_BASE_URL",
        "RESERVOIR_API_URL",
    }
)


def _effective_lean_toolchain(project_dir: Path) -> Path | None:
    for directory in (project_dir, *project_dir.parents):
        toolchain = directory / "lean-toolchain"
        try:
            toolchain.lstat()
        except FileNotFoundError:
            continue
        return toolchain
    return None


def _normalized_environment_overrides(
    overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    explicit = dict(overrides or {})
    if explicit.get("ELAN_TOOLCHAIN") == "":
        explicit.pop("ELAN_TOOLCHAIN")
    return explicit


def _read_lean_toolchain(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Lean toolchain path is not a regular file: {path}")
        if info.st_size > MAX_LEAN_TOOLCHAIN_BYTES:
            raise ValueError(f"Lean toolchain file is too large: {path}")
        content = bytearray()
        while len(content) <= MAX_LEAN_TOOLCHAIN_BYTES:
            chunk = os.read(
                descriptor,
                MAX_LEAN_TOOLCHAIN_BYTES + 1 - len(content),
            )
            if not chunk:
                break
            content.extend(chunk)
    finally:
        os.close(descriptor)
    if len(content) > MAX_LEAN_TOOLCHAIN_BYTES:
        raise ValueError(f"Lean toolchain file is too large: {path}")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError(f"Lean toolchain file is not UTF-8: {path}") from error
    toolchain = lines[0].strip() if lines else ""
    if not toolchain:
        raise ValueError(f"Lean toolchain file is empty: {path}")
    return toolchain


def clean_lake_environment(
    project_dir: str | Path | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
    require_elan_proxy: bool = True,
) -> dict[str, str]:
    """Return a project-safe environment for a trusted Lean/Lake command."""
    explicit = _normalized_environment_overrides(overrides)
    environment = os.environ.copy()
    original_path = environment.get("PATH")
    for name in _SCRUBBED_LAKE_ENVIRONMENT:
        environment.pop(name, None)
    elan_home = Path(
        explicit.get(
            "ELAN_HOME",
            environment.get("ELAN_HOME", str(Path.home() / ".elan")),
        )
    ).expanduser().resolve()
    toolchains = elan_home / "toolchains"

    def is_project_build_path(candidate: Path) -> bool:
        parts = candidate.parts
        return (
            ".lake" in parts
            and "build" in parts
            and parts[-1:] == ("bin",)
        )

    def is_elan_toolchain_path(candidate: Path) -> bool:
        return candidate.is_relative_to(toolchains)

    def is_elan_proxy_pair(elan: Path, lake: Path) -> bool:
        try:
            return (
                elan.is_file()
                and os.access(elan, os.X_OK)
                and lake.is_file()
                and os.access(lake, os.X_OK)
                and os.path.samefile(elan, lake)
            )
        except OSError:
            return False

    proxy_dir: Path | None = None
    if "PATH" not in explicit:
        search_path = os.defpath if original_path is None else original_path
        resolved_entries: list[Path] = []
        seen_entries: set[Path] = set()
        for entry in search_path.split(os.pathsep):
            lexical_path = Path(os.path.abspath(entry or "."))
            path = lexical_path.resolve()
            if (
                path in seen_entries
                or is_project_build_path(lexical_path)
                or is_project_build_path(path)
            ):
                continue
            seen_entries.add(path)
            resolved_entries.append(path)
        elan = elan_home / "bin" / "elan"
        lake = elan_home / "bin" / "lake"
        if is_elan_proxy_pair(elan, lake):
            proxy_dir = elan.parent.resolve()
        else:
            for directory in resolved_entries:
                if is_elan_toolchain_path(directory):
                    continue
                discovered_elan = directory / "elan"
                discovered_lake = directory / "lake"
                if is_elan_proxy_pair(discovered_elan, discovered_lake):
                    proxy_dir = directory
                    break
        if proxy_dir is not None:
            resolved_entries = [
                path
                for path in resolved_entries
                if path != proxy_dir and not is_elan_toolchain_path(path)
            ]
            resolved_entries.insert(0, proxy_dir)
        environment["PATH"] = os.pathsep.join(map(str, resolved_entries))
    explicit_toolchain = explicit.get("ELAN_TOOLCHAIN")
    project_toolchain: str | None = None
    if project_dir is not None and explicit_toolchain is None:
        toolchain_file = _effective_lean_toolchain(Path(project_dir).resolve())
        if toolchain_file is not None:
            project_toolchain = _read_lean_toolchain(toolchain_file)
    if (
        (explicit_toolchain is not None or project_toolchain is not None)
        and require_elan_proxy
        and "PATH" not in explicit
        and proxy_dir is None
    ):
        raise ValueError(
            "a pinned Lean project requires an Elan proxy on PATH; "
            "a direct Lake executable cannot honor the selected toolchain"
        )
    environment.update(explicit)
    environment["ELAN_HOME"] = str(elan_home)
    return environment


@dataclass(frozen=True, slots=True)
class ProjectFingerprint:
    """Filesystem identity of a project root and its Lean configuration."""

    root: tuple[int, int, int]
    files: tuple[tuple[str, int, int, int, int, int, int, int], ...]
    environment: tuple[tuple[str, str], ...] = ()


def lean_project_fingerprint(
    project_dir: Path,
    *,
    environment_overrides: Mapping[str, str] | None = None,
) -> ProjectFingerprint:
    """Return the project metadata that makes resident Lean state stale."""
    explicit = _normalized_environment_overrides(environment_overrides)
    root = project_dir.stat()
    fingerprint: list[tuple[str, int, int, int, int, int, int, int]] = []
    toolchain = (
        None
        if "ELAN_TOOLCHAIN" in explicit
        else _effective_lean_toolchain(project_dir)
    )
    if toolchain is not None:
        link = toolchain.lstat()
        info = toolchain.stat()
        fingerprint.append(
            (
                str(toolchain),
                link.st_mode,
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        )
    for name in LEAN_PROJECT_CONFIG_FILES:
        path = project_dir / name
        try:
            link = path.lstat()
            info = path.stat()
        except FileNotFoundError:
            continue
        fingerprint.append(
            (
                name,
                link.st_mode,
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        )
    return ProjectFingerprint(
        root=(root.st_dev, root.st_ino, root.st_mode),
        files=tuple(fingerprint),
        environment=tuple(sorted(explicit.items())),
    )


def is_initial_manifest_materialization(
    before: ProjectFingerprint,
    after: ProjectFingerprint,
) -> bool:
    """Return whether only a regular, previously absent manifest appeared."""
    manifest = "lake-manifest.json"
    created = [item for item in after.files if item[0] == manifest]
    return (
        before.root == after.root
        and before.environment == after.environment
        and all(item[0] != manifest for item in before.files)
        and len(created) == 1
        and stat.S_ISREG(created[0][1])
        and before.files
        == tuple(item for item in after.files if item[0] != manifest)
    )


def resolve_lean_project_dir(project_dir: str) -> Path:
    """Return a validated, absolute Lake project directory."""
    if not isinstance(project_dir, str) or not project_dir.strip():
        raise ValueError("project_dir is required and must be an absolute Lake project path")

    path = Path(project_dir).expanduser()
    if not path.is_absolute():
        raise ValueError(f"project_dir must be absolute, got {project_dir!r}")

    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"project_dir does not exist: {project_dir}") from exc

    if not path.is_dir():
        raise ValueError(f"project_dir is not a directory: {path}")
    if not any((path / marker).is_file() for marker in LAKE_PROJECT_MARKERS):
        markers = ", ".join(LAKE_PROJECT_MARKERS)
        raise ValueError(f"project_dir is not a Lake project: {path} (expected one of: {markers})")
    return path


def resolve_lean_file(project_dir: str, file_path: str) -> tuple[Path, Path]:
    """Resolve an existing in-project Lean file without using cwd."""
    root = resolve_lean_project_dir(project_dir)
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path is required")
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"file_path must stay inside project_dir: {path}") from exc
    if path.suffix != ".lean":
        raise ValueError(f"file_path must name a .lean file: {path}")
    if not path.is_file():
        raise ValueError(f"file_path does not exist: {path}")
    return root, path
