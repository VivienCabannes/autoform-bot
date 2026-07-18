"""The UNIFIED DRIVER — one loop that drives ANY backend identically.

This module is the whole point of the unified prover: the loop below is written
against the :class:`~servers.prover.base.ProverAdapter` interface and the shared
:class:`~servers.prover.steerer.Steerer` **only**. It contains **zero**
backend-specific code — per-backend behaviour differences are keyed off the
adapter's declared :class:`~servers.prover.base.SteeringCapability`, never off
its name — so the *same* ``prove`` drives the Claude adapter and the Aristotle
adapter with no branch on ``backend`` anywhere. Swapping the prover is swapping
the ``adapter`` argument — nothing else changes.

The contract::

    prove(adapter, node, spec, project_dir, max_steers=3) -> ProofResult

1. ``adapter.start`` launches the run.
2. We consume ``adapter.events`` one at a time, appending each to a rolling
   ``window``.
3. Every event also feeds the **structured trigger engine**
   (:mod:`servers.prover.triggers`) — deterministic signals (repeated build
   error, sorry-count stuck, off-goal edits, stall, forbidden token) with
   per-signal cooldowns. Under the default ``judge_policy="auto"``, an
   ``IN_FLIGHT`` backend (Aristotle) is steered when a signal fires: a
   self-composing signal steers directly (zero judge calls); the one
   judgement-call signal (off-goal) summons the shared steerer as
   *confirmation*. For a turn-granular ``BETWEEN_TURNS`` backend
   (``claude -p`` / ``codex exec``) a correction can land only as the *next*
   resumed turn, so no mid-run steering happens at all — signals accumulate
   silently into the result meta, and the backend is steered by the verify-gate
   fold below. ``judge_policy="always"`` restores the old per-window cadence
   judging for every backend; ``"never"`` disables all mid-run steering. See
   :class:`~servers.prover.base.SteeringCapability`.
4. When the event stream ends we take ``adapter.result(run)``.
5. **Honesty gate** — a backend's ``proved`` is the worker's *claim*. Before it
   stands, :mod:`servers.prover.verify` independently checks the landed Lean
   (build-clean + no ``sorry``/``admit`` + a clean axiom set); a failed gate
   downgrades the verdict to ``failed``. This runs once, in the shared driver, so
   it protects every backend.
6. **Verify-gate fold** — the single highest-signal, zero-cost correction we have
   is the gate's own rejection reason, and before this existed it was thrown away
   into ``result.reason``. For a backend whose session can take another turn
   (``BETWEEN_TURNS`` / ``AT_TOOL_CALLS``), a rejected ``proved`` claim is folded
   back **once** (``max_gate_folds``) as a deterministic corrective turn — no
   judge call — and the renewed claim is re-verified. An ``IN_FLIGHT`` backend's
   ``result`` is terminal (files landed, session closed), so it downgrades
   immediately exactly as before.

That is the equivalence the spec demands: identical driver + identical steerer +
identical honesty gate, only the adapter differs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .base import ProofResult, ProverAdapter, SteeringCapability
from .steerer import Steerer
from .triggers import TriggerEngine
from .verify import VerifyResult, capture_baseline, verify_proof

logger = logging.getLogger(__name__)

#: Capabilities whose session can accept a post-run corrective turn — the fold
#: targets. ``IN_FLIGHT`` is deliberately absent: its ``result()`` is terminal
#: (Aristotle lands files and closes its loop), so a rejected claim downgrades
#: rather than folds, and ``result()`` is never called twice.
_FOLD_CAPABLE = frozenset(
    {SteeringCapability.BETWEEN_TURNS, SteeringCapability.AT_TOOL_CALLS}
)


def _live_judge_enabled(capability: SteeringCapability, judge_policy: str) -> bool:
    """Whether the live judge may run AT ALL for this backend.

    Under ``"auto"`` this is a *permission*, not a cadence: an in-flight backend
    is judged only when a structured trigger fires (see the consume loop). A
    turn-granular backend can act on a correction only as its NEXT resumed turn,
    so per-event judging is low-value for its cost — it is steered by the
    gate-fold, and its triggers accumulate silently as telemetry.
    """
    if judge_policy == "always":
        return True
    if judge_policy == "never":
        return False
    return capability is SteeringCapability.IN_FLIGHT


def _fold_correction(reason: str) -> str:
    """The deterministic corrective turn composed from the gate's own reason."""
    return (
        "Independent verification of your proof claim FAILED: "
        f"{reason}\n"
        "Fix exactly this in the project and reconfirm. If it cannot be fixed "
        "honestly, reply with FAILED — <the concrete blocker>."
    )


