"""LaTeX execution tool — compilation and log parsing.

Public API:
- LatexConfig: Configuration for a LaTeX executor instance
- LatexExecutor: LaTeX compilation manager
- latex_exec_server: MCPServerConfig factory for in-process LaTeX execution
"""

from .core import LatexConfig, LatexExecutor
from .server import create_latex_server, latex_exec_server

__all__ = [
    "LatexConfig",
    "LatexExecutor",
    "create_latex_server",
    "latex_exec_server",
]
