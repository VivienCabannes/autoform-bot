# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean LSP MCP server factory.

Provides:
- LeanLspServerArgs / LeanLspServer: PerRdvServer subclass with file-lock coordination
- start_lsp_server: Launch lean-lsp-mcp as a background HTTP server
- lsp_server_config: Return an MCPServerConfig pointing to a running LSP server

Each worktree gets its own LSP server instance, keyed by project_path.
Multiple agents on the same worktree share one server via file-lock coordination.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import ClassVar, TextIO

from core.mcp import MCPServerConfig, TransportMethod
from core.process import PerRdvServer, RdvServerArgs

logger = getLogger(__name__)

DEFAULT_HEALTH_CHECK_TIMEOUT = 2

_DEFAULT_DISABLE_TOOLS = (
    "lean_build,lean_run_code,lean_multi_attempt,lean_write_file,lean_file_contents,lean_apply_diff"
)


@dataclass(frozen=True)
class LspConfig:
    """Configuration for the Lean LSP tool.

    Specifies the project directory for the LSP server.
    """

    project_path: str
    disable_tools: str = _DEFAULT_DISABLE_TOOLS
    dump_dir: str | None = None


@dataclass
class LeanLspServerArgs(RdvServerArgs):
    """Configuration for the Lean LSP MCP server.

    Overrides get_rdv_identifier to key by project_path — each worktree
    gets its own LSP server instance.
    """

    NAME: ClassVar[str] = "lean-lsp"

    project_path: str = ""
    disable_tools: str = _DEFAULT_DISABLE_TOOLS
    dump_dir: str = field(
        default_factory=lambda: os.path.join(tempfile.gettempdir(), f"autoform-{os.getuid()}", "lean-lsp")
    )

    def get_rdv_identifier(self) -> str:
        """Key by project path so each worktree gets its own server."""
        return self.project_path.replace("/", "_").strip("_")


class LeanLspServer(PerRdvServer):
    """Lean LSP MCP server managed via PerRdvServer file-lock coordination.

    Adds health checking: if a previous server died but left its address
    file behind, start() detects the stale state and restarts.
    """

    def __init__(self, args: LeanLspServerArgs) -> None:
        super().__init__(args)
        # lean-lsp-mcp must bind to 127.0.0.1, not the hostname.
        self._host = "127.0.0.1"

    def start(self) -> None:
        """Start the server, with stale server detection.

        If the address file exists but the server is unresponsive
        (TCP connect fails), clean up and start fresh.
        """
        if os.path.exists(self._server_addr_file):
            try:
                addr = open(self._server_addr_file).read().strip()
                parts = addr.replace("http://", "").split(":")
                host = parts[0]
                port = int(parts[1].split("/")[0])
                with socket.create_connection((host, port), timeout=DEFAULT_HEALTH_CHECK_TIMEOUT):
                    pass  # server is alive
            except (OSError, ValueError, IndexError):
                logger.warning("Stale LSP server detected — cleaning up before restart")
                for f in (self._server_addr_file, self._client_counter_file):
                    if os.path.exists(f):
                        os.remove(f)

        super().start()

    def _start_server_process(
        self,
        port: int,
        env: dict[str, str],
        stdout: TextIO,
        stderr: TextIO,
        args: RdvServerArgs,
    ) -> str:
        assert isinstance(args, LeanLspServerArgs)

        bin_dir = str(Path(sys.executable).parent)
        lsp_bin = shutil.which("lean-lsp-mcp", path=bin_dir) or shutil.which("lean-lsp-mcp")
        if not lsp_bin:
            raise FileNotFoundError("lean-lsp-mcp not found. Install with: pip install lean-lsp-mcp")

        cmd = [
            lsp_bin,
            "--transport",
            "streamable-http",
            "--host",
            self._host,
            "--port",
            str(port),
        ]
        if args.disable_tools:
            cmd.extend(["--disable-tools", args.disable_tools])

        env["LEAN_PROJECT_PATH"] = args.project_path

        logger.info("Starting Lean LSP server on %s:%d for %s", self._host, port, args.project_path)

        self.process = subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr)

        return f"http://{self._host}:{port}"


def start_lsp_server(
    project_path: str,
    *,
    dump_dir: str | None = None,
    disable_tools: str = _DEFAULT_DISABLE_TOOLS,
) -> LeanLspServer:
    """Launch lean-lsp-mcp as a streamable HTTP server and wait until ready.

    Uses PerRdvServer file-lock coordination for process-safe startup.
    Keyed by project_path — each worktree gets its own server instance.
    Multiple callers for the same project_path share one server.

    Args:
        project_path: Path to the Lean project root (where lakefile.toml is).
        dump_dir: Directory for logs and coordination files.
        disable_tools: Comma-separated tool names to disable.

    Returns:
        The LeanLspServer instance.
    """
    kwargs: dict = {"project_path": project_path, "disable_tools": disable_tools}
    if dump_dir is not None:
        kwargs["dump_dir"] = dump_dir

    args = LeanLspServerArgs(**kwargs)
    server = LeanLspServer(args)
    server.start()
    return server


def lsp_server_config(config: LspConfig) -> MCPServerConfig:
    """Start-or-join an LSP server and return its MCPServerConfig.

    Uses rdv file-lock coordination — first caller for a given project_path
    starts the server, others reuse it.
    """
    server = start_lsp_server(
        config.project_path,
        disable_tools=config.disable_tools,
        dump_dir=config.dump_dir,
    )
    atexit.register(server.close)
    url = server.get_server_address() + "/mcp"
    return MCPServerConfig(
        server_key="lsp",
        description="Lean language server protocol client",
        transport=TransportMethod.STREAMABLE_HTTP,
        url=url,
    )
