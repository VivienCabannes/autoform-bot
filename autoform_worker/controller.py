"""Durable planning primitives for the Autoform execution controller."""

from __future__ import annotations

from dataclasses import dataclass

from autoform_cli.runtime import RuntimeGraph, RuntimeNode
from autoform_cli.status import DEFINITION_DECLARATIONS

from .ledger import TaskRecord
from .scheduler import WorkPhase, _ready_phase


class ControllerError(RuntimeError):
    """The durable controller cannot safely continue."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


@dataclass(frozen=True, order=True, slots=True)
class TaskSpec:
    """One immutable phase the run must integrate before completion."""

    article_id: str
    phase: str
    node_id: str


def build_task_specs(runtime: RuntimeGraph) -> tuple[TaskSpec, ...]:
    """Derive the complete missing task set from one initial runtime snapshot."""

    tasks: list[TaskSpec] = []
    seen_articles: set[str] = set()
    for node in runtime.nodes:
        article_id = _article_id(node)
        if article_id in seen_articles:
            raise ControllerError("duplicate-article-id", f"duplicate durable article id: {article_id}")
        seen_articles.add(article_id)
        if not node.formalizable or not node.dispatchable or node.mathlib:
            continue
        if not node.status.stated:
            tasks.append(TaskSpec(article_id, WorkPhase.STATEMENT.value, node.id))
        if not _is_definition(node) and not node.status.proved:
            tasks.append(TaskSpec(article_id, WorkPhase.PROOF.value, node.id))
    return tuple(sorted(tasks))


def select_ready_task(
    runtime: RuntimeGraph,
    tasks: tuple[TaskRecord, ...],
) -> tuple[TaskRecord, RuntimeNode, WorkPhase] | None:
    """Select the first runtime-ready durable task, or return no work."""

    nodes = {_article_id(node): node for node in runtime.nodes}
    by_key = {(task.article_id, task.phase): task for task in tasks}
    for task in tasks:
        node = nodes.get(task.article_id)
        if node is None:
            raise ControllerError(
                "task-article-missing",
                f"task article is absent from the current runtime: {task.article_id}",
            )
        if task.status == "integrated" and not _phase_satisfied(node, task.phase):
            raise ControllerError(
                "integrated-task-regressed",
                f"integrated task is no longer satisfied: {task.article_id}:{task.phase}",
            )
        if task.status in {"running", "candidate", "queued"}:
            raise ControllerError(
                "recovery-required",
                f"task requires recovery before scheduling: {task.article_id}:{task.phase}",
            )

    for node in runtime.nodes:
        phase = _ready_phase(node)
        if phase is None:
            continue
        article_id = _article_id(node)
        task = by_key.get((article_id, phase.value))
        if task is None:
            if _phase_satisfied(node, phase.value):
                continue
            raise ControllerError(
                "task-set-incomplete",
                f"runtime-ready phase is absent from the ledger: {article_id}:{phase.value}",
            )
        if task.status in {"pending", "retrying"}:
            return task, node, phase
    return None


def classify_no_work(tasks: tuple[TaskRecord, ...]) -> str:
    """Classify a round with no selectable work without conflating outcomes."""

    if all(task.status == "integrated" for task in tasks):
        return "complete"
    if any(task.status == "failed" for task in tasks):
        return "failed"
    if any(task.status == "stopped" for task in tasks):
        return "stopped"
    if any(task.status in {"running", "candidate", "queued"} for task in tasks):
        return "recovery-required"
    return "blocked"


def _phase_satisfied(node: RuntimeNode, phase: str) -> bool:
    if phase == WorkPhase.STATEMENT.value:
        return node.status.stated
    if phase == WorkPhase.PROOF.value:
        return node.status.proved
    raise ControllerError("task-phase-invalid", f"unknown task phase: {phase!r}")


def _is_definition(node: RuntimeNode) -> bool:
    return (node.declaration or "").casefold() in DEFINITION_DECLARATIONS


def _article_id(node: RuntimeNode) -> str:
    if node.article_id is None:
        raise ControllerError(
            "article-id-required",
            f"autonomous execution requires a durable article id: {node.id}",
        )
    return node.article_id


__all__ = [
    "ControllerError",
    "TaskSpec",
    "build_task_specs",
    "classify_no_work",
    "select_ready_task",
]
