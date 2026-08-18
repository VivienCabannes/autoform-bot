"""Host-agent spawning for fix-like units (fix / fix-ci / rebase).

Prove units go through the existing prover stack (``dispatch_runner.run_worker``
→ adapters → verification gate); this module covers the *general repo work* the
prover adapters don't: addressing review feedback, greening CI, resolving
merges. It spawns the operator's own agent CLI (``claude`` or ``codex``) with an
allowlisted permission surface — never ``--dangerously-skip-permissions`` unless
the operator explicitly opts in via ``AUTOFORM_UNSAFE_FULL_ACCESS=1`` (the same
escape hatch the Claude prover adapter honors).

Agents edit and commit; they never push. The round harness pushes afterward via
the CAS ``safe_push`` — with the claim re-checked — so a rogue or confused agent
cannot clobber a branch.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .errors import Die

# What a fix-like agent may do: lake/lean for builds, local git for commits, gh
# read-only for checks/logs. `git push` is deliberately absent — prompts forbid
# it and the harness owns the (claim-checked, CAS) push after the agent exits.
_CLAUDE_ALLOWED_TOOLS = (
    "Read,Grep,Glob,Edit,Write,"
    "Bash(lake *),Bash(lake env lean *),Bash(lean *),Bash(elan *),Bash(rg *),Bash(mkdir *),"
    "Bash(git status *),Bash(git diff *),Bash(git log *),Bash(git add *),Bash(git commit *),"
    "Bash(git merge *),Bash(git fetch *),Bash(git checkout *),Bash(git restore *),Bash(git rev-parse *),"
    "Bash(gh pr view *),Bash(gh pr checks *),Bash(gh pr diff *),Bash(gh run view *),Bash(gh api *)"
)

_INFRA_STATUS_RE = re.compile(r"API Error:?\s*(\d{3})")
_INFRA_STATUSES = {"408", "429", "500", "502", "503", "504", "529"}
_TRANSPORT_RE = re.compile(
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENETUNREACH|EAI_AGAIN|socket hang up"
    r"|Connection (?:error|reset)|TLS handshake timeout",
    re.IGNORECASE,
)


def _scrubbed_env() -> dict:
    """Claude runs bill the operator's subscription, never a stray API key —
    the same scrub the prover's Claude adapter performs."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def fixlike_provider(backend: str) -> str:
    """Which host CLI runs fix-like prompts for a user-facing backend.

    ``max`` → claude, ``codex`` → codex. Every other backend (aristotle, muse,
    openai, avocado) has no general repo-work CLI, so fall back to whichever of
    claude/codex is installed — fail closed when neither is.
    """
    if backend == "max":
        return "claude"
    if backend == "codex":
        return "codex"
    for cli, provider in (("claude", "claude"), ("codex", "codex")):
        if shutil.which(cli):
            return provider
    raise Die(
        f"backend {backend!r} cannot run repo-fix work and neither `claude` nor `codex` "
        "is installed — install one or restrict --only to review/progress/prove"
    )


# The same session isolation the prover's Claude adapter enforces: the checkout
# being fixed is UNTRUSTED input (a PR can carry .claude/ settings, hooks, or
# MCP config), so nothing repo-controlled may reach the spawned agent.
_CLAUDE_ISOLATION_ARGS = (
    "--setting-sources", "user",
    "--settings", '{"disableAllHooks":true}',
    "--disable-slash-commands",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
)


def host_agent_argv(provider: str, prompt: str, model: str | None = None) -> list[str]:
    if provider == "claude":
        argv = [os.environ.get("AUTOFORM_CLAUDE_BIN", "claude"), "-p", prompt,
                "--model", model or "opus", "--permission-mode", "dontAsk",
                *_CLAUDE_ISOLATION_ARGS]
        if os.environ.get("AUTOFORM_UNSAFE_FULL_ACCESS") == "1":
            argv.append("--dangerously-skip-permissions")
        else:
            argv += ["--allowedTools", _CLAUDE_ALLOWED_TOOLS]
        return argv
    if provider == "codex":
        argv = [os.environ.get("AUTOFORM_CODEX_BIN", "codex"), "exec", "--skip-git-repo-check"]
        if os.environ.get("AUTOFORM_UNSAFE_FULL_ACCESS") == "1":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", "workspace-write"]
        if model:
            argv += ["-m", model]
        argv.append(prompt)
        return argv
    raise Die(f"unknown fix-like provider {provider!r} (claude|codex)")


def run_host_agent(
    provider: str,
    cwd: Path,
    prompt: str,
    log_dir: Path,
    timeout: int,
    model: str | None = None,
) -> tuple[int, Path]:
    """Run one agent to completion; combined output lands in a timestamped log.

    The child gets its own session (process group) and the whole group is killed
    at ``timeout`` — an abandoned agent never outlives the round.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"agent-{provider}-{stamp}.log"
    argv = host_agent_argv(provider, prompt, model=model)
    env = _scrubbed_env() if provider == "claude" else dict(os.environ)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# argv: {argv[:2]}… (prompt {len(prompt)} chars)\n# cwd: {cwd}\n\n")
        log.flush()
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            log.write(f"\n# TIMEOUT after {timeout}s — process group killed\n")
            rc = 124
    return rc, log_path


def _kill_group(proc: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=30)
            return
        except subprocess.TimeoutExpired:
            pass
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def classify_infra_failure(log_path: Path, tail_lines: int = 6) -> str | None:
    """Whether a failed agent run was the *provider's* fault (refundable).

    Inspects only the log tail; a long transcript ending in an agent-authored
    failure is the agent's own fault and burns the attempt.
    """
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:])
    except OSError:
        return None
    match = _INFRA_STATUS_RE.search(tail)
    if match and match.group(1) in _INFRA_STATUSES:
        return f"provider returned {match.group(1)}"
    if _TRANSPORT_RE.search(tail):
        return "could not reach the provider"
    return None


def fill_prompt(template_path: Path, **subs: str) -> str:
    """``__KEY__`` substitution — TauCeti's prompt-template convention."""
    text = template_path.read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace(f"__{key.upper()}__", str(value))
    return text


def prompts_dir() -> Path:
    return Path(__file__).resolve().parent / "prompts"
