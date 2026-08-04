"""Structured steering triggers (phase B) — the engine, and its driver wiring.

The engine is pure (injectable clock, no model, no I/O), so every signal is
tested deterministically; the driver tests then prove the ``auto`` policy:
an in-flight backend steers exactly when a signal fires — directly for a
self-composing signal (zero judge calls), via the judge for the off-goal
judgement call — while a between-turns backend accumulates signals silently
into the result meta.
"""

from __future__ import annotations

from autoform.prover.base import Event, EventKind, SteeringCapability
from autoform.prover.driver import prove
from autoform.prover.triggers import (
    SIGNAL_FORBIDDEN,
    SIGNAL_OFF_GOAL,
    SIGNAL_REPEATED_ERROR,
    SIGNAL_SORRY_STUCK,
    SIGNAL_STALL,
    TriggerConfig,
    TriggerEngine,
    error_fingerprint,
)

# Reuse the phase-A fakes: the fold-capable adapter and the counting steerer.
from tests.test_steering_phase_a import _FakeSteerer, _FoldFakeAdapter, _proved


class _Clock:
    """A settable monotonic clock."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _engine(**kw) -> tuple[TriggerEngine, _Clock]:
    clock = kw.pop("clock", _Clock())
    return TriggerEngine(clock=clock, **kw), clock


def _edit(path: str = "", payload: str = "") -> Event:
    return Event(EventKind.EDIT, f"Edit {path}".strip(), path=path, payload=payload)


def _err(text: str) -> Event:
    return Event(EventKind.ERROR, text)


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def test_error_fingerprint_ignores_paths_and_numbers():
    a = error_fingerprint("Book/Chap1/Thm.lean:37:2: unknown identifier 'foo_bar'")
    b = error_fingerprint("Book/Chap2/Lem.lean:214:8: unknown identifier   'foo_bar'")
    assert a == b
    assert a != error_fingerprint("Book/Chap1/Thm.lean:37:2: type mismatch")


# ---------------------------------------------------------------------------
# The signals
# ---------------------------------------------------------------------------


def test_repeated_error_fires_at_threshold_once_per_fingerprint():
    eng, _ = _engine()
    boom = "Thm.lean:12:1: unknown identifier 'Nat.foo'"
    assert eng.observe(_err(boom)) == []
    assert eng.observe(_err(boom.replace("12", "99"))) == []   # same fingerprint
    fired = eng.observe(_err(boom))
    assert [t.signal for t in fired] == [SIGNAL_REPEATED_ERROR]
    assert "3 times" in fired[0].correction                     # self-composing
    # The SAME stuck loop never re-fires — no new information.
    assert eng.observe(_err(boom)) == []
    assert eng.fired[SIGNAL_REPEATED_ERROR] == 1


def test_distinct_errors_accumulate_separately():
    eng, clock = _engine()
    for _ in range(2):
        eng.observe(_err("error: type mismatch at foo"))
    for _ in range(2):
        eng.observe(_err("error: unknown constant bar"))
    assert eng.fired[SIGNAL_REPEATED_ERROR] == 0               # neither reached 3
    assert eng.observe(_err("error: type mismatch at foo"))    # third of the first
    clock.t += 1000                                            # clear the signal cooldown
    assert eng.observe(_err("error: unknown constant bar"))    # third of the second


def test_sorry_stuck_fires_on_non_decreasing_counts():
    eng, _ = _engine()
    assert eng.observe(_edit("A.lean", "sorry\nsorry")) == []
    assert eng.observe(_edit("A.lean", "sorry\nsorry")) == []
    fired = eng.observe(_edit("A.lean", "sorry sorry sorry"))
    assert [t.signal for t in fired] == [SIGNAL_SORRY_STUCK]
    assert "3 edits" in fired[0].detail and fired[0].correction


def test_sorry_decreasing_does_not_fire():
    eng, _ = _engine()
    eng.observe(_edit("A.lean", "sorry sorry sorry"))
    eng.observe(_edit("A.lean", "sorry sorry"))
    fired = eng.observe(_edit("A.lean", "sorry"))
    assert fired == []                                          # progress: 3 → 2 → 1
    # …and reaching zero is success, never a signal.
    assert eng.observe(_edit("A.lean", "-- clean")) == []


def test_payloadless_edits_are_ignored_by_sorry_signal():
    eng, _ = _engine()
    for _ in range(5):
        assert eng.observe(_edit("A.lean")) == []               # unknown payload → no count


def test_forbidden_token_fires_immediately_and_precisely():
    eng, _ = _engine()
    fired = eng.observe(_edit("A.lean", "axiom cheat : False\n"))
    assert [t.signal for t in fired] == [SIGNAL_FORBIDDEN]
    assert "`axiom`" in fired[0].correction and "FAILED" in fired[0].correction

    eng2, _ = _engine()
    # An identifier CONTAINING "axiom", or a commented mention, must not fire.
    assert eng2.observe(_edit("A.lean", "exact axiom_of_choice h\n")) == []
    assert eng2.observe(_edit("A.lean", "-- no axiom needed here\n")) == []
    fired = eng2.observe(_edit("A.lean", "simp [foo]\nnative_decide\n"))
    assert [t.signal for t in fired] == [SIGNAL_FORBIDDEN]


def test_off_goal_fires_after_streak_and_needs_judge():
    eng, _ = _engine(node_hint="Foo.Bar.baz_thm")
    assert eng.observe(_edit("src/Other/Qux.lean", "x")) == []
    fired = eng.observe(_edit("src/Other/Zap.lean", "x"))
    assert [t.signal for t in fired] == [SIGNAL_OFF_GOAL]
    assert fired[0].correction == ""                            # judgement call → judge


def test_on_goal_edit_resets_the_streak():
    eng, _ = _engine(node_hint="Foo.Bar.baz_thm")
    eng.observe(_edit("src/Other/Qux.lean", "x"))
    eng.observe(_edit("Foo/Bar.lean", "x"))                     # on-goal: token "Bar"
    assert eng.observe(_edit("src/Other/Qux.lean", "x")) == []  # streak restarted
    assert eng.fired[SIGNAL_OFF_GOAL] == 0


def test_off_goal_disabled_without_hint():
    eng, _ = _engine()                                          # no node_hint
    for _ in range(5):
        assert eng.observe(_edit("src/Anywhere/File.lean", "x")) == []


def test_natural_language_node_id_matches_its_file():
    """Production node ids are plan phrases ('Chernoff bound'), not dotted Lean
    names — the on-goal match must work word-wise, or every edit of an on-goal
    run gets flagged (the adversarial-review bug)."""
    eng, _ = _engine(node_hint="Chernoff bound")
    assert eng.observe(_edit("ProbBook/Chernoff.lean", "x")) == []
    assert eng.observe(_edit("ProbBook/Chernoff.lean", "x")) == []
    assert eng.observe(_edit("ProbBook/Bound.lean", "x")) == []   # word "bound" matches
    assert eng.fired[SIGNAL_OFF_GOAL] == 0


def test_word_match_rejects_substring_false_positives():
    """'Bar' must NOT match 'Barrier' — whole-word matching, not substring
    (the other half of the adversarial-review bug)."""
    eng, _ = _engine(node_hint="Foo.Bar.baz_thm")
    eng.observe(_edit("src/Barrier/One.lean", "x"))
    fired = eng.observe(_edit("src/Barrier/Two.lean", "x"))
    assert [t.signal for t in fired] == [SIGNAL_OFF_GOAL]


def test_lean_extension_never_counts_as_goal_word():
    # A node mentioning "Lean" must not blanket-match every .lean file via
    # the extension; only real stem words count.
    eng, _ = _engine(node_hint="Lean encoding of graphs")
    eng.observe(_edit("src/Other/Qux.lean", "x"))
    fired = eng.observe(_edit("src/Other/Zap.lean", "x"))
    assert [t.signal for t in fired] == [SIGNAL_OFF_GOAL]
    eng2, _ = _engine(node_hint="Lean encoding of graphs")
    assert eng2.observe(_edit("src/Graphs/Encoding.lean", "x")) == []  # real word hit


def test_suppressed_fingerprint_rearms_after_cooldown():
    """A threshold-crossing swallowed by the signal cooldown must NOT consume
    the fingerprint's one fire (the adversarial-review bug): the loop gets its
    steer on a later repeat once the cooldown clears."""
    eng, clock = _engine()
    for _ in range(3):
        eng.observe(_err("error: alpha failed"))                # alpha fires at t=0
    for _ in range(3):
        eng.observe(_err("error: beta failed"))                 # beta crossing suppressed
    assert eng.suppressed[SIGNAL_REPEATED_ERROR] == 1
    clock.t = 1000                                              # cooldown cleared
    fired = eng.observe(_err("error: beta failed"))             # beta repeats → re-armed
    assert [t.signal for t in fired] == [SIGNAL_REPEATED_ERROR]
    assert eng.fired[SIGNAL_REPEATED_ERROR] == 2


def test_stall_fires_on_quiet_reasoning_and_resets():
    eng, clock = _engine(config=TriggerConfig(stall_seconds=900))
    eng.observe(_edit("A.lean", "x"))                           # progress at t=0
    clock.t = 500
    assert eng.observe(Event(EventKind.THINKING, "hmm")) == []  # not yet
    clock.t = 1000
    fired = eng.observe(Event(EventKind.THINKING, "hmm"))
    assert [t.signal for t in fired] == [SIGNAL_STALL]
    assert fired[0].correction                                  # self-composing
    clock.t = 1100
    assert eng.observe(Event(EventKind.THINKING, "hmm")) == []  # clock restarted


def test_cooldown_suppresses_and_counts():
    eng, clock = _engine()
    for _ in range(3):
        eng.observe(_err("error: alpha failed"))                # fires at t=0
    for _ in range(3):
        eng.observe(_err("error: beta failed"))                 # distinct fp, within cooldown
    assert eng.fired[SIGNAL_REPEATED_ERROR] == 1
    assert eng.suppressed[SIGNAL_REPEATED_ERROR] == 1
    clock.t = 1000                                              # past the cooldown
    assert eng.observe(_err("error: gamma failed")) == []
    eng.observe(_err("error: gamma failed"))
    fired = eng.observe(_err("error: gamma failed"))
    assert [t.signal for t in fired] == [SIGNAL_REPEATED_ERROR]
    assert eng.summary() == {
        "fired": {SIGNAL_REPEATED_ERROR: 2},
        "suppressed": {SIGNAL_REPEATED_ERROR: 1},
    }


# ---------------------------------------------------------------------------
# Driver wiring: the "auto" policy is trigger-gated
# ---------------------------------------------------------------------------


def test_auto_in_flight_self_composing_trigger_steers_without_judge():
    boom = "Thm.lean:1:1: unknown identifier 'foo'"
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.IN_FLIGHT,
        script=[_err(boom), _err(boom), _err(boom)],
        corrective_script=[],
        results=[_proved()],
    )
    steerer = _FakeSteerer(steer_at={1, 2, 3})     # would fire if ever consulted
    result = prove(adapter, "N", "spec", "/proj", steerer=steerer, verifier=None)
    assert result.proved
    assert len(adapter.steers) == 1
    assert "Stop repeating the failing approach" in adapter.steers[0]
    assert steerer.off_course_calls == 0            # zero judge involvement
    assert result.meta["steering"]["signals"]["fired"] == {SIGNAL_REPEATED_ERROR: 1}
    assert result.meta["steering"]["steers"] == 1


def test_auto_in_flight_judgement_signal_summons_judge():
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.IN_FLIGHT,
        script=[_edit("src/Other/A.lean", "x"), _edit("src/Other/B.lean", "x")],
        corrective_script=[],
        results=[_proved()],
    )
    steerer = _FakeSteerer(steer_at={2})            # confirms when consulted
    result = prove(adapter, "Foo.Bar.baz_thm", "spec", "/proj",
                   steerer=steerer, verifier=None)
    assert result.proved
    assert steerer.off_course_calls == 1            # consulted ON the trigger only
    assert adapter.steers == ["course-correct at 2"]


def test_auto_between_turns_accumulates_silently_into_meta():
    boom = "Thm.lean:1:1: unknown identifier 'foo'"
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.BETWEEN_TURNS,
        script=[_err(boom), _err(boom), _err(boom)],
        corrective_script=[],
        results=[_proved()],
    )
    steerer = _FakeSteerer(steer_at={1, 2, 3})
    result = prove(adapter, "N", "spec", "/proj", steerer=steerer, verifier=None)
    assert result.proved
    assert adapter.steers == []                     # no mid-run steering…
    assert steerer.off_course_calls == 0
    # …but the signal is on the record for the dispatch layer / next attempt.
    assert result.meta["steering"]["signals"]["fired"] == {SIGNAL_REPEATED_ERROR: 1}
    assert result.meta["steering"]["capability"] == "between_turns"


def test_never_policy_observes_but_never_steers():
    boom = "Thm.lean:1:1: unknown identifier 'foo'"
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.IN_FLIGHT,
        script=[_err(boom), _err(boom), _err(boom)],
        corrective_script=[],
        results=[_proved()],
    )
    result = prove(adapter, "N", "spec", "/proj", judge_policy="never",
                   steerer=_FakeSteerer(), verifier=None)
    assert adapter.steers == []
    assert result.meta["steering"]["signals"]["fired"] == {SIGNAL_REPEATED_ERROR: 1}


def test_trigger_steers_respect_max_steers_budget():
    a, b = "error: alpha", "error: beta"
    eng = TriggerEngine(clock=_Clock(), config=TriggerConfig(
        cooldown_s={SIGNAL_REPEATED_ERROR: 0.0}))
    adapter = _FoldFakeAdapter(
        capability=SteeringCapability.IN_FLIGHT,
        script=[_err(a), _err(a), _err(a), _err(b), _err(b), _err(b)],
        corrective_script=[],
        results=[_proved()],
    )
    result = prove(adapter, "N", "spec", "/proj", max_steers=1,
                   steerer=_FakeSteerer(), verifier=None, triggers=eng)
    assert len(adapter.steers) == 1                 # both signals fired; budget capped
    assert result.meta["steering"]["steers"] == 1


def test_injected_engine_and_steerer_call_counter():
    from autoform.prover.steerer import Steerer

    s = Steerer(min_gap_s=0.0, judge=lambda p: '{"steer": false, "prompt": ""}')
    s.off_course("g", [Event(EventKind.EDIT, "e")])
    assert s.calls == 1                             # telemetry counter increments


# ---------------------------------------------------------------------------
# Adapter enrichment: path/payload reach the normalized events
# ---------------------------------------------------------------------------


def test_claude_edit_events_carry_path_and_payload():
    from autoform.prover.claude_adapter import _classify_stream_event

    ev = _classify_stream_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit",
         "input": {"file_path": "A.lean", "new_string": "theorem t : True := sorry"}}]}})
    assert ev.kind is EventKind.EDIT
    assert ev.path == "A.lean"
    assert "sorry" in ev.payload

    ev = _classify_stream_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": "B.lean", "content": "axiom nope : False"}}]}})
    assert ev.payload == "axiom nope : False"

    ev = _classify_stream_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "MultiEdit",
         "input": {"file_path": "C.lean", "edits": [
             {"old_string": "a", "new_string": "one sorry"},
             {"old_string": "b", "new_string": "two"}]}}]}})
    assert "one sorry" in ev.payload and "two" in ev.payload

    ev = _classify_stream_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"path": "x.sh"}}]}})
    assert ev.kind is EventKind.TOOL and ev.payload == ""


def test_codex_edit_events_carry_path_and_payload():
    from autoform.prover.codex_adapter import _classify_codex_event

    ev, _, _ = _classify_codex_event(
        {"type": "item.completed",
         "item": {"type": "file_change", "path": "A.lean", "text": "+ sorry"}})
    assert ev.kind is EventKind.EDIT
    assert ev.path == "A.lean"
    assert ev.payload == "+ sorry"


def test_aristotle_edit_events_carry_path():
    from autoform.prover.aristotle_adapter import _normalize

    class _Raw:
        class event_type:
            name = "EDITING_FILE"
        content = "Book/Thm.lean"

    ev = _normalize(_Raw())
    assert ev.kind is EventKind.EDIT and ev.path == "Book/Thm.lean"
