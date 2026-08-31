"""Package-owned root-package artifact audit primitives.

The checked-in workflow helper remains the single implementation of the
archive and kernel-probe policy.  Controller code imports it from the installed
Autoform package instead of executing a helper from the candidate repository.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .templates.github.autoform_audit import (
    AuditInputError,
    modules_from_archive,
    mathlib_modules_from_lake,
    render_probe,
    root_package_from_config,
    targets_from_blueprint,
)


@dataclass(frozen=True, slots=True)
class RootPackageAudit:
    """Stable summary of one prepared root-package kernel audit."""

    root_package: str
    modules: tuple[str, ...]
    target_count: int
    mathlib_modules: tuple[str, ...]
    evaluated_config_sha256: str
    archive_sha256: str
    probe_sha256: str

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["modules"] = list(self.modules)
        value["mathlib_modules"] = list(self.mathlib_modules)
        return value


def prepare_root_package_audit(
    root_package: str,
    evaluated_config: Path,
    archive: Path,
    blueprint: Path,
    lean_root: Path,
    output_probe: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> RootPackageAudit:
    """Validate package artifacts and write the package-owned Lean probe."""

    evaluated_config_sha256 = _stable_sha256(evaluated_config, "evaluated Lake configuration")
    archive_sha256 = _stable_sha256(archive, "root-package build archive")
    modules = modules_from_archive(archive, root_package)
    targets = targets_from_blueprint(blueprint)
    mathlib_modules = mathlib_modules_from_lake(
        lean_root,
        targets,
        runner=runner,
        environment=environment,
    )
    if _stable_sha256(archive, "root-package build archive") != archive_sha256:
        raise AuditInputError("root-package build archive changed during artifact validation")
    probe_bytes = render_probe(modules, targets, mathlib_modules).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(output_probe, flags, 0o600)
    except OSError as error:
        raise AuditInputError(f"cannot create root-package audit probe: {error}") from error
    try:
        offset = 0
        while offset < len(probe_bytes):
            written = os.write(descriptor, probe_bytes[offset:])
            if written <= 0:
                raise AuditInputError("could not finish writing root-package audit probe")
            offset += written
    finally:
        os.close(descriptor)
    probe_sha256 = _stable_sha256(output_probe, "root-package audit probe")
    result = RootPackageAudit(
        root_package=root_package,
        modules=modules,
        target_count=len(targets),
        mathlib_modules=mathlib_modules,
        evaluated_config_sha256=evaluated_config_sha256,
        archive_sha256=archive_sha256,
        probe_sha256=probe_sha256,
    )
    verify_root_package_audit(result, evaluated_config, archive, output_probe)
    return result


def verify_root_package_audit(
    result: RootPackageAudit,
    evaluated_config: Path,
    archive: Path,
    probe: Path,
) -> None:
    """Fail if any evidence-bearing artifact changed after preparation."""

    if root_package_from_config(evaluated_config) != result.root_package:
        raise AuditInputError("evaluated Lake root package changed during artifact validation")
    expected = {
        "evaluated Lake configuration": (evaluated_config, result.evaluated_config_sha256),
        "root-package build archive": (archive, result.archive_sha256),
        "root-package audit probe": (probe, result.probe_sha256),
    }
    for label, (path, digest) in expected.items():
        if _stable_sha256(path, label) != digest:
            raise AuditInputError(f"{label} changed after artifact validation")


def _stable_sha256(path: Path, label: str) -> str:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise AuditInputError(f"cannot inspect {label}: {error}") from error
    if stat.S_ISLNK(path_status.st_mode):
        raise AuditInputError(f"{label} must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AuditInputError(f"cannot open {label} as a regular file: {error}") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AuditInputError(f"{label} is not a regular file")
        if (before.st_dev, before.st_ino) != (path_status.st_dev, path_status.st_ino):
            raise AuditInputError(f"{label} changed before it was opened")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature:
        raise AuditInputError(f"{label} changed while it was read")
    return digest.hexdigest()


__all__ = [
    "AuditInputError",
    "RootPackageAudit",
    "prepare_root_package_audit",
    "root_package_from_config",
    "verify_root_package_audit",
]
