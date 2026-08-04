"""One round: survey → first actionable stage → execute one unit.

TauCeti's cascade discipline: a round does at most ONE unit of work, so the
loop stays observable and interruptible, and parallel workers interleave
instead of monopolizing. ``NoProgress`` (exit 75) when nothing is actionable.
"""
from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import agent_work
from . import survey as survey_mod
from . import work_units
from .registry import Registry
from .claims import ClaimBoard
from .config import WorkerConfig, scripts_modules
from .constants import GH_MIN_BUDGET, STAGES
from .counters import Counters
from .errors import Die, NoProgress
from .githost import GitHost
from .gitutil import origin_url, parse_slug, slug_url


@dataclass
class RoundOpts:
    only: tuple = ()
    skip: tuple = ()
    backend: str = "max"
    judge_backend: str = "claude"
    allowed_egress: frozenset = frozenset()
    dry_run: bool = False
    extra_identities: tuple = ()
    push_progress: bool = True
    review_foreign: bool = False
    merge_without_ci: bool = False

    def stages(self) -> list[str]:
        stages = [s for s in STAGES if not self.only or s in self.only]
        return [s for s in stages if s not in self.skip]

    def validate(self) -> None:
        """Fail closed BEFORE any work: unknown judges error, and an API judge
        needs the same per-process egress consent the dispatcher demands —
        jury prompts carry project content off-machine exactly like proofs."""
        from .config import scripts_modules

        judge_runtime = scripts_modules()["judge_runtime"]
        supported = tuple(getattr(judge_runtime, "SUPPORTED_JUDGES", ("claude", "codex")))
        if self.judge_backend not in supported:
            raise Die(f"unknown judge backend {self.judge_backend!r}; known: {', '.join(supported)}")
        if self.judge_backend in {"openai", "avocado"} and self.judge_backend not in self.allowed_egress:
            raise Die(
                f"judge backend {self.judge_backend!r} sends jury prompts (project content) to a "
                f"configured API endpoint; re-run with --allow-api-egress {self.judge_backend} "
                "after explicit user approval"
            )


@dataclass
class RoundDeps:
    """Injection seam for tests."""
    host: GitHost = field(default_factory=GitHost)
    board_factory: object = None  # (cfg, canonical) -> ClaimBoard | None


class RoundLock:
    """One round per worker id per machine (flock on the state dir)."""

    def __init__(self, cfg: WorkerConfig):
        self.path = cfg.state_dir / "round.lock"
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise Die(f"another round is already running for this worker id ({self.path})") from error
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh:
            self._fh.close()


def resolve_repo(cfg: WorkerConfig, host: GitHost) -> tuple[str, str]:
    """(canonical slug, default branch) for the Lean repo's origin."""
    url = origin_url(cfg.lean_root)
    if not url:
        raise Die(f"{cfg.lean_root} has no `origin` remote — distributed mode needs a GitHub repo")
    slug = parse_slug(url)
    if not slug:
        raise Die(f"origin remote {url!r} is not a GitHub repo — distributed mode needs one")
    return host.canonical_of(slug)


def default_board(cfg: WorkerConfig, canonical: str) -> ClaimBoard:
    claims_repo = os.environ.get("AUTOFORM_CLAIM_REPO") or slug_url(canonical)
    if "/" in claims_repo and "://" not in claims_repo and not claims_repo.startswith("git@") \
            and not Path(claims_repo).exists():
        claims_repo = slug_url(claims_repo)  # bare owner/repo slug
    return ClaimBoard(claims_repo, cfg.worker_id, cfg.claims_scratch)


def build_survey(cfg: WorkerConfig, opts: RoundOpts, deps: RoundDeps):
    host = deps.host
    canonical, default_branch = resolve_repo(cfg, host)
    factory = deps.board_factory or default_board
    board = factory(cfg, canonical) if cfg.respect_claims else None
    counters = Counters(cfg.counters_path)
    picture = survey_mod.collect(cfg, host, board, counters, canonical, default_branch,
                                 extra_identities=list(opts.extra_identities),
                                 allow_foreign_review=opts.review_foreign,
                                 allow_unchecked_merge=opts.merge_without_ci,
                                 proof_backend=opts.backend)
    return picture, host, board, counters


def run_round(cfg: WorkerConfig, opts: RoundOpts, deps: RoundDeps | None = None) -> str:
    """Execute at most one unit. Returns the unit summary; raises NoProgress."""
    deps = deps or RoundDeps()
    opts.validate()
    budget = deps.host.rate_budget()
    core_left = (budget.get("core") or {}).get("remaining")
    if isinstance(core_left, int) and core_left < GH_MIN_BUDGET:
        raise NoProgress(f"GitHub REST budget too low ({core_left} left) — backing off")

    picture, host, board, counters = build_survey(cfg, opts, deps)

    # Revive parked recoveries whose durable inputs moved (a merged sibling, a
    # Mathlib bump, a re-plan). Parking must never be permanent — an unattended
    # fleet has no one to un-park by hand. The revived recovery is re-queued and
    # owns its node again, so it runs (and can then hand the node back to the
    # prover) rather than the node becoming prove-eligible on this same pass.
    if picture.resumable_parks and not opts.dry_run:
        dq = scripts_modules()["dispatch_queue"]
        for node_id, task_id in picture.resumable_parks:
            if not task_id:
                continue
            try:
                dq.main([str(cfg.project), "resume", task_id])
            except Exception:
                continue
        picture, host, board, counters = build_survey(cfg, opts, deps)

    for stage in opts.stages():
        for candidate in picture.actionable(stage):
            if opts.dry_run:
                target = candidate.node or (f"#{candidate.pr.number}" if candidate.pr else "?")
                return f"[dry-run] would run {stage} on {target}: {candidate.reason}"
            result = _execute(stage, cfg, host, board, counters, picture, candidate, opts)
            if result.progressed:
                return result.summary
            # Not progressed: fall through to the next candidate/stage — the
            # reason is already reflected in counters/claims for next survey.
    raise NoProgress("no actionable work found across stages: " + ", ".join(opts.stages()))


def _execute(stage, cfg, host, board, counters, picture, candidate, opts) -> work_units.UnitResult:
    if stage == "prove":
        return work_units.do_prove(cfg, host, board, counters, picture, candidate,
                                   backend=opts.backend, judge_backend=opts.judge_backend,
                                   allowed_egress=opts.allowed_egress)
    if stage == "review":
        return work_units.do_review(cfg, host, counters, picture, candidate,
                                    judge_backend=opts.judge_backend)
    if stage == "merge":
        return work_units.do_merge(cfg, host, counters, picture, candidate)
    if stage == "progress":
        return work_units.do_progress(cfg, host, board, counters, picture,
                                      push=opts.push_progress)
    if stage == "agents":
        return agent_work.do_agent_task(cfg, host, board, counters, picture, candidate,
                                        registry=registry_for(cfg), backend=opts.backend)
    if stage in ("fix", "fix-ci", "rebase"):
        return work_units.do_fixlike(stage, cfg, host, board, counters, picture, candidate,
                                     backend=opts.backend)
    raise Die(f"unknown stage {stage!r}")


def registry_for(cfg: WorkerConfig) -> Registry:
    """The role registry for this project (plugin roles + project-local roles)."""
    return Registry(cfg.plugin_root, cfg.project)
