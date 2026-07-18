"""Steering phase A — the SteeringCapability flag and the verify-gate fold.

Two mechanisms, both keyed off the adapter's declared capability (never its
name), tested against FAKE adapters/verifiers — no live network, no ``claude``:

* **Capability-gated live judge** (``judge_policy``): the per-event steering
  judge runs by default only for an ``IN_FLIGHT`` backend; ``"always"`` restores
  the old every-backend behaviour, ``"never"`` disables it.
* **Verify-gate fold**: a rejected ``proved`` claim on a fold-capable backend
  (``BETWEEN_TURNS`` / ``AT_TOOL_CALLS``) is folded back ONCE as a deterministic
  corrective turn (the gate's own reason — no judge call), then re-verified.
  ``IN_FLIGHT`` / ``NONE`` downgrade immediately exactly as before the fold.
"""

from __future__ import annotations

import json

from servers.prover.base import (
    Event,
    EventKind,
    ProofResult,
    ProverAdapter,
    Run,
    SteeringCapability,
)
from servers.prover.claude_adapter import ClaudeAdapter
from servers.prover.driver import _live_judge_enabled, prove
from servers.prover.verify import VerifyResult


def _ev(kind: EventKind, content: str = "x") -> Event:
    return Event(kind, content)


class _FoldFakeAdapter(ProverAdapter):
    """A re-entrant fake mirroring the CLI adapters' fold mechanics.

    The first ``events()`` call yields ``script``; a ``steer()`` queues the
    corrective turn, and a *re-entry* into ``events()`` drains only
    ``corrective_script`` — exactly the ``started``-guard contract the real
    adapters implement. ``result()`` pops scripted results in order, so a test
    can express "first claim proved, corrective turn failed" etc.
    """

    name = "foldfake"

    def __init__(
        self,
        *,
        capability: SteeringCapability,
        results: list[ProofResult],
        script: list[Event] | None = None,
        corrective_script: list[Event] | None = None,
    ) -> None:
        self.steering = capability
        self._script = list(script if script is not None else [_ev(EventKind.EDIT)])
        self._corrective = (list(corrective_script) if corrective_script is not None
                            else [_ev(EventKind.RESULT, "reconfirmed")])
        self._results = list(results)
        self._started = False
        self._pending: list[str] = []
        self.steers: list[str] = []
        self.result_calls = 0

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        return Run(backend=self.name, goal=spec, project_dir=project_dir)

    def events(self, run: Run):
        if not self._started:
            self._started = True
            yield from self._script
        while self._pending:
            self._pending.pop(0)
            yield from self._corrective

    def steer(self, run: Run, message: str) -> None:
        self.steers.append(message)
        self._pending.append(message)

    def result(self, run: Run) -> ProofResult:
        self.result_calls += 1
        return self._results.pop(0)


class _FakeVerifier:
    """Pops one scripted :class:`VerifyResult` per gate call, recording calls."""

    def __init__(self, gates: list[VerifyResult]) -> None:
        self._gates = list(gates)
        self.calls = 0

    def __call__(self, node: str, project_dir: str, baseline=None) -> VerifyResult:
        self.calls += 1
        return self._gates.pop(0)


class _FakeSteerer:
    """Off-course at fixed window lengths; never calls a model."""

    def __init__(self, steer_at: set[int] | None = None) -> None:
        self.steer_at = steer_at or set()
        self.off_course_calls = 0

    def off_course(self, goal: str, window: list) -> bool:
        self.off_course_calls += 1
        return len(window) in self.steer_at

    def correction(self, goal: str, window: list) -> str:
        return f"course-correct at {len(window)}"


def _proved(**meta) -> ProofResult:
    return ProofResult(status="proved", proof_text="theorem t : True := trivial", meta=dict(meta))


# ---------------------------------------------------------------------------
# The capability flag
# ---------------------------------------------------------------------------


def test_capability_declarations():
    from servers.prover.aristotle_adapter import AristotleAdapter
    from servers.prover.codex_adapter import CodexAdapter

    assert ClaudeAdapter.steering is SteeringCapability.BETWEEN_TURNS
    assert CodexAdapter.steering is SteeringCapability.BETWEEN_TURNS
    assert AristotleAdapter.steering is SteeringCapability.IN_FLIGHT
    # The ABC default is the honest floor for a headless CLI.
    assert ProverAdapter.steering is SteeringCapability.BETWEEN_TURNS


def test_live_judge_policy_matrix():
    between, live = SteeringCapability.BETWEEN_TURNS, SteeringCapability.IN_FLIGHT
    assert _live_judge_enabled(live, "auto") is True
    assert _live_judge_enabled(between, "auto") is False
    assert _live_judge_enabled(SteeringCapability.AT_TOOL_CALLS, "auto") is False
    assert _live_judge_enabled(SteeringCapability.NONE, "auto") is False
    assert _live_judge_enabled(between, "always") is True
    assert _live_judge_enabled(live, "never") is False


