"""The UNIFIED DRIVER — one loop that drives EITHER backend identically.

This module is the whole point of the unified prover: the loop below is written
against the :class:`~servers.prover.base.ProverAdapter` interface and the shared
:class:`~servers.prover.steerer.Steerer` **only**. It contains **zero**
backend-specific code, so the *same* ``prove`` drives the Claude adapter and the
Aristotle adapter with no branch on ``backend`` anywhere. Swapping the prover is
swapping the ``adapter`` argument — nothing else changes.

The contract::

    prove(adapter, node, spec, project_dir, max_steers=3) -> ProofResult

1. ``adapter.start`` launches the run.
2. We consume ``adapter.events`` one at a time, appending each to a rolling
   ``window``.
3. While under the ``max_steers`` cap, we ask the shared steerer whether the run
   is off-course; if so we inject ``adapter.steer(run, correction)``, count it,
   and clear the window (so the next judgement is made on post-steer behaviour).
4. When the event stream ends we return ``adapter.result(run)``.

That is the equivalence the spec demands: identical driver + identical steerer,
only the adapter differs.
"""

from __future__ import annotations

import logging

from .base import ProofResult, ProverAdapter
from .steerer import Steerer

logger = logging.getLogger(__name__)


def prove(
    adapter: ProverAdapter,
    node: str,
    spec: str,
    project_dir: str,
    *,
    max_steers: int = 3,
    steerer: Steerer | None = None,
) -> ProofResult:
    """Drive ``adapter`` to prove ``node`` against ``spec``, steering as needed.

    The loop is backend-agnostic: ``adapter`` is the ONLY thing that differs
    between Claude-on-Max and Aristotle. ``steerer`` is the shared judge; when
    ``None`` a default :class:`Steerer` (scrubbed ``claude`` CLI) is used.

    Args:
        adapter: A :class:`ProverAdapter` (Claude or Aristotle).
        node: The target node id.
        spec: The node's spec prompt (statement + structural hints).
        project_dir: The Lean project directory.
        max_steers: Cap on in-flight steers for this run (the high-bar judge
            rarely reaches it).
        steerer: The shared steering judge; injected in tests.

    Returns:
        The adapter's terminal :class:`ProofResult` (``proved`` or ``failed``).
    """
    judge = steerer if steerer is not None else Steerer()
    run = adapter.start(node, spec, project_dir)
    goal = run.goal or spec

    steers = 0
    window: list = []
    for event in adapter.events(run):
        window.append(event)
        if steers < max_steers and judge.off_course(goal, window):
            correction = judge.correction(goal, window)
            if correction:
                logger.info("driver: steering %s run (#%d): %s", adapter.name, steers + 1, correction[:120])
                adapter.steer(run, correction)
                steers += 1
                window = []  # judge post-steer behaviour afresh

    result = adapter.result(run)
    if not result.backend:
        result.backend = adapter.name
    return result
