"""Codex adapter — drives a headless OpenAI ``codex exec`` worker as a prover backend.

A third swappable backend alongside Claude-on-Max and Aristotle. It mirrors the
Claude adapter: launch a headless coding-agent CLI on the node's spec, normalize
its event stream onto the shared :class:`~servers.prover.base.Event` vocabulary,
steer turn-granularly by resuming the session, and parse the final report into a
:class:`~servers.prover.base.ProofResult` — held to the SAME no-cheating /
honest-``FAILED`` discipline. Only the CLI and its output schema differ, so the
shared driver + steerer are unchanged.

**Billing / auth.** Codex runs on its OWN auth — the ``codex`` CLI's logged-in
account (a ChatGPT subscription, or an OpenAI API key), **not** the Claude Max
subscription. This backend therefore does not depend on ``ANTHROPIC_API_KEY`` (it
drops it as hygiene) and simply inherits the environment ``codex login`` set up.

**Interface assumptions** (``codex exec`` JSON mode). This targets
``codex exec --json`` emitting JSONL events and ``codex exec resume <id>`` for a
follow-up (steer) turn. Event-classification and the session-id capture are
deliberately DEFENSIVE — several codex schema shapes are tolerated (top-level
``type`` or nested ``item.type``) — and the proved/failed verdict rests on the
worker's final ``FAILED — <reason>`` line, **not** on any single schema field. So a
codex build whose JSON differs still yields a correct verdict from the final text;
steering merely degrades to a no-op if no session id is seen. Override the binary,
model, or flags via the ctor / ``AUTOFORM_CODEX_BIN`` if your codex differs.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .base import Event, EventKind, ProofResult, ProverAdapter, Run

# Reuse the SHARED honesty parsing + the generic subprocess runner so "what counts
# as an honest FAILED" has ONE definition across every CLI-agent backend.
from .claude_adapter import _failure_reason, _looks_failed, _subprocess_stream_runner

logger = logging.getLogger(__name__)

#: The codex binary (overridable so a pinned path / wrapper can be used).
DEFAULT_CODEX_BIN = os.environ.get("AUTOFORM_CODEX_BIN", "codex")
#: Full autonomy (edit files + run ``lake``) — codex's analogue of Claude's
#: ``--dangerously-skip-permissions``. Pass ``autonomy_args=[]`` to run codex under
#: its default approvals/sandbox instead.
DEFAULT_AUTONOMY_ARGS = ["--dangerously-bypass-approvals-and-sandbox"]

# The SAME no-cheating / honest-FAILED contract the Claude backend states, framed
# for codex (no separate system-prompt flag, so it is inlined into the first turn).
CODEX_SYSTEM_PROMPT = (
    "You are a Lean 4 / Mathlib formalization worker — a prover backend. Given a target "
    "node and its spec, search Mathlib, write a GENUINE Lean 4 proof, and compile-to-iterate "
    "(run `lake env lean` / the project's REPL) until it compiles cleanly with no gaps.\n\n"
    "Hard rule — no cheating: `sorry`, `admit`, raw `axiom`, and `native_decide` are NEVER an "
    "acceptable finished proof; do not hide a gap behind an `opaque`/`macro`/structure field or "
    "a vacuous `False.elim`. The statement must be proved faithfully — no weakening, no smuggled "
    "hypotheses. Grep the whole project for `sorry`/`admit`/`axiom` before calling anything done.\n\n"
    "Output: on success, write the proof into the node's file and report the final Lean content "
    "plus a one-line compilation status. If you could NOT discharge the target (does not compile, "
    "a `sorry` remains, the build will not run, or you ran out of road), do NOT deliver a "
    "success-shaped result — end with a line `FAILED — <one-line reason>` naming the concrete "
    "blocker. Reporting FAILED honestly is correct; delivering a sorry'd file as done is the one "
    "thing you must never do."
)


def _codex_env() -> dict[str, str]:
    """Codex runs on its own auth (``codex login``); inherit the environment. We
    still drop ``ANTHROPIC_API_KEY`` (irrelevant to codex) as project hygiene."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _build_spec_prompt(node: str, spec: str) -> str:
    return (
        f"# Formalization target: {node}\n\n{spec}\n\n"
        "Prove this node now. Write the proof into the project and report the result "
        "(or an honest `FAILED — <reason>` if you cannot)."
    )


# codex ``exec --json`` item types → normalized EventKind (defensive sets; matching
# is also substring-based below so schema drift still classifies sensibly).
_MSG_ITEMS = {"agent_message", "assistant_message", "message"}
_THINK_ITEMS = {"reasoning", "agent_reasoning", "thinking"}
_EDIT_ITEMS = {"file_change", "patch", "apply_patch", "file_update"}
_TOOL_ITEMS = {"command_execution", "function_call", "mcp_tool_call", "local_shell_call", "exec_command"}


