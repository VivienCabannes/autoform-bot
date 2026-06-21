"""The SHARED steering policy — backend-agnostic, a pure function of (goal, window).

This is the live-steering judge, lifted from Marathon's proven ``make_claude_steer``
(``autoform/bot/aristotle_agent.py``) and generalized so it drives **either**
backend identically. The driver calls:

* :func:`off_course` ``(goal, window) -> bool`` — is the prover abandoning the
  goal? (``sorry``-ing / weakening / pinning a parameter / looping / building the
  wrong thing).
* :func:`correction` ``(goal, window) -> str`` — the short corrective instruction
  to inject.

Both read **only** ``(goal, list[Event])`` — they know nothing about Claude vs
Aristotle — so the same steerer steers any :class:`~servers.prover.base.ProverAdapter`.

The judge itself is a **rate-limited ``claude -p`` call** with the
``ANTHROPIC_API_KEY`` scrubbed (so it runs on the Max subscription, never billed
API). It has a **high bar to intervene** (a needless steer wastes a backend turn)
and a **``max_steers`` cap** enforced by the driver. One judge call decides both
questions; :func:`off_course` runs it and caches the verdict, and
:func:`correction` returns the cached corrective prompt — so the driver's
``off_course`` / ``correction`` pair costs exactly one judge call per window.

Determinism / testability: the underlying judge is injectable. The default judge
shells out to ``claude``; tests (and the FAKE-adapter driver tests) pass their own
``judge`` so no live ``claude`` process is ever spawned.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import Event

logger = logging.getLogger(__name__)

# A live-steering rubric: judge whether the prover is going OFF-COURSE relative to
# the GOAL, with a high bar to intervene.
STEER_JUDGE_RUBRIC = (
    "You are the live-steering judge for an autonomous Lean prover. You see a "
    "window of its recent events (thinking, file edits, errors, build output). Decide whether "
    "it is going OFF-COURSE relative to the GOAL — e.g. abandoning the goal, axiomatizing / "
    "`sorry`-ing / `admit`-ing what it was asked to prove, weakening or pinning a parameter it "
    "was told to keep general, smuggling the claim into a definition/structure field, going in "
    "circles, or building the wrong thing. Only steer when genuinely warranted; a needless steer "
    "wastes a backend turn, so the bar to intervene is HIGH. If steering, give a SHORT, concrete "
    "corrective instruction the prover can act on immediately."
)

# A judge takes the assembled prompt and returns the model's raw text reply.
Judge = Callable[[str], str]


def _claude_cli_judge(prompt: str, *, timeout: int = 180) -> str:
    """Default judge: invoke the ``claude`` CLI on Max (``ANTHROPIC_API_KEY`` scrubbed).

    Mirrors ``aristotle_agent._claude_cli``. Returns stdout, or ``""`` on any
    failure (a judge that errors simply declines to steer).
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # → Max OAuth, never API-billed
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return (proc.stdout or "").strip()
    except Exception as err:  # pragma: no cover - environment-dependent
        logger.warning("claude steer-judge CLI failed: %s", err)
        return ""


def _render_window(window: Sequence[Event], *, last: int = 8) -> str:
    """Render the most recent steer-relevant events into the judge prompt."""
    relevant = [
        e for e in window
        if e.kind.value in ("thinking", "edit", "message", "error", "result")
    ]
    if not relevant:
        return ""
    return "\n".join(e.render() for e in relevant[-last:])


def _build_prompt(goal: str, window: Sequence[Event], prior_reasons: Sequence[str]) -> str:
    rendered = _render_window(window)
    return (
        f"{STEER_JUDGE_RUBRIC}\n\n"
        f"## GOAL\n{goal}\n\n"
        f"## RECENT EVENTS\n{rendered}\n\n"
        f"## PRIOR STEER REASONS\n{list(prior_reasons) or '(none)'}\n\n"
        'Return ONE LINE of JSON: '
        '{"steer": <bool>, "reason": "<short>", "prompt": "<corrective instruction or empty>"}'
    )


def _parse_decision(raw: str) -> dict[str, Any] | None:
    """Parse the judge's one-line JSON verdict; ``None`` if unparseable."""
    if not raw or "{" not in raw or "}" not in raw:
        return None
    try:
        return json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        return None


@dataclass
class Steerer:
    """A rate-limited, backend-agnostic steering judge.

    Pure over ``(goal, window)``: it never inspects the backend or the run, so a
    single :class:`Steerer` instance drives Claude or Aristotle identically.

    Args:
        min_gap_s: Minimum wall-clock gap between *judge calls* (rate limit) — a
            second window arriving within the gap is skipped without calling the
            judge. Mirrors Marathon's ``min_gap_s``.
        judge: The text-in/text-out judge. Defaults to the scrubbed ``claude``
            CLI; injected in tests so no live process is spawned.

    The driver owns the ``max_steers`` cap (it counts accepted steers); the
    Steerer caps only the judge-call *rate*.
    """

    min_gap_s: float = 120.0
    judge: Judge = _claude_cli_judge
    _last_call: float = field(default=0.0, init=False)
    _reasons: list[str] = field(default_factory=list, init=False)
    # Cache so off_course() + correction() over the SAME window cost one judge call.
    _cached_key: int | None = field(default=None, init=False)
    _cached: dict[str, Any] | None = field(default=None, init=False)

    def _decide(self, goal: str, window: Sequence[Event]) -> dict[str, Any] | None:
        """Run (or reuse) the judge for this window; returns the parsed verdict.

        The decision is cached on ``(id(window), len(window))`` so that the driver's
        paired ``off_course`` / ``correction`` calls over one window invoke the
        judge once.
        """
        key = (id(window), len(window))
        cache_key = hash(key)
        if self._cached_key == cache_key:
            return self._cached

        # Reset cache for this window up front so a rate-limit/no-op short-circuit
        # below is still remembered (we don't re-call the judge for correction()).
        self._cached_key = cache_key
        self._cached = None

        rendered = _render_window(window)
        if not rendered:
            return None  # nothing steer-relevant yet

        now = time.monotonic()
        if self._last_call and (now - self._last_call) < self.min_gap_s:
            return None  # rate-limited: decline without spending a judge call

        prompt = _build_prompt(goal, window, self._reasons)
        raw = self.judge(prompt)
        self._last_call = now
        decision = _parse_decision(raw)
        self._cached = decision
        return decision

    def off_course(self, goal: str, window: Sequence[Event]) -> bool:
        """True iff the judge says the prover is off-course AND gives a correction.

        A ``steer: true`` with an empty ``prompt`` is treated as *no* steer (we
        never inject an empty instruction).
        """
        decision = self._decide(goal, window)
        if not decision:
            return False
        return bool(decision.get("steer")) and bool((decision.get("prompt") or "").strip())

    def correction(self, goal: str, window: Sequence[Event]) -> str:
        """The corrective instruction for the current window (after ``off_course``).

        Records the reason so the next judge call sees the prior-steer context
        (suppressing repeated identical steers). Returns ``""`` if, somehow, no
        decision is cached — the driver guards with ``off_course`` first, so this
        is belt-and-suspenders.
        """
        decision = self._decide(goal, window)
        if not decision:
            return ""
        prompt = (decision.get("prompt") or "").strip()
        if prompt:
            self._reasons.append((decision.get("reason") or "")[:120])
            logger.info("steer #%d: %s", len(self._reasons), self._reasons[-1])
        return prompt
