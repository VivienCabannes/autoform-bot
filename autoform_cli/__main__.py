"""Command-line entry point for Autoform's project utilities."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import status
from .engine_graph import ProjectionError, write_engine_graph
from .graph import GraphValidationError, load_graph
from .lean import build_linker, declaration_names
from .render import PublicationError, render_site


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoform-blueprint")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate a Markdown blueprint")
    check.add_argument("blueprint_dir")
    check.add_argument(
        "--lean-root",
        type=Path,
        help="Lean project to resolve 'lean:' declarations against (enables declaration checking)",
    )

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

    engine_graph = subparsers.add_parser(
        "engine-graph",
        help="derive the compatibility graph consumed by workers and the local dashboard",
    )
    engine_graph.add_argument("blueprint_dir")
    engine_graph.add_argument("-o", "--output", default="graph.json")
    engine_graph.add_argument("--project-root", type=Path)
    engine_graph.add_argument("--lean-root", type=Path)
    engine_graph.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the existing projection is stale",
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return _check(args)
    if args.command == "render":
        return _render(args)
    if args.command == "engine-graph":
        return _engine_graph(args)
    return 2


def _check(args: argparse.Namespace) -> int:
    try:
        graph = load_graph(args.blueprint_dir)
    except GraphValidationError as exc:
        for issue in exc.issues:
            print(f"error: {issue}")
        return 1

    statuses = status.derive(graph)
    summary = " · ".join(f"{count} {state.label}" for state, count in status.summarize(statuses))
    print(f"OK: {len(graph.nodes)} nodes, {graph.edge_count} dependencies")
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


def _engine_graph(args: argparse.Namespace) -> int:
    try:
        current = write_engine_graph(
            args.blueprint_dir,
            args.output,
            project_root=args.project_root,
            lean_root=args.lean_root,
            check=args.check,
        )
    except (GraphValidationError, ProjectionError, OSError) as exc:
        issues = getattr(exc, "issues", (str(exc),))
        for issue in issues:
            print(f"error: {issue}")
        return 1
    if args.check and not current:
        print(f"error: stale generated engine graph: {args.output}")
        return 1
    print(f"{'current' if args.check else 'wrote'}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