def test_auto_policy_skips_judge_for_between_turns():
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.BETWEEN_TURNS,
        script=[_ev(EventKind.EDIT) for _ in range(4)],
        results=[_proved()],
    )
    steerer = _FakeSteerer(steer_at={1, 2, 3, 4})
    result = prove(adapter, "N", "spec", "/proj", steerer=steerer, verifier=None)
    assert result.proved
    assert adapter.steers == []          # no turn-granular judge steers…
    assert steerer.off_course_calls == 0  # …and the judge was never even consulted


def test_auto_policy_in_flight_is_trigger_gated_not_cadence():
    """Under ``auto`` even an in-flight backend is not cadence-judged: with no
    structured trigger firing, the judge is never consulted. (The positive
    trigger-fires cases live in test_triggers.py.)"""
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.IN_FLIGHT,
        script=[_ev(EventKind.EDIT) for _ in range(3)],  # no path/payload → no signals
        corrective_script=[],
        results=[_proved()],
    )
    steerer = _FakeSteerer(steer_at={1, 2, 3})
    result = prove(adapter, "N", "spec", "/proj", steerer=steerer, verifier=None)
    assert result.proved
    assert adapter.steers == []
    assert steerer.off_course_calls == 0


def test_always_policy_restores_judge_for_between_turns():
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.BETWEEN_TURNS,
        script=[_ev(EventKind.EDIT) for _ in range(3)],
        corrective_script=[],
        results=[_proved()],
    )
    result = prove(adapter, "N", "spec", "/proj", judge_policy="always",
                   steerer=_FakeSteerer(steer_at={2}), verifier=None)
    assert result.proved
    assert adapter.steers == ["course-correct at 2"]


def test_never_policy_disables_judge_even_in_flight():
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.IN_FLIGHT,
        script=[_ev(EventKind.EDIT) for _ in range(3)],
        results=[_proved()],
    )
    steerer = _FakeSteerer(steer_at={1, 2, 3})
    result = prove(adapter, "N", "spec", "/proj", judge_policy="never",
                   steerer=steerer, verifier=None)
    assert result.proved
    assert adapter.steers == []
    assert steerer.off_course_calls == 0


# ---------------------------------------------------------------------------
# The verify-gate fold
# ---------------------------------------------------------------------------


def test_gate_pass_no_fold(tmp_path):
    adapter = _FoldFakeAdapter(capability=SteeringCapability.BETWEEN_TURNS,
                               results=[_proved()])
    verifier = _FakeVerifier([VerifyResult(ok=True, checks={"build": "ok"})])

    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.proved
    assert verifier.calls == 1
    assert adapter.steers == []
    assert adapter.result_calls == 1
    assert result.meta["verify"] == {"build": "ok"}
    assert "gate_folds" not in result.meta
    assert "claimed_proved" not in result.meta


def test_fold_recovers(tmp_path):
    """Gate fails once → the reason folds back as ONE corrective turn → gate passes."""
    adapter = _FoldFakeAdapter(capability=SteeringCapability.BETWEEN_TURNS,
                               results=[_proved(), _proved()])
    verifier = _FakeVerifier([
        VerifyResult(ok=False, reason="sorry remains in Book/Thm.lean",
                     checks={"sorry": "found"}),
        VerifyResult(ok=True, checks={"build": "ok"}),
    ])

    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.proved
    assert verifier.calls == 2                       # rejected, folded, re-verified
    assert len(adapter.steers) == 1                  # exactly one corrective turn
    assert "sorry remains in Book/Thm.lean" in adapter.steers[0]  # the gate's own reason
    assert "FAILED" in adapter.steers[0]             # …and the honest way out
    assert adapter.result_calls == 2
    assert result.meta["gate_folds"] == 1
    assert result.meta["verify"] == {"build": "ok"}  # meta reflects the LATEST gate
    assert "claimed_proved" not in result.meta       # the claim was made good


def test_fold_fails_again_downgrades(tmp_path):
    adapter = _FoldFakeAdapter(capability=SteeringCapability.BETWEEN_TURNS,
                               results=[_proved(), _proved()])
    verifier = _FakeVerifier([
        VerifyResult(ok=False, reason="sorry remains", checks={"try": 1}),
        VerifyResult(ok=False, reason="sorry STILL remains", checks={"try": 2}),
    ])

    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert verifier.calls == 2
    assert len(adapter.steers) == 1                  # max_gate_folds=1: one fold only
    assert result.meta["claimed_proved"] is True
    assert result.meta["gate_folds"] == 1
    assert result.meta["verify"] == {"try": 2}       # the latest gate's checks
    assert "sorry STILL remains" in result.reason


def test_corrective_turn_failed_stands(tmp_path):
    """An honest FAILED on the corrective turn stands as-is — no re-verify."""
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.BETWEEN_TURNS,
        results=[_proved(),
                 ProofResult(status="failed", reason="FAILED — missing lemma foo")],
    )
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="does not compile")])

    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert result.reason == "FAILED — missing lemma foo"   # the worker's honest reason
    assert verifier.calls == 1                             # nothing left to verify
    assert result.meta["gate_folds"] == 1
    assert "claimed_proved" not in result.meta             # it withdrew the claim itself


