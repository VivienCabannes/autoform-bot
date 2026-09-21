"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import status
from ._directory_binding import RetainedDirectory, open_directory
from ._tree_snapshot import (
    BoundDirectoryTree,
    TreeCaptureLimits,
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
)
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import (
    CLAIM_OPERATION_TIMEOUT_S,
    CLAIM_NOTE_MAX_BYTES,
    CLAIM_TTL_S,
    LEGACY_BLOCK_SCHEMA,
    ClaimBoard,
    ClaimTransportError,
    _claim_git_environment,
    _bounded_text,
    _run_bounded_process,
    _validate_ttl,
    author_claim_key,
    claim_repository_is_remote,
    pin_claim_repository,
    pin_claim_scratch,
    resource_claim_key,
)
from .doctor import diagnose_project
from .graph import ARTICLE_ID_PATTERN, GraphValidationError, load_graph
from .lean import build_linker, declaration_names
from .render import PublicationError, render_site
from .runtime import RuntimeProjectionError, resolve_runtime_paths
from .scaffold import ScaffoldError, scaffold_project

_CLAIM_GRAPH_SELECTION = TreeSelection(
    include=lambda path, mode: bool(
        path.parts and path.parts[0] == "roadmap" and (not stat.S_ISREG(mode) or path.suffix == ".md")
    ),
    descend=lambda path: path == PurePosixPath("roadmap") or (bool(path.parts) and path.parts[0] == "roadmap"),
    limits=TreeCaptureLimits(
        max_entries=100_000,
        max_depth=128,
        max_file_bytes=2 << 20,
        max_total_bytes=64 << 20,
    ),
)


@dataclass(frozen=True, slots=True)
class _ClaimBoardIdentity:
    repo: str
    repo_identity: tuple[int, int] | None
    session_id: str
    scratch: Path
    scratch_identity: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _ResolvedClaimTarget:
    key: str
    label: str
    legacy_key: str | None
    canonical_keys: tuple[str, ...]
    board_identity: _ClaimBoardIdentity
    blueprint_binding: BoundDirectoryTree | None = None
    blueprint_snapshot: TreeSnapshot | None = None
    require_existing_legacy_fence: bool = False

    def verify(self) -> None:
        if self.blueprint_binding is None or self.blueprint_snapshot is None:
            return
        try:
            current = self.blueprint_binding.capture()
        except TreeSnapshotError as exc:
            raise ValueError("claim blueprint changed while resolving the claim") from exc
        if current != self.blueprint_snapshot:
            raise ValueError("claim blueprint changed while resolving the claim")

    def close(self) -> None:
        if self.blueprint_binding is not None:
            self.blueprint_binding.close()


