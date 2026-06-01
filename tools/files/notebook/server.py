"""Notebook MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import NotebookOps


def create_notebook_server(allowed_dirs: list[str]) -> FastMCP:
    """Create an MCP server with Jupyter notebook tools."""
    ops = NotebookOps(allowed_dirs)
    server = FastMCP(name="notebook")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def read_notebook(path: str) -> str:
        """Read a Jupyter notebook and display all cells with outputs."""
        return ops.read_notebook(path)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.WRITE)
    def edit_notebook_cell(
        path: str,
        cell_number: int,
        new_source: str,
        cell_type: str = "",
        edit_mode: str = "replace",
    ) -> str:
        """Edit a cell in a Jupyter notebook.

        Args:
            path: Path to the .ipynb file.
            cell_number: 0-indexed cell number.
            new_source: New source content for the cell.
            cell_type: Cell type ("code" or "markdown"). Required for insert.
            edit_mode: "replace", "insert", or "delete".
        """
        return ops.edit_notebook_cell(path, cell_number, new_source, cell_type=cell_type, edit_mode=edit_mode)

    return server


def notebook_server(allowed_dirs: list[str]) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for notebook editing."""
    mcp_instance = create_notebook_server(allowed_dirs)
    return MCPServerConfig(
        server_key="notebook",
        description="Jupyter notebook editing and execution",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