def prove(
    adapter: ProverAdapter,
    node: str,
    spec: str,
    project_dir: str,
    *,
    max_steers: int = 3,
    steerer: Steerer | None = None,
    verifier: Callable[..., VerifyResult] | None = verify_proof,
    judge_policy: str = "auto",
    max_gate_folds: int = 1,
    triggers: TriggerEngine | None = None,
) -> ProofResult:
    """Drive ``adapter`` to prove ``node`` against ``spec``, steering as needed.

    The loop is backend-agnostic: ``adapter`` is the ONLY thing that differs
    between Claude-on-Max and Aristotle. ``steerer`` is the shared judge; when
    ``None`` a default :class:`Steerer` (scrubbed ``claude`` CLI) is used.

    Args:
        adapter: A :class:`ProverAdapter` (Claude, Aristotle, or Codex).
        node: The target node id.
        spec: The node's spec prompt (statement + structural hints).
        project_dir: The Lean project directory.
        max_steers: Cap on steers for this run — live-judge steers and gate folds
            both count against it (the high-bar judge rarely reaches it).
        steerer: The shared steering judge; injected in tests.
        verifier: The honesty gate run on a *claimed* ``proved`` — it independently
            checks the landed Lean compiles with no ``sorry``/``admit`` and, on
            failure, the verdict is downgraded to ``failed``. ``None`` disables it
            (and tests inject a fake). Defaults to :func:`servers.prover.verify.verify_proof`.
            Called as ``verifier(node, project_dir, baseline=baseline)`` where
            ``baseline`` is the git snapshot captured below.
        judge_policy: When mid-run steering happens. ``"auto"`` (default) —
            trigger-gated steering for an ``IN_FLIGHT`` backend only (a
            self-composing signal steers directly; the off-goal signal summons
            the judge as confirmation); ``"always"`` — per-window cadence
            judging for every backend (the pre-capability behaviour, restoring
            turn-granular drift-steering for the CLI backends); ``"never"`` —
            no mid-run steering at all (signals still accumulate as telemetry).
        max_gate_folds: How many times a rejected ``proved`` claim may be folded
            back as a corrective turn for a fold-capable backend. ``0`` disables
            the fold (a rejected claim downgrades immediately, pre-fold behaviour).
        triggers: The structured-signal engine; injected in tests (a fresh
            engine keyed to ``node`` is built when ``None``). Its summary lands
            in ``result.meta["steering"]["signals"]`` for every policy/backend.

    Returns:
        The adapter's terminal :class:`ProofResult` (``proved`` or ``failed``) —
        with a claimed ``proved`` only allowed to stand once the gate confirms it.
    """
    judge = steerer if steerer is not None else Steerer()
    capability = getattr(adapter, "steering", SteeringCapability.BETWEEN_TURNS)
    judge_live = _live_judge_enabled(capability, judge_policy)
    # Snapshot the project's git state BEFORE the backend starts, so the gate can
    # attribute changes to THIS run (pre-existing dirty files must neither pass a
    # run that landed nothing nor fail one on a sibling's in-progress sorry).
    # Threaded explicitly into the verifier — no global state. The SAME baseline
    # is reused on a post-fold re-verify: it is a static pre-run snapshot, so the
    # corrective turn's edits are attributed exactly like the first turn's.
    baseline = capture_baseline(project_dir) if verifier is not None else None
    started_at = time.monotonic()
    run = adapter.start(node, spec, project_dir)
    goal = run.goal or spec

    engine = triggers if triggers is not None else TriggerEngine(node_hint=node)
    # Judge-usage BASELINE: a caller may inject one shared Steerer across many
    # runs; stamping its cumulative counters would double-count earlier runs in
    # every later ledger entry. Stamp per-run deltas instead.
    judge_calls0 = getattr(judge, "calls", 0) or 0
    judge_usage0 = dict(getattr(judge, "usage", None) or {})

    # Shared across the initial consume and any post-fold corrective consume, so
    # max_steers is a genuine per-run cap and the window never leaks across folds.
    state: dict[str, Any] = {"steers": 0, "window": []}

    def _deliver(correction: str, source: str) -> None:
        logger.info(
            "driver: steering %s run (#%d, %s): %s",
            adapter.name, state["steers"] + 1, source, correction[:120],
        )
        adapter.steer(run, correction)
        state["steers"] += 1
        state["window"] = []  # judge post-steer behaviour afresh

    def _judge_steer() -> None:
        """Consult the shared judge over the current window; steer if it says so."""
        if judge.off_course(goal, state["window"]):
            correction = judge.correction(goal, state["window"])
            if correction:
                _deliver(correction, "judge")

    def _consume() -> None:
        """Drain ``adapter.events(run)``, steering per the capability policy.

        Adapters guard re-entry (a ``started`` flag): after the initial consume
        exhausted the stream, a fold's ``steer()`` + re-entry runs ONLY the
        queued corrective turn — the first turn is never replayed.
        """
        for event in adapter.events(run):
            state["window"].append(event)
            fired = engine.observe(event)  # always observed — telemetry is free
            if state["steers"] >= max_steers:
                continue
            if judge_policy == "always":
                # Legacy escape hatch: per-window cadence judging, any backend.
                _judge_steer()
                continue
            if not judge_live:
                continue  # "never", or auto on a turn-granular backend
            # "auto" on an in-flight backend: steer only when a signal fires.
            for trig in fired:
                if state["steers"] >= max_steers:
                    break
                if trig.correction:
                    _deliver(trig.correction, trig.signal)  # zero judge calls
                else:
                    _judge_steer()  # the judgement-call signal → confirmation

    def _stamp_steering(res: ProofResult) -> None:
        """Merge steering telemetry AND the run's usage rollup into the meta.

        The adapter reports its own flat worker usage in ``meta["usage"]``;
        here it is nested under ``usage.worker`` and joined by the judge's
        accumulated usage (when the steerer tracks it — injected fakes may
        not) and the run's wall clock. This is the only place worker and
        judge totals meet, so the ledger entry one level up (the prover
        server) sees the complete, final numbers on every exit path.
        """
        meta = dict(res.meta or {})
        worker_usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        if isinstance(worker_usage, dict) and "worker" in worker_usage:
            worker_usage = worker_usage["worker"]  # idempotent re-stamp
        usage: dict[str, Any] = {
            "worker": worker_usage,
            "wall_seconds": round(time.monotonic() - started_at, 3),
        }
        judge_calls = getattr(judge, "calls", None)
        if judge_calls is not None:
            judge_now = getattr(judge, "usage", None) or {}
            delta = {k: round(v - float(judge_usage0.get(k) or 0), 6)
                     for k, v in judge_now.items()
                     if isinstance(v, (int, float))}
            usage["judge"] = {**delta, "calls": judge_calls - judge_calls0}
        meta["usage"] = usage
        meta["steering"] = {
            "capability": capability.value,
            "policy": judge_policy,
            "steers": state["steers"],
            "signals": engine.summary(),
        }
        res.meta = meta

    _consume()
    result = adapter.result(run)
    if not result.backend:
        result.backend = adapter.name
    _stamp_steering(result)

    if not (result.proved and verifier is not None):
        return result

    # Honesty gate: a backend's "proved" is the worker's CLAIM. Independently verify
    # the landed Lean before letting it stand — folding the rejection back as one
    # corrective turn where the session allows it, downgrading otherwise, so no
    # backend can report a sorry'd or non-compiling file as proved.
    folds = 0
    while True:
        gate = verifier(node, project_dir, baseline=baseline)
        result.meta = {**(result.meta or {}), "verify": gate.checks}
        if folds:
            result.meta["gate_folds"] = folds
        if gate.ok:
            _stamp_steering(result)  # refresh wall_seconds to include the gate
            result.meta = {**result.meta, "verify": gate.checks}
            if folds:
                result.meta["gate_folds"] = folds
            return result

        logger.warning(
            "driver: verification gate REJECTED %s's proof claim for %s: %s",
            adapter.name, node, gate.reason,
        )
        can_fold = (
            capability in _FOLD_CAPABLE
            and folds < max_gate_folds
            and state["steers"] < max_steers
        )
        if not can_fold:
            # Terminal downgrade — the pre-fold behaviour, and the only path for
            # an IN_FLIGHT backend (whose result() must not be called twice).
            _stamp_steering(result)  # refresh wall_seconds to include the gate
            result.meta = {**result.meta, "verify": gate.checks}
            if folds:
                result.meta["gate_folds"] = folds
            result.meta["claimed_proved"] = True
            result.status = "failed"
            result.reason = f"verification gate: {gate.reason}"
            return result

        folds += 1
        state["steers"] += 1  # a fold consumes steer budget like any steer
        correction = _fold_correction(gate.reason)
        logger.info(
            "driver: folding gate reason back into %s (fold #%d): %s",
            adapter.name, folds, gate.reason[:120],
        )
        adapter.steer(run, correction)
        state["window"] = []  # judge the corrective turn afresh
        _consume()  # drains ONLY the corrective turn (adapters guard re-entry)
        result = adapter.result(run)
        if not result.backend:
            result.backend = adapter.name
        _stamp_steering(result)
        result.meta = {**(result.meta or {}), "gate_folds": folds}
        if not result.proved:
            # The corrective turn ended in an honest FAILED (or a timeout) —
            # stand as-is; re-verifying a non-claim would be meaningless.
            return result
        # A renewed proved claim: loop back and re-verify it.
