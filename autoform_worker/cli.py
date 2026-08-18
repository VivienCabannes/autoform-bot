"""The ``autoform`` CLI — entry point for the distributed worker.

``work`` runs rounds (optionally ``--loop``); ``status`` is the machine-readable
survey; ``claim``/``push``/``pr-create`` are the primitives agents and skills
call directly; ``doctor`` audits the machine; ``sync`` converges local review
state from merged scoreboards; ``dashboard`` wraps the existing local/static
dashboard scripts.

Exit codes: 0 progress/ok · 1 error · 75 no-progress · 130/143 interrupted.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, doctor as doctor_mod, gitutil, loop as loop_mod, round as round_mod
from .config import WorkerConfig, plugin_root, resolve_config, scripts_modules
from .constants import CLAIM_TTL_S, STAGES, TARGET_MARK_RE
from .errors import EX_ERROR, EX_NOPROGRESS, EX_OK, ClaimTransportError, Die, NoProgress
from .githost import GitHost
from .gitutil import safe_push
from .scoreboard import parse_target


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=None,
                        help="dispatch project dir (owns graph.json); default: $AUTOFORM_DISPATCH_PROJECT or cwd")
    parser.add_argument("--worker-id", default=None, help="stable worker identity (default: <user>-<host>)")


def _add_round_flags(parser: argparse.ArgumentParser) -> None:
    _add_common(parser)
    parser.add_argument("--only", default="", help=f"comma-separated stages ({','.join(STAGES)})")
    parser.add_argument("--skip", default="", help="comma-separated stages to exclude")
    parser.add_argument("--backend", default=None,
                        help="prover/agent backend (default: Orchestrate's persisted choice)")
    parser.add_argument("--judge-backend", default=None,
                        help="jury backend (default: $AUTOFORM_JUDGE_BACKEND, else claude/codex autodetect)")
    parser.add_argument("--allow-api-egress", action="append", default=[],
                        metavar="PROVIDER", choices=["openai", "avocado"],
                        help="per-process consent that PROVIDER may receive project data (repeatable)")
    parser.add_argument("--ignore-claims", action="store_true",
                        help="skip the cooperative claim board entirely (CAS safety still applies)")
    parser.add_argument("--extra-identities", default="",
                        help="comma-separated extra GitHub logins whose PRs count as yours")
    parser.add_argument("--merge-without-ci", action="store_true",
                        help="allow auto-merge on heads with no CI checks (unsafe: the jury "
                             "verdict becomes the only gate)")
    parser.add_argument("--review-foreign", action="store_true",
                        help="also review PRs from non-collaborators (reviewing BUILDS their code locally)")
    parser.add_argument("--dry-run", action="store_true", help="survey + report; execute nothing")


def _parse_stages(raw: str, flag: str) -> tuple:
    if not raw:
        return ()
    stages = tuple(s.strip() for s in raw.split(",") if s.strip())
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        raise Die(f"{flag}: unknown stage(s) {', '.join(unknown)} (known: {', '.join(STAGES)})")
    return stages


def _resolve_backend(explicit: str | None) -> str:
    mods = scripts_modules()
    backend_config = mods["backend_config"]
    if explicit is not None:
        if explicit not in backend_config.BACKENDS:
            raise Die(f"unknown backend {explicit!r}; known: {', '.join(backend_config.BACKENDS)}")
        return explicit
    return backend_config.get_backend("max")


def _resolve_judge_backend(explicit: str | None) -> str:
    import shutil

    if explicit:
        return explicit
    env = os.environ.get("AUTOFORM_JUDGE_BACKEND")
    if env:
        return env
    if shutil.which("claude"):
        return "claude"
    if shutil.which("codex"):
        return "codex"
    raise Die("no judge backend available — install claude or codex, or pass --judge-backend")


def _round_opts(args) -> round_mod.RoundOpts:
    return round_mod.RoundOpts(
        only=_parse_stages(args.only, "--only"),
        skip=_parse_stages(args.skip, "--skip"),
        backend=_resolve_backend(args.backend),
        judge_backend=_resolve_judge_backend(args.judge_backend),
        allowed_egress=frozenset(args.allow_api_egress),
        dry_run=args.dry_run,
        extra_identities=tuple(s for s in args.extra_identities.split(",") if s.strip()),
        review_foreign=args.review_foreign,
        merge_without_ci=args.merge_without_ci,
    )


def _config(args) -> WorkerConfig:
    return resolve_config(project=args.project, worker_id=args.worker_id,
                          respect_claims=not getattr(args, "ignore_claims", False))


def _round_passthrough(args) -> list[str]:
    """Re-serialize round flags for the `_round` subprocess (loop mode)."""
    out: list[str] = []
    if args.project:
        out += ["--project", str(args.project)]
    if args.worker_id:
        out += ["--worker-id", args.worker_id]
    if args.only:
        out += ["--only", args.only]
    if args.skip:
        out += ["--skip", args.skip]
    if args.backend:
        out += ["--backend", args.backend]
    if args.judge_backend:
        out += ["--judge-backend", args.judge_backend]
    for provider in args.allow_api_egress:
        out += ["--allow-api-egress", provider]
    if args.ignore_claims:
        out.append("--ignore-claims")
    if args.extra_identities:
        out += ["--extra-identities", args.extra_identities]
    if args.review_foreign:
        out.append("--review-foreign")
    if args.merge_without_ci:
        out.append("--merge-without-ci")
    return out


# ---------------------------------------------------------------------------
# subcommand bodies
# ---------------------------------------------------------------------------

def cmd_work(args) -> int:
    if args.loop:
        if args.dry_run:
            raise Die("--loop and --dry-run are incompatible — a dry-run loop would spin forever; "
                      "use `autoform status` or a single `work --dry-run` round")
        return loop_mod.cmd_loop(_round_passthrough(args))
    return cmd_round(args)


def cmd_round(args) -> int:
    import signal

    # SIGTERM (loop timeout, operator stop) must unwind context managers so
    # the worktree restore + inflight-marker cleanup run before exit.
    def _sigterm(signum, frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _sigterm)
    cfg = _config(args)
    opts = _round_opts(args)
    with round_mod.RoundLock(cfg):
        try:
            summary = round_mod.run_round(cfg, opts)
        except NoProgress as np:
            print(f"no progress: {np}")
            return EX_NOPROGRESS
    print(summary)
    return EX_OK


def cmd_status(args) -> int:
    cfg = _config(args)
    opts = _round_opts(args)
    picture, _host, _board, _counters = round_mod.build_survey(cfg, opts, round_mod.RoundDeps())
    if args.json:
        print(json.dumps(picture.to_json(), indent=2))
        return EX_OK
    print(f"repo: {picture.canonical} (default {picture.default_branch}) · you: {picture.me} "
          f"· push: {'yes' if picture.can_push else 'via fork'} "
          f"· issues: {'on' if picture.issues_enabled else 'off'}")
    for stage in STAGES:
        ready = picture.actionable(stage)
        held = picture.suppressed.get(stage, [])
        if not ready and not held:
            continue
        print(f"\n{stage} — {len(ready)} ready, {len(held)} suppressed")
        for cand in ready[:10]:
            target = cand.node or (f"#{cand.pr.number}" if cand.pr else "?")
            print(f"  • {target}: {cand.reason}")
        for cand in held[:5]:
            target = cand.node or (f"#{cand.pr.number}" if cand.pr else "?")
            print(f"  ◦ {target}: {cand.reason}")
    for tgt, m in (picture.targets or {}).items():
        print(f"\ntarget {tgt}: cone {m['cone_size']} · unproved {m['unproved_mass']} · "
              f"ready {m['ready']} · critical path {m['critical_path']}"
              + (" · DONE" if m.get("done") else ""))
    if picture.claims:
        print("\nclaims:")
        for lease in picture.claims:
            flag = " (expired)" if lease.get("_expired") else ""
            print(f"  {lease.get('_key')}: {lease.get('owner')}{flag}")
    for note in picture.notes:
        print(f"note: {note}")
    return EX_OK


def cmd_claim(args) -> int:
    cfg = _config(args)
    host = GitHost()
    canonical, _default = round_mod.resolve_repo(cfg, host)
    board = round_mod.default_board(cfg, canonical)
    if getattr(args, "node", None):
        if args.key:
            raise Die("pass either a key or --node, not both")
        from .claims import author_claim_key

        args.key = author_claim_key(args.node)
        print(f"key: {args.key}")
    try:
        if args.action == "list":
            for lease in board.list():
                flag = " (expired)" if lease.get("_expired") else ""
                print(f"{lease.get('_key')}: {lease.get('owner')} "
                      f"expires_at={lease.get('expires_at')}{flag}")
            return EX_OK
        if args.action == "gc":
            print(f"removed {board.gc()} expired lease(s)")
            return EX_OK
        if not args.key:
            raise Die(f"claim {args.action} needs a key")
        if args.action == "acquire":
            ok = board.acquire(args.key, ttl=args.ttl, steal=args.steal)
            print("acquired" if ok else "held by a live peer")
            return EX_OK if ok else EX_ERROR
        if args.action == "renew":
            ok = board.renew(args.key, ttl=args.ttl)
            print("renewed" if ok else "lost")
            return EX_OK if ok else EX_ERROR
        if args.action == "release":
            ok = board.release(args.key)
            print("released" if ok else "not yours — left alone")
            return EX_OK if ok else EX_ERROR
        if args.action == "holds":
            ok = board.holds(args.key)
            print("held" if ok else "not held")
            return EX_OK if ok else EX_ERROR
        if args.action == "read":
            lease = board.read(args.key)
            print(json.dumps(lease, indent=2) if lease else "(no lease)")
            return EX_OK
    except ClaimTransportError as error:
        print(f"claim board error: {error}", file=sys.stderr)
        return 2
    raise Die(f"unknown claim action {args.action!r}")


def cmd_push(args) -> int:
    """CAS push (git-safe-push semantics) — the only push agents may perform."""
    cfg = _config(args)
    ref = args.ref or os.environ.get("AUTOFORM_PUSH_REF", "")
    remote = args.remote or os.environ.get("AUTOFORM_PUSH_REMOTE", "origin")
    expect = args.expect if args.expect is not None else os.environ.get("AUTOFORM_PUSH_EXPECT")
    if not ref:
        raise Die("push needs a ref (positional or AUTOFORM_PUSH_REF)")
    claim_key = os.environ.get("AUTOFORM_CLAIM_KEY")
    if claim_key:
        host = GitHost()
        canonical, _default = round_mod.resolve_repo(cfg, host)
        board = round_mod.default_board(cfg, canonical)
        if not board.holds(claim_key) and not board.renew(claim_key):
            print(f"lease {claim_key!r} lost (another worker took over) — refusing to push",
                  file=sys.stderr)
            return EX_ERROR
    ok = safe_push(cfg.lean_root, ref, remote=remote, expect=expect or None)
    print("pushed" if ok else "push refused (CAS lost or nothing to push)")
    return EX_OK if ok else EX_ERROR


def cmd_pr_create(args, gh_args: list[str]) -> int:
    """Marker+lease-gated ``gh pr create`` (gh-safe-pr-create semantics)."""
    body_text = Path(args.body_file).read_text(encoding="utf-8")
    node = parse_target(body_text)
    if not node or not TARGET_MARK_RE.search(body_text):
        raise Die("PR body must carry a well-formed <!--autoform-target:v1 {\"node\": …}--> marker")
    claim_key = os.environ.get("AUTOFORM_AUTHOR_CLAIM_KEY")
    if claim_key:
        cfg = _config(args)
        host = GitHost()
        canonical, _default = round_mod.resolve_repo(cfg, host)
        board = round_mod.default_board(cfg, canonical)
        if not board.holds(claim_key):
            print(f"author lease {claim_key!r} lost — do NOT create the PR", file=sys.stderr)
            return EX_ERROR
    proc = subprocess.run(["gh", "pr", "create", "--body-file", str(args.body_file), *gh_args])
    return proc.returncode


def cmd_sync(args) -> int:
    """Fast-forward the local default branch to committed canonical state."""
    cfg = _config(args)
    host = GitHost()
    canonical, default_branch = round_mod.resolve_repo(cfg, host)
    if gitutil.current_branch(cfg.lean_root) != default_branch:
        raise Die(f"sync requires the local {default_branch!r} branch")
    if not gitutil.clean_tree(cfg.lean_root):
        raise Die("sync requires a clean working tree")
    before = gitutil.head_oid(cfg.lean_root)
    gitutil.fetch(cfg.lean_root, gitutil.slug_url(canonical), default_branch)
    proc = gitutil.run_git(["merge", "--ff-only", "FETCH_HEAD"], cwd=cfg.lean_root, check=False)
    if proc.returncode != 0:
        raise Die(f"local {default_branch} has diverged from {canonical}; resolve it manually")
    after = gitutil.head_oid(cfg.lean_root)
    result = {"branch": default_branch, "before": before, "after": after, "changed": before != after}
    print(json.dumps(result) if args.json else
          f"{default_branch} {'fast-forwarded to ' + after[:12] if before != after else 'already current'}")
    return EX_OK


def cmd_issues_sync(args) -> int:
    from .work_units import _sync_escalation_issues

    cfg = _config(args)
    host = GitHost()
    canonical, _default = round_mod.resolve_repo(cfg, host)
    if not host.has_issues(canonical):
        print(f"issues are disabled on {canonical} — nothing to sync "
              "(enable them in repo settings for cross-machine escalations)")
        return EX_OK
    if args.dry_run:
        print("[dry-run] would sync open escalations to issues and close resolved ones")
        return EX_OK
    changed = _sync_escalation_issues(cfg, host, canonical)
    print(f"synced {changed} escalation issue(s)")
    return EX_OK


def cmd_dashboard(args) -> int:
    """Thin wrappers over the existing dashboard scripts (single source of truth)."""
    cfg = _config(args)
    root = plugin_root()
    if args.dashboard_cmd == "export":
        argv = [sys.executable, str(root / "scripts" / "export_github_dashboard.py"),
                "--graph", str(cfg.graph_path), "--repo-root", str(cfg.lean_root)]
    else:  # serve
        argv = [sys.executable, str(root / "scripts" / "service_control.py"), "start", "review",
                "--project", str(cfg.project), "--plugin-root", str(root),
                "--graph", str(cfg.graph_path), "--lean-root", str(cfg.lean_root), "--port", "0"]
    proc = subprocess.run(argv, cwd=str(root))
    return proc.returncode


def cmd_audit(args) -> int:
    """Run the roadmap completeness audit (scripts/roadmap_audit.py)."""
    cfg = _config(args)
    root = plugin_root()
    argv = [sys.executable, str(root / "scripts" / "roadmap_audit.py"), str(cfg.graph_path)]
    for flag in ("json", "enqueue", "verify_decls", "stamp_verified"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    if args.mathlib:
        argv += ["--mathlib", args.mathlib]
    return subprocess.run(argv, cwd=str(root)).returncode


def cmd_agents(args) -> int:
    """Show the role registry — what `work` can dispatch, and where each came from."""
    from .agent_work import role_summary
    from .registry import Registry

    try:
        cfg = _config(args)
        registry = Registry(cfg.plugin_root, cfg.project)
    except Die:
        registry = Registry(plugin_root())      # no project — plugin roles only
    if args.json:
        print(json.dumps(registry.palette(), indent=2, ensure_ascii=False))
        return EX_OK
    print(f"{len(registry.roles)} role(s) discovered "
          f"({len(registry.agent_kinds())} drained by the worker's agent stage):\n")
    for line in role_summary(registry):
        print(line)
    print("\nAdd a role by dropping agents/<kind>.md (or <project>/.autoform/agents/<kind>.md) "
          "with `kind:` and `drained_by: agent` in its frontmatter.")
    return EX_OK


def cmd_doctor(args) -> int:
    cfg = None
    try:
        cfg = _config(args)
    except Die:
        pass
    checks = doctor_mod.run_doctor(cfg)
    if args.json:
        print(json.dumps([{"check": name, "ok": ok, "detail": detail}
                          for name, ok, detail in checks], indent=2))
    else:
        for name, ok, detail in checks:
            print(f"  {'✅' if ok else '✗'} {name:18} {detail}")
    return EX_OK if all(ok for _name, ok, _detail in checks) else EX_ERROR


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoform",
        description="Autoform distributed worker — many machines, one formalization roadmap.",
    )
    parser.add_argument("--version", action="version", version=f"autoform-worker {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_work = sub.add_parser("work", help="run one work round (or --loop forever)")
    _add_round_flags(p_work)
    p_work.add_argument("--loop", action="store_true", help="run rounds forever with backoff")

    p_round = sub.add_parser("_round")  # hidden: one isolated round (loop child)
    _add_round_flags(p_round)

    p_status = sub.add_parser("status", help="survey + claims snapshot")
    _add_round_flags(p_status)
    p_status.add_argument("--json", action="store_true")

    p_claim = sub.add_parser("claim", help="cooperative lease board (git refs)")
    _add_common(p_claim)
    p_claim.add_argument("action", choices=["acquire", "renew", "release", "holds", "read", "list", "gc"])
    p_claim.add_argument("key", nargs="?")
    p_claim.add_argument("--node", default=None,
                         help="derive the canonical author/<slug>-<hash> key from a graph node id")
    p_claim.add_argument("--ttl", type=int, default=CLAIM_TTL_S)
    p_claim.add_argument("--steal", action="store_true", help="take over a live foreign lease (be sure)")

    p_push = sub.add_parser("push", help="CAS branch push (git-safe-push semantics)")
    _add_common(p_push)
    p_push.add_argument("ref", nargs="?", help="branch to push (or AUTOFORM_PUSH_REF)")
    p_push.add_argument("--remote", default=None, help="push remote URL (or AUTOFORM_PUSH_REMOTE)")
    p_push.add_argument("--expect", default=None,
                        help="observed remote OID; empty = create-only (or AUTOFORM_PUSH_EXPECT)")

    p_pr = sub.add_parser("pr-create", help="marker+lease-gated gh pr create (extra args pass through)")
    _add_common(p_pr)
    p_pr.add_argument("--body-file", required=True)

    p_sync = sub.add_parser("sync", help="fold merged scoreboards into the local sidecar")
    _add_common(p_sync)
    p_sync.add_argument("--json", action="store_true")

    p_issues = sub.add_parser("issues", help="GitHub issue integration")
    issues_sub = p_issues.add_subparsers(dest="issues_cmd", required=True)
    p_isync = issues_sub.add_parser("sync", help="mirror escalations to issues; close resolved ones")
    _add_common(p_isync)
    p_isync.add_argument("--dry-run", action="store_true")

    p_dash = sub.add_parser("dashboard", help="local/static dashboards (wraps the existing scripts)")
    _add_common(p_dash)
    p_dash.add_argument("dashboard_cmd", choices=["export", "serve"])

    p_agents = sub.add_parser("agents", help="list discovered agent roles (agents/*.md)")
    _add_common(p_agents)
    p_agents.add_argument("--json", action="store_true")

    p_audit = sub.add_parser("audit", help="roadmap completeness audit (offenders → queued gap tasks)")
    _add_common(p_audit)
    p_audit.add_argument("--json", action="store_true")
    p_audit.add_argument("--enqueue", action="store_true",
                         help="queue one role task per auditable offender (deduplicated)")
    p_audit.add_argument("--verify-decls", action="store_true",
                         help="grep claimed declarations in the local Mathlib checkout")
    p_audit.add_argument("--stamp-verified", action="store_true",
                         help="stamp mathlib_verified on fully-resolving nodes (implies --verify-decls)")
    p_audit.add_argument("--mathlib", default=None, help="explicit Mathlib checkout root")

    p_doc = sub.add_parser("doctor", help="environment/auth/repo capability audit")
    _add_common(p_doc)
    p_doc.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known, extra = build_parser().parse_known_args(argv)
    if known.cmd is None:
        build_parser().print_help()
        return EX_OK
    if extra and known.cmd != "pr-create":
        raise Die(f"unrecognized arguments: {' '.join(extra)}")
    if known.cmd == "work":
        return cmd_work(known)
    if known.cmd == "_round":
        return cmd_round(known)
    if known.cmd == "status":
        return cmd_status(known)
    if known.cmd == "claim":
        return cmd_claim(known)
    if known.cmd == "push":
        return cmd_push(known)
    if known.cmd == "pr-create":
        return cmd_pr_create(known, extra)
    if known.cmd == "sync":
        return cmd_sync(known)
    if known.cmd == "issues":
        return cmd_issues_sync(known)
    if known.cmd == "dashboard":
        return cmd_dashboard(known)
    if known.cmd == "agents":
        return cmd_agents(known)
    if known.cmd == "audit":
        return cmd_audit(known)
    if known.cmd == "doctor":
        return cmd_doctor(known)
    raise Die(f"unknown command {known.cmd!r}")


def cli_main() -> int:
    try:
        return main()
    except Die as error:
        print(f"autoform: {error}", file=sys.stderr)
        return EX_ERROR
    except NoProgress as np:
        print(f"autoform: no progress: {np}", file=sys.stderr)
        return EX_NOPROGRESS
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(cli_main())
