"""Web search tool — DuckDuckGo-based web search."""

from .core import WebSearcher
from .server import create_web_search_server, web_search_server

__all__ = ["WebSearcher", "create_web_search_server", "web_search_server"]
