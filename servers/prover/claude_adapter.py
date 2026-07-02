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

Shared CLI-agent internals (the honest-FAILED parse, the spec prompt, the env
scrub, the JSONL parse, the worker-discipline skeleton) live in ``_cli_common`` —
one definition across the Claude and Codex backends.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._cli_common import (
    _build_spec_prompt,
    _failure_reason,
    _iter_json_lines,
    _looks_failed,
    _scrubbed_env,
    _subprocess_line_runner,
    build_worker_prompt,
)
from .base import Event, EventKind, ProofResult, ProverAdapter, Run

logger = logging.getLogger(__name__)

# Default model for the headless worker (overridable via ctor / env).
DEFAULT_MODEL = "opus"

#: Full autonomy for the headless worker (edit files + run ``lake``/git) — without
#: this a bare ``claude -p`` child can approve nothing and cannot Edit/Write/Bash,
#: so the WORKER_SYSTEM_PROMPT's compile-to-iterate loop is impossible. The same
#: flag the dispatch runner (PR #13) and codex's ``DEFAULT_AUTONOMY_ARGS`` analogue
#: use. Pass ``autonomy_args=[]`` to run under the default permission prompts.
DEFAULT_AUTONOMY_ARGS = ["--dangerously-skip-permissions"]


def _default_mcp_config() -> str | None:
    """Auto-discover the MCP config for the headless worker.

    The worker discipline promises the ``autoform-repl`` / ``autoform-lsp`` MCP
    tools, so the child needs a ``--mcp-config``. Resolution order:

    1. ``AUTOFORM_MCP_CONFIG`` env var (explicit override), else
    2. the plugin's own ``.mcp.json`` at the repo root relative to this package,
       if present, else
    3. ``None`` (no flag — the worker falls back to plain ``lake`` builds).
    """
    env = os.environ.get("AUTOFORM_MCP_CONFIG", "").strip()
    if env:
        return env
    candidate = Path(__file__).resolve().parents[2] / ".mcp.json"
    if candidate.exists():
        return str(candidate)
    return None

# The prover discipline the headless worker is held to — the SAME no-cheating /
# honest-FAILED contract the in-session ``autoform-worker`` agent carries
# (agents/autoform-worker.md), assembled from the shared skeleton in ``_cli_common``
# so it cannot drift from the Codex backend's copy.
WORKER_SYSTEM_PROMPT = build_worker_prompt(
    tools_clause="via the autoform-repl / autoform-lsp MCP tools",
    extra_hyp_clause=", no pinned-general parameter",
    billing_paragraph=(
        "Billing: scrub `ANTHROPIC_API_KEY` from every subprocess you spawn (`env -u "
        "ANTHROPIC_API_KEY …`) so no `lake`/`git`/script child can bill the Anthropic API.\n\n"
    ),
    repl_word="REPL ",
    build_phrase="build will not run",
    blocker_phrase="and the concrete blocker.",
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
        autonomy_args: Permission flags for the headless worker (defaults to
            :data:`DEFAULT_AUTONOMY_ARGS`, i.e. ``--dangerously-skip-permissions``
            — without it the child cannot Edit/Write/Bash). ``[]`` disables.
        mcp_config: Path passed to ``--mcp-config`` so the worker gets the
            ``autoform-repl``/``autoform-lsp`` tools its discipline promises.
            ``None`` (default) auto-discovers via :func:`_default_mcp_config`
            (``AUTOFORM_MCP_CONFIG`` env, else the plugin's own ``.mcp.json``);
            ``""`` disables the flag entirely.
        extra_args: Extra ``claude`` CLI args the caller wants threaded through.
        runner: Injectable launcher ``(args, env, cwd, deadline=None) ->
            Iterator[str]`` yielding stream-json lines. Defaults to a real
            ``subprocess`` launcher; tests inject a fake so no live ``claude``
            process is spawned.
    """

    name = "claude"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        system_prompt: str = WORKER_SYSTEM_PROMPT,
        autonomy_args: list[str] | None = None,
        mcp_config: str | None = None,
        extra_args: list[str] | None = None,
        runner: Any | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._autonomy_args = list(autonomy_args if autonomy_args is not None else DEFAULT_AUTONOMY_ARGS)
        self._mcp_config = _default_mcp_config() if mcp_config is None else (mcp_config or None)
        self._extra_args = list(extra_args or [])
        self._runner = runner or _subprocess_line_runner

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
        args += self._autonomy_args
        if self._mcp_config:
            args += ["--mcp-config", self._mcp_config]
        args += state.extra_args

        for obj in _iter_json_lines(self._runner(args, _scrubbed_env(), state.project_dir)):
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
