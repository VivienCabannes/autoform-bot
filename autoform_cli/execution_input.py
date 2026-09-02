"""Build the immutable input contract for autonomous Autoform execution."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .coverage import (
    COVERAGE_V2_SCHEMA,
    CoverageSummary,
    _roadmap_source_provenance,
    load_coverage,
)
from .graph import GraphValidationError
from .lean import SourceIndex, project_source_revision, snapshot_project_sources
from .runtime import RuntimeGraph, RuntimeProjectionError, load_runtime_graph, resolve_runtime_paths
from .workspace import _path_contains_symlink
from .workspace_manifest import WorkspaceError

# V3 replaces V2's whole-manifest workspace digest with a selected-project
# binding. A controller must not reinterpret a V2 digest under the new meaning.
EXECUTION_INPUT_SCHEMA = "autoform-execution-input/v3"
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
class _ExecutionAuthorityRevision:
    """Digests needed to prove loader results came from this generation."""

    sha256: str
    runtime_source_revision: str
    roadmap_sha256: str
    coverage_sha256: str | None
    source_sha256s: tuple[tuple[str, str], ...]
    lean_source_revision: str | None


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
    authority_sha256: str | None = None
    lean_source_revision: str | None = None
    workspace_project_id: str | None = None
    workspace_project_binding_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact": {
                "path": self.artifact_path,
                "sha256": self.artifact_sha256,
            },
            "authority_sha256": self.authority_sha256,
            "coverage": {
                "path": self.coverage_path,
                "schema": self.coverage_schema,
                "sha256": self.coverage_sha256,
            },
            "node_bindings": [binding.as_dict() for binding in self.node_bindings],
            "runtime": self.runtime.as_dict(),
            "runtime_sha256": self.runtime_sha256,
            "lean_source_revision": self.lean_source_revision,
            "schema": self.schema,
            "units": [unit.as_dict() for unit in self.units],
            "workspace": {
                "blueprint_path": self.runtime.blueprint_path,
                "project_binding_sha256": self.workspace_project_binding_sha256,
                "project_id": self.workspace_project_id,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @property
    def source_contract_sha256(self) -> str:
        """Hash the immutable source-coverage contract without progress state."""

        payload = {
            "artifact": {"path": self.artifact_path, "sha256": self.artifact_sha256},
            "coverage": {
                "path": self.coverage_path,
                "schema": self.coverage_schema,
                "sha256": self.coverage_sha256,
            },
            "node_bindings": [binding.as_dict() for binding in self.node_bindings],
            "units": [unit.as_dict() for unit in self.units],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def load_execution_input(
    project_or_blueprint: str | Path,
    *,
    lean_root: str | Path | None = None,
    project_id: str | None = None,
) -> ExecutionInput:
    """Read a stable runtime and exhaustive coverage snapshot, or fail closed."""

    if lean_root is None:
        resolved_lean_root = None
    else:
        requested_lean_root = Path(lean_root).expanduser()
        try:
            if _path_contains_symlink(requested_lean_root.absolute()):
                raise WorkspaceError(["Lean root path contains a symbolic link"])
            resolved_lean_root = requested_lean_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError, WorkspaceError):
            raise ExecutionInputError(
                [ExecutionInputIssue("lean-root-unsafe", "Lean root path cannot be resolved safely")]
            ) from None
        if not resolved_lean_root.is_dir():
            raise ExecutionInputError(
                [ExecutionInputIssue("lean-root-unsafe", "Lean root path is not a directory")]
            )
    paths = None
    runtime: RuntimeGraph | None = None
    coverage: CoverageSummary | None = None
    authority: _ExecutionAuthorityRevision | None = None
    for _ in range(_EXECUTION_INPUT_READ_ATTEMPTS):
        try:
            paths = resolve_runtime_paths(project_or_blueprint, project_id=project_id)
        except (GraphValidationError, RuntimeProjectionError) as error:
            raise ExecutionInputError(
                [ExecutionInputIssue("runtime-invalid", reason) for reason in error.issues]
            ) from error
        except OSError:
            continue
        if paths.workspace_managed and paths.workspace_project_id is None:
            raise ExecutionInputError(
                [
                    ExecutionInputIssue(
                        "workspace-project-required",
                        "autonomous execution requires a registered workspace project",
                    )
                ]
            )
        binding = (
            paths.project_root,
            paths.blueprint_dir,
            paths.workspace_project_id,
            paths.workspace_project_binding_sha256,
        )
        before: _ExecutionAuthorityRevision | None = None
        lean_index: SourceIndex | None = None
        try:
            before = _execution_authority_revision(paths.blueprint_dir, resolved_lean_root)
            if resolved_lean_root is not None:
                lean_snapshot = snapshot_project_sources(resolved_lean_root)
                if lean_snapshot.revision != before.lean_source_revision:
                    continue
                lean_index = lean_snapshot.index
            runtime = load_runtime_graph(
                paths.blueprint_dir,
                lean_root=lean_root,
            )
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
        if (
            before != between
            or runtime.source_revision != before.runtime_source_revision
            or not _runtime_matches_lean_index(runtime, lean_index)
        ):
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
        try:
            final_paths = resolve_runtime_paths(project_or_blueprint, project_id=project_id)
        except (GraphValidationError, RuntimeProjectionError, OSError):
            continue
        final_binding = (
            final_paths.project_root,
            final_paths.blueprint_dir,
            final_paths.workspace_project_id,
            final_paths.workspace_project_binding_sha256,
        )
        expected_blueprint_path = paths.blueprint_dir.relative_to(paths.project_root).as_posix()
        if (
            before == between == after
            and binding == final_binding
            and runtime.blueprint_path == expected_blueprint_path
            and _coverage_matches_authority(coverage, before)
        ):
            authority = after
            break
    else:
        raise _changed_execution_input()

    assert paths is not None and runtime is not None and coverage is not None and authority is not None
    missing_article_ids = tuple(node.id for node in runtime.nodes if node.article_id is None)
    if missing_article_ids:
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "article-id-required",
                    "autonomous execution requires durable article_id frontmatter on every "
                    f"roadmap article; missing: {', '.join(missing_article_ids)}",
                )
            ]
        )
    runtime_json = runtime.to_json()
    return ExecutionInput(
        schema=EXECUTION_INPUT_SCHEMA,
        runtime=runtime,
        authority_sha256=authority.sha256,
        runtime_sha256=hashlib.sha256(runtime_json.encode("utf-8")).hexdigest(),
        lean_source_revision=authority.lean_source_revision,
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
        workspace_project_id=paths.workspace_project_id,
        workspace_project_binding_sha256=paths.workspace_project_binding_sha256,
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
    blueprint: Path,
    lean_root: Path | None,
    expected: _ExecutionAuthorityRevision | None,
) -> bool:
    if expected is None:
        return True
    try:
        return _execution_authority_revision(blueprint, lean_root) != expected
    except OSError:
        return True


def _execution_authority_revision(
    blueprint: Path,
    lean_root: Path | None,
) -> _ExecutionAuthorityRevision:
    """Identify every file that can affect runtime or exhaustive coverage.

    Boundary equality detects ordinary concurrent edits. The component digests
    also authenticate each loader result, so an A-to-B-to-A edit wholly inside
    one loader cannot hide behind equal endpoint hashes. A changed or unreadable
    generation is retried only a fixed number of times.
    """

    digest = hashlib.sha256(b"autoform-execution-authority/v1\0")
    runtime_digest = hashlib.sha256(b"autoform-runtime-source/v1\0")
    roadmap_sources: list[tuple[str, str]] = []
    source_sha256s: list[tuple[str, str]] = []
    coverage_sha256: str | None = None
    first = _authority_entries(blueprint)
    for path, relative in first:
        data = _stable_authority_bytes(path)
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        if not data.startswith(b"F"):
            continue
        file_bytes = data[1:]
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        if relative == Path("coverage/README.md"):
            coverage_sha256 = file_sha256
        if relative.parts[:1] == ("sources",):
            source_sha256s.append((relative.as_posix(), file_sha256))
        if relative.parts[:1] == ("roadmap",) and relative.suffix == ".md":
            roadmap_sources.append((relative.as_posix(), file_sha256))
            article_path = relative.as_posix().encode("utf-8")
            runtime_digest.update(len(article_path).to_bytes(8, "big"))
            runtime_digest.update(article_path)
            runtime_digest.update(len(file_bytes).to_bytes(8, "big"))
            runtime_digest.update(file_bytes)
    if first != _authority_entries(blueprint):
        raise OSError("execution authority changed while it was enumerated")
    lean_source_revision: str | None = None
    if lean_root is not None:
        lean_source_revision = project_source_revision(lean_root)
        lean_revision = lean_source_revision.encode("ascii")
        digest.update(len(lean_revision).to_bytes(8, "big"))
        digest.update(lean_revision)
    return _ExecutionAuthorityRevision(
        sha256=digest.hexdigest(),
        runtime_source_revision=runtime_digest.hexdigest(),
        roadmap_sha256=_roadmap_source_provenance(roadmap_sources),
        coverage_sha256=coverage_sha256,
        source_sha256s=tuple(sorted(source_sha256s)),
        lean_source_revision=lean_source_revision,
    )


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
    entries = tuple((path, path.relative_to(blueprint)) for path in paths)
    return tuple(sorted(entries, key=lambda entry: _authority_entry_sort_key(entry[1])))


def _authority_entry_sort_key(relative: Path) -> tuple[int, str]:
    """Put roadmap files in the node order used by RuntimeGraph provenance."""

    if relative.parts[:1] != ("roadmap",) or relative.suffix != ".md":
        return 1, relative.as_posix()
    article = relative.relative_to("roadmap")
    if article.name == "README.md":
        node_id = article.parent.as_posix()
        if node_id == ".":
            node_id = "roadmap"
    else:
        node_id = article.with_suffix("").as_posix()
    return 0, node_id


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


def _coverage_matches_authority(
    coverage: CoverageSummary,
    authority: _ExecutionAuthorityRevision,
) -> bool:
    artifact_sha256s = dict(authority.source_sha256s)
    return (
        coverage.source_sha256 == authority.coverage_sha256
        and coverage._roadmap_sha256 == authority.roadmap_sha256
        and coverage.artifact_path is not None
        and coverage.artifact_sha256 == artifact_sha256s.get(coverage.artifact_path)
    )


def _runtime_matches_lean_index(runtime: RuntimeGraph, index: SourceIndex | None) -> bool:
    if index is None:
        return True
    for node in runtime.nodes:
        for target in node.lean_targets:
            declaration = index.find(target.declaration)
            expected = declaration.path.as_posix() if declaration is not None else None
            if target.source_file != expected:
                return False
    return True


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
    if not coverage.complete:
        mapped_count = coverage.counts["MAPPED"]
        subject = "unit remains" if mapped_count == 1 else "units remain"
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "coverage-incomplete",
                    "autonomous execution requires a terminal coverage disposition for every "
                    f"v2 source unit; {mapped_count} {subject} MAPPED",
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
