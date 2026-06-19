"""Claude-Max adapter — drives a headless ``claude -p`` worker as a prover backend.

This is the **default** backend: a full Claude Code session running headless
(``claude -p``), so the prover can use the project's ``autoform-repl`` /
``autoform-lsp`` / ``autoform-mathlib`` MCP tools to compile-to-iterate exactly as
the in-session ``autoform-worker`` does. It runs on the **Claude Max
subscription** — every ``claude`` invocation has ``ANTHROPIC_API_KEY`` scrubbed
from its environment, so it is billed to the subscription, never the API.

The four adapter methods:

* ``start``  — assemble the system prompt (the ``autoform-worker`` discipline +
  the node's spec) and launch the first ``claude -p`` turn with
  ``--output-format stream-json`` (streamed events) + ``--print``.
* ``events`` — parse the stream-json lines into normalized
  :class:`~servers.prover.base.Event`\\ s. Captures the ``session_id`` from the
  stream so a later steer can ``--resume`` the SAME session.
* ``steer``  — inject the correction as a **follow-up turn** on the captured
  session (``claude --resume <session_id> -p <correction>``). See the module
  note below for why this (rather than stdin streaming) is the mechanism.
* ``result`` — the final assistant text (the Lean proof, or an honest ``FAILED``)
  parsed into a :class:`~servers.prover.base.ProofResult`.

THE STEER MECHANISM (the one real design choice — documented for the summary):
``claude -p`` is a *batch* invocation: it reads one prompt, streams its work, and
exits. There is no live stdin channel to interrupt a turn mid-flight. So a steer
is delivered as the **next turn of the same conversation**: we capture the
``session_id`` emitted on the stream and, when the shared steerer asks to steer,
queue the correction; the driver's event loop, on reaching the end of the current
turn's stream, sees a queued steer and launches a follow-up turn with
``claude --resume <session_id> -p "<correction>"`` (full conversation context
preserved). This is the simplest mechanism that actually works with the public
CLI: turn-granular steering rather than token-granular interruption. It keeps the
adapter's surface identical to Aristotle's (whose ``project.ask`` is likewise a
new task on the live session), so the SHARED driver loop is unchanged. ``events``
transparently chains the resumed turn's stream after the current one, so to the
driver it is one continuous event iterator.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .base import Event, EventKind, ProofResult, ProverAdapter, Run

logger = logging.getLogger(__name__)

# Default model for the headless worker (overridable via ctor / env).
DEFAULT_MODEL = "opus"

# The prover discipline the headless worker is held to. This is the SAME
# no-cheating / honest-FAILED contract the in-session ``autoform-worker`` agent
# carries (agents/autoform-worker.md); kept here so the Claude *backend* states
# its contract even when launched outside an agent harness.
WORKER_SYSTEM_PROMPT = (
    "You are a Lean 4 / Mathlib formalization worker — a prover backend. Given a target "
    "node and its spec, search Mathlib, write a GENUINE Lean 4 proof, and compile-to-iterate "
    "via the autoform-repl / autoform-lsp MCP tools until it compiles cleanly with no gaps.\n\n"
    "Hard rule — no cheating: `sorry`, `admit`, raw `axiom`, and `native_decide` are NEVER an "
    "acceptable finished proof; do not hide a gap behind an `opaque`/`macro`/structure field or "
    "a vacuous `False.elim`. The statement must be proved faithfully — no weakening, no smuggled "
    "hypotheses, no pinned-general parameter. Grep the whole project for `sorry`/`admit`/`axiom` "
    "before calling anything done.\n\n"
    "Billing: scrub `ANTHROPIC_API_KEY` from every subprocess you spawn (`env -u "
    "ANTHROPIC_API_KEY …`) so no `lake`/`git`/script child can bill the Anthropic API.\n\n"
    "Output: on success, write the proof into the node's file and report the final Lean content "
    "plus a one-line REPL compilation status. If you could NOT discharge the target (does not "
    "compile, a `sorry` remains, build will not run, or you ran out of road), do NOT deliver a "
    "success-shaped result — end with a line `FAILED — <one-line reason>` and the concrete blocker. "
    "Reporting FAILED honestly is correct; delivering a sorry'd file as done is the one thing you "
    "must never do."
)


def _scrubbed_env() -> dict[str, str]:
    """A copy of the environment with ``ANTHROPIC_API_KEY`` removed → Max OAuth."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _build_spec_prompt(node: str, spec: str) -> str:
    """The first-turn user prompt: the node target + its spec."""
    return (
        f"# Formalization target: {node}\n\n"
        f"{spec}\n\n"
        "Prove this node now. Write the proof into the project and report the result "
        "(or an honest `FAILED — <reason>` if you cannot)."
    )


