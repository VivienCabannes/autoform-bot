"""The prover-backend ADAPTER interface — the one swappable contract.

A backend proves a node by implementing four methods. The *driver*
(:mod:`servers.prover.driver`) and the *steering judge*
(:mod:`servers.prover.steerer`) are written **against this interface alone**, so
they are byte-identical for every backend — Claude-on-Max or Aristotle. Only the
adapter's ``start`` / ``events`` / ``steer`` / ``result`` differ.

The contract the design pins down is::

    (target node + spec) -> proof written back into the node

so an adapter takes a ``node`` (the target id), a ``spec`` (its statement + the
structural hints that make it the right formalization), and the Lean
``project_dir``; it returns a :class:`ProofResult` whose ``status`` is
``"proved"`` or ``"failed"``. Producing the proof is the adapter's whole job — it
does NOT review, score, or touch the sidecar.

Everything here is plain ``dataclass`` / ``ABC`` with no third-party imports, so
the module (and the package contract) imports with no optional dependency
installed.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """Normalized event kinds the steerer reasons over.

    A backend maps its own native event vocabulary onto these so the *shared*
    steerer never sees a backend-specific event type. ``str``-valued so an event
    window serializes cleanly into the judge prompt.
    """

    THINKING = "thinking"      # the prover's reasoning / planning
    EDIT = "edit"              # a file edit / proof-state change
    MESSAGE = "message"        # assistant prose / status text
    TOOL = "tool"              # a tool call or its result (build, search, …)
    ERROR = "error"            # a compile/proof error or backend error
    RESULT = "result"          # a terminal/summary event
    OTHER = "other"            # anything else (kept, but rarely steered on)


@dataclass
class Event:
    """One normalized event from a running prover.

    Args:
        kind: The :class:`EventKind` this event maps to.
        content: A short text payload (reasoning excerpt, edited file, error
            text, …) — what the steering judge actually reads.
        raw: The backend's native event object, kept for adapters that need it
            (never read by the shared driver/steerer).
    """

    kind: EventKind
    content: str = ""
    raw: Any = None

    def render(self, *, limit: int = 300) -> str:
        """One-line ``[KIND] content`` rendering for the steer window."""
        text = (self.content or "").strip().replace("\n", " ")
        if len(text) > limit:
            text = text[:limit] + "…"
        return f"[{self.kind.value}] {text}"


@dataclass
class Run:
    """Opaque handle to one in-flight proving run.

    The driver threads this back into ``events`` / ``steer`` / ``result``; only
    the owning adapter interprets its fields. ``goal`` is carried here so the
    driver and steerer never need the spec separately.
    """

    backend: str
    goal: str = ""
    project_dir: str = ""
    handle: Any = None                       # the adapter's native run object
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProofResult:
    """Outcome of a proving run — the proof written into the node, or a failure.

    Args:
        status: ``"proved"`` or ``"failed"`` (the only two terminal verdicts the
            backend reports; it never self-certifies beyond this).
        proof_text: The Lean proof / changed content on success (or a best-effort
            summary of what was landed).
        reason: A short human-readable reason — required on ``"failed"`` (the
            honest blocker), optional on ``"proved"``.
        backend: Which backend produced the result.
        landed_files: Number of files written into the project (informational).
        meta: Backend-specific extras (project id, task id, …) — never required
            by the driver.
    """

    status: str
    proof_text: str = ""
    reason: str = ""
    backend: str = ""
    landed_files: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def proved(self) -> bool:
        return self.status == "proved"


class ProverAdapter(abc.ABC):
    """The one interface a backend implements; the driver/steerer use only this.

    Implementations:

    * :class:`servers.prover.claude_adapter.ClaudeAdapter`
    * :class:`servers.prover.aristotle_adapter.AristotleAdapter`
    * :class:`servers.prover.codex_adapter.CodexAdapter`

    The four methods are the *entire* per-backend surface. Adapters may be sync
    or async at the edges, but expose these synchronous signatures (the Aristotle
    adapter runs its async core via ``asyncio.run`` internally) so the driver is
    a plain loop with no event-loop assumptions.
    """

    #: ``"claude"`` / ``"aristotle"`` / ``"codex"`` — the value the MCP tool's ``backend`` arg
    #: selects on.
    name: str = "abstract"

    @abc.abstractmethod
    def start(self, node: str, spec: str, project_dir: str) -> Run:
        """Launch a proving run for ``node`` against ``spec`` in ``project_dir``.

        Returns a :class:`Run` handle (carrying the ``goal`` the steerer judges
        against). Must not block on completion — the driver pulls progress via
        :meth:`events`.
        """

    @abc.abstractmethod
    def events(self, run: Run):
        """Yield :class:`Event`\\ s as the run progresses, ending when terminal.

        An iterator (generator). Each item is a normalized :class:`Event`; the
        driver appends it to the steer window. When the iterator is exhausted the
        run is finished and the driver calls :meth:`result`.
        """

    @abc.abstractmethod
    def steer(self, run: Run, message: str) -> None:
        """Inject a corrective ``message`` into the live run (in-flight steer).

        Called by the driver only when the *shared* steerer decides the run is
        off-course. Best-effort: a steer that cannot be delivered (run already
        finished, transient API error) must not raise — it logs and is dropped.
        """

    @abc.abstractmethod
    def result(self, run: Run) -> ProofResult:
        """Collect the terminal :class:`ProofResult` once :meth:`events` ends."""