@dataclass(slots=True)
class _PinnedClaimGitContext:
    root: RetainedDirectory
    git_dir: RetainedDirectory
    common_dir: RetainedDirectory | None
    metadata_kind: str
    metadata_identity: tuple[int, ...]
    metadata_bytes: bytes | None
    commondir_identity: tuple[int, ...] | None
    commondir_bytes: bytes | None

    def verify(self) -> None:
        self.root.verify()
        self.git_dir.verify()
        if self.common_dir is not None:
            self.common_dir.verify()
            content, identity = _read_stable_named_file(
                self.git_dir.descriptor,
                "commondir",
                maximum=4096,
                label="Git common-directory locator",
            )
            if content != self.commondir_bytes or identity != self.commondir_identity:
                raise ValueError("Git common-directory locator changed")
        else:
            try:
                os.stat("commondir", dir_fd=self.git_dir.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("Git common-directory locator changed")
        if self.metadata_kind == "file":
            content, identity = _read_stable_named_file(
                self.root.descriptor,
                ".git",
                maximum=4096,
                label="Git worktree metadata file",
            )
            if content != self.metadata_bytes or identity != self.metadata_identity:
                raise ValueError("Git worktree metadata changed")
            return
        named = os.stat(".git", dir_fd=self.root.descriptor, follow_symlinks=False)
        identity = (named.st_dev, named.st_ino, named.st_mode)
        if not stat.S_ISDIR(named.st_mode) or identity != self.metadata_identity:
            raise ValueError("Git worktree metadata changed")

    def close(self) -> None:
        if self.common_dir is not None:
            self.common_dir.close()
        self.git_dir.close()
        self.root.close()


def _read_stable_named_file(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
    label: str,
) -> tuple[bytes, tuple[int, ...]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        named_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode) or opened_before.st_nlink != 1 or opened_before.st_size > maximum:
            raise OSError(f"{label} is not a bounded private regular file")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    opened_identity = identity(opened_before)
    if (
        len(content) > maximum
        or len(content) != opened_before.st_size
        or opened_identity != identity(named_before)
        or opened_identity != identity(opened_after)
        or opened_identity != identity(named_after)
    ):
        raise ValueError(f"{label} changed while it was read")
    return bytes(content), opened_identity


def _load_claim_graph(blueprint: Path):
    """Load one bounded, immutable roadmap generation for claim resolution."""

    binding = BoundDirectoryTree(blueprint, selection=_CLAIM_GRAPH_SELECTION)
    try:
        snapshot = binding.capture()
        unsupported = snapshot.unsupported_entries()
        if unsupported:
            relative, reason = unsupported[0]
            raise ValueError(f"claim blueprint {relative}: {reason}")
        with tempfile.TemporaryDirectory(prefix="autoform-claim-graph-") as temporary:
            materialized = Path(temporary) / "blueprint"
            snapshot.materialize(materialized)
            graph = load_graph(materialized)
        if binding.capture() != snapshot:
            raise ValueError("claim blueprint changed while resolving the claim")
        return binding, snapshot, graph
    except BaseException:
        binding.close()
        raise


def _claim_article_legacy_contract(graph) -> tuple[dict[str, str], tuple[str, ...]]:
    mappings = {
        author_claim_key(node.id): author_claim_key(node.article_id)
        for node in graph.nodes.values()
        if node.article_id is not None
    }
    canonical_owners = {
        author_claim_key(node.article_id): node.id for node in graph.nodes.values() if node.article_id is not None
    }
    collisions = sorted(
        node.id
        for node in graph.nodes.values()
        if node.article_id is not None
        and (owner := canonical_owners.get(author_claim_key(node.id))) is not None
        and owner != node.id
    )
    if collisions:
        raise ValueError("legacy article path collides with a durable canonical claim key: " + ", ".join(collisions))
    return mappings, tuple(canonical_owners)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoform")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="write the blueprint vault, site config, and CI")
    init.add_argument("target", nargs="?", default=".", help="project root (default: current directory)")
    init.add_argument("--title", help="human project title (default: the directory name)")
    init.add_argument("--repository-url", default="", help="project URL, e.g. https://github.com/owner/repo")
    init.add_argument(
        "--autoform-source",
        default="",
        help="Autoform Git source the generated workflows install from (default: this checkout's origin)",
    )
    init.add_argument(
        "--autoform-ref",
        default="",
        help="immutable ref the workflows pin (default: this checkout's HEAD commit)",
    )
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init.add_argument("--json", action="store_true", help="write stable machine-readable output")

    check = subparsers.add_parser("check", help="validate a Markdown blueprint")
    check.add_argument("blueprint_dir")
    check.add_argument(
        "--lean-root",
        type=Path,
        help="Lean project to resolve 'lean:' declarations against (enables declaration checking)",
    )

    audit = subparsers.add_parser("audit", help="audit roadmap completeness and checked facts")
    audit.add_argument("blueprint_dir")
    audit.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    audit.add_argument("--json", action="store_true", help="write stable machine-readable output")

    doctor = subparsers.add_parser("doctor", help="diagnose the local Markdown runtime contract")
    doctor.add_argument("project_or_blueprint")
    doctor.add_argument("--lean-root", type=Path, help="Lean project to resolve local targets against")
    doctor.add_argument("--json", action="store_true", help="write stable machine-readable output")

    claim = subparsers.add_parser("claim", help="coordinate temporary article and resource ownership through Git refs")
    claim_subparsers = claim.add_subparsers(dest="claim_command", required=True)
    claim_help = {
        "acquire": "acquire an available claim or refresh this session's claim",
        "renew": "renew this session's live claim",
        "release": "release this session's live claim",
    }
    for operation in ("acquire", "renew", "release"):
        command = claim_subparsers.add_parser(operation, help=claim_help[operation])
        command.add_argument("node_id", nargs="?", help="roadmap path id or exact article_id")
        command.add_argument("--resource", help="claim a raw shared resource instead of an article")
        command.add_argument(
            "--blueprint",
            help=(
                "project or blueprint used to resolve articles and validate resource "
                "acquisition (default: current project)"
            ),
        )
        _add_claim_board_arguments(command)
        if operation in {"acquire", "renew"}:
            command.add_argument(
                "--ttl",
                type=int,
                default=CLAIM_TTL_S,
                help=f"lease lifetime in seconds (default: {CLAIM_TTL_S})",
            )
        if operation == "acquire":
            command.add_argument("--note", default="", help="short claim context for collaborators")
    claim_list = claim_subparsers.add_parser("list", help="list remote claims and safety records")
    _add_claim_board_arguments(claim_list)
    claim_cleanup = claim_subparsers.add_parser(
        "cleanup",
        help="recover expired or unsafe claims with exact compare-and-swap",
    )
    _add_claim_board_arguments(claim_cleanup)
    claim_cleanup.add_argument(
        "--blueprint",
        help="project or blueprint directory required to retire legacy author refs safely",
    )

    migrate = subparsers.add_parser("migrate", help="inspect authored migration contracts")
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command", required=True)
    article_ids = migrate_subparsers.add_parser(
        "article-ids",
        help="plan durable roadmap article identifiers without writing files",
    )
    article_ids.add_argument("blueprint_dir")
    article_ids.add_argument(
        "--check",
        action="store_true",
        help="fail when an article is missing article_id frontmatter",
    )
    article_ids.add_argument("--json", action="store_true", help="write stable machine-readable output")

    render = subparsers.add_parser("render", help="build the publishable blueprint")
    render.add_argument("blueprint_dir")
    render.add_argument("-o", "--output", default="site-src", help="output directory")
    render.add_argument("--lean-root", type=Path, help="Lean project to link code from")
    render.add_argument("--repository-url", help="project URL, e.g. https://github.com/owner/repo")
    render.add_argument("--ref", help="commit or branch the code links should pin")
    render.add_argument(
        "--require-declarations",
        action="store_true",
        help="fail when a 'lean:' declaration is not found in the Lean sources",
    )

    args = parser.parse_args(argv)

    if args.command == "init":
        return _init(args)
    if args.command == "check":
        return _check(args)
    if args.command == "audit":
        return _audit(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "claim":
        return _claim(args)
    if args.command == "migrate":
        return _migrate(args)
    if args.command == "render":
        return _render(args)
    return 2


def _add_claim_board_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="claim-board Git repository; defaults to this checkout's origin")
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("AUTOFORM_WORKER_ID"),
        help="display identity for this agent (or set AUTOFORM_WORKER_ID)",
    )
    parser.add_argument(
        "--session-id",
        default=os.environ.get("AUTOFORM_CLAIM_SESSION_ID"),
        help="stable work session identity (or set AUTOFORM_CLAIM_SESSION_ID)",
    )
    parser.add_argument("--scratch", type=Path, help="local bare Git object cache")
    parser.add_argument(
        "--object-format",
        choices=("sha1", "sha256"),
        default=os.environ.get("AUTOFORM_GIT_OBJECT_FORMAT"),
        help="Git object format for an empty network claim repository",
    )


