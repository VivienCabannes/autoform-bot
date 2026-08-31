from __future__ import annotations

import hashlib
import threading
from dataclasses import replace

from autoform_cli.execution_input import ExecutionInput
from autoform_cli.runtime import RuntimeAssertions, RuntimeGraph, RuntimeNode, RuntimeStatus
from autoform_worker.scheduler import AttemptResult, Scheduler, WorkPhase


def _node(
    node_id: str,
    *,
    stated: bool = False,
    can_state: bool = True,
    can_prove: bool = False,
) -> RuntimeNode:
    return RuntimeNode(
        id=node_id,
        article_id=f"af_{hashlib.sha256(node_id.encode()).hexdigest()[:24]}",
        title=node_id.title(),
        article_path=f"blueprint/roadmap/{node_id}.md",
        parent=None,
        depth=0,
        declaration="theorem",
        formalizable=True,
        dispatchable=True,
        statement_dependencies=(),
        proof_dependencies=(),
        dependencies=(),
        assertions=RuntimeAssertions(
            statement_formalized=stated,
            proof_formalized=False,
            not_ready=False,
        ),
        status=RuntimeStatus(
            state="can_prove" if can_prove else "can_state",
            can_state=can_state,
            can_prove=can_prove,
            stated=stated,
            proved=False,
            fully_proved=False,
            defined=False,
        ),
        origin=None,
        source_targets=(),
        lean_targets=(),
        mathlib=False,
        mathlib_declarations=(),
        mathlib_file=None,
    )


def _runtime(revision: str, *nodes: RuntimeNode) -> RuntimeGraph:
    return RuntimeGraph(
        schema="autoform-runtime/v1",
        authority="markdown-articles",
        source_revision=revision,
        blueprint_path="blueprint",
        nodes=nodes,
        article_count=len(nodes),
        formalizable_count=len(nodes),
        dispatchable_count=len(nodes),
        dependency_count=0,
        maximum_depth=0,
    )


def _input(revision: str, coverage_sha256: str, *nodes: RuntimeNode) -> ExecutionInput:
    return ExecutionInput(
        schema="autoform-execution-input/v1",
        runtime=_runtime(revision, *nodes),
        authority_sha256="a" * 64,
        runtime_sha256="b" * 64,
        lean_source_revision=None,
        coverage_schema="autoform-coverage/v2",
        coverage_path="coverage/README.md",
        coverage_sha256=coverage_sha256,
        artifact_path="sources/book.md",
        artifact_sha256="d" * 64,
        units=(),
        node_bindings=(),
    )


class _Heartbeat:
    def __init__(self) -> None:
        self.lost = threading.Event()

    def __enter__(self) -> _Heartbeat:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _Board:
    def __init__(self, on_acquire) -> None:
        self._on_acquire = on_acquire
        self.released: list[str] = []
        self.heartbeat_keys: list[str] = []

    def prepare_v2_claim(self, canonical_key, compatibility_keys, *, canonical_keys=()):
        return True

    def acquire(self, key: str, ttl: int | float = 1500, steal: bool = False, note: str = "") -> bool:
        self._on_acquire()
        return True

    def release(self, key: str) -> bool:
        self.released.append(key)
        return True

    def heartbeat(self, key: str, *, interval: float = 300, ttl: int | float = 1500) -> _Heartbeat:
        self.heartbeat_keys.append(key)
        return _Heartbeat()


def test_run_once_executes_refreshed_node_after_claim_acquisition() -> None:
    original = _node("target")
    refreshed = _node("target")
    current = [_runtime("revision-1", original)]
    executed = []

    def refresh_during_acquire() -> None:
        current[0] = _runtime("revision-2", refreshed)

    scheduler = Scheduler(
        lambda: current[0],
        _Board(refresh_during_acquire),
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert result.item is not None
    assert result.item.node is refreshed
    assert result.item.source_revision == "revision-2"
    assert executed == [result.item]


def test_run_once_does_not_execute_when_claimed_node_disappears() -> None:
    current = [_runtime("revision-1", _node("target"))]
    board = _Board(lambda: current.__setitem__(0, _runtime("revision-2")))
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert not result.progressed
    assert "no longer exists" in result.detail
    assert executed == []
    assert board.heartbeat_keys == []
    assert len(board.released) == 1


def test_run_once_does_not_execute_when_claimed_phase_changes() -> None:
    current = [_runtime("revision-1", _node("target"))]
    board = _Board(
        lambda: current.__setitem__(
            0,
            _runtime("revision-2", _node("target", stated=True, can_state=False, can_prove=True)),
        )
    )
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert not result.progressed
    assert "phase changed from statement to proof" in result.detail
    assert executed == []
    assert scheduler.record("target").attempts == 0
    assert scheduler.ready_items(current[0])[0].phase is WorkPhase.PROOF


def test_run_once_does_not_execute_when_claimed_article_id_changes() -> None:
    original = _node("target")
    changed = replace(original, article_id="af_aaaaaaaaaaaaaaaaaaaaaaaa")
    current = [_runtime("revision-1", original)]
    board = _Board(lambda: current.__setitem__(0, _runtime("revision-2", changed)))
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert not result.progressed
    assert "changed durable article_id" in result.detail
    assert executed == []
    assert board.heartbeat_keys == []
    assert len(board.released) == 1


def test_run_once_does_not_execute_when_source_contract_changes_after_claim() -> None:
    original = _node("target")
    current = [_input("revision-1", "c" * 64, original)]
    board = _Board(
        lambda: current.__setitem__(0, _input("revision-2", "e" * 64, original))
    )
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once()

    assert not result.progressed
    assert result.detail == "claimed work source-coverage contract changed"
    assert executed == []
    assert board.heartbeat_keys == []
    assert len(board.released) == 1


def test_run_once_does_not_execute_when_another_article_changes_after_claim() -> None:
    target = replace(_node("target"), source_sha256="1" * 64)
    sibling = replace(_node("sibling"), source_sha256="2" * 64)
    changed_sibling = replace(sibling, source_sha256="3" * 64)
    current = [_input("revision-1", "c" * 64, target, sibling)]
    board = _Board(
        lambda: current.__setitem__(
            0,
            _input("revision-2", "c" * 64, target, changed_sibling),
        )
    )
    executed = []
    scheduler = Scheduler(
        lambda: current[0],
        board,
        lambda item, cancelled: executed.append(item) or AttemptResult.succeeded(),
        claim_ttl=60,
        heartbeat_interval=5,
    )

    result = scheduler.run_once(node_id="target")

    assert not result.progressed
    assert result.detail == "roadmap outside the claimed article changed"
    assert executed == []
