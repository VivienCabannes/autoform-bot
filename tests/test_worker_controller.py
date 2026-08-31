from __future__ import annotations

from dataclasses import replace

import pytest

from autoform_cli.runtime import RuntimeAssertions, RuntimeGraph, RuntimeNode, RuntimeStatus
from autoform_worker.controller import (
    ControllerError,
    build_task_specs,
    classify_no_work,
    select_ready_task,
)
from autoform_worker.ledger import TaskRecord
from autoform_worker.scheduler import WorkPhase


def _node(
    name: str,
    article_id: str,
    *,
    declaration: str = "theorem",
    state: str = "can_state",
    dispatchable: bool = True,
    formalizable: bool = True,
    mathlib: bool = False,
) -> RuntimeNode:
    stated = state in {"can_prove", "proved", "fully_proved", "defined", "mathlib"}
    proved = state in {"proved", "fully_proved", "defined", "mathlib"}
    return RuntimeNode(
        id=name,
        title=name,
        article_path=f"blueprint/roadmap/{name}.md",
        parent=None,
        depth=0,
        declaration=declaration if formalizable else None,
        formalizable=formalizable,
        dispatchable=dispatchable,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(stated, proved, False),
        status=RuntimeStatus(
            state,
            state == "can_state",
            state == "can_prove",
            stated,
            proved,
            proved,
            state == "not_ready",
        ),
        origin=None,
        source_targets=(),
        lean_targets=(),
        mathlib=mathlib,
        mathlib_declarations=(),
        mathlib_file=None,
        article_id=article_id,
    )


def _runtime(*nodes: RuntimeNode) -> RuntimeGraph:
    return RuntimeGraph(
        "autoform-runtime/v1",
        "markdown-articles",
        "revision",
        "blueprint",
        nodes,
        len(nodes),
        sum(node.formalizable for node in nodes),
        sum(node.dispatchable for node in nodes),
        0,
        0,
    )


def _task(article_id: str, phase: str, status: str = "pending", generation: int = 0) -> TaskRecord:
    return TaskRecord(
        run_id="run",
        article_id=article_id,
        phase=phase,
        status=status,
        attempts=0,
        generation=generation,
        blocked_by=(),
        detail="",
        candidate_oid=None,
        integrated_oid=None,
    )


def test_task_plan_covers_missing_theorem_phases_and_definition_statement() -> None:
    theorem = _node("theorem", "af_000000000000000000000001")
    definition = _node("definition", "af_000000000000000000000002", declaration="def")
    proved = _node("proved", "af_000000000000000000000003", state="fully_proved")
    upstream = _node("upstream", "af_000000000000000000000004", state="mathlib", mathlib=True)
    container = _node(
        "chapter",
        "af_000000000000000000000005",
        formalizable=False,
        dispatchable=False,
    )

    tasks = build_task_specs(_runtime(theorem, definition, proved, upstream, container))

    assert [(task.article_id, task.phase) for task in tasks] == [
        (theorem.article_id, "proof"),
        (theorem.article_id, "statement"),
        (definition.article_id, "statement"),
    ]


def test_task_plan_keeps_not_ready_work_in_completion_denominator() -> None:
    node = _node("deferred", "af_000000000000000000000001", state="not_ready")

    assert [(task.article_id, task.phase) for task in build_task_specs(_runtime(node))] == [
        (node.article_id, "proof"),
        (node.article_id, "statement"),
    ]


def test_select_ready_task_uses_runtime_phase_and_durable_article_id() -> None:
    node = _node("result", "af_000000000000000000000001")
    statement = _task(node.article_id, "statement")
    proof = _task(node.article_id, "proof")

    selected = select_ready_task(_runtime(node), (proof, statement))

    assert selected is not None
    assert selected[0] == statement
    assert selected[1] == node
    assert selected[2] is WorkPhase.STATEMENT


def test_select_ready_task_rejects_regressed_integrated_state() -> None:
    node = _node("result", "af_000000000000000000000001")

    with pytest.raises(ControllerError, match="no longer satisfied"):
        select_ready_task(_runtime(node), (_task(node.article_id, "statement", "integrated"),))


def test_select_ready_task_requires_recovery_before_new_work() -> None:
    first = _node("first", "af_000000000000000000000001", state="can_prove")
    second = _node("second", "af_000000000000000000000002")

    with pytest.raises(ControllerError, match="requires recovery"):
        select_ready_task(
            _runtime(first, second),
            (
                _task(first.article_id, "proof", "candidate"),
                _task(second.article_id, "statement"),
            ),
        )


def test_select_ready_task_detects_incomplete_task_set() -> None:
    node = _node("result", "af_000000000000000000000001")

    with pytest.raises(ControllerError, match="absent from the ledger"):
        select_ready_task(_runtime(node), ())


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("integrated", "integrated"), "complete"),
        (("integrated", "failed"), "failed"),
        (("pending", "stopped"), "stopped"),
        (("candidate", "pending"), "recovery-required"),
        (("pending", "retrying"), "blocked"),
    ],
)
def test_no_work_has_distinct_terminal_classification(statuses: tuple[str, ...], expected: str) -> None:
    tasks = tuple(
        _task(f"af_{index:024x}", "statement", status)
        for index, status in enumerate(statuses, start=1)
    )

    assert classify_no_work(tasks) == expected


def test_planning_requires_durable_article_ids() -> None:
    node = replace(_node("result", "af_000000000000000000000001"), article_id=None)

    with pytest.raises(ControllerError, match="durable article id"):
        build_task_specs(_runtime(node))
