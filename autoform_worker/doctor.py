"""``autoform-worker doctor`` — environment/auth/repo capability audit.

Answers "can this machine be a worker?" before any round runs: tool presence,
gh auth, project resolution, remote shape, push access, fork, Issues, and a
claim-board probe. Each check is (name, ok, detail); ``--json`` for machines.
"""
from __future__ import annotations

import shutil

from .claims import ClaimBoard
from .config import WorkerConfig
from .errors import ClaimTransportError, Die
from .githost import GitHost
from .gitutil import is_git_repo, origin_url, parse_slug, slug_url


def _ci_check(cfg: WorkerConfig, canonical: str) -> tuple[str, bool, str]:
    """Whether the project repo actually verifies its PR heads.

    This is what gives the auto-merge gate teeth: with no workflows, every PR
    head has an empty check rollup, the gate refuses to merge, and the loop
    silently stops short of merging anything.
    """
    workflows = cfg.lean_root / ".github" / "workflows"
    present = sorted(p.name for p in workflows.glob("*.yml")) + \
        sorted(p.name for p in workflows.glob("*.yaml"))
    if present:
        return ("project CI", True, f"{len(present)} workflow(s): {', '.join(present[:4])}")
    return ("project CI", False,
            f"no workflows in {workflows} — heads have no checks, so auto-merge stays shut. "
            "Install templates/github/autoform-verify.yml (or pass --merge-without-ci to "
            "merge on the jury verdict alone)")


def _tool(name: str) -> tuple[str, bool, str]:
    path = shutil.which(name)
    return (name, path is not None, path or "not on PATH")


def run_doctor(cfg: WorkerConfig | None, host: GitHost | None = None) -> list[tuple[str, bool, str]]:
    host = host or GitHost()
    checks: list[tuple[str, bool, str]] = []
    for tool in ("git", "gh", "uv", "lake", "lean"):
        checks.append(_tool(tool))
    checks.append(("claude or codex CLI",
                   bool(shutil.which("claude") or shutil.which("codex")),
                   "needed for fix-like units and the max/codex backends"))

    try:
        login = host.me()
        checks.append(("gh auth", True, f"authenticated as {login}"))
    except Die as error:
        checks.append(("gh auth", False, str(error)))
        return checks

    if cfg is None:
        checks.append(("project", False, "no dispatch project resolved — pass --project"))
        return checks
    checks.append(("project", True, str(cfg.project)))
    checks.append((
        "Markdown runtime",
        cfg.runtime.authority == "markdown-articles",
        f"{cfg.runtime.article_count} articles at {cfg.runtime.source_revision[:12]}",
    ))
    checks.append((
        "durable article identity",
        cfg.durable_identity_ready,
        "not configured - stateful worker execution remains disabled",
    ))
    checks.append(("lean repo", is_git_repo(cfg.lean_root), str(cfg.lean_root)))

    url = origin_url(cfg.lean_root)
    slug = parse_slug(url) if url else None
    checks.append(("origin remote", slug is not None, url or "missing"))
    if not slug:
        return checks

    try:
        canonical, default_branch = host.canonical_of(slug)
        checks.append(("canonical repo", True, f"{canonical} (default: {default_branch})"))
        can_push = host.can_push(canonical)
        checks.append(("push access", True,
                       f"{'yes' if can_push else 'no'} — "
                       f"{'PRs push directly' if can_push else 'PRs go through your fork'}"))
        issues = host.has_issues(canonical)
        checks.append(("issues enabled", True,
                       "yes — escalation/intention sync active" if issues
                       else "no (default on forks) — escalation issue sync degrades to local-only"))
        checks.append(_ci_check(cfg, canonical))
        board = ClaimBoard(slug_url(canonical), cfg.worker_id, cfg.claims_scratch)
        try:
            board.list()
            checks.append(("claim board", True, f"refs/autoform-claims/* reachable on {canonical}"))
        except ClaimTransportError as error:
            checks.append(("claim board", False, f"{error} - coordinated mutation disabled"))
    except Die as error:
        checks.append(("canonical repo", False, str(error)))

    try:
        from .config import scripts_modules
        spend = scripts_modules()["spend_governor"].check(cfg.lean_root, "claude")
        if not spend.get("paced"):
            checks.append(("spend pacing", True,
                           "no budget configured — prover runs are unpaced "
                           "(.autoform/budget.json sets a rolling window)"))
        else:
            checks.append(("spend pacing", spend["allowed"], spend["reason"]))
    except Exception as error:
        checks.append(("spend pacing", True, f"not evaluated: {error}"))

    checks.append(("worker id", True, cfg.worker_id))
    checks.append(("state dir", True, str(cfg.state_dir)))
    return checks
