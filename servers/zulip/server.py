"""Zulip search MCP server — find community discussions about Lean and Mathlib."""

from __future__ import annotations

import configparser
import json

from fastmcp.server import FastMCP

from .core import ZulipClient, find_zuliprc, get_client


def create_zulip_server() -> FastMCP:
    """Create a FastMCP server for searching Zulip discussions."""
    server = FastMCP(name="autoform-zulip")

    _client: ZulipClient | None = None

    def _get_client() -> ZulipClient:
        nonlocal _client
        if _client is None:
            _client = get_client()
        return _client

    def _not_configured_response() -> str:
        """Return a helpful error message when Zulip is not set up."""
        return json.dumps({
            "error": "Zulip is not configured.",
            "setup": (
                "Run /setup-zulip to configure Zulip access, or manually:\n"
                "1. Get your API key at https://leanprover.zulipchat.com/#settings/account\n"
                "2. Create ~/.zuliprc with [api] email, key, and site fields\n"
                "3. Ensure the 'zulip' Python package is installed (handled by uv)"
            ),
        })

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
        try:
            return json.dumps(_get_client().search_messages(query, stream=stream, topic=topic, limit=limit), indent=2)
        except (FileNotFoundError, RuntimeError):
            return _not_configured_response()

    @server.tool
    def zulip_messages(stream: str, topic: str = "", limit: int = 30) -> str:
        """Fetch recent messages from a Zulip stream, optionally filtered by topic.

        Use after zulip_search finds a relevant topic, to read the full thread.

        Args:
            stream: Stream name (e.g., "mathlib4").
            topic: Optional topic name (e.g., "Hoeffding's inequality").
            limit: Maximum number of messages to return (default: 30).
        """
        try:
            return json.dumps(_get_client().get_messages(stream, topic, limit=limit), indent=2)
        except (FileNotFoundError, RuntimeError):
            return _not_configured_response()

    @server.tool
    def zulip_streams(filter_text: str = "") -> str:
        """List available Zulip streams.

        Args:
            filter_text: Optional case-insensitive filter on stream names.
        """
        try:
            return json.dumps(_get_client().list_streams(filter_text=filter_text), indent=2)
        except (FileNotFoundError, RuntimeError):
            return _not_configured_response()

    @server.tool
    def zulip_topics(stream: str, limit: int = 20) -> str:
        """List recent topics in a Zulip stream.

        Args:
            stream: Stream name (e.g., "mathlib4").
            limit: Maximum number of topics to return (default: 20).
        """
        try:
            return json.dumps(_get_client().get_topics(stream, limit=limit), indent=2)
        except (FileNotFoundError, RuntimeError):
            return _not_configured_response()

    @server.tool
    def zulip_status() -> str:
        """Check Zulip configuration status.

        Reports which .zuliprc file was found and which site it points to.
        Does not reveal the API key.
        """
        rc = find_zuliprc()
        if rc is None:
            return json.dumps({
                "configured": False,
                "error": (
                    "No .zuliprc found. Searched: $ZULIPRC, $LEAN_PROJECT_DIR/.zuliprc, "
                    "~/.zuliprc, ~/.config/.zuliprc, ~/.config/zulip/.zuliprc, ~/.config/zuliprc"
                ),
            })
        config = configparser.ConfigParser()
        config.read(str(rc))
        return json.dumps({
            "configured": True,
            "config_file": str(rc),
            "site": config.get("api", "site", fallback="unknown"),
            "email": config.get("api", "email", fallback="unknown"),
        })

    return server


if __name__ == "__main__":
    server = create_zulip_server()
    server.run(transport="stdio")
