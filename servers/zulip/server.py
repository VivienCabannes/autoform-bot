"""Zulip search MCP server — find community discussions about Lean and Mathlib.

Thin FastMCP wrapper around skills/zulip/zulip-search.py.
The skill script is the single source of truth for ZulipClient and config discovery.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from fastmcp.server import FastMCP


def _load_skill():
    """Import the zulip-search.py skill script."""
    script = Path(__file__).resolve().parent.parent.parent / "skills" / "zulip" / "zulip-search.py"
    spec = importlib.util.spec_from_file_location("zulip_search", str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_skill = _load_skill()


def create_zulip_server() -> FastMCP:
    """Create a FastMCP server for searching Zulip discussions."""
    server = FastMCP(name="autoform-zulip")

    _client = None

    def _get_client():
        nonlocal _client
        if _client is None:
            rc = _skill.find_zuliprc()
            if rc is None:
                raise FileNotFoundError(
                    "No .zuliprc found. Run zulip_status for search locations."
                )
            _client = _skill.ZulipClient(config_file=str(rc))
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
        return json.dumps(_get_client().search_messages(query, stream=stream, topic=topic, limit=limit), indent=2)

    @server.tool
    def zulip_messages(stream: str, topic: str = "", limit: int = 30) -> str:
        """Fetch recent messages from a Zulip stream, optionally filtered by topic.

        Use after zulip_search finds a relevant topic, to read the full thread.

        Args:
            stream: Stream name (e.g., "mathlib4").
            topic: Optional topic name (e.g., "Hoeffding's inequality").
            limit: Maximum number of messages to return (default: 30).
        """
        return json.dumps(_get_client().get_messages(stream, topic, limit=limit), indent=2)

    @server.tool
    def zulip_streams(filter_text: str = "") -> str:
        """List available Zulip streams.

        Args:
            filter_text: Optional case-insensitive filter on stream names.
        """
        return json.dumps(_get_client().list_streams(filter_text=filter_text), indent=2)

    @server.tool
    def zulip_topics(stream: str, limit: int = 20) -> str:
        """List recent topics in a Zulip stream.

        Args:
            stream: Stream name (e.g., "mathlib4").
            limit: Maximum number of topics to return (default: 20).
        """
        return json.dumps(_get_client().get_topics(stream, limit=limit), indent=2)

    @server.tool
    def zulip_status() -> str:
        """Check Zulip configuration status.

        Reports which .zuliprc file was found and which site it points to.
        Does not reveal the API key.
        """
        rc = _skill.find_zuliprc()
        if rc is None:
            return json.dumps({
                "configured": False,
                "error": (
                    "No .zuliprc found. Searched: $ZULIPRC, $LEAN_PROJECT_DIR/.zuliprc, "
                    "~/.zuliprc, ~/.config/.zuliprc, ~/.config/zulip/.zuliprc, ~/.config/zuliprc"
                ),
            })
        import configparser
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