def _classify_stream_event(obj: dict[str, Any]) -> Event | None:
    """Map one parsed stream-json object onto a normalized :class:`Event`.

    The ``claude -p --output-format stream-json`` stream emits objects with a
    ``type`` field (``system`` / ``assistant`` / ``user`` / ``result``). We pull
    out a short text payload and a normalized kind; objects with no useful
    payload return ``None`` (skipped).
    """
    etype = obj.get("type")

    if etype == "assistant":
        message = obj.get("message", {})
        for block in message.get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                return Event(EventKind.MESSAGE, block["text"], raw=obj)
            if btype == "thinking" and block.get("thinking", "").strip():
                return Event(EventKind.THINKING, block["thinking"], raw=obj)
            if btype == "tool_use":
                name = block.get("name", "tool")
                tin = block.get("input", {})
                # Edits to .lean files are the load-bearing "edit" signal.
                target = str(tin.get("file_path") or tin.get("path") or "")
                kind = EventKind.EDIT if name in ("Edit", "Write", "MultiEdit") else EventKind.TOOL
                return Event(kind, f"{name} {target}".strip(), raw=obj)
        return None

    if etype == "user":
        # Tool results (build output, REPL diagnostics) come back as user turns.
        message = obj.get("message", {})
        for block in message.get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = str(content)
                kind = EventKind.ERROR if block.get("is_error") else EventKind.TOOL
                return Event(kind, text, raw=obj)
        return None

    if etype == "result":
        return Event(EventKind.RESULT, str(obj.get("result", "")), raw=obj)

    return None


@dataclass
class _ClaudeRun:
    """Native run state for the Claude backend (held inside ``Run.handle``)."""

    node: str
    spec: str
    project_dir: str
    model: str
    session_id: str = ""
    pending_steer: str | None = None
    final_text: str = ""
    started: bool = False
    extra_args: list[str] = field(default_factory=list)


