"""LaTeX MCP server — FastMCP tool definition and config factory."""

from __future__ import annotations

import json

from fastmcp.server import FastMCP

from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec

from .core import LatexConfig, LatexExecutor


def create_latex_server(executor: LatexExecutor) -> FastMCP:
    """Create a FastMCP server wrapping a LatexExecutor instance.

    Exposes three tools:
    - compile_latex: Compile LaTeX source to PDF
    - compile_latex_file: Compile an existing .tex file to PDF
    - check_latex_engine: Check LaTeX engine availability

    Args:
        executor: A LatexExecutor instance.

    Returns:
        A FastMCP server instance.
    """
    server = FastMCP(name="latex-exec")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE_RESTRICTED)
    def compile_latex(
        source: str,
        filename: str = "document.tex",
        timeout: float | None = None,
        num_passes: int | None = None,
    ) -> str:
        """Compile LaTeX source code to PDF and return the result.

        Args:
            source: Full LaTeX source (must include documentclass through end{document}).
            filename: Optional filename for the .tex file (default: document.tex).
            timeout: Optional timeout in seconds for compilation.
            num_passes: Optional number of compilation passes (for TOC/references).

        Returns:
            JSON string with success, pdf_path, log, errors, warnings.
        """
        result = executor.compile_source(
            source,
            filename=filename,
            timeout=timeout,
            num_passes=num_passes,
        )
        return json.dumps(result)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE_RESTRICTED)
    def compile_latex_file(
        tex_path: str,
        timeout: float | None = None,
        num_passes: int | None = None,
    ) -> str:
        """Compile an existing .tex file to PDF and return the result.

        Args:
            tex_path: Path to the .tex file to compile.
            timeout: Optional timeout in seconds for compilation.
            num_passes: Optional number of compilation passes.

        Returns:
            JSON string with success, pdf_path, log, errors, warnings.
        """
        result = executor.compile_file(
            tex_path,
            timeout=timeout,
            num_passes=num_passes,
        )
        return json.dumps(result)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.BARE)
    def check_latex_engine() -> str:
        """Check whether the configured LaTeX engine is available.

        Returns:
            JSON string with engine name, availability, and version.
        """
        return json.dumps(executor.check_engine())

    return server


def latex_exec_server(
    config: LatexConfig | None = None,
) -> MCPServerConfig:
    """Create an in-process MCPServerConfig for LaTeX execution.

    Args:
        config: Optional LatexConfig (defaults will be used if omitted).

    Returns:
        MCPServerConfig with transport="inprocess".
    """
    executor = LatexExecutor(config)
    mcp_instance = create_latex_server(executor)
    return MCPServerConfig(
        server_key="latex-exec",
        description="LaTeX document compilation and rendering",
        transport=TransportMethod.INPROCESS,
        mcp_instance=mcp_instance,
    )
