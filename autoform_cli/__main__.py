"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from . import status
from .article_identity import plan_article_ids
from .audit import audit_blueprint
from .claims import CLAIM_TTL_S, ClaimBoard, ClaimTransportError, author_claim_key
from .doctor import diagnose_project
from .graph import GraphValidationError, load_graph
from .lean import build_linker, declaration_names
from .project import (
    ProjectCatalogError,
    ProjectCreateError,
    ProjectRepairError,
    create_project,
    inspect_project,
    load_release_catalog,
    repair_project,
)
from .provenance import ProvenanceError, verify_plugin_provenance
from .render import PublicationError, render_site
from .scaffold import ScaffoldError, scaffold_project


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
        help="Autoform Git source for generated workflows (default: verified installation source)",
    )
    init.add_argument(
        "--autoform-ref",
        default="",
        help="full commit for generated workflows (default: verified installation revision)",
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

    project = subparsers.add_parser("project", help="inspect local project configuration and releases")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_new = project_subparsers.add_parser(
        "new", help="atomically create a complete Lean and Autoform project"
    )
    project_new.add_argument(
        "target",
        nargs="?",
        help="new absent directory, or '.' for the empty current directory",
    )
    project_new.add_argument("--package", help="UpperCamelCase Lean package name")
    project_new.add_argument("--release", help="release id from 'project versions'")
    project_new.add_argument(
        "--autoform-source",
        default="",
        help="trusted Autoform Git source for generated workflows",
    )
    project_new.add_argument(
        "--autoform-ref",
        default="",
        help="full 40-character Autoform commit for generated workflows",
    )
    project_new.add_argument("--json", action="store_true", help="write stable machine-readable output")
    project_repair = project_subparsers.add_parser(
        "repair", help="conservatively add unambiguous missing project files"
    )
    project_repair.add_argument("target", help="existing project directory")
    project_repair.add_argument(
        "--title", help="exact human project title for missing generated files"
    )
    project_repair.add_argument(
        "--repository-url",
        help="exact project URL for a missing site configuration (empty is allowed)",
    )
    project_repair.add_argument(
        "--autoform-source",
        help="exact Autoform Git source for missing workflows",
    )
    project_repair.add_argument(
        "--autoform-ref",
        help="exact immutable Autoform commit for missing workflows",
    )
    project_repair.add_argument("--dry-run", action="store_true", help="report without writing")
    project_repair.add_argument("--json", action="store_true", help="write stable machine-readable output")
    project_inspect = project_subparsers.add_parser(
        "inspect", help="inspect a project without running Lake, Git, or network operations"
    )
    project_inspect.add_argument(
        "target", nargs="?", default=".", help="a path inside the project (default: current directory)"
    )
    project_inspect.add_argument("--json", action="store_true", help="write stable machine-readable output")
    project_versions = project_subparsers.add_parser(
        "versions", help="list bundled known-good Lean and Mathlib releases"
    )
    project_versions.add_argument("--json", action="store_true", help="write stable machine-readable output")
    project_provenance = project_subparsers.add_parser(
        "provenance",
        help="verify immutable provenance for this Autoform installation",
    )
    project_provenance.add_argument(
        "--json", action="store_true", help="write stable machine-readable output"
    )

    claim = subparsers.add_parser("claim", help="coordinate temporary node ownership through Git refs")
    claim_subparsers = claim.add_subparsers(dest="claim_command", required=True)
    for operation in ("acquire", "renew", "release"):
        command = claim_subparsers.add_parser(operation)
        command.add_argument("node_id")
        _add_claim_board_arguments(command)
        if operation in {"acquire", "renew"}:
            command.add_argument("--ttl", type=int, default=CLAIM_TTL_S)
        if operation == "acquire":
            command.add_argument("--note", default="")
    claim_list = claim_subparsers.add_parser("list")
    _add_claim_board_arguments(claim_list)
    claim_cleanup = claim_subparsers.add_parser("cleanup")
    _add_claim_board_arguments(claim_cleanup)

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

    render = subparsers.add_parser

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
    if args.command == "project":
        return _project(args)
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
        help="stable identity for this agent (or set AUTOFORM_WORKER_ID)",
    )
    parser.add_argument("--scratch", type=Path, help="local bare Git object cache")


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
    print("Next: describe the project in blueprint/README.md, then add chapters "
          "as roadmap/<chapter>/README.md.")
    if result.unpinned:
        # Flush first: stdout is block-buffered when piped, so without this the
        # warning jumps ahead of the file list it is explaining.
        sys.stdout.flush()
        print(
            "\nCI was not written: generated workflows install Autoform from a Git\n"
            "ref, and this installation has no verified source and commit.\n"
            "Re-run with the complete pair to add them:\n"
            "  autoform init --autoform-source <git-url> "
            "--autoform-ref <40-char-sha>",
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


def _project(args: argparse.Namespace) -> int:
    try:
        if args.project_command == "new":
            result = create_project(
                args.target,
                package=args.package,
                release_id=args.release,
                autoform_source=args.autoform_source,
                autoform_ref=args.autoform_ref,
            )
            if args.json:
                print(result.to_json())
            else:
                print(f"Created {result.package} at {result.target} ({result.release})")
                if not result.workflows_pinned:
                    print("warning: workflows were omitted because no immutable Autoform pin was available")
            return 0
        if args.project_command == "repair":
            result = repair_project(
                args.target,
                dry_run=args.dry_run,
                title=args.title,
                repository_url=args.repository_url,
                autoform_source=args.autoform_source,
                autoform_ref=args.autoform_ref,
            )
            if args.json:
                print(result.to_json())
            else:
                action = "Would add" if result.dry_run else "Added"
                print(f"{action} {len(result.planned if result.dry_run else result.written)} file(s)")
                for path in result.planned if result.dry_run else result.written:
                    print(f"  {path}")
            return 0
        if args.project_command == "inspect":
            result = inspect_project(args.target)
            if args.json:
                print(result.to_json())
            else:
                _print_project_inspection(result)
            return 0 if result.ok else 1
        if args.project_command == "versions":
            catalog = load_release_catalog()
            if args.json:
                print(catalog.to_json())
            else:
                print("Supported Lean/Mathlib releases:")
                for release in catalog.releases:
                    suffix = " [recommended]" if release.recommended else ""
                    print(f"  {release.id}{suffix}")
                    print(f"    Lean: {release.lean.toolchain}")
                    print(f"    Mathlib: {release.mathlib.revision} ({release.mathlib.git})")
            return 0
        if args.project_command == "provenance":
            result = verify_plugin_provenance()
            if args.json:
                print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
            else:
                print(f"Source: {result.source}")
                print(f"Revision: {result.revision}")
            return 0
    except ProjectRepairError as error:
        if getattr(args, "json", False):
            print(error.to_json())
        else:
            print(f"error[{error.code}]: {error.message}", file=sys.stderr)
            for conflict in error.conflicts:
                location = f" {conflict.path}" if conflict.path else ""
                print(f"  {conflict.code}{location}: {conflict.message}", file=sys.stderr)
            if error.written:
                print("  files already published:", file=sys.stderr)
                for path in error.written:
                    print(f"    {path}", file=sys.stderr)
        return 1
    except ProjectCreateError as error:
        if getattr(args, "json", False):
            print(error.to_json())
        else:
            print(f"error[{error.code}]: {error.message}", file=sys.stderr)
        return 1
    except ProjectCatalogError:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "project-catalog-invalid",
                            "message": "The bundled project release catalog is invalid.",
                        },
                        "ok": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print("error: bundled project release catalog is invalid", file=sys.stderr)
        return 1
    except ProvenanceError as error:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "error": {"code": error.code, "message": error.message},
                        "ok": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print(f"error[{error.code}]: {error.message}", file=sys.stderr)
        return 1
    return 2


