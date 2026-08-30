"""Build the immutable input contract for autonomous Autoform execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .coverage import COVERAGE_V2_SCHEMA, CoverageSummary, load_coverage
from .runtime import RuntimeGraph, RuntimeProjectionError, load_runtime_graph, resolve_runtime_paths

EXECUTION_INPUT_SCHEMA = "autoform-execution-input/v1"


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
        first_runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    except RuntimeProjectionError as error:
        raise ExecutionInputError(
            [ExecutionInputIssue("runtime-invalid", reason) for reason in error.issues]
        ) from error
    first_coverage = _require_v2_coverage(paths.blueprint_dir)

    # Re-read both authorities around each other. A concurrent edit then
    # becomes an explicit refusal instead of a runtime/coverage hybrid.
    try:
        second_runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    except RuntimeProjectionError as error:
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "execution-input-changed",
                    f"runtime authority changed while the execution input was read: {reason}",
                )
                for reason in error.issues
            ]
        ) from error
    second_coverage = _require_v2_coverage(paths.blueprint_dir)
    if (
        first_runtime.to_json() != second_runtime.to_json()
        or first_coverage.to_json() != second_coverage.to_json()
    ):
        raise ExecutionInputError(
            [
                ExecutionInputIssue(
                    "execution-input-changed",
                    "runtime or coverage authority changed while the execution input was read",
                )
            ]
        )

    runtime_json = second_runtime.to_json()
    return ExecutionInput(
        schema=EXECUTION_INPUT_SCHEMA,
        runtime=second_runtime,
        runtime_sha256=hashlib.sha256(runtime_json.encode("utf-8")).hexdigest(),
        coverage_schema=second_coverage.schema,
        coverage_path=second_coverage.source_path,
        coverage_sha256=second_coverage.source_sha256,
        artifact_path=_required(second_coverage.artifact_path),
        artifact_sha256=_required(second_coverage.artifact_sha256),
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
            for unit in second_coverage.units
        ),
        node_bindings=tuple(
            ExecutionNodeBinding(binding.node_id, binding.unit)
            for binding in second_coverage.node_bindings
        ),
    )


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