def _init(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser()
    title = args.title or target.resolve().name
    try:
        result = scaffold_project(
            target,
            title=title,
            repository_url=args.repository_url,
            autoform_source=args.autoform_source,
            autoform_ref=args.autoform_ref,
            force=args.force,
        )
    except ScaffoldError as error:
        for issue in error.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
        return 0

    print(f"{target}: {len(result.written)} files written")
    for path in result.written:
        print(f"  + {path}")
    for path in result.skipped:
        note = "no Autoform ref to pin" if result.unpinned and ".github" in path else "exists, left alone"
        print(f"  = {path} ({note})")
    print("Next: describe the project in blueprint/README.md, then add chapters as roadmap/<chapter>/README.md.")
    if result.unpinned:
        # Flush first: stdout is block-buffered when piped, so without this the
        # warning jumps ahead of the file list it is explaining.
        sys.stdout.flush()
        print(
            "\nCI was not written: generated workflows install Autoform from a Git\n"
            "ref, and this Autoform is not running from a checkout, so there is\n"
            "nothing to pin. Re-run with the commit to add them:\n"
            "  autoform init --autoform-ref <40-char-sha>",
            file=sys.stderr,
        )
    return 0


def _check(args: argparse.Namespace) -> int:
    try:
        graph = load_graph(args.blueprint_dir)
    except GraphValidationError as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    statuses = status.derive(graph)
    summary = " · ".join(f"{count} {state.label}" for state, count in status.summarize(statuses))
    print(f"OK: {len(graph.nodes)} articles, {graph.edge_count} dependencies")
    if summary:
        print(f"    {summary}")

    if args.lean_root is None:
        return 0

    linker = build_linker(args.lean_root)
    missing = [
        f"{node.id}: declaration not found in {args.lean_root}: {name}"
        for node in graph.nodes.values()
        for name in declaration_names(node.lean or "")
        if linker.location(name) is None
    ]
    for issue in missing:
        print(f"error: {issue}")
    if missing:
        return 1
    declared = sum(1 for node in graph.nodes.values() if node.lean)
    print(f"    {declared} declaration(s) resolved in the Lean sources")
    return 0


def _audit(args: argparse.Namespace) -> int:
    result = audit_blueprint(args.blueprint_dir, lean_root=args.lean_root)
    if args.json:
        print(result.to_json())
    else:
        if result.clean:
            print("OK: roadmap audit passed")
        if result.coverage is not None:
            counts = result.coverage.counts
            print(
                "    coverage: "
                f"{counts['MAPPED']} mapped · "
                f"{counts['DECOMPOSED']} decomposed · "
                f"{counts['DEFERRED']} deferred · "
                f"{counts['OUT']} out"
            )
        for finding in result.findings:
            print(f"error: {finding.article_path}: {finding.code}: {finding.reason}")
    return 0 if result.clean else 1


def _doctor(args: argparse.Namespace) -> int:
    result = diagnose_project(args.project_or_blueprint, lean_root=args.lean_root)
    if args.json:
        print(result.to_json())
    else:
        for check in result.checks:
            marker = "PASS" if check.ok else "FAIL"
            print(f"{marker}: {check.name}: {check.detail}")
    return 0 if result.clean else 1


def _claim(args: argparse.Namespace) -> int:
    target: _ResolvedClaimTarget | None = None
    try:
        operation = args.claim_command
        if operation == "list":
            with _claim_board(args, require_identity=False) as board:
                print(json.dumps(board.list(), sort_keys=True, separators=(",", ":")))
            return 0
        if operation == "cleanup":
            legacy_mappings, board_identity, blueprint_binding, blueprint_snapshot = _resolve_claim_cleanup(args)
            try:
                with _claim_board(
                    args,
                    identity=board_identity,
                    require_identity=False,
                ) as board:
                    if blueprint_binding is not None and blueprint_binding.capture() != blueprint_snapshot:
                        raise ValueError("claim blueprint changed while resolving the claim")
                    recovered = board.cleanup(legacy_mappings=legacy_mappings)
                    if blueprint_binding is not None and blueprint_binding.capture() != blueprint_snapshot:
                        raise ClaimTransportError(
                            "claim blueprint changed during cleanup; monotonic recovery "
                            "from the pinned generation completed"
                        )
                print(f"recovered {recovered} expired or unsafe-timestamp claim(s)")
            finally:
                if blueprint_binding is not None:
                    blueprint_binding.close()
            return 0

        if operation in {"acquire", "renew"}:
            _validate_ttl(args.ttl)
        if operation == "acquire":
            _bounded_text(
                args.note,
                label="claim note",
                maximum=CLAIM_NOTE_MAX_BYTES,
                allow_empty=True,
            )

        target = _resolve_claim_target(args, operation=operation)
        with _claim_board(args, identity=target.board_identity) as board:
            mutations = []
            try:
                target.verify()
                fence = board.held_claim_fence(target.key) if operation == "renew" else None
                if operation == "renew" and fence is None:
                    succeeded = False
                else:
                    if target.require_existing_legacy_fence:
                        assert target.legacy_key is not None
                        compatibility = board.read(target.legacy_key)
                        if (
                            compatibility is None
                            or compatibility.get("schema") != LEGACY_BLOCK_SCHEMA
                            or compatibility.get("canonical_resource") != target.key
                        ):
                            succeeded = False
                        else:
                            succeeded = None
                    else:
                        succeeded = None
                    if succeeded is None and operation in {"acquire", "renew"} and target.legacy_key is not None:
                        prepared = board.prepare_v2_claim(
                            target.key,
                            [target.legacy_key],
                            canonical_keys=target.canonical_keys,
                        )
                        if not prepared:
                            print(
                                f"error: could not {operation} {target.label}; "
                                "a live legacy v1 claim or incompatible path claim blocks "
                                "v2 rollout",
                                file=sys.stderr,
                            )
                            return 1
                    target.verify()
                    if succeeded is False:
                        pass
                    elif operation == "acquire":
                        succeeded = board.acquire(
                            target.key,
                            ttl=args.ttl,
                            note=args.note,
                            mutations=mutations,
                        )
                    elif operation == "renew":
                        assert fence is not None
                        succeeded = board.renew(
                            target.key,
                            ttl=args.ttl,
                            lease_id=fence.lease_id,
                            mutations=mutations,
                        )
                    else:
                        succeeded = board.release(target.key)
                    if succeeded and operation in {"acquire", "renew"}:
                        target.verify()
                if not succeeded and mutations:
                    board._rollback_claim_mutations(mutations)
                    mutations.clear()
            except BaseException:
                if mutations:
                    board._rollback_claim_mutations(mutations)
                raise
        if succeeded:
            past_tense = {"acquire": "acquired", "renew": "renewed", "release": "released"}
            print(f"{past_tense[operation]} {target.label} ({target.key})")
            return 0
        print(
            f"error: could not {operation} {target.label}; ownership is held or unverifiable",
            file=sys.stderr,
        )
        return 1
    except (ClaimTransportError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if target is not None:
            target.close()


def _resolve_claim_cleanup(
    args: argparse.Namespace,
) -> tuple[
    dict[str, str] | None,
    _ClaimBoardIdentity | None,
    BoundDirectoryTree | None,
    TreeSnapshot | None,
]:
    if args.blueprint is None:
        return None, None, None, None
    binding: BoundDirectoryTree | None = None
    try:
        paths = resolve_runtime_paths(args.blueprint)
        binding, snapshot, graph = _load_claim_graph(paths.blueprint_dir)
        identity = _resolve_claim_board_identity(
            args,
            context=paths.project_root,
            require_identity=False,
        )
        if binding.capture() != snapshot:
            raise ValueError("claim blueprint changed while resolving the claim")
        legacy_mappings, _canonical_keys = _claim_article_legacy_contract(graph)
        return legacy_mappings, identity, binding, snapshot
    except RuntimeProjectionError as exc:
        if binding is not None:
            binding.close()
        raise ValueError(str(exc)) from exc
    except GraphValidationError as exc:
        if binding is not None:
            binding.close()
        raise ValueError("; ".join(exc.issues)) from exc
    except BaseException:
        if binding is not None:
            binding.close()
        raise


def _migrate(args: argparse.Namespace) -> int:
    if args.migrate_command != "article-ids":
        return 2
    try:
        plan = plan_article_ids(args.blueprint_dir)
    except GraphValidationError as error:
        for issue in error.issues:
            print(f"error: {issue}", file=sys.stderr)
        return 2

    if args.json:
        print(plan.to_json())
    elif plan.complete:
        print(f"OK: {len(plan.entries)} articles have durable article_id metadata")
    else:
        print(f"{plan.missing_count} article(s) need article_id metadata")
        for entry in plan.entries:
            if not entry.assigned:
                print(f"  {entry.article_path}: {entry.article_id}")
    return 1 if args.check and not plan.complete else 0


def _resolve_claim_board_identity(
    args: argparse.Namespace,
    *,
    context: str | Path | None = None,
    require_identity: bool = True,
) -> _ClaimBoardIdentity:
    repo = args.repo
    session_id = args.session_id
    context_binding: RetainedDirectory | None = None
    if repo is None or (require_identity and session_id is None):
        if context is None:
            context = getattr(args, "blueprint", None) or "."
        context = Path(context).expanduser().resolve()
        try:
            context_binding = open_directory(context)
        except OSError as exc:
            raise ValueError("claim context cannot be pinned safely") from exc
    deadline = time.monotonic() + CLAIM_OPERATION_TIMEOUT_S
    try:
        need_origin = repo is None
        need_session = session_id is None and require_identity
        if need_origin or need_session:
            assert context_binding is not None
            resolved_repo, resolved_session = _claim_context_values(
                context_binding,
                deadline=deadline,
                need_origin=need_origin,
                need_session=need_session,
            )
            if need_origin:
                assert resolved_repo is not None
                repo = resolved_repo
            if need_session:
                assert resolved_session is not None
                session_id = resolved_session
        if session_id is None:
            session_id = "claim-maintenance"
        if context_binding is not None:
            context_binding.verify()
    except OSError as exc:
        raise ValueError("claim context was replaced while resolving the claim") from exc
    finally:
        if context_binding is not None:
            context_binding.close()
    normalized_repo, repo_identity = pin_claim_repository(repo)
    scratch, scratch_identity = pin_claim_scratch(args.scratch or _default_claim_scratch(normalized_repo, session_id))
    return _ClaimBoardIdentity(
        repo=normalized_repo,
        repo_identity=repo_identity,
        session_id=session_id,
        scratch=scratch,
        scratch_identity=scratch_identity,
    )


def _claim_board(
    args: argparse.Namespace,
    *,
    identity: _ClaimBoardIdentity | None = None,
    require_identity: bool = True,
) -> ClaimBoard:
    worker_id = args.worker_id or ("claim-maintenance" if not require_identity else None)
    if worker_id is None:
        raise ValueError("--worker-id or AUTOFORM_WORKER_ID is required")
    identity = identity or _resolve_claim_board_identity(args, require_identity=require_identity)
    return ClaimBoard(
        identity.repo,
        worker_id,
        identity.scratch,
        session_id=identity.session_id,
        expected_object_format=args.object_format,
        expected_repo_identity=identity.repo_identity,
        expected_scratch_identity=identity.scratch_identity,
    )


def _resolve_claim_target(
    args: argparse.Namespace,
    *,
    operation: str,
) -> _ResolvedClaimTarget:
    article_target = args.node_id
    resource = args.resource
    if article_target and resource:
        raise ValueError("article target and --resource are mutually exclusive")
    if resource:
        if ARTICLE_ID_PATTERN.fullmatch(resource):
            raise ValueError("resource names must not use the reserved article_id format")
        if operation != "acquire":
            identity = _resolve_claim_board_identity(
                args,
                context=args.blueprint or ".",
            )
            return _ResolvedClaimTarget(
                resource_claim_key(resource),
                resource,
                author_claim_key(resource) if operation == "renew" else None,
                (),
                identity,
                require_existing_legacy_fence=operation == "renew",
            )
        blueprint_binding: BoundDirectoryTree | None = None
        try:
            paths = None
            if args.blueprint is not None:
                paths = resolve_runtime_paths(args.blueprint)
            else:
                try:
                    paths = resolve_runtime_paths(".")
                except RuntimeProjectionError as exc:
                    if exc.issues != ("roadmap directory does not exist",):
                        raise
            if paths is None:
                raise ValueError("resource claims require --blueprint outside an Autoform project")

            blueprint_binding, blueprint_snapshot, graph = _load_claim_graph(paths.blueprint_dir)
            if any(node.id == resource for node in graph.nodes.values()):
                raise ValueError(f"resource name {resource!r} collides with an existing article path")
            identity = _resolve_claim_board_identity(args, context=paths.project_root)
            if blueprint_binding.capture() != blueprint_snapshot:
                raise ValueError("claim blueprint changed while resolving the claim")
            return _ResolvedClaimTarget(
                resource_claim_key(resource),
                resource,
                author_claim_key(resource),
                (),
                identity,
                blueprint_binding,
                blueprint_snapshot,
            )
        except RuntimeProjectionError as exc:
            if blueprint_binding is not None:
                blueprint_binding.close()
            raise ValueError(str(exc)) from exc
        except TreeSnapshotError as exc:
            if blueprint_binding is not None:
                blueprint_binding.close()
            raise ValueError(str(exc)) from exc
        except GraphValidationError as exc:
            if blueprint_binding is not None:
                blueprint_binding.close()
            raise ValueError("; ".join(exc.issues)) from exc
        except BaseException:
            if blueprint_binding is not None:
                blueprint_binding.close()
            raise
    if not article_target:
        raise ValueError("an article target or --resource is required")

    def release_by_durable_id(*, context: str | Path) -> _ResolvedClaimTarget:
        identity = _resolve_claim_board_identity(
            args,
            context=context,
        )
        return _ResolvedClaimTarget(
            author_claim_key(article_target),
            article_target,
            None,
            (),
            identity,
        )

    blueprint_binding = None
    try:
        paths = resolve_runtime_paths(args.blueprint or ".")
        blueprint = paths.blueprint_dir
        blueprint_binding, blueprint_snapshot, graph = _load_claim_graph(blueprint)
    except RuntimeProjectionError as exc:
        if operation == "release" and ARTICLE_ID_PATTERN.fullmatch(article_target):
            return release_by_durable_id(context=args.blueprint or ".")
        raise ValueError(str(exc)) from exc
    except TreeSnapshotError as exc:
        raise ValueError(str(exc)) from exc
    except GraphValidationError as exc:
        raise ValueError("; ".join(exc.issues)) from exc
    try:
        matches = [
            node for node in graph.nodes.values() if article_target == node.id or article_target == node.article_id
        ]
        if not matches:
            if operation == "release" and ARTICLE_ID_PATTERN.fullmatch(article_target):
                blueprint_binding.close()
                blueprint_binding = None
                return release_by_durable_id(context=paths.project_root)
            if article_target == "lake-build":
                raise ValueError(
                    f"article target {article_target!r} does not exist in {blueprint}; "
                    "use --resource lake-build for the shared build lock"
                )
            raise ValueError(f"article target {article_target!r} does not exist in {blueprint}")
        if len(matches) != 1:
            matches_text = ", ".join(sorted(node.id for node in matches))
            raise ValueError(f"article target {article_target!r} is ambiguous: {matches_text}")
        node = matches[0]
        if node.article_id is None:
            raise ValueError(
                f"article {node.id!r} has no durable article_id; "
                f"run 'autoform migrate article-ids {blueprint}' and add the proposed ID"
            )
        if operation in {"acquire", "renew"}:
            _legacy_mappings, canonical_keys = _claim_article_legacy_contract(graph)
        else:
            canonical_keys = ()
        identity = _resolve_claim_board_identity(args, context=paths.project_root)
        if blueprint_binding.capture() != blueprint_snapshot:
            raise ValueError("claim blueprint changed while resolving the claim")
        return _ResolvedClaimTarget(
            author_claim_key(node.article_id),
            node.id,
            author_claim_key(node.id),
            canonical_keys,
            identity,
            blueprint_binding,
            blueprint_snapshot,
        )
    except BaseException:
        if blueprint_binding is not None:
            blueprint_binding.close()
        raise


def _bind_claim_git_context(
    context: RetainedDirectory,
    *,
    need_origin: bool,
    need_session: bool,
) -> _PinnedClaimGitContext:
    context.verify()
    metadata_index: int | None = None
    metadata_stat: os.stat_result | None = None
    for index in range(len(context.descriptors) - 1, -1, -1):
        try:
            candidate = os.stat(
                ".git",
                dir_fd=context.descriptors[index],
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("could not inspect the Git worktree metadata") from exc
        metadata_index = index
        metadata_stat = candidate
        break
    if metadata_index is None or metadata_stat is None:
        if need_origin and need_session:
            raise ValueError("--repo and --session-id are required outside a Git checkout")
        if need_origin:
            raise ValueError("--repo is required outside a Git checkout")
        raise ValueError("--session-id or AUTOFORM_CLAIM_SESSION_ID is required outside a Git worktree")

    root_path = Path(*context.path.parts[: metadata_index + 1])
    try:
        root = open_directory(root_path)
    except OSError as exc:
        raise ValueError("could not pin the Git worktree root") from exc
    git_dir: RetainedDirectory | None = None
    common_dir: RetainedDirectory | None = None
    try:
        if root.identity != context.identities[metadata_index]:
            raise ValueError("Git worktree root changed while it was inspected")
        if stat.S_ISDIR(metadata_stat.st_mode):
            metadata_kind = "directory"
            metadata_identity = (
                metadata_stat.st_dev,
                metadata_stat.st_ino,
                metadata_stat.st_mode,
            )
            metadata_bytes = None
            git_dir_path = root_path / ".git"
        elif stat.S_ISREG(metadata_stat.st_mode):
            metadata_kind = "file"
            metadata_bytes, metadata_identity = _read_stable_named_file(
                root.descriptor,
                ".git",
                maximum=4096,
                label="Git worktree metadata file",
            )
            initial_identity = (
                metadata_stat.st_dev,
                metadata_stat.st_ino,
                metadata_stat.st_mode,
                metadata_stat.st_nlink,
                metadata_stat.st_size,
                metadata_stat.st_mtime_ns,
                metadata_stat.st_ctime_ns,
            )
            if metadata_identity != initial_identity:
                raise ValueError("Git worktree metadata changed while it was inspected")
            line = metadata_bytes.rstrip(b"\r\n")
            if b"\n" in line or b"\r" in line or not line.startswith(b"gitdir: "):
                raise ValueError("Git worktree metadata file is malformed")
            raw_git_dir = Path(os.fsdecode(line[len(b"gitdir: ") :]))
            git_dir_path = (raw_git_dir if raw_git_dir.is_absolute() else root_path / raw_git_dir).resolve()
        else:
            raise ValueError("Git worktree metadata must be a regular file or directory")
        try:
            git_dir = open_directory(git_dir_path)
        except OSError as exc:
            raise ValueError("could not pin the Git worktree metadata directory") from exc
        if metadata_kind == "directory" and git_dir.identity != metadata_identity[:2]:
            raise ValueError("Git worktree metadata changed while it was inspected")
        try:
            os.stat("commondir", dir_fd=git_dir.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            commondir_bytes = None
            commondir_identity = None
        else:
            commondir_bytes, commondir_identity = _read_stable_named_file(
                git_dir.descriptor,
                "commondir",
                maximum=4096,
                label="Git common-directory locator",
            )
            line = commondir_bytes.rstrip(b"\r\n")
            if b"\n" in line or b"\r" in line or not line:
                raise ValueError("Git common-directory locator is malformed")
            raw_common_dir = Path(os.fsdecode(line))
            common_dir_path = (
                raw_common_dir if raw_common_dir.is_absolute() else git_dir.path / raw_common_dir
            ).resolve()
            try:
                common_dir = open_directory(common_dir_path)
            except OSError as exc:
                raise ValueError("could not pin the Git common directory") from exc
        pinned = _PinnedClaimGitContext(
            root=root,
            git_dir=git_dir,
            common_dir=common_dir,
            metadata_kind=metadata_kind,
            metadata_identity=metadata_identity,
            metadata_bytes=metadata_bytes,
            commondir_identity=commondir_identity,
            commondir_bytes=commondir_bytes,
        )
        pinned.verify()
        context.verify()
        return pinned
    except BaseException:
        if common_dir is not None:
            common_dir.close()
        if git_dir is not None:
            git_dir.close()
        root.close()
        raise


def _claim_context_values(
    context: RetainedDirectory,
    *,
    deadline: float,
    need_origin: bool,
    need_session: bool,
) -> tuple[str | None, str | None]:
    pinned = _bind_claim_git_context(
        context,
        need_origin=need_origin,
        need_session=need_session,
    )
    try:
        origin: str | None = None
        if need_origin:
            config_directory = pinned.common_dir or pinned.git_dir
            config_bytes, _config_identity = _read_stable_named_file(
                config_directory.descriptor,
                "config",
                maximum=64 << 10,
                label="Git repository config",
            )
            try:
                config_text = config_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Git repository config is not valid UTF-8") from exc
            worktree_config = _run_bounded_process(
                [
                    "git",
                    "config",
                    "--file",
                    "-",
                    "--no-includes",
                    "--bool",
                    "--get",
                    "extensions.worktreeConfig",
                ],
                deadline=deadline,
                env=_claim_git_environment(),
                input_text=config_text,
            )
            if worktree_config.returncode not in {0, 1}:
                raise ValueError("Git repository config has an invalid worktreeConfig value")
            worktree_text: str | None = None
            if worktree_config.returncode == 0 and worktree_config.stdout.strip() == "true":
                try:
                    os.stat(
                        "config.worktree",
                        dir_fd=pinned.git_dir.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    worktree_bytes, _worktree_identity = _read_stable_named_file(
                        pinned.git_dir.descriptor,
                        "config.worktree",
                        maximum=64 << 10,
                        label="Git worktree config",
                    )
                    try:
                        worktree_text = worktree_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError("Git worktree config is not valid UTF-8") from exc
            result = _run_bounded_process(
                [
                    "git",
                    "config",
                    "--file",
                    "-",
                    "--no-includes",
                    "--get-all",
                    "remote.origin.url",
                ],
                deadline=deadline,
                env=_claim_git_environment(),
                input_text=config_text,
            )
            if result.returncode == 1 and worktree_text is not None:
                result = _run_bounded_process(
                    [
                        "git",
                        "config",
                        "--file",
                        "-",
                        "--no-includes",
                        "--get-all",
                        "remote.origin.url",
                    ],
                    deadline=deadline,
                    env=_claim_git_environment(),
                    input_text=worktree_text,
                )
            if result.returncode != 0:
                raise ValueError("--repo is required outside a Git checkout with an origin remote")
            origin_lines = result.stdout.splitlines()
            if not origin_lines or any(not line for line in origin_lines):
                raise ValueError("Git checkout has an invalid origin remote")
            origin = origin_lines[0]
            if not claim_repository_is_remote(origin):
                origin_path = Path(origin).expanduser()
                if not origin_path.is_absolute():
                    origin_path = pinned.root.path / origin_path
                origin = os.path.abspath(os.path.normpath(origin_path))

        session_id: str | None = None
        if need_session:
            token = _worktree_claim_token(pinned.git_dir)
            root_identity = pinned.root.identity
            git_dir_identity = pinned.git_dir.identity
            identity = (
                f"{socket.gethostname()}\0{token}\0{root_identity[0]}:{root_identity[1]}"
                f"\0{git_dir_identity[0]}:{git_dir_identity[1]}"
            )
            session_id = f"worktree-{hashlib.sha256(identity.encode()).hexdigest()}"
        pinned.verify()
        context.verify()
        return origin, session_id
    finally:
        pinned.close()


def _origin_url(
    project_or_blueprint: str | Path | RetainedDirectory = ".",
    *,
    deadline: float | None = None,
) -> str:
    owns_binding = not isinstance(project_or_blueprint, RetainedDirectory)
    try:
        context = (
            open_directory(Path(project_or_blueprint).expanduser().resolve()) if owns_binding else project_or_blueprint
        )
    except OSError as exc:
        raise ValueError("claim context cannot be pinned safely") from exc
    assert isinstance(context, RetainedDirectory)
    operation_deadline = time.monotonic() + CLAIM_OPERATION_TIMEOUT_S if deadline is None else deadline
    try:
        origin, _session_id = _claim_context_values(
            context,
            deadline=operation_deadline,
            need_origin=True,
            need_session=False,
        )
        assert origin is not None
        return origin
    except OSError as exc:
        raise ValueError("claim context was replaced while resolving the claim") from exc
    finally:
        if owns_binding:
            context.close()


def _worktree_claim_session_id(
    project_or_blueprint: str | Path | RetainedDirectory = ".",
    *,
    deadline: float | None = None,
) -> str:
    owns_binding = not isinstance(project_or_blueprint, RetainedDirectory)
    try:
        context = (
            open_directory(Path(project_or_blueprint).expanduser().resolve()) if owns_binding else project_or_blueprint
        )
    except OSError as exc:
        raise ValueError("could not pin the Git worktree identity") from exc
    assert isinstance(context, RetainedDirectory)
    operation_deadline = time.monotonic() + CLAIM_OPERATION_TIMEOUT_S if deadline is None else deadline
    try:
        _origin, session_id = _claim_context_values(
            context,
            deadline=operation_deadline,
            need_origin=False,
            need_session=True,
        )
        assert session_id is not None
        return session_id
    except OSError as exc:
        raise ValueError("could not inspect the Git worktree identity") from exc
    finally:
        if owns_binding:
            context.close()


def _worktree_claim_token(git_dir: Path | RetainedDirectory) -> str:
    owns_binding = not isinstance(git_dir, RetainedDirectory)
    try:
        binding = open_directory(git_dir) if owns_binding else git_dir
    except OSError as exc:
        raise ValueError("could not pin the Git worktree identity") from exc
    assert isinstance(binding, RetainedDirectory)
    try:
        stored_token = _read_worktree_claim_token_fd(binding.descriptor)
        if stored_token is not None:
            return stored_token

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        token = secrets.token_hex(32)
        temporary_name = f".autoform-claim-session-{secrets.token_hex(16)}"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=binding.descriptor)
            remaining = memoryview(f"{token}\n".encode("ascii"))
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while installing claim session identity")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(
                    temporary_name,
                    "autoform-claim-session",
                    src_dir_fd=binding.descriptor,
                    dst_dir_fd=binding.descriptor,
                    follow_symlinks=False,
                )
                os.fsync(binding.descriptor)
            except FileExistsError:
                pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=binding.descriptor)
            except FileNotFoundError:
                pass
        binding.verify()
        stored_token = _read_worktree_claim_token_fd(binding.descriptor)
        if stored_token is None:
            raise ValueError("could not install the Git worktree claim identity")
        return stored_token
    except OSError as exc:
        raise ValueError("could not install the Git worktree claim identity") from exc
    finally:
        if owns_binding:
            binding.close()


def _read_worktree_claim_token(token_path: Path) -> str | None:
    try:
        binding = open_directory(token_path.parent)
    except OSError as exc:
        raise ValueError("could not read the Git worktree claim identity") from exc
    try:
        value = _read_worktree_claim_token_fd(binding.descriptor)
        binding.verify()
        return value
    except OSError as exc:
        raise ValueError("could not read the Git worktree claim identity") from exc
    finally:
        binding.close()


def _repair_worktree_token_install(
    directory_fd: int,
    token_info: os.stat_result,
) -> bool:
    try:
        names = os.listdir(directory_fd)
        if len(names) > 100_000:
            raise ValueError("Git worktree metadata contains too many entries")
        candidates: list[str] = []
        for name in names:
            if re.fullmatch(r"\.autoform-claim-session-[0-9a-f]{32}", name) is None:
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == (token_info.st_dev, token_info.st_ino):
                candidates.append(name)
        current = os.stat(
            "autoform-claim-session",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (token_info.st_dev, token_info.st_ino):
            return False
        if current.st_nlink == 1:
            return True
        if current.st_nlink != 2 or len(candidates) != 1:
            return False
        try:
            os.unlink(candidates[0], dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)
        repaired = os.stat(
            "autoform-claim-session",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ValueError("could not recover the Git worktree claim identity") from exc
    return bool(
        stat.S_ISREG(repaired.st_mode)
        and repaired.st_nlink == 1
        and (repaired.st_dev, repaired.st_ino) == (token_info.st_dev, token_info.st_ino)
    )


def _read_worktree_claim_token_fd(
    directory_fd: int,
    *,
    repair_install: bool = True,
) -> str | None:
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        read_flags |= os.O_NONBLOCK
    try:
        descriptor = os.open("autoform-claim-session", read_flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("could not read the Git worktree claim identity") from exc
    try:
        try:
            token_info = os.fstat(descriptor)
            path_info = os.stat(
                "autoform-claim-session",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            raw_token = os.read(descriptor, 256)
            final_info = os.fstat(descriptor)
            final_path_info = os.stat(
                "autoform-claim-session",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("could not read the Git worktree claim identity") from exc
    if not stat.S_ISREG(token_info.st_mode):
        raise ValueError("Git worktree claim identity must be a regular file")
    identity = (
        token_info.st_dev,
        token_info.st_ino,
        token_info.st_size,
        token_info.st_mtime_ns,
        token_info.st_ctime_ns,
        token_info.st_nlink,
    )
    identities_match = bool(
        identity
        == (
            path_info.st_dev,
            path_info.st_ino,
            path_info.st_size,
            path_info.st_mtime_ns,
            path_info.st_ctime_ns,
            path_info.st_nlink,
        )
        == (
            final_info.st_dev,
            final_info.st_ino,
            final_info.st_size,
            final_info.st_mtime_ns,
            final_info.st_ctime_ns,
            final_info.st_nlink,
        )
        == (
            final_path_info.st_dev,
            final_path_info.st_ino,
            final_path_info.st_size,
            final_path_info.st_mtime_ns,
            final_path_info.st_ctime_ns,
            final_path_info.st_nlink,
        )
    )
    if (
        repair_install
        and identities_match
        and token_info.st_nlink == 2
        and _repair_worktree_token_install(directory_fd, token_info)
    ):
        return _read_worktree_claim_token_fd(directory_fd, repair_install=False)
    if token_info.st_nlink != 1 or not identities_match:
        raise ValueError("Git worktree claim identity changed while it was read")
    try:
        stored_token = raw_token.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Git worktree claim identity is malformed") from exc
    if len(raw_token) != token_info.st_size or not re.fullmatch(
        r"[0-9a-f]{64}\n?",
        stored_token,
    ):
        raise ValueError("Git worktree claim identity is malformed")
    return stored_token.rstrip("\n")


def _default_claim_scratch(repo: str, session_id: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    identity = hashlib.sha256(f"{repo}\0{session_id}\0{socket.gethostname()}".encode()).hexdigest()[:24]
    return cache / "autoform" / "claims" / identity


def _render(args: argparse.Namespace) -> int:
    try:
        report = render_site(
            args.blueprint_dir,
            args.output,
            lean_root=args.lean_root,
            repository_url=args.repository_url,
            ref=args.ref,
        )
    except (GraphValidationError, PublicationError) as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    print(f"{report.output_dir}: {report.pages} pages, {report.nodes} nodes, {report.linked} code links")
    for issue in report.unresolved:
        print(f"warning: declaration not found in the Lean sources: {issue}")
    if report.unresolved and args.require_declarations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
