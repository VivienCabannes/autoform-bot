"""PDF MCP server — FastMCP tool definitions and config factory."""

from __future__ import annotations

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import PdfOps


def create_pdf_server(allowed_dirs: list[str]) -> FastMCP:
    """Create an MCP server with PDF reading tools."""
    ops = PdfOps(allowed_dirs)
    server = FastMCP(name="pdf")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def read_pdf(path: str, pages: str = "") -> str:
        """Read and extract text from a PDF file.

        Args:
            path: Path to the PDF file.
            pages: Page range to read — e.g. "5" (single page), "1-10" (range),
                   or "3-" (page 3 to end). 1-indexed. Required for PDFs with
                   more than 10 pages. Maximum 20 pages per call.
        """
        return ops.read_pdf(path, pages=pages or None)

    return server


def pdf_server(allowed_dirs: list[str]) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for PDF reading."""
    mcp_instance = create_pdf_server(allowed_dirs)
    return MCPServerConfig(
        server_key="pdf",
        description="PDF document reading and text extraction",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
