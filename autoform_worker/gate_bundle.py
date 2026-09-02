"""Canonical identities for offline Lean dependency bundles used by hard gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .gate_provider import GateProviderError


RUNTIME_BUNDLE_SCHEMA = "autoform-gate-runtime-bundle/v1"
RUNTIME_BUNDLE_MANIFEST = ".autoform-bundle.json"

_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_PROJECT_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_TOOL_OUTPUT_BYTES = 16 * 1024
_MAX_PATH_BYTES = 4096
_MAX_COMPONENT_BYTES = 255
_MAX_SYMLINK_HOPS = 64
_MAX_TRACKED_PATHS = 2_000_000
_DEFAULT_MAX_ENTRIES = 2_000_000
_DEFAULT_MAX_FILE_BYTES = 128 * 1024**3
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PLATFORM = re.compile(r"linux/[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)?")
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}")
_TOOLCHAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]{0,255}")
_PINNED_TOOLCHAIN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}:"
    r"(?:v?[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._-]+)?|nightly-[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9a-f]{40})"
)
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_TREE_DOMAIN = b"autoform-gate-runtime-tree/v1\0"
_LAKE_MANIFEST_VERSION = "1.2.0"
_LAKE_MANIFEST_FIELDS = frozenset(
    {"fixedToolchain", "lakeDir", "name", "packages", "packagesDir", "version"}
)
_LAKE_GIT_PACKAGE_FIELDS = frozenset(
    {
        "configFile",
        "inherited",
        "inputRev",
        "manifestFile",
        "name",
        "rev",
        "scope",
        "subDir",
        "type",
        "url",
    }
)


@dataclass(frozen=True, slots=True)
class BundleTreeIdentity:
    """Deterministic identity and accounting for every package-tree entry."""

    sha256: str
    entry_count: int
    file_count: int
    directory_count: int
    symlink_count: int
    file_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise GateProviderError("runtime bundle tree SHA-256 is invalid")
        for label, value in (
            ("entry count", self.entry_count),
            ("file count", self.file_count),
            ("directory count", self.directory_count),
            ("symlink count", self.symlink_count),
            ("file bytes", self.file_bytes),
        ):
            if type(value) is not int or value < 0:
                raise GateProviderError(f"runtime bundle tree {label} must be a nonnegative integer")
        if self.entry_count != self.file_count + self.directory_count + self.symlink_count:
            raise GateProviderError("runtime bundle tree entry counts do not add up")

    def as_dict(self) -> dict[str, int | str]:
        return {
            "directory_count": self.directory_count,
            "entry_count": self.entry_count,
            "file_bytes": self.file_bytes,
            "file_count": self.file_count,
            "sha256": self.sha256,
            "symlink_count": self.symlink_count,
        }

    @classmethod
    def from_value(cls, value: object) -> BundleTreeIdentity:
        if not isinstance(value, dict) or set(value) != {
            "directory_count",
            "entry_count",
            "file_bytes",
            "file_count",
            "sha256",
            "symlink_count",
        }:
            raise GateProviderError("runtime bundle tree fields do not match the schema")
        try:
            return cls(**value)
        except TypeError as error:
            raise GateProviderError("runtime bundle tree contains invalid values") from error


@dataclass(frozen=True, slots=True)
class LakePackageIdentity:
    """One complete, immutable Lake manifest package object."""

    name: str
    canonical_json: bytes

    def __post_init__(self) -> None:
        _package_name(self.name)
        if not isinstance(self.canonical_json, bytes) or not self.canonical_json:
            raise GateProviderError("Lake package identity must be canonical JSON bytes")
        value = _strict_json_value(
            self.canonical_json,
            label="Lake package identity",
            maximum=_MAX_PROJECT_MANIFEST_BYTES,
        )
        if not isinstance(value, dict) or value.get("name") != self.name:
            raise GateProviderError("Lake package identity name does not match its object")
        _validate_package_identity(value)
        if _json_bytes(value) != self.canonical_json:
            raise GateProviderError("Lake package identity is not canonical JSON")

    def as_dict(self) -> dict[str, Any]:
        value = _strict_json_value(
            self.canonical_json,
            label="Lake package identity",
            maximum=_MAX_PROJECT_MANIFEST_BYTES,
        )
        if not isinstance(value, dict):
            raise GateProviderError("Lake package identity must contain one object")
        return value

    @classmethod
    def from_value(cls, value: object) -> LakePackageIdentity:
        if not isinstance(value, dict):
            raise GateProviderError("each Lake dependency identity must be one object")
        name = _validate_package_identity(value)
        return cls(name=name, canonical_json=_json_bytes(value))


@dataclass(frozen=True, slots=True)
class LakeDependencyLock:
    """The complete dependency-bearing projection of a Lake manifest."""

    manifest_version: str
    packages_dir: str
    packages: tuple[LakePackageIdentity, ...]

    def __post_init__(self) -> None:
        if self.manifest_version != _LAKE_MANIFEST_VERSION:
            raise GateProviderError(
                f"Lake manifest version must be exactly {_LAKE_MANIFEST_VERSION}"
            )
        if self.packages_dir != ".lake/packages":
            raise GateProviderError("Lake packagesDir must be exactly .lake/packages")
        if type(self.packages) is not tuple or any(
            not isinstance(package, LakePackageIdentity) for package in self.packages
        ):
            raise GateProviderError("Lake dependency packages must be an immutable tuple")
        names = tuple(package.name for package in self.packages)
        expected = tuple(sorted(names, key=lambda name: name.encode("utf-8")))
        if names != expected:
            raise GateProviderError("Lake dependency identities must be sorted by package name")
        if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
            raise GateProviderError("Lake dependency package names must be unique")
        if any(name.casefold() == RUNTIME_BUNDLE_MANIFEST.casefold() for name in names):
            raise GateProviderError("Lake dependency name collides with the runtime bundle manifest")

    def as_dict(self) -> dict[str, object]:
        return {
            "packages": [package.as_dict() for package in self.packages],
            "packagesDir": self.packages_dir,
            "version": self.manifest_version,
        }

    @classmethod
    def from_value(cls, value: object) -> LakeDependencyLock:
        if not isinstance(value, dict) or set(value) != {"packages", "packagesDir", "version"}:
            raise GateProviderError("Lake dependency lock fields do not match the schema")
        packages = value["packages"]
        if not isinstance(packages, list):
            raise GateProviderError("Lake dependency lock packages must be one array")
        identities = tuple(
            sorted(
                (LakePackageIdentity.from_value(package) for package in packages),
                key=lambda package: package.name.encode("utf-8"),
            )
        )
        return cls(
            manifest_version=value["version"],
            packages_dir=value["packagesDir"],
            packages=identities,
        )

    @classmethod
    def from_lake_manifest_bytes(cls, content: bytes) -> LakeDependencyLock:
        value = _strict_json_value(
            content,
            label="lake-manifest.json",
            maximum=_MAX_PROJECT_MANIFEST_BYTES,
        )
        if not isinstance(value, dict):
            raise GateProviderError("lake-manifest.json must contain one object")
        if set(value) != _LAKE_MANIFEST_FIELDS:
            raise GateProviderError("lake-manifest.json fields do not match the supported schema")
        if value["version"] != _LAKE_MANIFEST_VERSION:
            raise GateProviderError(
                f"Lake manifest version must be exactly {_LAKE_MANIFEST_VERSION}"
            )
        if value["lakeDir"] != ".lake":
            raise GateProviderError("Lake lakeDir must be exactly .lake")
        if type(value["fixedToolchain"]) is not bool:
            raise GateProviderError("Lake fixedToolchain must be a boolean")
        _canonical_lock_text("Lake project name", value["name"])
        return cls.from_value(
            {
                "packages": value["packages"],
                "packagesDir": value["packagesDir"],
                "version": value["version"],
            }
        )


@dataclass(frozen=True, slots=True)
class AutoformReleaseIdentity:
    """Exact Autoform package version and source revision in the image."""

    version: str
    revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or _VERSION.fullmatch(self.version) is None:
            raise GateProviderError("Autoform version must be one canonical release token")
        if not isinstance(self.revision, str) or _REVISION.fullmatch(self.revision) is None:
            raise GateProviderError("Autoform revision must be a full lowercase Git object ID")
        if self.revision == "0" * len(self.revision):
            raise GateProviderError("Autoform revision must not be all-zero")

    def as_dict(self) -> dict[str, str]:
        return {"revision": self.revision, "version": self.version}

    @classmethod
    def from_value(cls, value: object) -> AutoformReleaseIdentity:
        if not isinstance(value, dict) or set(value) != {"revision", "version"}:
            raise GateProviderError("runtime bundle Autoform fields do not match the schema")
        try:
            return cls(**value)
        except TypeError as error:
            raise GateProviderError("runtime bundle Autoform identity contains invalid values") from error


@dataclass(frozen=True, slots=True)
class RuntimeToolVersions:
    """Exact version command outputs from the native Linux runtime."""

    git: str
    lake: str
    lean: str
    python: str

    def __post_init__(self) -> None:
        for label, value in (
            ("Git", self.git),
            ("Lake", self.lake),
            ("Lean", self.lean),
            ("Python", self.python),
        ):
            _version_output(label, value)

    def as_dict(self) -> dict[str, str]:
        return {
            "git": self.git,
            "lake": self.lake,
            "lean": self.lean,
            "python": self.python,
        }

    @classmethod
    def from_value(cls, value: object) -> RuntimeToolVersions:
        if not isinstance(value, dict) or set(value) != {"git", "lake", "lean", "python"}:
            raise GateProviderError("runtime bundle tool fields do not match the schema")
        try:
            return cls(**value)
        except TypeError as error:
            raise GateProviderError("runtime bundle tool identity contains invalid values") from error


@dataclass(frozen=True, slots=True)
class RuntimeBundleManifest:
    """Canonical manifest for one immutable native-Linux gate runtime bundle."""

    release_id: str
    platform: str
    autoform: AutoformReleaseIdentity
    lean_toolchain: str
    tools: RuntimeToolVersions
    lake_lock: LakeDependencyLock
    tree: BundleTreeIdentity
    schema: str = RUNTIME_BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RUNTIME_BUNDLE_SCHEMA:
            raise GateProviderError("unsupported runtime bundle schema")
        if not isinstance(self.release_id, str) or _RELEASE_ID.fullmatch(self.release_id) is None:
            raise GateProviderError("runtime bundle release ID must be one canonical token")
        if not isinstance(self.platform, str) or _PLATFORM.fullmatch(self.platform) is None:
            raise GateProviderError("runtime bundle platform must be an exact Linux OCI platform")
        _lean_toolchain(self.lean_toolchain)
        if not isinstance(self.autoform, AutoformReleaseIdentity):
            raise GateProviderError("runtime bundle Autoform identity is invalid")
        if not isinstance(self.tools, RuntimeToolVersions):
            raise GateProviderError("runtime bundle tool identity is invalid")
        if not isinstance(self.lake_lock, LakeDependencyLock):
            raise GateProviderError("runtime bundle Lake dependency lock is invalid")
        if not isinstance(self.tree, BundleTreeIdentity):
            raise GateProviderError("runtime bundle tree identity is invalid")
        if self.tree.directory_count < len(self.lake_lock.packages):
            raise GateProviderError("runtime bundle tree omits one or more package roots")

    def as_dict(self) -> dict[str, object]:
        return {
            "autoform": self.autoform.as_dict(),
            "lake_lock": self.lake_lock.as_dict(),
            "lean_toolchain": self.lean_toolchain,
            "platform": self.platform,
            "release_id": self.release_id,
            "schema": self.schema,
            "tools": self.tools.as_dict(),
            "tree": self.tree.as_dict(),
        }

    def evidence_bytes(self) -> bytes:
        return _json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, content: bytes) -> RuntimeBundleManifest:
        value = _strict_json_value(
            content,
            label="runtime bundle manifest",
            maximum=_MAX_MANIFEST_BYTES,
        )
        if not isinstance(value, dict) or set(value) != {
            "autoform",
            "lake_lock",
            "lean_toolchain",
            "platform",
            "release_id",
            "schema",
            "tools",
            "tree",
        }:
            raise GateProviderError("runtime bundle manifest fields do not match the schema")
        manifest = cls(
            autoform=AutoformReleaseIdentity.from_value(value["autoform"]),
            lake_lock=LakeDependencyLock.from_value(value["lake_lock"]),
            lean_toolchain=value["lean_toolchain"],
            platform=value["platform"],
            release_id=value["release_id"],
            schema=value["schema"],
            tools=RuntimeToolVersions.from_value(value["tools"]),
            tree=BundleTreeIdentity.from_value(value["tree"]),
        )
        if manifest.evidence_bytes() != content:
            raise GateProviderError("runtime bundle manifest is not canonical JSON")
        return manifest


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: Path
    relative: str
    package_root: Path
    kind: str
    identity: tuple[int, int, int, int, int, int]


def build_runtime_bundle_manifest(
    bundle_root: str | Path,
    *,
    release_id: str,
    platform: str,
    autoform_version: str,
    autoform_revision: str,
    lean_toolchain: str,
    lean_version: str,
    lake_version: str,
    git_version: str,
    python_version: str,
    lake_manifest: bytes,
    maximum_entries: int = _DEFAULT_MAX_ENTRIES,
    maximum_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> RuntimeBundleManifest:
    """Construct, but do not write, a manifest for an existing package bundle."""

    lock = LakeDependencyLock.from_lake_manifest_bytes(lake_manifest)
    tree = _bundle_tree_identity(
        bundle_root,
        lock,
        require_manifest=False,
        maximum_entries=maximum_entries,
        maximum_file_bytes=maximum_file_bytes,
    )
    return RuntimeBundleManifest(
        release_id=release_id,
        platform=platform,
        autoform=AutoformReleaseIdentity(
            version=autoform_version,
            revision=autoform_revision,
        ),
        lean_toolchain=lean_toolchain,
        tools=RuntimeToolVersions(
            git=git_version,
            lake=lake_version,
            lean=lean_version,
            python=python_version,
        ),
        lake_lock=lock,
        tree=tree,
    )


def load_and_verify_runtime_bundle(
    bundle_root: str | Path,
    *,
    maximum_entries: int = _DEFAULT_MAX_ENTRIES,
    maximum_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> RuntimeBundleManifest:
    """Load exact canonical evidence and rehash the complete package tree."""

    root = _real_directory(bundle_root, label="runtime bundle root")
    content = _read_regular_file(
        root / RUNTIME_BUNDLE_MANIFEST,
        maximum=_MAX_MANIFEST_BYTES,
        label="runtime bundle manifest",
    )
    manifest = RuntimeBundleManifest.from_bytes(content)
    observed = _bundle_tree_identity(
        root,
        manifest.lake_lock,
        require_manifest=True,
        maximum_entries=maximum_entries,
        maximum_file_bytes=maximum_file_bytes,
    )
    if observed != manifest.tree:
        raise GateProviderError("runtime bundle package tree does not match its manifest")
    if (
        _read_regular_file(
            root / RUNTIME_BUNDLE_MANIFEST,
            maximum=_MAX_MANIFEST_BYTES,
            label="runtime bundle manifest",
        )
        != content
    ):
        raise GateProviderError("runtime bundle manifest changed while its tree was verified")
    return manifest


def validate_project_bundle_compatibility(
    manifest: RuntimeBundleManifest,
    project_root: str | Path,
    *,
    tracked_source_paths: Iterable[str],
) -> None:
    """Require exact toolchain/lock identities and a source tree that omits .lake."""

    if not isinstance(manifest, RuntimeBundleManifest):
        raise GateProviderError("runtime bundle manifest is invalid")
    root = _real_directory(project_root, label="project root")
    root_identity = _directory_identity(root)
    tracked = _validate_tracked_source_paths(tracked_source_paths)
    required = {"lake-manifest.json", "lean-toolchain"}
    if not required.issubset(tracked):
        raise GateProviderError("project compatibility files must be tracked by the source commit")
    toolchain = _parse_lean_toolchain(
        _read_regular_file(root / "lean-toolchain", maximum=4096, label="project lean-toolchain")
    )
    if toolchain != manifest.lean_toolchain:
        raise GateProviderError("project lean-toolchain does not match the runtime bundle")
    lock = LakeDependencyLock.from_lake_manifest_bytes(
        _read_regular_file(
            root / "lake-manifest.json",
            maximum=_MAX_PROJECT_MANIFEST_BYTES,
            label="project lake-manifest.json",
        )
    )
    if lock != manifest.lake_lock:
        raise GateProviderError("project Lake dependency identities do not match the runtime bundle")
    if _directory_identity(root) != root_identity:
        raise GateProviderError("project root changed during runtime bundle compatibility validation")


def _bundle_tree_identity(
    bundle_root: str | Path,
    lock: LakeDependencyLock,
    *,
    require_manifest: bool,
    maximum_entries: int,
    maximum_file_bytes: int,
) -> BundleTreeIdentity:
    _positive_limit("runtime bundle entry limit", maximum_entries)
    _positive_limit("runtime bundle byte limit", maximum_file_bytes)
    root = _real_directory(bundle_root, label="runtime bundle root")
    root_identity = _directory_identity(root)
    entries = _collect_tree_entries(
        root,
        lock,
        require_manifest=require_manifest,
        maximum_entries=maximum_entries,
    )
    digest = hashlib.sha256(_TREE_DOMAIN)
    files = 0
    directories = 0
    symlinks = 0
    file_bytes = 0
    for entry in entries:
        _require_entry_identity(entry)
        digest.update(entry.kind.encode("ascii"))
        _hash_field(digest, entry.relative.encode("utf-8"))
        digest.update(stat.S_IMODE(entry.identity[2]).to_bytes(4, "big"))
        if entry.kind == "D":
            directories += 1
        elif entry.kind == "F":
            files += 1
            expected_size = entry.identity[3]
            if expected_size < 0 or file_bytes + expected_size > maximum_file_bytes:
                raise GateProviderError("runtime bundle package files exceed the configured byte limit")
            size, content_sha256 = _hash_regular_file(entry)
            file_bytes += size
            digest.update(size.to_bytes(8, "big"))
            digest.update(bytes.fromhex(content_sha256))
        else:
            symlinks += 1
            link_text = _stable_link_text(entry)
            _validate_resolved_symlink(entry.path, link_text, entry.package_root)
            _hash_field(digest, link_text.encode("utf-8"))
    if _directory_identity(root) != root_identity:
        raise GateProviderError("runtime bundle root changed while its package tree was hashed")
    after = _collect_tree_entries(
        root,
        lock,
        require_manifest=require_manifest,
        maximum_entries=maximum_entries,
    )
    if _tree_snapshot(entries) != _tree_snapshot(after):
        raise GateProviderError("runtime bundle package tree changed while it was hashed")
    return BundleTreeIdentity(
        sha256=digest.hexdigest(),
        entry_count=len(entries),
        file_count=files,
        directory_count=directories,
        symlink_count=symlinks,
        file_bytes=file_bytes,
    )


def _collect_tree_entries(
    root: Path,
    lock: LakeDependencyLock,
    *,
    require_manifest: bool,
    maximum_entries: int,
) -> list[_TreeEntry]:
    package_names = tuple(package.name for package in lock.packages)
    top: dict[str, os.DirEntry[str]] = {}
    try:
        with os.scandir(root) as iterator:
            for entry in iterator:
                if entry.name in top:
                    raise GateProviderError("runtime bundle root contains duplicate entries")
                top[entry.name] = entry
                if len(top) > len(package_names) + 1:
                    raise GateProviderError("runtime bundle root contains entries outside its dependency lock")
    except GateProviderError:
        raise
    except OSError as error:
        raise GateProviderError("runtime bundle root cannot be enumerated") from error
    expected = set(package_names)
    manifest_entry = top.get(RUNTIME_BUNDLE_MANIFEST)
    if require_manifest and manifest_entry is None:
        raise GateProviderError("runtime bundle manifest is missing")
    if manifest_entry is not None:
        try:
            manifest_info = manifest_entry.stat(follow_symlinks=False)
        except OSError as error:
            raise GateProviderError("runtime bundle manifest cannot be inspected") from error
        if not stat.S_ISREG(manifest_info.st_mode):
            raise GateProviderError("runtime bundle manifest must be one regular file")
        if manifest_info.st_nlink != 1:
            raise GateProviderError("runtime bundle manifest must not be hard-linked")
        expected.add(RUNTIME_BUNDLE_MANIFEST)
    if set(top) != expected:
        raise GateProviderError("runtime bundle root contains entries outside its dependency lock")

    entries: list[_TreeEntry] = []
    for package_name in package_names:
        package_root = root / package_name
        package_info = _lstat(package_root, label=f"runtime bundle package {package_name}")
        if not stat.S_ISDIR(package_info.st_mode):
            raise GateProviderError("each runtime bundle package root must be one real directory")
        entries.append(_tree_entry(package_root, package_name, package_root, "D", package_info))
        if len(entries) > maximum_entries:
            raise GateProviderError("runtime bundle package tree exceeds the configured entry limit")
        pending = [package_root]
        while pending:
            directory = pending.pop()
            try:
                iterator = os.scandir(directory)
            except OSError as error:
                raise GateProviderError("runtime bundle package directory cannot be enumerated") from error
            try:
                with iterator:
                    for child in iterator:
                        _path_component(child.name, label="runtime bundle path component")
                        path = directory / child.name
                        info = _lstat(path, label="runtime bundle package entry")
                        relative = path.relative_to(root).as_posix()
                        _relative_path(relative, label="runtime bundle package path")
                        if stat.S_ISDIR(info.st_mode):
                            kind = "D"
                            pending.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            kind = "F"
                        elif stat.S_ISLNK(info.st_mode):
                            kind = "L"
                        else:
                            raise GateProviderError(
                                "runtime bundle package tree contains a device, socket, FIFO, or unsupported entry"
                            )
                        if kind in {"F", "L"} and info.st_nlink != 1:
                            raise GateProviderError(
                                "runtime bundle package files and symbolic links must not be hard-linked"
                            )
                        entries.append(_tree_entry(path, relative, package_root, kind, info))
                        if len(entries) > maximum_entries:
                            raise GateProviderError("runtime bundle package tree exceeds the configured entry limit")
            except GateProviderError:
                raise
            except OSError as error:
                raise GateProviderError("runtime bundle package directory cannot be enumerated") from error
    entries.sort(key=lambda entry: entry.relative.encode("utf-8"))
    return entries


def _tree_entry(
    path: Path,
    relative: str,
    package_root: Path,
    kind: str,
    info: os.stat_result,
) -> _TreeEntry:
    return _TreeEntry(
        path=path,
        relative=relative,
        package_root=package_root,
        kind=kind,
        identity=_stat_identity(info),
    )


def _tree_snapshot(
    entries: list[_TreeEntry],
) -> tuple[tuple[str, str, tuple[int, int, int, int, int, int]], ...]:
    return tuple((entry.relative, entry.kind, entry.identity) for entry in entries)


def _require_entry_identity(entry: _TreeEntry) -> None:
    if _stat_identity(_lstat(entry.path, label="runtime bundle package entry")) != entry.identity:
        raise GateProviderError("runtime bundle package entry changed while it was inspected")


def _hash_regular_file(entry: _TreeEntry) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(entry.path, flags)
    except OSError as error:
        raise GateProviderError("runtime bundle package file cannot be opened safely") from error
    digest = hashlib.sha256()
    size = 0
    failed = False
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != entry.identity or not stat.S_ISREG(opened.st_mode):
            raise GateProviderError("runtime bundle package file changed while it was opened")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > entry.identity[3]:
                raise GateProviderError("runtime bundle package file grew while it was hashed")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(after) != entry.identity:
            raise GateProviderError("runtime bundle package file changed while it was hashed")
    except OSError as error:
        failed = True
        raise GateProviderError("runtime bundle package file cannot be hashed safely") from error
    except BaseException:
        failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if not failed:
                raise GateProviderError("runtime bundle package file could not be closed") from error
    if size != entry.identity[3]:
        raise GateProviderError("runtime bundle package file size changed while it was hashed")
    _require_entry_identity(entry)
    return size, digest.hexdigest()


def _stable_link_text(entry: _TreeEntry) -> str:
    try:
        value = os.readlink(entry.path)
    except OSError as error:
        raise GateProviderError("runtime bundle symbolic link cannot be read") from error
    _link_text(value)
    _require_entry_identity(entry)
    return value


def _validate_resolved_symlink(path: Path, link_text: str, package_root: Path) -> None:
    parent_parts = list(path.parent.relative_to(package_root).parts)
    pending = parent_parts + list(PurePosixPath(link_text).parts)
    resolved: list[str] = []
    seen: set[tuple[int, int]] = set()
    hops = 0
    while pending:
        component = pending.pop(0)
        if component == ".":
            continue
        if component == "..":
            if not resolved:
                raise GateProviderError("runtime bundle symbolic link traverses outside its package root")
            resolved.pop()
            continue
        _path_component(component, label="runtime bundle symbolic link component")
        candidate = package_root.joinpath(*resolved, component)
        info = _lstat(candidate, label="runtime bundle symbolic link target")
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            identity = (info.st_dev, info.st_ino)
            if hops > _MAX_SYMLINK_HOPS or identity in seen:
                raise GateProviderError("runtime bundle symbolic link target contains a cycle")
            seen.add(identity)
            try:
                nested = os.readlink(candidate)
            except OSError as error:
                raise GateProviderError("runtime bundle symbolic link target cannot be read") from error
            _link_text(nested)
            pending = list(PurePosixPath(nested).parts) + pending
            continue
        resolved.append(component)
        if pending and not stat.S_ISDIR(info.st_mode):
            raise GateProviderError("runtime bundle symbolic link target traverses a nondirectory")
    target = package_root.joinpath(*resolved)
    info = _lstat(target, label="runtime bundle symbolic link target")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise GateProviderError("runtime bundle symbolic link target has an unsupported type")


def _validate_tracked_source_paths(paths: Iterable[str]) -> set[str]:
    if isinstance(paths, (str, bytes)):
        raise GateProviderError("tracked source paths must be an exact iterable of paths")
    tracked: set[str] = set()
    try:
        for value in paths:
            if len(tracked) >= _MAX_TRACKED_PATHS:
                raise GateProviderError("tracked source path set exceeds its configured limit")
            _relative_path(value, label="tracked source path")
            if value in tracked:
                raise GateProviderError("tracked source paths must not contain duplicates")
            tracked.add(value)
            if any(part.casefold() == ".lake" for part in PurePosixPath(value).parts):
                raise GateProviderError("source commits must not track .lake")
    except GateProviderError:
        raise
    except (TypeError, ValueError) as error:
        raise GateProviderError("tracked source paths must be an exact iterable of paths") from error
    return tracked


def _parse_lean_toolchain(content: bytes) -> str:
    try:
        value = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GateProviderError("project lean-toolchain is not UTF-8") from error
    if value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or "\r" in value:
        raise GateProviderError("project lean-toolchain must contain exactly one toolchain")
    _lean_toolchain(value)
    return value


def _lean_toolchain(value: object) -> None:
    if (
        not isinstance(value, str)
        or _TOOLCHAIN.fullmatch(value) is None
        or _PINNED_TOOLCHAIN.fullmatch(value) is None
    ):
        raise GateProviderError("Lean toolchain must be one canonical pinned identifier")


def _version_output(label: str, value: object) -> None:
    if not isinstance(value, str) or not value or not value.endswith("\n"):
        raise GateProviderError(f"{label} version output must retain its exact trailing newline")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise GateProviderError(f"{label} version output is not valid UTF-8") from error
    if len(encoded) > _MAX_TOOL_OUTPUT_BYTES or "\r" in value:
        raise GateProviderError(f"{label} version output has an invalid size or newline")
    if any(_is_forbidden_text_character(character, allow_newline=True) for character in value):
        raise GateProviderError(f"{label} version output contains control text")


def _package_name(value: object) -> None:
    if not isinstance(value, str) or _PACKAGE_NAME.fullmatch(value) is None:
        raise GateProviderError("Lake dependency package names must be safe path components")
    _path_component(value, label="Lake dependency package name")


def _canonical_lock_text(label: str, value: object, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not value and not allow_empty):
        qualifier = "bounded" if allow_empty else "nonempty bounded"
        raise GateProviderError(f"{label} must be {qualifier} text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise GateProviderError(f"{label} is not valid UTF-8") from error
    if len(encoded) > _MAX_PATH_BYTES:
        raise GateProviderError(f"{label} must be bounded text")
    if value != unicodedata.normalize("NFC", value) or any(
        _is_forbidden_text_character(character) for character in value
    ):
        raise GateProviderError(f"{label} contains invalid or control text")


def _validate_package_identity(value: dict[str, Any]) -> str:
    _validate_lock_json(value)
    if set(value) != _LAKE_GIT_PACKAGE_FIELDS:
        raise GateProviderError("Lake Git dependency fields do not match the supported schema")
    name = value["name"]
    package_type = value["type"]
    _package_name(name)
    if package_type != "git":
        raise GateProviderError("runtime bundles support only immutable Git Lake dependencies")
    if type(value["inherited"]) is not bool:
        raise GateProviderError("Lake dependency inherited must be a boolean")
    _canonical_lock_text("Lake dependency scope", value["scope"], allow_empty=True)
    _relative_path(value["configFile"], label="Lake dependency configFile")
    for field in ("manifestFile", "subDir"):
        path = value[field]
        if path is not None:
            _relative_path(path, label=f"Lake dependency {field}")
    input_revision = value["inputRev"]
    if input_revision is not None:
        _canonical_lock_text("Lake dependency inputRev", input_revision)
    revision = value["rev"]
    url = value["url"]
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise GateProviderError("each Git Lake dependency must bind one full revision")
    if revision == "0" * len(revision):
        raise GateProviderError("Git Lake dependency revisions must not be all-zero")
    _canonical_lock_text("Git Lake dependency URL", url)
    return name


def _validate_lock_json(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise GateProviderError("Lake dependency identity is nested too deeply")
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise GateProviderError("Lake dependency identity text is not valid UTF-8") from error
        if (
            len(encoded) > _MAX_PATH_BYTES
            or value != unicodedata.normalize("NFC", value)
            or any(_is_forbidden_text_character(character) for character in value)
        ):
            raise GateProviderError("Lake dependency identity contains invalid or control text")
        return
    if isinstance(value, list):
        for item in value:
            _validate_lock_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _canonical_lock_text("Lake dependency identity key", key)
            _validate_lock_json(item, depth=depth + 1)
        return
    raise GateProviderError("Lake dependency identity contains a noncanonical JSON value")


def _relative_path(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise GateProviderError(f"{label} must be nonempty relative UTF-8 text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise GateProviderError(f"{label} is not valid UTF-8") from error
    parsed = PurePosixPath(value)
    if (
        len(encoded) > _MAX_PATH_BYTES
        or parsed.is_absolute()
        or parsed.as_posix() != value
        or not parsed.parts
        or any(part in {".", ".."} for part in parsed.parts)
    ):
        raise GateProviderError(f"{label} must be one canonical relative path")
    for component in parsed.parts:
        _path_component(component, label=label)


def _path_component(value: object, *, label: str) -> None:
    if not isinstance(value, str) or not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise GateProviderError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise GateProviderError(f"{label} is not valid UTF-8") from error
    if (
        len(encoded) > _MAX_COMPONENT_BYTES
        or value != unicodedata.normalize("NFC", value)
        or any(_is_forbidden_text_character(character) for character in value)
    ):
        raise GateProviderError(f"{label} contains invalid or control text")


def _link_text(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise GateProviderError("runtime bundle symbolic links must have nonempty text targets")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise GateProviderError("runtime bundle symbolic link target is not UTF-8") from error
    parsed = PurePosixPath(value)
    if (
        len(encoded) > _MAX_PATH_BYTES
        or parsed.is_absolute()
        or parsed.as_posix() != value
        or "\\" in value
        or value != unicodedata.normalize("NFC", value)
        or any(_is_forbidden_text_character(character) for character in value)
    ):
        raise GateProviderError("runtime bundle symbolic link target is not canonical relative text")
    for component in parsed.parts:
        if component not in {".", ".."}:
            _path_component(component, label="runtime bundle symbolic link component")


def _is_forbidden_text_character(character: str, *, allow_newline: bool = False) -> bool:
    if allow_newline and character in {"\n", "\t"}:
        return False
    return unicodedata.category(character) in {"Cc", "Cf", "Cs"}


def _real_directory(path_value: str | Path, *, label: str) -> Path:
    try:
        path = Path(path_value)
        if path.is_symlink():
            raise GateProviderError(f"{label} must not be a symbolic link")
        resolved = path.expanduser().resolve(strict=True)
        info = resolved.stat(follow_symlinks=False)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    if not stat.S_ISDIR(info.st_mode):
        raise GateProviderError(f"{label} must be one real directory")
    return resolved


def _directory_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    info = _lstat(path, label="directory")
    if not stat.S_ISDIR(info.st_mode):
        raise GateProviderError("directory identity changed")
    return _stat_identity(info)


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.stat(follow_symlinks=False)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_nlink


def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
    _positive_limit(f"{label} byte limit", maximum)
    before = _lstat(path, label=label)
    if not stat.S_ISREG(before.st_mode):
        raise GateProviderError(f"{label} must be one regular file")
    if before.st_nlink != 1:
        raise GateProviderError(f"{label} must not be hard-linked")
    if before.st_size < 0 or before.st_size > maximum:
        raise GateProviderError(f"{label} exceeds its configured byte limit")
    identity = _stat_identity(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be opened safely") from error
    content = bytearray()
    failed = False
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != identity or not stat.S_ISREG(opened.st_mode):
            raise GateProviderError(f"{label} changed while it was opened")
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum:
                raise GateProviderError(f"{label} exceeds its configured byte limit")
        if _stat_identity(os.fstat(descriptor)) != identity:
            raise GateProviderError(f"{label} changed while it was read")
    except OSError as error:
        failed = True
        raise GateProviderError(f"{label} cannot be read safely") from error
    except BaseException:
        failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if not failed:
                raise GateProviderError(f"{label} could not be closed") from error
    if _stat_identity(_lstat(path, label=label)) != identity:
        raise GateProviderError(f"{label} changed while it was read")
    return bytes(content)


def _strict_json_value(content: bytes, *, label: str, maximum: int) -> object:
    if not isinstance(content, bytes) or not content or len(content) > maximum:
        raise GateProviderError(f"{label} has an invalid size")
    try:
        return json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_raise_json_constant,
        )
    except (RecursionError, UnicodeError, ValueError, TypeError) as error:
        raise GateProviderError(f"{label} is not strict JSON") from error


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _raise_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise GateProviderError("runtime bundle evidence is not canonical JSON") from error


def _hash_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _positive_limit(label: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise GateProviderError(f"{label} must be a positive integer")


__all__ = [
    "RUNTIME_BUNDLE_MANIFEST",
    "RUNTIME_BUNDLE_SCHEMA",
    "AutoformReleaseIdentity",
    "BundleTreeIdentity",
    "LakeDependencyLock",
    "LakePackageIdentity",
    "RuntimeBundleManifest",
    "RuntimeToolVersions",
    "build_runtime_bundle_manifest",
    "load_and_verify_runtime_bundle",
    "validate_project_bundle_compatibility",
]
