"""Zulip search MCP server — find community discussions about Lean and Mathlib.

Adapted from the Zulip server on the additional-infra branch
(tools/communication/zulip/server.py), stripped of core.mcp/core.tool deps,
extended with config auto-discovery and a status tool.
"""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from .core import ZulipClient, find_zuliprc, get_client


def create_zulip_server() -> FastMCP:
    """Create a FastMCP server for searching Zulip discussions."""
    server = FastMCP(name="autoform-zulip")

    # Lazy client — created on first tool call so the server starts even without a .zuliprc
    _client: ZulipClient | None = None

    def _get_client() -> ZulipClient:
        nonlocal _client
        if _client is None:
            _client = get_client()
        return _client

    @server.tool
    def zulip_search(query: str, stream: str = "", topic: str = "", limit: int = 20) -> str:
        """Search Zulip messages for discussions relevant to a formalization task.

        Use this to find prior community discussions about naming conventions,
        proof strategies, API design decisions, or existing work on a topic.

        Args:
            query: Search terms (e.g., "Hoeffding inequality", "Finset.sum naming").
            stream: Restrict to a specific stream (e.g., "mathlib4", "new members").
            topic: Restrict to a specific topic within a stream.
            limit: Maximum number of messages to return (default: 20).
        """
        result = _get_client().search_messages(query, stream=stream, topic=topic, limit=limit)
        return json.dumps(result, indent=2)

    @server.tool
    def zulip_messages(stream: str, topic: str = "", limit: int = 30) -> str:
        """Fetch recent messages from a Zulip stream, optionally filtered by topic.

        Use this after zulip_search finds a relevant topic, to read the
        complete discussion in context.

        Args:
            stream: Stream name (e.g., "mathlib4").
            topic: Optional topic name to narrow results (e.g., "Hoeffding's inequality").
            limit: Maximum number of messages to return (default: 30).
        """
        result = _get_client().get_messages(stream, topic, limit=limit)
        return json.dumps(result, indent=2)

    @server.tool
    def zulip_streams(filter_text: str = "") -> str:
        """List available Zulip streams.

        Use this to discover which streams exist (e.g., "mathlib4",
        "Is there code for X?", "new members").

        Args:
            filter_text: Optional case-insensitive filter on stream names.
        """
        result = _get_client().list_streams(filter_text=filter_text)
        return json.dumps(result, indent=2)

    @server.tool
    def zulip_topics(stream: str, limit: int = 20) -> str:
        """List recent topics in a Zulip stream.

        Use this to browse what's being discussed in a stream, or to find
        the exact topic name before reading a thread with zulip_messages.

        Args:
            stream: Stream name (e.g., "mathlib4").
            limit: Maximum number of topics to return (default: 20).
        """
        result = _get_client().get_topics(stream, limit=limit)
        return json.dumps(result, indent=2)

    @server.tool
    def zulip_status() -> str:
        """Check Zulip configuration status.

        Reports which .zuliprc file was found (or not) and which site
        it points to. Does not reveal the API key.
        """
        rc_path = find_zuliprc()
        if rc_path is None:
            return json.dumps({
                "configured": False,
                "error": (
                    "No .zuliprc found. Searched: $ZULIPRC, $LEAN_PROJECT_DIR/.zuliprc, "
                    "~/.zuliprc, ~/.config/zulip/.zuliprc, ~/.config/zuliprc"
                ),
            })

        import configparser
        config = configparser.ConfigParser()
        config.read(str(rc_path))
        site = config.get("api", "site", fallback="unknown")
        email = config.get("api", "email", fallback="unknown")

        return json.dumps({
            "configured": True,
            "config_file": str(rc_path),
            "site": site,
            "email": email,
        })

    return server


if __name__ == "__main__":
    server = create_zulip_server()
    server.run(transport="stdio")
