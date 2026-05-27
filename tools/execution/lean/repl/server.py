# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Lean REPL MCP server — FastMCP tool definitions and run/connect helpers."""

from __future__ import annotations

import atexit
import json
import os
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, ClassVar

from fastmcp.server import FastMCP

from core.constants import REPO_ROOT
from core.mcp import MCPServerConfig, TransportMethod
from core.tool import Autonomy, ToolSpec
from core.process import PerRdvServer, RdvServerArgs

from .core import format_repl_response
from .pool import LeanReplPool, LeanReplPoolConfig

logger = getLogger(__name__)

_DEFAULT_TEMPLATE_DIR = str(REPO_ROOT / "template")
_DEFAULT_REPL_COMMAND = (
    "lake",
    "env",
    str(REPO_ROOT / "submodules" / "repl" / ".lake" / "build" / "bin" / "repl"),
)


@dataclass(frozen=True)
class ReplConfig:
    """Configuration for the Lean REPL tool.

    Specifies the Lean project directory and REPL binary command.
    Defaults point to the v4.29 template and submodules/repl binary.
    """

    cwd: str = _DEFAULT_TEMPLATE_DIR
    repl_command: tuple[str, ...] = _DEFAULT_REPL_COMMAND
    num_repls: int | None = None
    startup_stagger: float | None = None
    dump_dir: str | None = None


def create_repl_mcp(pool: LeanReplPool) -> FastMCP:
    """Create a FastMCP server wrapping a LeanReplPool.

    Exposes two tools:
    - run_lean_code: Send Lean code to the REPL pool
    - get_repl_status: Check pool health and memory usage
    """
    server = FastMCP(name="lean-repl")

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.EXECUTE_RESTRICTED)
    def run_lean_code(code: str, timeout: float | None = None) -> str:
        """Send Lean code to the REPL and return formatted diagnostics.

        Imports are cached automatically — repeated calls with the same
        imports reuse the cached environment for speed.

        Args:
            code: Lean code to execute (imports + body).
            timeout: Optional timeout in seconds (overrides the default).

        Returns:
            Formatted diagnostic output: compilation status, errors,
            sorries with goals, and warnings.
        """
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = pool.run(code, **kwargs)
        return format_repl_response(result)

    @server.tool
    @ToolSpec.define(autonomy=Autonomy.READ)
    def get_repl_status() -> str:
        """Check the REPL pool's health and memory usage.

        Returns:
            JSON string with capacity, memory_usage_gb, and shutdown status.
        """
        return json.dumps(
            {
                "capacity": pool.capacity,
                "memory_usage_gb": round(pool.get_memory_usage(unit="GB"), 2),
                "shutdown": pool._shutdown,
            }
        )

    return server


# ---------------------------------------------------------------------------
# PerRdvServer subclass for the REPL
# ---------------------------------------------------------------------------


@dataclass
class LeanReplServerArgs(RdvServerArgs):
    """Configuration for the Lean REPL MCP server."""

    NAME: ClassVar[str] = "lean-repl"

    cwd: str = ""
    repl_command: list[str] = field(default_factory=list)
    num_repls: int | None = None
    startup_stagger: float | None = None
    dump_dir: str = field(
        default_factory=lambda: os.path.join(tempfile.gettempdir(), f"autoform-{os.getuid()}", "lean-repl")
    )


