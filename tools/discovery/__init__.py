"""Tool discovery — on-demand tool documentation via list_tools/check_tools."""

from .server import discovery_server, DiscoveryConfig

__all__ = ["discovery_server", "DiscoveryConfig"]
