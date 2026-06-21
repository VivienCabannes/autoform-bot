"""Tests for the shared CLI-agent internals (`_cli_common`) — the helpers and the
worker-prompt builder that the Claude and Codex adapters now share, so the two
backends cannot drift on "what counts as an honest FAILED" or the discipline text.
"""
from __future__ import annotations

from servers.prover._cli_common import (
    _build_spec_prompt,
    _failure_reason,
    _iter_json_lines,
    _looks_failed,
    _scrubbed_env,
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
    monkeypatch.setenv("KEEP_ME", "yes")
    env = _scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env and env.get("KEEP_ME") == "yes"


def test_iter_json_lines_skips_blank_and_unparseable():
    lines = ['{"a": 1}', "", "   ", "not json", '{"b": 2}']
    assert list(_iter_json_lines(iter(lines))) == [{"a": 1}, {"b": 2}]


def test_looks_failed_and_failure_reason():
    assert _looks_failed("") and _looks_failed("FAILED — nope")
    assert not _looks_failed("theorem t : True := trivial")
    assert _failure_reason("tried stuff\nFAILED — missing lemma foo") == "missing lemma foo"
    assert _failure_reason("") == "worker produced no output"   # backend-neutral (was claude-specific)


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