def _item_text(item: dict[str, Any]) -> str:
    """Best-effort text payload from a codex item across schema variants."""
    for k in ("text", "message", "content", "delta", "output", "aggregated_output", "command"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            parts = [c.get("text", "") for c in v if isinstance(c, dict)]
            if any(parts):
                return " ".join(p for p in parts if p)
    return ""


def _classify_codex_event(obj: dict[str, Any]) -> tuple[Event | None, str | None, str | None]:
    """Map one codex JSON line → ``(Event|None, agent_text|None, session_id|None)``.

    The 2nd element is the final-answer text to remember (only for agent messages);
    the 3rd is a session/thread id to capture for resume-steering. Tolerant of both
    a top-level ``type`` and a nested ``item.type``."""
    sid = (obj.get("session_id") or obj.get("thread_id")
           or obj.get("conversation_id") or obj.get("id_session"))
    item = obj.get("item") if isinstance(obj.get("item"), dict) else obj
    itype = str(item.get("type") or obj.get("type") or "").lower().split(".")[-1]
    text = _item_text(item)

    if "error" in itype or obj.get("is_error"):
        return Event(EventKind.ERROR, text, raw=obj), None, sid
    if itype in _MSG_ITEMS or itype.endswith("message"):
        return Event(EventKind.MESSAGE, text, raw=obj), (text or None), sid
    if itype in _THINK_ITEMS or "reason" in itype or "think" in itype:
        return Event(EventKind.THINKING, text, raw=obj), None, sid
    if itype in _EDIT_ITEMS or "patch" in itype or "file_change" in itype:
        return Event(EventKind.EDIT, text, raw=obj), None, sid
    if itype in _TOOL_ITEMS or "command" in itype or "tool" in itype or "exec" in itype:
        return Event(EventKind.TOOL, text, raw=obj), None, sid
    if itype in ("completed", "result") and text:
        return Event(EventKind.RESULT, text, raw=obj), None, sid
    return None, None, sid


@dataclass
class _CodexRun:
    """Native run state for the Codex backend (held inside ``Run.handle``)."""

    node: str
    spec: str
    project_dir: str
    model: str | None
    session_id: str = ""
    pending_steer: str | None = None
    final_text: str = ""
    extra_args: list[str] = field(default_factory=list)


class CodexAdapter(ProverAdapter):
    """Drive a headless ``codex exec`` worker as a swappable prover backend.

    Args mirror :class:`~servers.prover.claude_adapter.ClaudeAdapter`. ``runner`` is
    injectable ``(args, env, cwd) -> Iterator[str]`` (tests pass a fake so no live
    ``codex`` runs). ``autonomy_args`` defaults to codex's full-access flag (parity
    with Claude's ``--dangerously-skip-permissions``).
    """

    name = "codex"

    def __init__(
        self,
        *,
        model: str | None = None,
        system_prompt: str = CODEX_SYSTEM_PROMPT,
        codex_bin: str = DEFAULT_CODEX_BIN,
        autonomy_args: list[str] | None = None,
        extra_args: list[str] | None = None,
        runner: Any | None = None,
    ) -> None:
        self._model = model
        self._system_prompt = system_prompt
        self._codex_bin = codex_bin
        self._autonomy_args = list(autonomy_args if autonomy_args is not None else DEFAULT_AUTONOMY_ARGS)
        self._extra_args = list(extra_args or [])
        self._runner = runner or _subprocess_stream_runner

    # ------------------------------------------------------------------ surface

    def start(self, node: str, spec: str, project_dir: str) -> Run:
        state = _CodexRun(node=node, spec=spec, project_dir=str(project_dir),
                          model=self._model, extra_args=self._extra_args)
        return Run(backend=self.name, goal=spec, project_dir=str(project_dir), handle=state)

    def events(self, run: Run) -> Iterator[Event]:
        """First turn (discipline + spec), then any steered resume turns."""
        state: _CodexRun = run.handle
        # codex exec has no separate system-prompt flag, so the worker discipline is
        # prepended to the first user prompt.
        first = f"{self._system_prompt}\n\n{_build_spec_prompt(state.node, state.spec)}"
        yield from self._run_turn(state, first, resume=False)

        while state.pending_steer:
            correction = state.pending_steer
            state.pending_steer = None
            if not state.session_id:
                # No session captured → cannot resume with context; drop the steer
                # rather than run a context-less turn (best-effort, never raises).
                logger.info("codex adapter: no session id; dropping steer (no resume context)")
                break
            yield from self._run_turn(state, correction, resume=True)

    def steer(self, run: Run, message: str) -> None:
        """Queue ``message`` as the next resume turn (delivered between turns)."""
        state: _CodexRun = run.handle
        state.pending_steer = message
        logger.info("codex adapter: queued steer for next turn: %s", message[:120])

    def result(self, run: Run) -> ProofResult:
        state: _CodexRun = run.handle
        text = (state.final_text or "").strip()
        proved = not _looks_failed(text)
        return ProofResult(
            status="proved" if proved else "failed",
            proof_text=text,
            reason="" if proved else _failure_reason(text),
            backend=self.name,
            landed_files=0,  # files are written in-place by codex's own tools
            meta={"session_id": state.session_id, "model": state.model or "codex-default"},
        )

    # ---------------------------------------------------------------- internals

    def _run_turn(self, state: _CodexRun, prompt: str, *, resume: bool) -> Iterator[Event]:
        args = [self._codex_bin, "exec"]
        if resume and state.session_id:
            args += ["resume", state.session_id]
        args += ["--json"]
        if state.model:
            args += ["-m", state.model]
        args += self._autonomy_args + state.extra_args + [prompt]

        for line in self._runner(args, _codex_env(), state.project_dir):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            event, final, sid = _classify_codex_event(obj)
            if sid:
                state.session_id = sid
            if final:
                state.final_text = final
            if event is not None:
                if event.kind is EventKind.RESULT and event.content and not state.final_text:
                    state.final_text = event.content
                yield event