def _print_project_inspection(result) -> None:
    if result.project_root is not None:
        print(f"Project: {result.project_root}")
    if result.lake is not None:
        package = result.lake.name or "unknown package"
        version = f" {result.lake.version}" if result.lake.version else ""
        print(f"Lake: {package}{version} ({result.lake.path})")
        for target in result.lake.targets:
            source_parts = [
                part
                for part in (result.lake.package_src_dir, target.src_dir)
                if part is not None
            ]
            source = PurePosixPath(*source_parts).as_posix() if source_parts else "."
            modules = target.roots or ((target.root,) if target.root is not None else ())
            module_note = f", roots: {', '.join(modules)}" if modules else ""
            print(f"  {target.kind} {target.name} (srcDir: {source}{module_note})")
    if result.lean is not None:
        print(f"Lean: {result.lean.toolchain}")
    if result.mathlib is not None:
        print(f"Mathlib: {result.mathlib.revision or 'none'} ({result.mathlib.git or 'none'})")
    print(
        f"Compatibility: {result.compatibility.status}"
        + (f" ({result.compatibility.release})" if result.compatibility.release else "")
    )
    for diagnostic in result.diagnostics:
        location = f" {diagnostic.path}" if diagnostic.path else ""
        print(
            f"{diagnostic.severity}[{diagnostic.code}]{location}: {diagnostic.message}",
            file=sys.stderr,
        )


def _claim(args: argparse.Namespace) -> int:
    try:
        board = _claim_board(args)
        operation = args.claim_command
        if operation == "list":
            print(json.dumps(board.list(), sort_keys=True, separators=(",", ":")))
            return 0
        if operation == "cleanup":
            print(f"removed {board.cleanup()} expired claim(s)")
            return 0

        key = author_claim_key(args.node_id)
        if operation == "acquire":
            succeeded = board.acquire(key, ttl=args.ttl, note=args.note)
        elif operation == "renew":
            succeeded = board.renew(key, ttl=args.ttl)
        else:
            succeeded = board.release(key)
        if succeeded:
            past_tense = {"acquire": "acquired", "renew": "renewed", "release": "released"}
            print(f"{past_tense[operation]} {args.node_id} ({key})")
            return 0
        print(f"error: could not {operation} {args.node_id}; ownership is held or unverifiable")
        return 1
    except (ClaimTransportError, ValueError) as exc:
        print(f"error: {exc}")
        return 1


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


def _claim_board(args: argparse.Namespace) -> ClaimBoard:
    worker_id = args.worker_id
    if not worker_id:
        raise ValueError("--worker-id or AUTOFORM_WORKER_ID is required")
    repo = args.repo or _origin_url()
    scratch = args.scratch or _default_claim_scratch(repo, worker_id)
    return ClaimBoard(repo, worker_id, scratch)


def _origin_url() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("--repo is required outside a Git checkout with an origin remote") from exc
    return result.stdout.strip()


def _default_claim_scratch(repo: str, worker_id: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    identity = hashlib.sha256(f"{repo}\0{worker_id}\0{socket.gethostname()}".encode()).hexdigest()[:24]
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
