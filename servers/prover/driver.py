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
3. While under the ``max_steers`` cap, we ask the shared steerer whether the run
   is off-course; if so we inject ``adapter.steer(run, correction)``, count it,
   and clear the window (so the next judgement is made on post-steer behaviour).
   This runs only when the correction can act on the *current* run — i.e. for an
   ``IN_FLIGHT`` backend (Aristotle). For a turn-granular ``BETWEEN_TURNS`` backend
   (``claude -p`` / ``codex exec``) a correction can land only as the *next* resumed
   turn, so per-event judging is low-value for its cost (a judge call per window
   plus an extra turn) and is **skipped by default** (``judge_policy="auto"``);
   such backends are steered by the verify-gate fold instead, which fires on the
   highest-signal event — a rejected proof. See
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
from collections.abc import Callable
from typing import Any

from .base import ProofResult, ProverAdapter, SteeringCapability
from .steerer import Steerer
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
    """Whether the per-event steering judge runs for this backend."""
    if judge_policy == "always":
        return True
    if judge_policy == "never":
        return False
    # "auto": reserve the per-event judge for a live-steerable backend. A
    # turn-granular one can act on a correction only as its NEXT resumed turn, so
    # per-event judging is low-value for its cost — it is steered by the gate-fold.
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
        judge_policy: When the per-event live judge runs. ``"auto"`` (default) —
            only for an ``IN_FLIGHT`` backend; ``"always"`` — every backend (the
            pre-capability behaviour, restoring turn-granular drift-steering for
            the CLI backends); ``"never"`` — no live judge at all.
        max_gate_folds: How many times a rejected ``proved`` claim may be folded
            back as a corrective turn for a fold-capable backend. ``0`` disables
            the fold (a rejected claim downgrades immediately, pre-fold behaviour).

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
    run = adapter.start(node, spec, project_dir)
    goal = run.goal or spec

    # Shared across the initial consume and any post-fold corrective consume, so
    # max_steers is a genuine per-run cap and the window never leaks across folds.
    state: dict[str, Any] = {"steers": 0, "window": []}

    def _consume() -> None:
        """Drain ``adapter.events(run)``, live-judging when enabled.

        Adapters guard re-entry (a ``started`` flag): after the initial consume
        exhausted the stream, a fold's ``steer()`` + re-entry runs ONLY the
        queued corrective turn — the first turn is never replayed.
        """
        for event in adapter.events(run):
            state["window"].append(event)
            if not judge_live or state["steers"] >= max_steers:
                continue
            if judge.off_course(goal, state["window"]):
                correction = judge.correction(goal, state["window"])
                if correction:
                    logger.info(
                        "driver: steering %s run (#%d): %s",
                        adapter.name, state["steers"] + 1, correction[:120],
                    )
                    adapter.steer(run, correction)
                    state["steers"] += 1
                    state["window"] = []  # judge post-steer behaviour afresh

    _consume()
    result = adapter.result(run)
    if not result.backend:
        result.backend = adapter.name

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
        result.meta = {**(result.meta or {}), "gate_folds": folds}
        if not result.proved:
            # The corrective turn ended in an honest FAILED (or a timeout) —
            # stand as-is; re-verifying a non-claim would be meaningless.
            return result
        # A renewed proved claim: loop back and re-verify it.
