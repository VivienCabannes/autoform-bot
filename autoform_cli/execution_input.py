"""Build the immutable input contract for autonomous Autoform execution."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .coverage import COVERAGE_V2_SCHEMA, CoverageSummary, load_coverage
from .graph import GraphValidationError
from .lean import project_source_revision
from .runtime import RuntimeGraph, RuntimeProjectionError, load_runtime_graph, resolve_runtime_paths

EXECUTION_INPUT_SCHEMA = "autoform-execution-input/v1"
_EXECUTION_INPUT_READ_ATTEMPTS = 3


@dataclass(frozen=True, order=True, slots=True)
class ExecutionInputIssue:
    """One stable reason an execution snapshot could not be built."""

    code: str
    reason: str


class ExecutionInputError(ValueError):
    """The authored project cannot supply a safe autonomous input snapshot."""

    def __init__(self, issues: tuple[ExecutionInputIssue, ...] | list[ExecutionInputIssue]) -> None:
        self.issues = tuple(sorted(set(issues)))
        super().__init__("; ".join(f"{issue.code}: {issue.reason}" for issue in self.issues))


@dataclass(frozen=True, slots=True)
class ExecutionSourceUnit:
    """One source unit copied from the validated v2 coverage contract."""

    unit: str
    area: str
    start_line: int
    end_line: int
    locator: str
    unit_sha256: str
    disposition: str
    evidence: str
    roadmap_nodes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["roadmap_nodes"] = list(self.roadmap_nodes)
        return result


@dataclass(frozen=True, order=True, slots=True)
class ExecutionNodeBinding:
    """One validated reciprocal roadmap binding."""

    node_id: str
    unit: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionInput:
    """A deterministic snapshot suitable for a durable execution ledger."""

    schema: str
    runtime: RuntimeGraph
    runtime_sha256: str
    coverage_schema: str
    coverage_path: str
    coverage_sha256: str
    artifact_path: str
    artifact_sha256: str
    units: tuple[ExecutionSourceUnit, ...]
    node_bindings: tuple[ExecutionNodeBinding, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact": {
                "path": self.artifact_path,
                "sha256": self.artifact_sha256,
            },
            "coverage": {
                "path": self.coverage_path,
                "schema": self.coverage_schema,
                "sha256": self.coverage_sha256,
            },
            "node_bindings": [binding.as_dict() for binding in self.node_bindings],
            "runtime": self.runtime.as_dict(),
            "runtime_sha256": self.runtime_sha256,
            "schema": self.schema,
            "units": [unit.as_dict() for unit in self.units],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def load_execution_input(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
) -> ExecutionInput:
    """Read a stable runtime and exhaustive coverage snapshot, or fail closed."""

    try:
        paths = resolve_runtime_paths(project_or_blueprint)
    except (GraphValidationError, RuntimeProjectionError) as error:
        raise ExecutionInputError(
            [ExecutionInputIssue("runtime-invalid", reason) for reason in error.issues]
        ) from error

    resolved_lean_root = Path(lean_root).expanduser().resolve() if lean_root is not None else None
    runtime: RuntimeGraph | None = None
    coverage: CoverageSummary | None = None
    for _ in range(_EXECUTION_INPUT_READ_ATTEMPTS):
        before: str | None = None
        try:
            before = _execution_authority_revision(paths.blueprint_dir, resolved_lean_root)
            runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
        except (GraphValidationError, RuntimeProjectionError) as error:
            if _authority_changed(paths.blueprint_dir, resolved_lean_root, before):
                continue
            raise ExecutionInputError(
                [ExecutionInputIssue("runtime-invalid", reason) for reason in error.issues]
            ) from error
        except OSError:
            continue

        try:
            between = _execution_authority_revision(paths.blueprint_dir, resolved_lean_root)
        except OSError:
            continue
        if before != between:
            continue

        try:
            coverage = _require_v2_coverage(paths.blueprint_dir)
        except ExecutionInputError:
            if _authority_changed(paths.blueprint_dir, resolved_lean_root, between):
                continue
            raise
        try:
            after = _execution_authority_revision(paths.blueprint_dir, resolved_lean_root)
        except OSError:
            continue
        if before == between == after:
            break
    else:
        raise _changed_execution_input()

    assert runtime is not None and coverage is not None
    runtime_json = runtime.to_json()
    return ExecutionInput(
        schema=EXECUTION_INPUT_SCHEMA,
        runtime=runtime,
        runtime_sha256=hashlib.sha256(runtime_json.encode("utf-8")).hexdigest(),
        coverage_schema=coverage.schema,
        coverage_path=coverage.source_path,
        coverage_sha256=coverage.source_sha256,
        artifact_path=_required(coverage.artifact_path),
        artifact_sha256=_required(coverage.artifact_sha256),
        units=tuple(
            ExecutionSourceUnit(
                unit=unit.unit,
                area=unit.area,
                start_line=unit.start_line,
                end_line=unit.end_line,
                locator=unit.locator,
                unit_sha256=unit.unit_sha256,
                disposition=unit.disposition,
                evidence=unit.evidence,
                roadmap_nodes=unit.roadmap_nodes,
            )
            for unit in coverage.units
        ),
        node_bindings=tuple(
            ExecutionNodeBinding(binding.node_id, binding.unit)
            for binding in coverage.node_bindings
        ),
    )


def _changed_execution_input() -> ExecutionInputError:
    return ExecutionInputError(
        [
            ExecutionInputIssue(
                "execution-input-changed",
                "runtime or coverage authority kept changing while the execution input was read",
            )
        ]
    )


def _authority_changed(
    blueprint: Path, lean_root: Path | None, expected: str | None
) -> bool:
    if expected is None:
        return True
    try:
        return _execution_authority_revision(blueprint, lean_root) != expected
    except OSError:
        return True


def _execution_authority_revision(blueprint: Path, lean_root: Path | None) -> str:
    """Hash every file that can affect runtime or exhaustive coverage.

    The digest brackets each authority loader. Reading before, between, and
    after the loaders supplies a stable interval in which both results describe
    one generation. A changed or unreadable generation is retried only a fixed
    number of times.
    """

    digest = hashlib.sha256(b"autoform-execution-authority/v1\0")
    first = _authority_entries(blueprint)
    for path, relative in first:
        data = _stable_authority_bytes(path)
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    if first != _authority_entries(blueprint):
        raise OSError("execution authority changed while it was enumerated")
    if lean_root is not None:
        lean_revision = project_source_revision(lean_root).encode("ascii")
        digest.update(len(lean_revision).to_bytes(8, "big"))
        digest.update(lean_revision)
    return digest.hexdigest()


def _authority_entries(blueprint: Path) -> tuple[tuple[Path, Path], ...]:
    paths = {blueprint / "coverage" / "README.md"}
    for root, suffix in (
        (blueprint / "roadmap", ".md"),
        (blueprint / "sources", None),
    ):
        try:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    continue
                if suffix is None or path.suffix == suffix:
                    paths.add(path)
        except OSError as error:
            raise OSError("execution authority changed while it was enumerated") from error
    return tuple(sorted((path, path.relative_to(blueprint)) for path in paths))


def _stable_authority_bytes(path: Path) -> bytes:
    """Read one authority entry without accepting a replacement mid-read."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return b"M"
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(path).encode("utf-8", errors="surrogateescape")
        current = path.lstat()
        if (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns) != (
            current.st_dev,
            current.st_ino,
            current.st_mtime_ns,
        ):
            raise OSError("execution authority link changed while it was read")
        return b"L" + target
    if not stat.S_ISREG(metadata.st_mode):
        return b"N" + metadata.st_mode.to_bytes(8, "big")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
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
    if before_signature != after_signature or (after.st_dev, after.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise OSError("execution authority changed while it was read")
    return b"F" + b"".join(chunks)


def _require_v2_coverage(blueprint: Path) -> CoverageSummary:
    coverage, issues = load_coverage(blueprint)
    if issues:
        raise ExecutionInputError(
            [ExecutionInputIssue(issue.code, issue.reason) for issue in issues]
        )
    if coverage is None or coverage.schema != COVERAGE_V2_SCHEMA:
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "coverage-v2-required",
                    "autonomous execution requires an exhaustive autoform-coverage/v2 contract",
                )
            ]
        )
    return coverage


def _required(value: str | None) -> str:
    if value is None:  # Defensive: a valid v2 summary always carries both.
        raise ExecutionInputError(
            [ExecutionInputIssue("coverage-v2-invalid", "v2 coverage binding is incomplete")]
        )
    return value


__all__ = [
    "EXECUTION_INPUT_SCHEMA",
    "ExecutionInput",
    "ExecutionInputError",
    "ExecutionInputIssue",
    "ExecutionNodeBinding",
    "ExecutionSourceUnit",
    "load_execution_input",
]
