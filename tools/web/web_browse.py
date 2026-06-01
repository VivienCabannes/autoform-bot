"""Web browsing MCP servers — bundles web_fetch and web_search."""

from __future__ import annotations

from core.mcp import MCPServerConfig, TransportMethod
from tools.web.web_fetch.server import create_web_fetch_server
from tools.web.web_search.server import create_web_search_server


def web_browse_server() -> list[MCPServerConfig]:
    """Return MCP server configs for web fetching and searching."""
    return [
        MCPServerConfig(
            server_key="web-fetch",
            description="Fetch a URL and return its content as markdown",
            transport=TransportMethod.INPROCESS,
            mcp_instance=create_web_fetch_server(),
        ),
        MCPServerConfig(
            server_key="web-search",
            description="Search the web and return results",
            transport=TransportMethod.INPROCESS,
            mcp_instance=create_web_search_server(),
        ),
    ]