def test_in_flight_never_folds(tmp_path):
    """IN_FLIGHT result() is terminal (side effects): rejected claim downgrades,
    result() is called exactly once, and no corrective steer is attempted."""
    adapter = _FoldFakeAdapter(capability=SteeringCapability.IN_FLIGHT,
                               results=[_proved()])
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="axiom smuggled")])

    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert adapter.steers == []
    assert adapter.result_calls == 1
    assert result.meta["claimed_proved"] is True
    assert "axiom smuggled" in result.reason


def test_none_capability_never_folds(tmp_path):
    adapter = _FoldFakeAdapter(capability=SteeringCapability.NONE,
                               results=[_proved()])
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="nope")])
    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)
    assert result.status == "failed"
    assert adapter.steers == []


def test_max_gate_folds_zero_disables(tmp_path):
    adapter = _FoldFakeAdapter(capability=SteeringCapability.BETWEEN_TURNS,
                               results=[_proved()])
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="nope")])
    result = prove(adapter, "N", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier, max_gate_folds=0)
    assert result.status == "failed"
    assert adapter.steers == []
    assert result.meta["claimed_proved"] is True


def test_fold_counts_against_steer_budget(tmp_path):
    """An exhausted steer budget also exhausts the fold (a fold IS a steer)."""
    adapter = _FoldFakeAdapter(capability=SteeringCapability.BETWEEN_TURNS,
                               results=[_proved()])
    verifier = _FakeVerifier([VerifyResult(ok=False, reason="nope")])
    result = prove(adapter, "N", "spec", str(tmp_path), max_steers=0,
                   steerer=_FakeSteerer(), verifier=verifier)
    assert result.status == "failed"
    assert adapter.steers == []
    assert result.meta["claimed_proved"] is True


# ---------------------------------------------------------------------------
# The fold through the REAL Claude adapter (re-entrancy: resume-only second turn)
# ---------------------------------------------------------------------------


def _stream(*objs: dict) -> list[str]:
    return [json.dumps(o) for o in objs]


class _Runner:
    """Scripted stream-json runner recording each launched turn's argv."""

    def __init__(self, turns: list[list[str]]) -> None:
        self._turns = turns
        self.calls: list[list[str]] = []

    def __call__(self, args, env, cwd, deadline=None):
        self.calls.append(args)
        lines = self._turns[len(self.calls) - 1] if len(self.calls) <= len(self._turns) else []
        yield from lines


def test_claude_fold_reentry_runs_resume_turn_only(tmp_path):
    turn1 = _stream(
        {"type": "system", "subtype": "init", "session_id": "sess-9"},
        {"type": "result", "session_id": "sess-9",
         "result": "theorem t : True := trivial"},
    )
    turn2 = _stream(
        {"type": "result", "session_id": "sess-9",
         "result": "fixed: removed the sorry; lake build green"},
    )
    runner = _Runner([turn1, turn2])
    adapter = ClaudeAdapter(runner=runner, autonomy_args=[], mcp_config="")
    verifier = _FakeVerifier([
        VerifyResult(ok=False, reason="sorry remains in Thm.lean"),
        VerifyResult(ok=True, checks={"build": "ok"}),
    ])

    result = prove(adapter, "T", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.proved
    assert verifier.calls == 2
    assert len(runner.calls) == 2                      # first turn + ONE corrective turn
    assert "--resume" in runner.calls[1]               # …delivered as a resume
    assert "sess-9" in runner.calls[1]
    assert "--append-system-prompt" not in runner.calls[1]  # not a replayed first turn
    # The corrective prompt is the gate's reason, verbatim.
    prompt = runner.calls[1][runner.calls[1].index("-p") + 1]
    assert "sorry remains in Thm.lean" in prompt
    assert result.meta["gate_folds"] == 1


def test_claude_fold_without_session_downgrades(tmp_path):
    """No session id captured → the fold's steer is dropped by the adapter; the
    re-entry yields nothing; the unchanged claim is re-verified and downgraded."""
    turn1 = _stream(  # NO session_id anywhere
        {"type": "result", "result": "theorem t : True := trivial"},
    )
    runner = _Runner([turn1])
    adapter = ClaudeAdapter(runner=runner, autonomy_args=[], mcp_config="")
    verifier = _FakeVerifier([
        VerifyResult(ok=False, reason="sorry remains"),
        VerifyResult(ok=False, reason="sorry remains"),
    ])

    result = prove(adapter, "T", "spec", str(tmp_path),
                   steerer=_FakeSteerer(), verifier=verifier)

    assert result.status == "failed"
    assert len(runner.calls) == 1          # no context-free second turn was launched
    assert result.meta["claimed_proved"] is True
