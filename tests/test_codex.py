"""Codex adapter tests — exercised with an injected fake runner (NO live ``codex``).

Mirrors the Claude adapter tests in ``test_prover.py``: a scripted JSONL stream is
fed through the adapter's ``runner`` seam so the full start → events → steer →
result path is covered without spawning any process.
"""
from __future__ import annotations

import json

from servers.prover.base import EventKind
from servers.prover.codex_adapter import (
    CODEX_SYSTEM_PROMPT,
    CodexAdapter,
    _classify_codex_event,
)
from servers.prover.driver import prove


def _lines(*objs: dict) -> list[str]:
    """Scripted codex ``exec --json`` JSONL for one turn."""
    return [json.dumps(o) for o in objs]


class FakeCodexRunner:
    """Injectable runner: yields scripted JSONL per turn; records each call."""

    def __init__(self, turns: list[list[str]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict] = []

    def __call__(self, args, env, cwd, deadline=None):
        self.calls.append({"args": args, "env": env, "cwd": cwd, "deadline": deadline})
        idx = len(self.calls) - 1
        yield from (self.turns[idx] if idx < len(self.turns) else [])


class _StubSteerer:
    """Minimal steerer: off-course exactly once, then quiet (no live judge call)."""

    def __init__(self) -> None:
        self._fired = False

    def off_course(self, goal, window) -> bool:
        if not self._fired:
            self._fired = True
            return True
        return False

    def correction(self, goal, window) -> str:
        return "focus on the inductive step"


def test_codex_classify_events():
    ev, final, sid = _classify_codex_event({"type": "thread.started", "thread_id": "sess-1"})
    assert ev is None and sid == "sess-1"
    ev, final, _ = _classify_codex_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "proved it"}})
    assert ev.kind is EventKind.MESSAGE and final == "proved it"
    ev, _, _ = _classify_codex_event(
        {"type": "item.completed", "item": {"type": "command_execution", "command": "lake build"}})
    assert ev.kind is EventKind.TOOL
    ev, _, _ = _classify_codex_event(
        {"type": "item.completed", "item": {"type": "file_change", "text": "Foo.lean"}})
    assert ev.kind is EventKind.EDIT
    ev, _, _ = _classify_codex_event({"type": "error", "message": "boom"})
    assert ev.kind is EventKind.ERROR


def test_codex_adapter_single_turn_proves():
    turns = [_lines(
        {"type": "thread.started", "thread_id": "sess-1"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "lake build"}},
        {"type": "item.completed", "item": {"type": "agent_message",
         "text": "theorem foo : 1 = 1 := rfl\nCompiled cleanly, no sorry."}},
    )]
    runner = FakeCodexRunner(turns)
    adapter = CodexAdapter(runner=runner)
    result = prove(adapter, "Foo", "prove 1=1", "/tmp/proj", max_steers=0, verifier=None)
    assert result.status == "proved"
    assert result.backend == "codex"
    assert result.meta["session_id"] == "sess-1"
    # autonomy flag + json mode in the invocation
    assert "--json" in runner.calls[0]["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.calls[0]["args"]


def test_codex_adapter_reports_honest_failed():
    turns = [_lines(
        {"type": "item.completed", "item": {"type": "agent_message",
         "text": "Tried nlinarith and ring.\nFAILED — could not discharge the inductive step"}},
    )]
    adapter = CodexAdapter(runner=FakeCodexRunner(turns))
    result = prove(adapter, "Foo", "spec", "/tmp/proj", max_steers=0, verifier=None)
    assert result.status == "failed"
    assert "inductive step" in result.reason


def test_codex_adapter_empty_output_fails():
    adapter = CodexAdapter(runner=FakeCodexRunner([[]]))
    result = prove(adapter, "Foo", "spec", "/tmp/proj", max_steers=0, verifier=None)
    assert result.status == "failed"


def test_codex_steer_resumes_session():
    turn1 = _lines(
        {"type": "thread.started", "thread_id": "sess-9"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "working on it"}},
    )
    turn2 = _lines(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done, proved cleanly"}},
    )
    runner = FakeCodexRunner([turn1, turn2])
    adapter = CodexAdapter(runner=runner)
    result = prove(adapter, "Foo", "spec", "/tmp/proj", max_steers=1, steerer=_StubSteerer(), verifier=None)
    assert len(runner.calls) == 2
    # second turn must resume the captured session
    assert "resume" in runner.calls[1]["args"] and "sess-9" in runner.calls[1]["args"]
    assert result.status == "proved"


def test_codex_steer_without_session_is_dropped():
    # No thread id emitted → a queued steer must be dropped (no context-less re-run).
    turn1 = _lines({"type": "item.completed", "item": {"type": "agent_message", "text": "no session here"}})
    runner = FakeCodexRunner([turn1])
    adapter = CodexAdapter(runner=runner)
    result = prove(adapter, "Foo", "spec", "/tmp/proj", max_steers=1, steerer=_StubSteerer(), verifier=None)
    assert len(runner.calls) == 1  # no second (resume) turn
    assert result.status == "proved"


def test_codex_env_scrubs_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    runner = FakeCodexRunner([_lines(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok proved"}})])
    adapter = CodexAdapter(runner=runner)
    prove(adapter, "Foo", "spec", "/tmp/proj", max_steers=0, verifier=None)
    assert "ANTHROPIC_API_KEY" not in runner.calls[0]["env"]


def test_codex_system_prompt_forbids_cheating():
    p = CODEX_SYSTEM_PROMPT.lower()
    assert "sorry" in p and "admit" in p and "failed" in p


def test_codex_adapter_timeout_is_terminal_failed_with_sub_status():
    from servers.prover._cli_common import ProverTimeout
    from servers.prover.base import EventKind

    def hung_runner(args, env, cwd, deadline=None):
        yield json.dumps({"type": "thread.started", "thread_id": "sess-t"})
        raise ProverTimeout("CLI worker exceeded its deadline: codex")

    adapter = CodexAdapter(runner=hung_runner, max_wait_seconds=1)
    run = adapter.start("Foo", "spec", "/tmp/proj")
    events = list(adapter.events(run))
    assert events and events[-1].kind is EventKind.ERROR and "timeout" in events[-1].content

    result = adapter.result(run)
    assert result.status == "failed"
    assert "timeout" in result.reason
    assert result.meta["sub_status"] == "timeout"


def test_codex_adapter_threads_deadline_to_runner():
    runner = FakeCodexRunner([_lines(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok proved"}})])
    adapter = CodexAdapter(runner=runner, max_wait_seconds=120)
    prove(adapter, "Foo", "spec", "/tmp/proj", max_steers=0, verifier=None)
    assert runner.calls[0]["deadline"] is not None
