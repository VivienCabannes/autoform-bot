"""Tests for the shared CLI-agent internals (`_cli_common`) — the helpers and the
worker-prompt builder that the Claude and Codex adapters now share, so the two
backends cannot drift on "what counts as an honest FAILED" or the discipline text.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from servers.prover._cli_common import (
    ProverTimeout,
    _build_spec_prompt,
    _failure_reason,
    _iter_json_lines,
    _looks_failed,
    _scrubbed_env,
    _subprocess_line_runner,
    build_worker_prompt,
)
from servers.prover.claude_adapter import WORKER_SYSTEM_PROMPT
from servers.prover.codex_adapter import CODEX_SYSTEM_PROMPT

# The claude-backend prompt parameters, pinned here so the lock-test below catches
# any silent change to the shared skeleton OR these deltas.
_CLAUDE_PARAMS = dict(
    tools_clause="via the autoform-repl / autoform-lsp MCP tools",
    extra_hyp_clause=", no pinned-general parameter",
    billing_paragraph=("Billing: scrub `ANTHROPIC_API_KEY` from every subprocess you spawn (`env -u "
                       "ANTHROPIC_API_KEY …`) so no `lake`/`git`/script child can bill the Anthropic API.\n\n"),
    repl_word="REPL ",
    build_phrase="build will not run",
    blocker_phrase="and the concrete blocker.",
)


def test_scrubbed_env_drops_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-secret")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = _scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env and env.get("KEEP_ME") == "yes"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_verify_scrubbed_env_drops_auth_token(monkeypatch):
    from servers.prover.verify import _scrubbed_env as verify_scrub

    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-secret")
    env = verify_scrub()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


def test_iter_json_lines_skips_blank_and_unparseable():
    lines = ['{"a": 1}', "", "   ", "not json", '{"b": 2}']
    assert list(_iter_json_lines(iter(lines))) == [{"a": 1}, {"b": 2}]


def test_looks_failed_and_failure_reason():
    assert _looks_failed("") and _looks_failed("FAILED — nope")
    assert not _looks_failed("theorem t : True := trivial")
    assert _failure_reason("tried stuff\nFAILED — missing lemma foo") == "missing lemma foo"
    assert _failure_reason("") == "worker produced no output"   # backend-neutral (was claude-specific)


def test_looks_failed_only_on_status_like_token():
    """`FAILED` in PROSE must not fail the run — a false failed silently discards
    a genuine proof and (unlike a false proved) has no verify-gate backstop."""
    # prose mentions — NOT failures
    assert not _looks_failed("The first attempt FAILED, so I used nlinarith instead.\n"
                             "theorem t : 1 = 1 := rfl")
    assert not _looks_failed("Note: earlier `simp` FAILED on this goal but omega closed it.")
    assert not _looks_failed("- the tactic FAILED once; retried and it compiled cleanly")
    # status-like tokens — failures
    assert _looks_failed("FAILED — missing lemma")
    assert _looks_failed("worked for a while\nFAILED — could not close the goal")
    assert _looks_failed("**FAILED** — the induction does not go through")
    assert _looks_failed("status: FAILED")
    assert _looks_failed("Status = FAILED (budget exhausted)")
    # lowercase prose 'failed' is never a status token
    assert not _looks_failed("failed attempts taught me the right invariant; proof landed")


def test_failure_reason_handles_markdown_and_status_forms():
    assert _failure_reason("**FAILED** — induction mismatch") == "induction mismatch"
    assert _failure_reason("status: FAILED — out of budget") == "out of budget"
    assert _failure_reason("FAILED") == "worker reported FAILED"


def test_build_spec_prompt():
    p = _build_spec_prompt("Foo.Bar", "prove X")
    assert "# Formalization target: Foo.Bar" in p and "prove X" in p and "FAILED" in p


def test_both_prompts_share_the_skeleton_and_differ_only_in_deltas():
    for shared in (
        "You are a Lean 4 / Mathlib formalization worker — a prover backend.",
        "Hard rule — no cheating: `sorry`, `admit`, raw `axiom`, and `native_decide`",
        "Reporting FAILED honestly is correct; delivering a sorry'd file as done is the one thing",
    ):
        assert shared in WORKER_SYSTEM_PROMPT and shared in CODEX_SYSTEM_PROMPT
    # claude-only deltas
    assert "autoform-repl / autoform-lsp" in WORKER_SYSTEM_PROMPT
    assert "ANTHROPIC_API_KEY" in WORKER_SYSTEM_PROMPT          # billing paragraph
    assert "no pinned-general parameter" in WORKER_SYSTEM_PROMPT
    # codex-only deltas
    assert "lake env lean" in CODEX_SYSTEM_PROMPT
    assert "ANTHROPIC_API_KEY" not in CODEX_SYSTEM_PROMPT       # codex drops billing
    assert "pinned-general" not in CODEX_SYSTEM_PROMPT


def test_builder_reproduces_the_claude_prompt_exactly():
    # Locks the skeleton + the claude deltas to the live prompt — a silent edit to
    # either side fails here.
    assert build_worker_prompt(**_CLAUDE_PARAMS) == WORKER_SYSTEM_PROMPT


# --- subprocess lifecycle: deadline enforcement + kill path ----------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_runner_kills_hung_process_on_deadline():
    """A silent, long-running child is killed when the wall-clock deadline passes."""
    args = [sys.executable, "-u", "-c", "import time; time.sleep(60)"]
    started = time.monotonic()
    gen = _subprocess_line_runner(args, os.environ.copy(), "", time.monotonic() + 0.5)
    with pytest.raises(ProverTimeout):
        list(gen)
    assert time.monotonic() - started < 20  # killed promptly, not after 60s


def test_runner_kill_path_on_generator_close():
    """Abandoning the generator (GeneratorExit) reaps the child process."""
    code = "import os, sys, time; print(os.getpid(), flush=True); time.sleep(60)"
    gen = _subprocess_line_runner([sys.executable, "-u", "-c", code], os.environ.copy(), "", None)
    pid = int(next(gen).strip())
    assert _alive(pid)
    gen.close()  # GeneratorExit → terminate()/kill() of the process group
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


def test_runner_normal_exhaustion_yields_all_lines():
    code = "print('a'); print('b')"
    out = list(_subprocess_line_runner([sys.executable, "-c", code], os.environ.copy(), "",
                                       time.monotonic() + 30))
    assert [ln.strip() for ln in out] == ["a", "b"]


def test_runner_kills_grandchildren_via_process_group():
    """The child is started in its own process group so grandchildren die too."""
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    gen = _subprocess_line_runner([sys.executable, "-u", "-c", code], os.environ.copy(), "",
                                  time.monotonic() + 30)
    grandchild = int(next(gen).strip())
    assert _alive(grandchild)
    gen.close()
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)
