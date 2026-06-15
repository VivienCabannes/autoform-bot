#!/usr/bin/env python3
"""Zulip CLI — search Lean/Mathlib community discussions.

Usage:
    python3 zulip.py status
    python3 zulip.py search "concentration inequality"
    python3 zulip.py search "Hoeffding" --stream mathlib4 --limit 5
    python3 zulip.py streams
    python3 zulip.py streams --filter math
    python3 zulip.py topics "mathlib4"
    python3 zulip.py messages "Autoformalization" "Trellis"
    python3 zulip.py messages "mathlib4" --limit 10

Requires: pip install zulip
Config:   .zuliprc (see --help for search locations)
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


def find_zuliprc(project_dir: str | None = None) -> Path | None:
    """Locate a .zuliprc file by checking standard paths in priority order.

    Search order:
    1. $ZULIPRC env var (explicit override)
    2. <project_dir>/.zuliprc (project-specific)
    3. ~/.zuliprc (standard Zulip client location)
    4. ~/.config/.zuliprc
    5. ~/.config/zulip/.zuliprc
    6. ~/.config/zuliprc
    """
    # 1. Explicit env var
    env_path = os.environ.get("ZULIPRC")
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return candidate

    # 2. Project-local
    if project_dir:
        candidate = Path(project_dir).expanduser() / ".zuliprc"
        if candidate.is_file():
            return candidate

    lean_dir = os.environ.get("LEAN_PROJECT_DIR")
    if lean_dir and "$" not in lean_dir:
        candidate = Path(lean_dir).expanduser() / ".zuliprc"
        if candidate.is_file():
            return candidate

    # 3-6. Home directory locations
    home = Path.home()
    for rel in (".zuliprc", ".config/.zuliprc", ".config/zulip/.zuliprc", ".config/zuliprc"):
        candidate = home / rel
        if candidate.is_file():
            return candidate

    return None


# ---------------------------------------------------------------------------
# Zulip client
# ---------------------------------------------------------------------------


class ZulipClient:
    """Synchronous client for searching a Zulip server."""

    def __init__(self, config_file: str) -> None:
        try:
            import zulip
        except ImportError:
            print(
                "Error: the 'zulip' package is required.\n"
                "Install it with: pip install zulip",
                file=sys.stderr,
            )
            sys.exit(1)
        self._client = zulip.Client(config_file=config_file)
        self._name_to_id: dict[str, int] = {}

    def _resolve_streams(self) -> None:
        result = self._client.get_streams()
        if result["result"] != "success":
            raise RuntimeError(f"Failed to list streams: {result.get('msg', '')}")
        self._name_to_id = {s["name"]: s["stream_id"] for s in result["streams"]}

    def _get_stream_id(self, stream: str) -> int:
        if not self._name_to_id:
            self._resolve_streams()
        for name, sid in self._name_to_id.items():
            if name.lower() == stream.lower():
                return sid
        raise ValueError(f"Stream '{stream}' not found.")

    def list_streams(self, *, filter_text: str = "") -> dict[str, Any]:
        result = self._client.get_streams()
        if result["result"] != "success":
            return {"streams": [], "error": result.get("msg", "Unknown API error")}
        for s in result.get("streams", []):
            self._name_to_id[s["name"]] = s["stream_id"]
        streams = []
        for s in result.get("streams", []):
            name = s.get("name", "")
            if filter_text and filter_text.lower() not in name.lower():
                continue
            streams.append({"name": name, "description": s.get("description", "").strip()})
        streams.sort(key=lambda s: s["name"].lower())
        return {"count": len(streams), "streams": streams}

    def get_topics(self, stream: str, *, limit: int = 20) -> dict[str, Any]:
        try:
            stream_id = self._get_stream_id(stream)
        except ValueError as e:
            return {"topics": [], "error": str(e)}
        result = self._client.get_stream_topics(stream_id)
        if result["result"] != "success":
            return {"topics": [], "error": result.get("msg", "Unknown API error")}
        topics = [{"name": t.get("name", "")} for t in result.get("topics", [])[:limit]]
        return {"stream": stream, "count": len(topics), "topics": topics}

    def get_messages(self, stream: str, topic: str = "", *, limit: int = 20) -> dict[str, Any]:
        narrow = [{"operator": "stream", "operand": stream}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})
        result = self._client.get_messages({
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": narrow,
            "apply_markdown": False,
        })
        if result["result"] != "success":
            return {"messages": [], "error": result.get("msg", "Unknown API error")}
        messages = self._format_messages(result.get("messages", []), show_context=bool(not topic))
        return {"stream": stream, "topic": topic, "count": len(messages), "messages": messages}

    def search_messages(self, query: str, *, stream: str = "", topic: str = "", limit: int = 20) -> dict[str, Any]:
        if not query.strip():
            return {"messages": [], "error": "Query must be non-empty."}
        narrow: list[dict[str, str]] = [{"operator": "search", "operand": query}]
        if stream:
            narrow.append({"operator": "stream", "operand": stream})
        if topic:
            narrow.append({"operator": "topic", "operand": topic})
        result = self._client.get_messages({
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": narrow,
            "apply_markdown": False,
        })
        if result["result"] != "success":
            return {"messages": [], "error": result.get("msg", "Unknown API error")}
        messages = self._format_messages(result.get("messages", []), show_context=True)
        return {"query": query, "count": len(messages), "messages": messages}

    def _format_messages(self, raw: list[dict], *, show_context: bool = False) -> list[dict[str, Any]]:
        messages = []
        for m in raw:
            entry: dict[str, Any] = {
                "sender": m.get("sender_full_name", ""),
                "date": datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                if m.get("timestamp") else None,
                "content": m.get("content", ""),
            }
            if show_context:
                entry["stream"] = m.get("display_recipient", "")
                entry["topic"] = m.get("subject", "")
            messages.append(entry)
        return messages


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _get_client(args) -> ZulipClient:
    config_file = getattr(args, "config", None)
    if config_file:
        rc_path = Path(config_file).expanduser()
        if not rc_path.is_file():
            print(f"Error: config file not found: {config_file}", file=sys.stderr)
            sys.exit(1)
    else:
        rc_path = find_zuliprc()
        if rc_path is None:
            print(
                "Error: no .zuliprc file found. Searched:\n"
                "  1. $ZULIPRC env var\n"
                "  2. $LEAN_PROJECT_DIR/.zuliprc\n"
                "  3. ~/.zuliprc\n"
                "  4. ~/.config/.zuliprc\n"
                "  5. ~/.config/zulip/.zuliprc\n"
                "  6. ~/.config/zuliprc\n\n"
                "Create one — see skills/zulip/README.md for instructions.",
                file=sys.stderr,
            )
            sys.exit(1)
    return ZulipClient(config_file=str(rc_path))


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
    client = _get_client(args)
    result = client.search_messages(args.query, stream=args.stream, topic=args.topic, limit=args.limit)
    print(json.dumps(result, indent=2))


def cmd_streams(args):
    client = _get_client(args)
    result = client.list_streams(filter_text=args.filter)
    print(json.dumps(result, indent=2))


def cmd_topics(args):
    client = _get_client(args)
    result = client.get_topics(args.stream, limit=args.limit)
    print(json.dumps(result, indent=2))


def cmd_messages(args):
    client = _get_client(args)
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
