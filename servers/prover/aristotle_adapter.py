"""Aristotle adapter — wraps the existing Aristotle integration as a prover backend.

A thin :class:`~servers.prover.base.ProverAdapter` over the proven Aristotle
integration in :mod:`servers.aristotle.core` (the C-side work). It exposes the
SAME four-method surface as the Claude adapter, so the SHARED driver + steerer
drive Aristotle with no change:

* ``start``  — build the node spec (``build_node_spec``) and ``submit`` it with
  the whole Lean project bundled (``AristotleManager.submit``).
* ``events`` — poll the running task and yield each new Aristotle event,
  normalized to a :class:`~servers.prover.base.Event`; ends when the task reaches
  a terminal status.
* ``steer``  — inject the correction via ``project.ask`` (``AristotleManager.steer``)
  — Aristotle's native in-flight steering, the analog of Claude's resumed turn.
* ``result`` — on terminal status, land the returned Lean files into the project,
  record the proof in the node's prose, and report a
  :class:`~servers.prover.base.ProofResult`.

Aristotle is **opt-in and free**: it needs the ``aristotle`` extra
(``aristotlelib``) plus ``ARISTOTLE_API_KEY`` and network. ``aristotlelib`` is
imported lazily by ``AristotleManager``, so this module imports cleanly without
the extra installed (the driver/steerer/contract never touch it).

The async ``AristotleManager`` is bridged to the synchronous adapter surface via
a single private event loop owned by the adapter, so the driver stays a plain
loop with no event-loop assumptions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from servers.aristotle.core import (
    DEFAULT_DELEGATE_SYSTEM,
    AristotleManager,
    _overlay_project,
    _record_proof_in_prose,
    load_node,
)

from .base import Event, EventKind, ProofResult, ProverAdapter, Run

logger = logging.getLogger(__name__)

_IN_FLIGHT = frozenset({"QUEUED", "IN_PROGRESS"})

# Map Aristotle's event-type names onto the normalized steer vocabulary.
_EVENT_KIND_MAP = {
    "EDITING_FILE": EventKind.EDIT,
    "THINKING": EventKind.THINKING,
    "MESSAGE": EventKind.MESSAGE,
    "ERROR": EventKind.ERROR,
}


def _normalize(raw_event: Any) -> Event:
    """Map one aristotlelib ``Event`` onto a normalized :class:`Event`."""
    name = getattr(getattr(raw_event, "event_type", None), "name", "") or ""
    kind = _EVENT_KIND_MAP.get(name, EventKind.OTHER)
    content = str(getattr(raw_event, "content", "") or "")
    return Event(kind, content, raw=raw_event)


@dataclass
class _AristotleRun:
    """Native run state for the Aristotle backend (held inside ``Run.handle``)."""

    node: str
    graph_path: str
    project_dir: str
    session_id: str
    poll_interval: int
    max_wait_seconds: float | None
    final_status: str = ""
    final_summary: str = ""


class AristotleAdapter(ProverAdapter):
    """Drive Harmonic's Aristotle as a swappable prover backend.

    Args:
        graph_path: Path to the plan's ``graph.json`` (the node spec source).
        manager: An :class:`AristotleManager`; when ``None`` one is built lazily
            (constructing it does NOT import ``aristotlelib``).
        system_prompt: The no-cheating delegate contract (defaults to
            :data:`servers.aristotle.core.DEFAULT_DELEGATE_SYSTEM`).
        poll_interval / max_wait_seconds: Forwarded to the polling loop.
        loop: An asyncio event loop to run the async manager on; created lazily
            when ``None``.
    """

    name = "aristotle"

    def __init__(
        self,
        *,
        graph_path: str | Path,
        manager: AristotleManager | None = None,
        system_prompt: str = DEFAULT_DELEGATE_SYSTEM,
        poll_interval: int = 20,
        max_wait_seconds: float | None = 5400,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._graph_path = str(graph_path)
        self._system_prompt = system_prompt
        self._poll_interval = poll_interval
        self._max_wait_seconds = max_wait_seconds
        self._manager = manager
        self._loop = loop

    # ------------------------------------------------------------------
    # Async bridge
    # ------------------------------------------------------------------

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._owns_loop = True
        return self._loop

    def _run(self, coro: Any) -> Any:
        return self._get_loop().run_until_complete(coro)

    def close(self) -> None:
        """Close the adapter's private event loop (if it created one).

        Called automatically at the end of :meth:`result`; idempotent. A loop
        injected via the ctor is the caller's to close."""
        if getattr(self, "_owns_loop", False) and self._loop is not None and not self._loop.is_closed():
            self._loop.close()

    def _mgr(self, project_dir: str) -> AristotleManager:
        if self._manager is None:
            self._manager = AristotleManager(download_dir=str(Path(project_dir) / ".aristotle-cache"))
        return self._manager

    # ------------------------------------------------------------------
    # Adapter surface
    # ------------------------------------------------------------------

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        """Submit the node + bundled project to Aristotle; return the run handle."""
        project_dir = str(project_dir)
        prompt = f"{self._system_prompt}\n\n{spec}"
        mgr = self._mgr(project_dir)
        self._run(mgr.submit(session_id=node, prompt=prompt, project_dir=project_dir))
        state = _AristotleRun(
            node=node,
            graph_path=self._graph_path,
            project_dir=project_dir,
            session_id=node,
            poll_interval=self._poll_interval,
            max_wait_seconds=self._max_wait_seconds,
        )
        return Run(backend=self.name, goal=spec, project_dir=project_dir, handle=state)

    def events(self, run: Run) -> Iterator[Event]:
        """Poll the task to terminal, yielding each new normalized event.

        Mirrors ``AristotleInference._poll_to_terminal`` but as a *generator*:
        the SHARED driver consumes events one at a time and may call
        :meth:`steer` between them, so the in-flight ``project.ask`` steering is
        driven by the same loop that drives the Claude backend.
        """
        state: _AristotleRun = run.handle
        mgr = self._mgr(state.project_dir)
        seen: set[str] = set()
        start = time.monotonic()

        while True:
            status_info = self._run(mgr.poll(state.session_id))
            status = str(status_info.get("status", "UNKNOWN"))
            state.final_status = status

            new_events = self._fetch_new(mgr, state.session_id, seen)
            for raw in new_events:
                yield _normalize(raw)

            if status not in _IN_FLIGHT:
                state.final_summary = str(status_info.get("output_summary", ""))
                break
            if state.max_wait_seconds is not None and (time.monotonic() - start) > state.max_wait_seconds:
                logger.warning("aristotle adapter: %s exceeded max_wait_seconds", state.node)
                break
            time.sleep(state.poll_interval)

    def steer(self, run: Run, message: str) -> None:
        """Redirect the live task via ``project.ask`` (Aristotle in-flight steer).

        Best-effort and non-raising: ``AristotleManager.steer`` already guards
        against steering a non-in-flight task and catches ask() failures.
        """
        state: _AristotleRun = run.handle
        mgr = self._mgr(state.project_dir)
        try:
            res = self._run(mgr.steer(session_id=state.session_id, prompt=message))
            if res.get("error"):
                logger.info("aristotle adapter: steer declined (%s)", res["error"])
            else:
                logger.info("aristotle adapter: steered in-flight: %s", message[:120])
        except Exception as err:  # pragma: no cover - defensive
            logger.warning("aristotle adapter: steer failed (continuing): %s", err)

    def result(self, run: Run) -> ProofResult:
        """Land the returned Lean files + record the proof; report the result."""
        state: _AristotleRun = run.handle
        mgr = self._mgr(state.project_dir)
        project_dir = Path(state.project_dir)

        try:
            node = load_node(Path(state.graph_path), state.node)
            summary = state.final_summary
            landed = 0
            with _temp_dir() as td:
                root = self._run(mgr.download_result(state.session_id, td))
                if root is not None:
                    landed = _overlay_project(root, project_dir)
            if landed:
                _record_proof_in_prose(Path(state.graph_path), state.node, node, project_dir, summary)
        finally:
            # The run is terminal — release the adapter's private event loop.
            self.close()

        # Aristotle "proved" = the fully-clean terminal status (COMPLETE) that
        # landed files. COMPLETE_WITH_ERRORS is NOT a proved-claim — the task hit
        # errors it could not fully resolve — but the server-side session is
        # resumable, so it is reported failed with sub-status "continuable". The
        # downstream verify gate / jury do the real verification either way.
        proved = state.final_status == "COMPLETE" and landed > 0
        reason = "" if proved else (
            f"Aristotle terminal status {state.final_status} landed {landed} file(s)"
        )
        meta: dict[str, Any] = {"aristotle_status": state.final_status}
        if state.final_status == "COMPLETE_WITH_ERRORS":
            meta["sub_status"] = "continuable"
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=summary,
            reason=reason,
            backend=self.name,
            landed_files=landed,
            meta=meta,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_new(self, mgr: AristotleManager, session_id: str, seen: set[str]) -> list[Any]:
        """Fetch this session's task events not already seen (oldest-first)."""
        session = mgr._sessions.get(session_id)
        if session is None or session.last_task is None:
            return []
        try:
            events, _ = self._run(session.last_task.get_events(limit=50, newest_first=True))
        except Exception as err:  # pragma: no cover - transient API hiccup
            logger.debug("aristotle adapter: event fetch failed (continuing): %s", err)
            return []
        fresh = [e for e in events if getattr(e, "event_id", None) not in seen]
        for e in fresh:
            seen.add(getattr(e, "event_id", None))
        fresh.reverse()
        return fresh


class _temp_dir:
    """Context manager yielding a fresh temp directory path (str)."""

    def __enter__(self) -> str:
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        return self._td.name

    def __exit__(self, *exc: Any) -> bool:
        self._td.cleanup()
        return False
