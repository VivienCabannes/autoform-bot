"""Scratchpad MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import ScratchpadOps


@dataclass(frozen=True)
class ScratchpadConfig:
    """Configuration for the scratchpad tool."""

    dir: str | ScratchpadOps = field(default_factory=lambda: tempfile.mkdtemp(prefix="fort-scratchpad-"))


def create_scratchpad_server(scratchpad_dir: str | ScratchpadOps) -> FastMCP:
    """Create a FastMCP server with scratchpad tools scoped to *scratchpad_dir*.

    Args:
        scratchpad_dir: Either a directory path string or a pre-created
            ``ScratchpadOps`` instance.  Passing an existing instance lets
            callers retain a reference to later update ``ops.scratchpad_dir``
            (e.g. when restoring a saved session).
    """
    ops = scratchpad_dir if isinstance(scratchpad_dir, ScratchpadOps) else ScratchpadOps(scratchpad_dir)
    server = FastMCP(name="scratchpad")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def scratchpad_read(path: str) -> str:
        """Read a file from the scratchpad."""
        return ops.read(path)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def scratchpad_write(path: str, content: str) -> str:
        """Write or overwrite a file in the scratchpad (creates parent dirs)."""
        return ops.write(path, content)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    def scratchpad_list() -> str:
        """List all files in the scratchpad recursively."""
        return ops.list_files()

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def scratchpad_delete(path: str) -> str:
        """Delete a file from the scratchpad."""
        return ops.delete(path)

    return server


def scratchpad_server(config: ScratchpadConfig) -> MCPServerConfig:
    """Create a scratchpad MCP server config."""
    return MCPServerConfig(
        server_key="scratchpad",
        description="Persistent scratch space for notes and intermediate results",
        transport=TransportMethod.INPROCESS,
        mcp_instance=create_scratchpad_server(config.dir),
    )
