"""Shared internals for the CLI-agent prover backends (Claude, Codex).

Both ``claude -p`` and ``codex exec`` are headless coding-agent CLIs driven the same
way: launch with the worker discipline + the node spec, stream JSONL events, steer by
resuming the session, and judge the run by its final ``FAILED — <reason>`` line. The
genuinely-identical pieces live here — one definition — so "what counts as an honest
FAILED", the spec prompt, the env scrub, the JSONL parse, and the shared
worker-discipline text never drift between backends. The parts that genuinely differ
(each CLI's args, event schema, and final-text rule) stay in the adapters.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from typing import Any


def _scrubbed_env() -> dict[str, str]:
    """A copy of the environment with ``ANTHROPIC_API_KEY`` removed.

    For the Claude backend this routes billing to the Max subscription (never the
    API); for Codex (its own auth) it is project hygiene. Same operation either way.
    """
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


def build_worker_prompt(
    *,
    tools_clause: str,
    build_phrase: str,
    blocker_phrase: str,
    extra_hyp_clause: str = "",
    billing_paragraph: str = "",
    repl_word: str = "",
) -> str:
    """Assemble the worker-discipline system prompt from the shared skeleton + the
    backend-specific bits, so the Claude and Codex prompts can't drift while each
    keeps its exact text. A backend supplies only how it compiles (``tools_clause``),
    its extra faithfulness clause, an optional billing paragraph, and small wording
    deltas (``repl_word`` / ``build_phrase`` / ``blocker_phrase``).
    """
    return (
        "You are a Lean 4 / Mathlib formalization worker — a prover backend. Given a target "
        "node and its spec, search Mathlib, write a GENUINE Lean 4 proof, and compile-to-iterate "
        f"{tools_clause} until it compiles cleanly with no gaps.\n\n"
        "Hard rule — no cheating: `sorry`, `admit`, raw `axiom`, and `native_decide` are NEVER an "
        "acceptable finished proof; do not hide a gap behind an `opaque`/`macro`/structure field or "
        "a vacuous `False.elim`. The statement must be proved faithfully — no weakening, no smuggled "
        f"hypotheses{extra_hyp_clause}. Grep the whole project for `sorry`/`admit`/`axiom` "
        "before calling anything done.\n\n"
        f"{billing_paragraph}"
        "Output: on success, write the proof into the node's file and report the final Lean content "
        f"plus a one-line {repl_word}compilation status. If you could NOT discharge the target (does not "
        f"compile, a `sorry` remains, {build_phrase}, or you ran out of road), do NOT deliver a "
        f"success-shaped result — end with a line `FAILED — <one-line reason>` {blocker_phrase} "
        "Reporting FAILED honestly is correct; delivering a sorry'd file as done is the one thing you "
        "must never do."
    )


def _iter_json_lines(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Parse a stream of JSONL lines into objects, skipping blanks and unparseable
    lines — the boilerplate both CLI event loops share before each backend classifies
    the object its own way."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _subprocess_line_runner(args: list[str], env: dict[str, str], cwd: str) -> Iterator[str]:
    """Real launcher: run a CLI and yield its stdout lines (JSONL).

    Lives behind the injectable ``runner`` seam so the adapters are unit-testable
    without spawning a live ``claude``/``codex`` process.
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
        return "worker produced no output"
    for line in text.splitlines():
        up = line.strip()
        if up.upper().startswith("FAILED"):
            # Strip the "FAILED —/-/:" lead-in.
            rest = up[6:].lstrip(" —-:")
            return rest or "worker reported FAILED"
    return "worker reported FAILED"