class ClaudeAdapter(ProverAdapter):
    """Drive a headless ``claude -p`` worker as a swappable prover backend.

    Args:
        model: Model id passed to ``claude --model`` (default ``"opus"``).
        system_prompt: The worker discipline (defaults to
            :data:`WORKER_SYSTEM_PROMPT`).
        extra_args: Extra ``claude`` CLI args (e.g. ``--mcp-config`` /
            ``--permission-mode``) the caller wants threaded through.
        runner: Injectable launcher ``(args, env, cwd) -> Iterator[str]`` yielding
            stream-json lines. Defaults to a real ``subprocess`` launcher; tests
            inject a fake so no live ``claude`` process is spawned.
    """

    name = "claude"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        system_prompt: str = WORKER_SYSTEM_PROMPT,
        extra_args: list[str] | None = None,
        runner: Any | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._extra_args = list(extra_args or [])
        self._runner = runner or _subprocess_stream_runner

    # ------------------------------------------------------------------
    # Adapter surface
    # ------------------------------------------------------------------

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        state = _ClaudeRun(
            node=node,
            spec=spec,
            project_dir=str(project_dir),
            model=self._model,
            extra_args=self._extra_args,
        )
        return Run(backend=self.name, goal=spec, project_dir=str(project_dir), handle=state)

    def events(self, run: Run) -> Iterator[Event]:
        """Stream events from the first turn, then chain any steered follow-up turns.

        Each turn is one ``claude -p`` invocation. We capture ``session_id`` from
        the stream so a steer (queued by the driver via :meth:`steer`) can
        ``--resume`` the same conversation as the *next* turn — chained
        transparently so the driver sees one continuous iterator.
        """
        state: _ClaudeRun = run.handle

        # First turn: system prompt + spec.
        first_prompt = _build_spec_prompt(state.node, state.spec)
        yield from self._run_turn(state, first_prompt, resume=False)

        # Drain any steers the driver queued during the turn (turn-granular
        # steering — see the module docstring on the mechanism).
        while state.pending_steer:
            correction = state.pending_steer
            state.pending_steer = None
            yield from self._run_turn(state, correction, resume=True)

    def steer(self, run: Run, message: str) -> None:
        """Queue ``message`` as the next follow-up turn (delivered between turns).

        Best-effort and non-raising: the actual ``--resume`` launch happens in
        :meth:`events` when the current turn's stream ends.
        """
        state: _ClaudeRun = run.handle
        # Coalesce: keep the latest correction if several arrive before the turn ends.
        state.pending_steer = message
        logger.info("claude adapter: queued steer for next turn: %s", message[:120])

    def result(self, run: Run) -> ProofResult:
        state: _ClaudeRun = run.handle
        text = (state.final_text or "").strip()
        proved = not _looks_failed(text)
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=text,
            reason="" if proved else _failure_reason(text),
            backend=self.name,
            landed_files=0,  # files are written in-place by the worker's own tools
            meta={"session_id": state.session_id, "model": state.model},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_turn(self, state: _ClaudeRun, prompt: str, *, resume: bool) -> Iterator[Event]:
        args = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", state.model]
        if resume and state.session_id:
            args += ["--resume", state.session_id]
        elif not resume:
            args += ["--append-system-prompt", self._system_prompt]
        args += state.extra_args

        for line in self._runner(args, _scrubbed_env(), state.project_dir):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Capture the session id (emitted on the ``system: init`` line and the
            # ``result`` line) so a steer can resume this exact conversation.
            sid = obj.get("session_id")
            if sid:
                state.session_id = sid
            event = _classify_stream_event(obj)
            if event is None:
                continue
            if event.kind is EventKind.RESULT and event.content:
                state.final_text = event.content
            yield event


def _subprocess_stream_runner(args: list[str], env: dict[str, str], cwd: str):
    """Real launcher: run ``claude`` and yield its stdout lines (stream-json).

    Lives behind the injectable ``runner`` seam so the adapter is unit-testable
    without spawning ``claude``.
    """
    proc = subprocess.Popen(
        args,
        cwd=cwd or None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line
    finally:
        proc.stdout and proc.stdout.close()
        proc.wait()


def _looks_failed(text: str) -> bool:
    """Heuristic: did the worker report an honest FAILED rather than a proof?

    The worker contract ends a failure with a ``FAILED — <reason>`` line; an empty
    result is also treated as a failure (no proof produced).
    """
    if not text.strip():
        return True
    return "FAILED" in text.upper().split("\n")[0] or "\nFAILED" in ("\n" + text).upper()


def _failure_reason(text: str) -> str:
    """Extract the one-line reason from a ``FAILED — <reason>`` report."""
    if not text.strip():
        return "claude worker produced no output"
    for line in text.splitlines():
        up = line.strip()
        if up.upper().startswith("FAILED"):
            # Strip the "FAILED —/-/:" lead-in.
            rest = up[6:].lstrip(" —-:")
            return rest or "worker reported FAILED"
    return "worker reported FAILED"
