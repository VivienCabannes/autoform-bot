"""Zulip client — search discussions on a Zulip server.

No MCP dependencies. Adapted from the ZulipClient on the additional-infra branch
(tools/communication/zulip/core.py), extended with multi-location config discovery.

Requires: pip install zulip
"""

from __future__ import annotations

import configparser
import os
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
            "Install it with: pip install 'autoform[zulip]' or pip install zulip"
        )
    return zulip


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

    # 3-5. Home directory locations
    home = Path.home()
    for rel in (".zuliprc", ".config/.zuliprc", ".config/zulip/.zuliprc", ".config/zuliprc"):
        candidate = home / rel
        if candidate.is_file():
            return candidate

    return None


def parse_zuliprc(path: Path) -> dict[str, str]:
    """Parse a .zuliprc file and return the [api] section as a dict."""
    config = configparser.ConfigParser()
    config.read(str(path))
    if "api" not in config:
        raise ValueError(f"No [api] section found in {path}")
    return dict(config["api"])


class ZulipClient:
    """Synchronous client for interacting with a Zulip server.

    Wraps the ``zulip`` Python package with a name→ID stream cache
    and formatted output methods.
    """

    def __init__(self, config_file: str) -> None:
        zulip = _zulip_module()
        self._client = zulip.Client(config_file=config_file)
        self._name_to_id: dict[str, int] = {}

    def _resolve_streams(self) -> None:
        """Fetch all streams and build the name→ID mapping."""
        result = self._client.get_streams()
        if result["result"] != "success":
            raise RuntimeError(f"Failed to list streams: {result.get('msg', '')}")
        self._name_to_id = {s["name"]: s["stream_id"] for s in result["streams"]}

    def _get_stream_id(self, stream: str) -> int:
        if not self._name_to_id:
            self._resolve_streams()
        # Case-insensitive lookup
        for name, sid in self._name_to_id.items():
            if name.lower() == stream.lower():
                return sid
        raise ValueError(f"Stream '{stream}' not found.")

    def list_streams(self, *, filter_text: str = "") -> dict[str, Any]:
        """List streams with descriptions, optionally filtered by name."""
        result = self._client.get_streams()
        if result["result"] != "success":
            return {"streams": [], "error": result.get("msg", "Unknown API error")}

        # Update cache
        for s in result.get("streams", []):
            self._name_to_id[s["name"]] = s["stream_id"]

        streams = []
        for s in result.get("streams", []):
            name = s.get("name", "")
            if filter_text and filter_text.lower() not in name.lower():
                continue
            streams.append({
                "name": name,
                "description": s.get("description", "").strip(),
                "stream_id": s.get("stream_id"),
            })

        streams.sort(key=lambda s: s["name"].lower())
        return {"count": len(streams), "streams": streams}

    def get_topics(self, stream: str, *, limit: int = 20) -> dict[str, Any]:
        """List topics in a stream."""
        try:
            stream_id = self._get_stream_id(stream)
        except ValueError as e:
            return {"topics": [], "error": str(e)}

        result = self._client.get_stream_topics(stream_id)
        if result["result"] != "success":
            return {"topics": [], "error": result.get("msg", "Unknown API error")}

        topics = [
            {"name": t.get("name", ""), "max_id": t.get("max_id")}
            for t in result.get("topics", [])[:limit]
        ]
        return {"stream": stream, "count": len(topics), "topics": topics}

    def get_messages(self, stream: str, topic: str = "", *, limit: int = 20) -> dict[str, Any]:
        """Fetch recent messages from a stream, optionally filtered by topic."""
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

        messages = self._extract_messages(result.get("messages", []), show_context=bool(not topic))
        return {"stream": stream, "topic": topic, "count": len(messages), "messages": messages}

    def search_messages(self, query: str, *, stream: str = "", topic: str = "", limit: int = 20) -> dict[str, Any]:
        """Search messages by keyword, optionally scoped to a stream/topic."""
        if not query.strip():
            return {"messages": [], "error": "Query must be non-empty."}

        # Build narrow with search + optional stream/topic filters
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

        messages = self._extract_messages(result.get("messages", []), show_context=True)
        return {"query": query, "stream": stream, "topic": topic, "count": len(messages), "messages": messages}

    def _extract_messages(self, raw_messages: list[dict], *, show_context: bool = False) -> list[dict[str, Any]]:
        """Extract relevant fields from raw Zulip message dicts."""
        messages = []
        for m in raw_messages:
            entry: dict[str, Any] = {
                "id": m.get("id"),
                "sender": m.get("sender_full_name", ""),
                "timestamp": m.get("timestamp"),
                "date": datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                if m.get("timestamp")
                else None,
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
                "Create one with:\n"
                "  [api]\n"
                "  email=your-email@example.com\n"
                "  key=your-api-key\n"
                "  site=https://leanprover.zulipchat.com"
            )
    return ZulipClient(config_file=str(rc_path))
