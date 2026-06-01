"""Zulip client — operations against a Zulip server.

No MCP dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import zulip


class ZulipClient:
    """Synchronous client for interacting with a Zulip server."""

    def __init__(self, config_file: str) -> None:
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
        sid = self._name_to_id.get(stream)
        if sid is None:
            raise ValueError(f"Stream '{stream}' not found.")
        return sid

    def list_streams(self, *, subscribed_only: bool = False) -> str:
        """List streams with descriptions.

        When subscribed_only is True, returns only streams the bot user
        is subscribed to. Otherwise returns all public streams.
        """
        if subscribed_only:
            result = self._client.get_subscriptions()
            key = "subscriptions"
        else:
            result = self._client.get_streams()
            key = "streams"

        if result["result"] != "success":
            raise RuntimeError(f"Failed to list streams: {result.get('msg', '')}")

        streams = result.get(key, [])
        # Keep name→ID cache up to date for all public streams
        if not subscribed_only:
            for s in streams:
                self._name_to_id[s["name"]] = s["stream_id"]

        if not streams:
            return "No streams found."

        lines = []
        for s in sorted(streams, key=lambda s: s["name"]):
            desc = s.get("description", "").strip()
            line = s["name"]
            if desc:
                line += f" — {desc}"
            lines.append(line)
        label = "Subscribed streams" if subscribed_only else "Streams"
        return f"{label}:\n" + "\n".join(f"  - {entry}" for entry in lines)

    def get_topics(self, stream: str) -> str:
        """List topics in a stream."""
        stream_id = self._get_stream_id(stream)
        result = self._client.get_stream_topics(stream_id)
        if result["result"] != "success":
            return f"Error: {result.get('msg', 'unknown error')}"
        topics = result.get("topics", [])
        if not topics:
            return f"No topics found in '{stream}'."
        lines = [t["name"] for t in topics]
        return f"Topics in '{stream}':\n" + "\n".join(f"  - {entry}" for entry in lines)

    def get_messages(self, stream: str, topic: str = "", limit: int = 20) -> str:
        """Fetch recent messages from a stream, optionally filtered by topic."""
        narrow = [{"operator": "stream", "operand": stream}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})
        request = {
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": narrow,
            "apply_markdown": False,
        }
        result = self._client.get_messages(request)
        if result["result"] != "success":
            return f"Error: {result.get('msg', 'unknown error')}"
        return self._format_messages(result.get("messages", []))

    def search_messages(self, query: str, limit: int = 20) -> str:
        """Search messages by keyword across all streams."""
        request = {
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": [{"operator": "search", "operand": query}],
            "apply_markdown": False,
        }
        result = self._client.get_messages(request)
        if result["result"] != "success":
            return f"Error: {result.get('msg', 'unknown error')}"
        return self._format_messages(result.get("messages", []), show_context=True)

    def get_direct_messages(self, limit: int = 20) -> str:
        """Fetch recent direct messages (private messages)."""
        request = {
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": [{"operator": "is", "operand": "dm"}],
            "apply_markdown": False,
        }
        result = self._client.get_messages(request)
        if result["result"] != "success":
            return f"Error: {result.get('msg', 'unknown error')}"
        return self._format_messages(result.get("messages", []), show_context=True)

    # def send_message(self, stream: str, topic: str, content: str) -> str:
    #     """Send a message to a stream and topic."""
    #     request = {
    #         "type": "stream",
    #         "to": stream,
    #         "topic": topic,
    #         "content": content,
    #     }
    #     result = self._client.send_message(request)
    #     if result["result"] != "success":
    #         return f"Error: {result.get('msg', 'unknown error')}"
    #     return f"Message sent to '{stream} > {topic}'."

    def _format_messages(self, messages: list[dict], *, show_context: bool = False) -> str:
        if not messages:
            return "No messages found."
        lines = []
        for m in messages:
            ts = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            sender = m.get("sender_full_name", "unknown")
            content = m.get("content", "")
            prefix = ""
            if show_context:
                prefix = f"[{m.get('display_recipient', '')} > {m.get('subject', '')}] "
            lines.append(f"[{ts}] {prefix}({sender}): {content}")
        return "\n".join(lines)
