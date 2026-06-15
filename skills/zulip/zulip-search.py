#!/usr/bin/env python3
"""Zulip CLI — search Lean/Mathlib community discussions.

Thin CLI wrapper around servers/zulip/core.py.

Usage:
    python3 zulip-search.py status
    python3 zulip-search.py search "concentration inequality"
    python3 zulip-search.py search "Hoeffding" --stream mathlib4 --limit 5
    python3 zulip-search.py streams
    python3 zulip-search.py streams --filter math
    python3 zulip-search.py topics "mathlib4"
    python3 zulip-search.py messages "Autoformalization" "Trellis"

Requires: pip install zulip
Config:   .zuliprc (see --help for search locations)
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

# Add the repo root to sys.path so we can import servers.zulip.core
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root))

from servers.zulip.core import ZulipClient, find_zuliprc, get_client  # noqa: E402


def _get_client_from_args(args) -> ZulipClient:
    config_file = getattr(args, "config", None)
    if config_file:
        rc_path = Path(config_file).expanduser()
        if not rc_path.is_file():
            print(f"Error: config file not found: {config_file}", file=sys.stderr)
            sys.exit(1)
        return ZulipClient(config_file=str(rc_path))
    try:
        return get_client()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    rc_path = find_zuliprc()
    if rc_path is None:
        print(json.dumps({"configured": False, "error": "No .zuliprc found"}, indent=2))
        return
    config = configparser.ConfigParser()
    config.read(str(rc_path))
    print(json.dumps({
        "configured": True,
        "config_file": str(rc_path),
        "site": config.get("api", "site", fallback="unknown"),
        "email": config.get("api", "email", fallback="unknown"),
    }, indent=2))


def cmd_search(args):
    client = _get_client_from_args(args)
    result = client.search_messages(args.query, stream=args.stream, topic=args.topic, limit=args.limit)
    print(json.dumps(result, indent=2))


def cmd_streams(args):
    client = _get_client_from_args(args)
    result = client.list_streams(filter_text=args.filter)
    print(json.dumps(result, indent=2))


def cmd_topics(args):
    client = _get_client_from_args(args)
    result = client.get_topics(args.stream, limit=args.limit)
    print(json.dumps(result, indent=2))


def cmd_messages(args):
    client = _get_client_from_args(args)
    result = client.get_messages(args.stream, args.topic, limit=args.limit)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Search Lean/Mathlib Zulip discussions")
    parser.add_argument("--config", help="Path to .zuliprc file (overrides auto-discovery)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Check .zuliprc configuration")

    p_search = sub.add_parser("search", help="Search messages by keyword")
    p_search.add_argument("query", help="Search terms")
    p_search.add_argument("--stream", default="", help="Restrict to a stream")
    p_search.add_argument("--topic", default="", help="Restrict to a topic")
    p_search.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")

    p_streams = sub.add_parser("streams", help="List available streams")
    p_streams.add_argument("--filter", default="", help="Filter stream names")

    p_topics = sub.add_parser("topics", help="List topics in a stream")
    p_topics.add_argument("stream", help="Stream name")
    p_topics.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")

    p_messages = sub.add_parser("messages", help="Fetch messages from a stream/topic")
    p_messages.add_argument("stream", help="Stream name")
    p_messages.add_argument("topic", nargs="?", default="", help="Topic name (optional)")
    p_messages.add_argument("--limit", type=int, default=30, help="Max results (default: 30)")

    args = parser.parse_args()
    {"status": cmd_status, "search": cmd_search, "streams": cmd_streams,
     "topics": cmd_topics, "messages": cmd_messages}[args.command](args)


if __name__ == "__main__":
    main()
