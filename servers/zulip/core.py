"""Zulip client — search discussions on a Zulip server.

Pure logic, no MCP or CLI dependencies. Handles .zuliprc discovery
and wraps the ``zulip`` Python package.

Dependency: ``zulip`` (provided by the ``zulip`` extra in pyproject.toml).
"""

from __future__ import annotations

import configparser
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _zulip_module():
    """Lazy-import the zulip package. Raises RuntimeError with install instructions if missing."""
    try:
        import zulip
    except ImportError:
        raise RuntimeError(
            "The 'zulip' package is required for Zulip search.\n"
            "It should be installed automatically via uv. Run: /setup-autoform"
        )
    return zulip


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
        zulip = _zulip_module()
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


def get_client(config_file: str | None = None) -> ZulipClient:
    """Create a ZulipClient from the discovered or specified config file."""
    if config_file:
        rc_path = Path(config_file).expanduser()
        if not rc_path.is_file():
            raise FileNotFoundError(f"Specified config file not found: {config_file}")
    else:
        rc_path = find_zuliprc()
        if rc_path is None:
            raise FileNotFoundError(
                "No .zuliprc file found. Searched:\n"
                "  1. $ZULIPRC env var\n"
                "  2. $LEAN_PROJECT_DIR/.zuliprc\n"
                "  3. ~/.zuliprc\n"
                "  4. ~/.config/.zuliprc\n"
                "  5. ~/.config/zulip/.zuliprc\n"
                "  6. ~/.config/zuliprc\n\n"
                "Create one — see skills/zulip/README.md for instructions."
            )
    return ZulipClient(config_file=str(rc_path))