class LeanReplServer(PerRdvServer):
    """Lean REPL pool server managed via PerRdvServer file-lock coordination.

    Unlike the subprocess-based LSP server, the REPL server runs a FastMCP
    server in a daemon thread. This requires overriding ``_start_server``
    to manage the thread lifecycle directly.
    """

    def __init__(self, args: LeanReplServerArgs) -> None:
        super().__init__(args)
        self.pool: LeanReplPool | None = None
        self._mcp_server: FastMCP | None = None

    def start(self) -> None:
        """Start the server, with stale server detection."""
        if os.path.exists(self._server_addr_file):
            try:
                addr = open(self._server_addr_file).read().strip()
                parts = addr.replace("http://", "").split(":")
                host = parts[0]
                port = int(parts[1].split("/")[0])
                with socket.create_connection((host, port), timeout=2):
                    pass  # server is alive
            except (OSError, ValueError, IndexError):
                logger.warning("Stale REPL server detected — cleaning up before restart")
                for f in (self._server_addr_file, self._client_counter_file):
                    if os.path.exists(f):
                        os.remove(f)

        super().start()

    def _start_server(self, timeout: float) -> None:
        assert isinstance(self.args, LeanReplServerArgs)

        # Allocate a dynamic port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self._host, 0))
        port = sock.getsockname()[1]
        sock.close()

        config_kwargs: dict[str, Any] = {
            "cwd": self.args.cwd,
            "repl_command": self.args.repl_command,
            "num_repls": self.args.num_repls,
            "host": self._host,
            "port": port,
        }
        if self.args.startup_stagger is not None:
            config_kwargs["startup_stagger"] = self.args.startup_stagger

        config = LeanReplPoolConfig(**config_kwargs)

        self.pool = LeanReplPool(config)
        self._mcp_server = create_repl_mcp(self.pool)

        thread = threading.Thread(
            target=self._mcp_server.run,
            kwargs={"transport": "streamable-http", "host": self._host, "port": port},
            daemon=True,
        )
        thread.start()

        self.init_connection_counter()

        server_addr = f"http://{self._host}:{port}"
        logger.info("%s server started on %s.", self.name, server_addr)
        with open(self._server_addr_file, "w") as f:
            f.write(server_addr)

        # Wait for server to accept connections
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((self._host, port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)

        raise RuntimeError(f"{self.name} server failed to start within {timeout}s on port {port}")

    def close(self) -> None:
        """Decrement the client counter; shut down only when no clients remain."""
        if not os.path.exists(self._client_counter_file):
            if self.pool is not None:
                self.pool.shutdown()
                self.pool = None
            logger.info("%s server stopped (no counter file).", self.name)
            return

        with self._acquire_lock(self._process_lock_file, timeout=self.timeout):
            try:
                with open(self._client_counter_file) as f:
                    counter = int(f.read().strip() or "0")
            except (FileNotFoundError, ValueError):
                counter = 0

            counter = max(0, counter - 1)

            if counter > 0:
                with open(self._client_counter_file, "w") as f:
                    f.write(str(counter))
                logger.info("%s client disconnected (%d still active).", self.name, counter)
                return

            # Last client — shut down pool if we own it and clean up.
            if self.pool is not None:
                self.pool.shutdown()
                self.pool = None

            for path in (self._server_addr_file, self._client_counter_file):
                if os.path.exists(path):
                    os.remove(path)

            logger.info("%s server stopped (last client).", self.name)


def start_repl_server(
    cwd: str,
    repl_command: list[str],
    *,
    num_repls: int | None = None,
    startup_stagger: float | None = None,
    dump_dir: str | None = None,
) -> LeanReplServer:
    """Start the Lean REPL pool as a streamable HTTP server.

    Uses PerRdvServer file-lock coordination for process-safe startup —
    concurrent calls will block and reuse the first instance.

    Args:
        cwd: Lean project directory (e.g. submodules/mathlib).
        repl_command: Command to start the REPL (e.g. ["lake", "env", "/path/to/repl"]).
        num_repls: Number of parallel REPL instances.
        startup_stagger: Optional override for per-worker cold-start staggering, in seconds.
        dump_dir: Directory for logs and coordination files.

    Returns:
        The LeanReplServer instance.
    """
    kwargs: dict = {
        "cwd": cwd,
        "repl_command": repl_command,
        "num_repls": num_repls,
    }
    if startup_stagger is not None:
        kwargs["startup_stagger"] = startup_stagger
    if dump_dir is not None:
        kwargs["dump_dir"] = dump_dir

    args = LeanReplServerArgs(**kwargs)
    server = LeanReplServer(args)
    server.start()
    return server


def repl_server_config(config: ReplConfig) -> MCPServerConfig:
    """Start-or-join a REPL server and return its MCPServerConfig.

    Uses rdv file-lock coordination — first caller starts, others reuse.
    Increments the client counter so the server stays alive until all
    clients disconnect.  Provides a ``reconnect`` callback so the MCP
    manager can auto-restart the server if it dies.
    """

    _atexit_registered = False

    def _start_and_register() -> MCPServerConfig:
        nonlocal _atexit_registered
        server = start_repl_server(
            cwd=config.cwd,
            repl_command=list(config.repl_command),
            num_repls=config.num_repls,
            startup_stagger=config.startup_stagger,
            dump_dir=config.dump_dir,
        )
        server.update_connection_counter(1)
        if not _atexit_registered:
            atexit.register(server.close)
            _atexit_registered = True
        url = server.get_server_address() + "/mcp"
        return MCPServerConfig(
            server_key="lean-repl",
            description="Lean 4 REPL for type-checking and executing Lean code",
            transport=TransportMethod.STREAMABLE_HTTP,
            url=url,
            reconnect=_start_and_register,
        )

    return _start_and_register()
