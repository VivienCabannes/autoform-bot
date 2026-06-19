"""Tests for the UNIFIED prover backend — driver, steerer, both adapters.

The single most important property under test: the **driver and steerer are
identical regardless of backend**. These tests prove the driver's steering loop
against FAKE adapters and a FAKE steer-judge — **no live network, no live
``claude`` process**. The Claude adapter is exercised with an injected stream
``runner`` (synthetic stream-json), and the Aristotle adapter with an injected
``AristotleManager`` ``lib`` (the same in-memory fake the C-side tests use).
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path

import pytest

from servers.prover.base import Event, EventKind, ProofResult, ProverAdapter, Run
from servers.prover.claude_adapter import ClaudeAdapter, _classify_stream_event, _looks_failed
from servers.prover.driver import prove
from servers.prover.steerer import Steerer


# ===========================================================================
# FAKE adapter — records start/steer/result, emits a scripted event stream
# ===========================================================================


class FakeAdapter(ProverAdapter):
    """A minimal :class:`ProverAdapter` that records the driver's calls.

    ``script`` is the list of events it yields; ``final`` is the result it
    returns. ``steers`` records every correction the driver injected — the
    evidence that the SHARED driver called ``steer`` exactly when the steerer
    said off-course.
    """

    name = "fake"

    def __init__(self, script: list[Event], final: ProofResult) -> None:
        self._script = script
        self._final = final
        self.started: tuple | None = None
        self.steers: list[str] = []

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        self.started = (node, spec, project_dir)
        return Run(backend=self.name, goal=spec, project_dir=project_dir, handle=None)

    def events(self, run: Run):
        yield from self._script

    def steer(self, run: Run, message: str) -> None:
        self.steers.append(message)

    def result(self, run: Run) -> ProofResult:
        return self._final


class FakeSteerer:
    """A FAKE steerer: says off-course on a fixed set of window lengths.

    Mirrors the real Steerer's ``off_course`` / ``correction`` surface but never
    calls a model — so the driver loop is tested with zero live judge calls.
    """

    def __init__(self, steer_at: set[int]) -> None:
        self.steer_at = steer_at  # window lengths at which to declare off-course
        self.off_course_calls = 0
        self.correction_calls = 0

    def off_course(self, goal: str, window: list) -> bool:
        self.off_course_calls += 1
        return len(window) in self.steer_at

    def correction(self, goal: str, window: list) -> str:
        self.correction_calls += 1
        return f"get back on course (saw {len(window)} events)"


def _ev(kind: EventKind, content: str = "x") -> Event:
    return Event(kind, content)


# ---------------------------------------------------------------------------
# Driver: the equivalence — same loop, steers iff the steerer says so
# ---------------------------------------------------------------------------


def test_driver_steers_when_steerer_says_off_course():
    script = [_ev(EventKind.THINKING), _ev(EventKind.EDIT), _ev(EventKind.MESSAGE)]
    adapter = FakeAdapter(script, ProofResult(status="proved", proof_text="done"))
    steerer = FakeSteerer(steer_at={2})  # off-course after the 2nd event

    result = prove(adapter, "N", "spec", "/proj", max_steers=3, steerer=steerer)

    assert result.proved
    assert adapter.steers == ["get back on course (saw 2 events)"]  # exactly one steer
    assert result.backend == "fake"  # driver stamps the adapter name


def test_driver_never_steers_when_on_course():
    script = [_ev(EventKind.THINKING), _ev(EventKind.EDIT)]
    adapter = FakeAdapter(script, ProofResult(status="proved"))
    steerer = FakeSteerer(steer_at=set())  # never off-course

    result = prove(adapter, "N", "spec", "/proj", steerer=steerer)

    assert adapter.steers == []
    assert result.proved


def test_driver_respects_max_steers_cap():
    # Off-course at EVERY window length, but capped at 2 steers.
    script = [_ev(EventKind.EDIT) for _ in range(6)]
    adapter = FakeAdapter(script, ProofResult(status="failed", reason="ran out"))
    # After each steer the window resets to [], so off-course fires at len==1.
    steerer = FakeSteerer(steer_at={1, 2, 3, 4, 5, 6})

    result = prove(adapter, "N", "spec", "/proj", max_steers=2, steerer=steerer)

    assert len(adapter.steers) == 2  # cap honoured
    assert result.status == "failed"


def test_driver_clears_window_after_steer():
    # Steer fires at window length 2; after a steer the window resets, so the
    # next steer can only fire again once 2 fresh events accumulate.
    script = [_ev(EventKind.EDIT) for _ in range(4)]
    adapter = FakeAdapter(script, ProofResult(status="proved"))
    steerer = FakeSteerer(steer_at={2})

    prove(adapter, "N", "spec", "/proj", max_steers=5, steerer=steerer)

    # events: 1,(2→steer,reset),1,(2→steer,reset)  -> exactly 2 steers
    assert len(adapter.steers) == 2


def test_driver_returns_adapter_result_verbatim():
    final = ProofResult(status="failed", proof_text="x", reason="blocked", backend="")
    adapter = FakeAdapter([_ev(EventKind.MESSAGE)], final)
    steerer = FakeSteerer(steer_at=set())

    result = prove(adapter, "N", "spec", "/proj", steerer=steerer)

    assert result is final
    assert result.reason == "blocked"


def test_driver_is_backend_agnostic_same_loop_two_adapters():
    """The SAME driver + SAME steerer drive two different adapters identically."""
    script = [_ev(EventKind.THINKING), _ev(EventKind.EDIT), _ev(EventKind.MESSAGE)]

    a1 = FakeAdapter(list(script), ProofResult(status="proved"))
    a1.name = "backendA"
    a2 = FakeAdapter(list(script), ProofResult(status="proved"))
    a2.name = "backendB"

    prove(a1, "N", "spec", "/proj", steerer=FakeSteerer(steer_at={2}))
    prove(a2, "N", "spec", "/proj", steerer=FakeSteerer(steer_at={2}))

    # Identical steering behaviour for both backends.
    assert a1.steers == a2.steers == ["get back on course (saw 2 events)"]


# ---------------------------------------------------------------------------
# Steerer: pure over (goal, window), high bar, rate limit — fake judge
# ---------------------------------------------------------------------------


def test_steerer_off_course_uses_judge_verdict():
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return '{"steer": true, "reason": "sorry-ing the goal", "prompt": "remove the sorry"}'

    s = Steerer(min_gap_s=0.0, judge=judge)
    window = [_ev(EventKind.EDIT, "added sorry")]
    assert s.off_course("prove T", window) is True
    assert s.correction("prove T", window) == "remove the sorry"
    # off_course + correction over the SAME window => ONE judge call (cached).
    assert len(calls) == 1
    # The goal and the event are threaded into the judge prompt.
    assert "prove T" in calls[0] and "added sorry" in calls[0]


def test_steerer_declines_when_judge_says_no():
    s = Steerer(min_gap_s=0.0, judge=lambda p: '{"steer": false, "reason": "fine", "prompt": ""}')
    assert s.off_course("g", [_ev(EventKind.EDIT)]) is False


def test_steerer_high_bar_empty_prompt_is_no_steer():
    # steer:true but no actionable prompt => treated as NO steer.
    s = Steerer(min_gap_s=0.0, judge=lambda p: '{"steer": true, "reason": "x", "prompt": ""}')
    assert s.off_course("g", [_ev(EventKind.EDIT)]) is False


def test_steerer_ignores_unparseable_judge_output():
    s = Steerer(min_gap_s=0.0, judge=lambda p: "the model rambled with no json")
    assert s.off_course("g", [_ev(EventKind.EDIT)]) is False


def test_steerer_no_relevant_events_does_not_call_judge():
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return '{"steer": true, "prompt": "x"}'

    s = Steerer(min_gap_s=0.0, judge=judge)
    # OTHER-kind events are not steer-relevant.
    assert s.off_course("g", [_ev(EventKind.OTHER)]) is False
    assert calls == []  # judge never invoked


def test_steerer_rate_limits_judge_calls():
    calls = []

    def judge(prompt: str) -> str:
        calls.append(prompt)
        return '{"steer": false, "prompt": ""}'

    s = Steerer(min_gap_s=10_000.0, judge=judge)  # effectively never re-call
    s.off_course("g", [_ev(EventKind.EDIT, "a")])          # window 1 -> calls judge
    s.off_course("g", [_ev(EventKind.EDIT, "a"), _ev(EventKind.EDIT, "b")])  # window 2 -> rate-limited
    assert len(calls) == 1


# ===========================================================================
# Claude adapter — synthetic stream-json (NO live claude process)
# ===========================================================================


def _stream(*objs: dict) -> list[str]:
    return [json.dumps(o) for o in objs]


class FakeClaudeRunner:
    """Injectable stream runner: yields scripted stream-json per turn.

    Each call returns the next turn's lines, recording the args so the test can
    assert on the steer mechanism (``--resume <session_id>``).
    """

    def __init__(self, turns: list[list[str]]) -> None:
        self._turns = turns
        self._i = 0
        self.calls: list[list[str]] = []

    def __call__(self, args, env, cwd):
        self.calls.append(args)
        # The key must NOT be visible to the launched claude (Max billing).
        assert "ANTHROPIC_API_KEY" not in env
        lines = self._turns[self._i] if self._i < len(self._turns) else []
        self._i += 1
        yield from lines


def test_claude_classify_stream_events():
    assert _classify_stream_event({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello"}]}}).kind is EventKind.MESSAGE
    assert _classify_stream_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "A.lean"}}]}}).kind is EventKind.EDIT
    assert _classify_stream_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "boom"}]}}).kind is EventKind.ERROR
    assert _classify_stream_event({"type": "result", "result": "final answer"}).kind is EventKind.RESULT


def test_claude_adapter_single_turn_proves():
    turns = [_stream(
        {"type": "system", "subtype": "init", "session_id": "sess-1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "proving…"}]}},
        {"type": "result", "session_id": "sess-1", "result": "theorem t : True := trivial -- done"},
    )]
    runner = FakeClaudeRunner(turns)
    adapter = ClaudeAdapter(runner=runner)

    result = prove(adapter, "T", "prove True", "/proj", steerer=FakeSteerer(steer_at=set()))

    assert result.proved
    assert "theorem t" in result.proof_text
    assert result.meta["session_id"] == "sess-1"
    assert len(runner.calls) == 1  # one turn, no steer


def test_claude_adapter_steer_resumes_session():
    """The steer mechanism: a queued steer launches a follow-up --resume turn."""
    turn1 = _stream(
        {"type": "system", "subtype": "init", "session_id": "sess-9"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                       "input": {"file_path": "A.lean"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                       "input": {"file_path": "A.lean"}}]}},
        {"type": "result", "session_id": "sess-9", "result": "still working"},
    )
    turn2 = _stream(
        {"type": "result", "session_id": "sess-9", "result": "theorem t : True := trivial"},
    )
    runner = FakeClaudeRunner([turn1, turn2])
    adapter = ClaudeAdapter(runner=runner)
    # Off-course after 2 events => the driver queues a steer; the adapter delivers
    # it as a resumed follow-up turn.
    steerer = FakeSteerer(steer_at={2})

    result = prove(adapter, "T", "prove True", "/proj", max_steers=1, steerer=steerer)

    assert result.proved
    assert "theorem t" in result.proof_text
    # Two turns ran: the original + the resumed steer turn.
    assert len(runner.calls) == 2
    # The second turn resumed the captured session id with the correction.
    assert "--resume" in runner.calls[1]
    assert "sess-9" in runner.calls[1]


def test_claude_adapter_reports_honest_failed():
    turns = [_stream(
        {"type": "system", "session_id": "s"},
        {"type": "result", "session_id": "s",
         "result": "FAILED — could not close the integral bound; missing lemma foo"},
    )]
    adapter = ClaudeAdapter(runner=FakeClaudeRunner(turns))

    result = prove(adapter, "T", "spec", "/proj", steerer=FakeSteerer(steer_at=set()))

    assert result.status == "failed"
    assert "missing lemma foo" in result.reason


def test_looks_failed_heuristic():
    assert _looks_failed("") is True
    assert _looks_failed("FAILED — nope") is True
    assert _looks_failed("theorem t : True := trivial") is False


def test_claude_system_prompt_forbids_cheating():
    from servers.prover.claude_adapter import WORKER_SYSTEM_PROMPT
    for token in ("sorry", "admit", "axiom", "native_decide", "ANTHROPIC_API_KEY", "FAILED"):
        assert token in WORKER_SYSTEM_PROMPT


# ===========================================================================
# Aristotle adapter — in-memory fake lib (NO network, NO aristotlelib)
# ===========================================================================


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _EvType:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEvent:
    def __init__(self, eid: str, name: str, content: str) -> None:
        self.event_id = eid
        self.event_type = _EvType(name)
        self.content = content


class _FakeTask:
    def __init__(self) -> None:
        self.agent_task_id = "task-1"
        self.status = _Status("COMPLETE")
        self.output_summary = "Proved the target; lake build is green."
        self._events = [
            _FakeEvent("e1", "THINKING", "planning the proof"),
            _FakeEvent("e2", "EDITING_FILE", "Book/Thm.lean"),
        ]

    async def refresh(self) -> None:
        return None

    async def get_events(self, limit: int = 50, newest_first: bool = True):
        evs = list(reversed(self._events)) if newest_first else list(self._events)
        return evs, None


class _FakeProject:
    def __init__(self, returned_files: dict[str, str]) -> None:
        self.project_id = "proj-aristotle"
        self._returned_files = returned_files
        self._task = _FakeTask()

    async def get_tasks(self, limit: int = 1, newest_first: bool = True):
        return [self._task], None

    async def ask(self, prompt: str):
        return self._task

    async def refresh(self) -> None:
        return None

    async def get_files(self, destination) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination, "w:gz") as tar:
            for rel, content in self._returned_files.items():
                data = content.encode()
                info = tarfile.TarInfo(name=f"proj-aristotle/{rel}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


class _FakeLib:
    def __init__(self, returned_files: dict[str, str]) -> None:
        self._returned_files = returned_files
        lib = self

        class Project:
            @staticmethod
            async def create(prompt: str):
                return _FakeProject(lib._returned_files)

            @staticmethod
            async def create_from_directory(prompt: str, project_dir: str):
                return _FakeProject(lib._returned_files)

        self.Project = Project


def _write_plan(tmp_path: Path) -> Path:
    (tmp_path / "informal_content").mkdir(parents=True, exist_ok=True)
    (tmp_path / "informal_content" / "thm.md").write_text("# Thm\n\nStatement.\n", encoding="utf-8")
    graph = {
        "version": 2,
        "nodes": {
            "Thm": {
                "id": "Thm", "tier": 2, "parent": "Cluster", "kind": "theorem",
                "depends_on": [], "mathlib_status": "missing",
                "mathlib_declarations": ["Foo.bar"], "source_refs": [],
                "content": "informal_content/thm.md",
            }
        },
    }
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph), encoding="utf-8")
    return gp


def test_aristotle_adapter_drives_via_shared_driver(tmp_path):
    """The Aristotle adapter runs under the SAME driver, lands files, reports proved."""
    from servers.aristotle.core import AristotleManager
    from servers.prover.aristotle_adapter import AristotleAdapter

    gp = _write_plan(tmp_path)
    mgr = AristotleManager(download_dir=str(tmp_path / ".cache"),
                           lib=_FakeLib({"Book/Thm.lean": "theorem bar : True := trivial\n"}))
    adapter = AristotleAdapter(graph_path=str(gp), manager=mgr, poll_interval=0, max_wait_seconds=5)
    spec = "prove Thm"

    # Drive through the unified driver with a no-op steerer (terminal immediately).
    result = prove(adapter, "Thm", spec, str(tmp_path), steerer=FakeSteerer(steer_at=set()))

    assert result.proved
    assert result.backend == "aristotle"
    assert result.landed_files >= 1
    assert (tmp_path / "Book" / "Thm.lean").exists()
    prose = (tmp_path / "informal_content" / "thm.md").read_text()
    assert "Proof (delegated to Aristotle)" in prose


def test_aristotle_adapter_events_normalized(tmp_path):
    from servers.aristotle.core import AristotleManager
    from servers.prover.aristotle_adapter import AristotleAdapter

    gp = _write_plan(tmp_path)
    mgr = AristotleManager(download_dir=str(tmp_path / ".cache"),
                           lib=_FakeLib({"Book/Thm.lean": "theorem bar : True := trivial\n"}))
    adapter = AristotleAdapter(graph_path=str(gp), manager=mgr, poll_interval=0, max_wait_seconds=5)
    run = adapter.start("Thm", "prove Thm", str(tmp_path))

    kinds = [e.kind for e in adapter.events(run)]
    assert EventKind.THINKING in kinds
    assert EventKind.EDIT in kinds  # EDITING_FILE -> EDIT


# ===========================================================================
# Server / import-hygiene
# ===========================================================================


def test_prover_package_imports_without_aristotlelib():
    """The contract/driver/steerer/claude adapter import with NO aristotlelib."""
    import importlib

    for mod in (
        "servers.prover.base",
        "servers.prover.steerer",
        "servers.prover.driver",
        "servers.prover.claude_adapter",
        "servers.prover.server",
    ):
        importlib.import_module(mod)
    # aristotlelib must not have been imported as a side effect.
    import sys
    assert "aristotlelib" not in sys.modules


def test_prover_server_exposes_single_prove_node_tool():
    from servers.prover.server import create_prover_server

    server = create_prover_server()
    assert server.name == "autoform-prover"
    names = {getattr(t, "name", t) for t in asyncio.run(server.list_tools())}
    assert names == {"prove_node"}


def test_prove_node_unknown_backend_fails_gracefully(tmp_path):
    from servers.prover.server import run_prove_node

    gp = _write_plan(tmp_path)
    with pytest.raises(ValueError):
        run_prove_node(graph_path=str(gp), node_id="Thm", project_dir=str(tmp_path), backend="bogus")
