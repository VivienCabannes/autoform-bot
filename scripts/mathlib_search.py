#!/usr/bin/env python3
"""Bash-callable Mathlib search — the same search the MCP server exposes, but
usable by subagents.

Plugin MCP tools are available only to the main orchestrator; subagents spawned
via the Agent/Workflow tools do NOT inherit them, even when their definition
lists `mcpServers:`. So `mathlib-checker`, `graph-reviewer`, and `splitter` would
be blind to Mathlib if they relied on the MCP tools. This CLI gives them the
identical search over the same resolved checkout, callable with Bash.

It reuses servers/mathlib/core.py for both path resolution and search, so the
MCP path and the Bash path can never drift.

Mathlib location is resolved exactly as the server does (see core.find_mathlib_path):
  1. $LEAN_PLANNER_MATHLIB or $MATHLIB_PATH (a dir containing Mathlib/)
  2. the project dir itself, if it is a Mathlib checkout
  3. a local `require mathlib` path in the project's lakefile.toml
  4. <project>/.lake/packages/mathlib
The project dir is $LEAN_PROJECT_DIR, else $CLAUDE_PROJECT_DIR, else cwd.

Usage:
    mathlib_search.py path
    mathlib_search.py name  <NAME> [--exact] [--max N]
    mathlib_search.py grep  <PATTERN> [--subdir D] [--kind theorem] [--context N] [--max N] [--literal]
    mathlib_search.py read  <FILE> [--start L] [--end L]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servers"))
from mathlib.core import (  # noqa: E402
    find_mathlib_path,
    find_name_in_mathlib,
    grep_mathlib,
    read_mathlib_file,
)


def _repo_root() -> Path:
    for env in ("LEAN_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        v = os.environ.get(env)
        if v:
            p = Path(v).expanduser()
            if p.is_dir():
                return p.resolve()
    return Path.cwd()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Search a local Mathlib checkout (Bash-callable).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("path", help="print the resolved Mathlib checkout path")

    p_name = sub.add_parser("name", help="find a declaration by name")
    p_name.add_argument("name")
    p_name.add_argument("--exact", action="store_true")
    p_name.add_argument("--max", type=int, default=30)

    p_grep = sub.add_parser("grep", help="ripgrep the Mathlib source")
    p_grep.add_argument("pattern")
    p_grep.add_argument("--subdir", default="")
    p_grep.add_argument("--kind", default="")
    p_grep.add_argument("--context", type=int, default=0)
    p_grep.add_argument("--max", type=int, default=50)
    p_grep.add_argument("--literal", action="store_true")

    p_read = sub.add_parser("read", help="read a Mathlib file (path relative to the checkout root)")
    p_read.add_argument("file")
    p_read.add_argument("--start", type=int, default=None)
    p_read.add_argument("--end", type=int, default=None)

    args = ap.parse_args(argv)
    root = _repo_root()

    if args.cmd == "path":
        mp = find_mathlib_path(root)
        if not mp:
            print("Error: Mathlib not found. Set LEAN_PLANNER_MATHLIB to a checkout "
                  "(a dir containing Mathlib/), or place it at "
                  f"{root}/.lake/packages/mathlib.", file=sys.stderr)
            return 1
        print(mp)
        return 0
    if args.cmd == "name":
        print(find_name_in_mathlib(root, args.name, exact=args.exact, max_results=args.max))
    elif args.cmd == "grep":
        print(grep_mathlib(root, args.pattern, kind=args.kind, subdir=args.subdir,
                           max_results=args.max, context_lines=args.context, literal=args.literal))
    elif args.cmd == "read":
        print(read_mathlib_file(root, args.file, start_line=args.start, end_line=args.end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
