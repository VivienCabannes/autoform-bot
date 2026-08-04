"""Command-line entry point for Autoform's small project utilities."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .graph import GraphValidationError, load_graph


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m autoform")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate a Markdown blueprint")
    check.add_argument("blueprint_dir")
    args = parser.parse_args(argv)

    if args.command == "check":
        try:
            graph = load_graph(args.blueprint_dir)
        except GraphValidationError as exc:
            for issue in exc.issues:
                print(f"error: {issue}")
            return 1
        print(f"OK: {len(graph.nodes)} nodes, {graph.edge_count} dependencies")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
