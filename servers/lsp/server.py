"""Lean LSP MCP server — diagnostics and type information via Language Server Protocol.

Wraps a Lean 4 language server process. Provides file diagnostics,
hover info, and go-to-definition without requiring the full REPL pool.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

from fastmcp.server import FastMCP

logger = getLogger(__name__)

DEFAULT_LSP_TIMEOUT = 60


@dataclass
class LspConfig:
    """Configuration for the Lean LSP server."""

    cwd: str = "."
    lake_command: list[str] = field(default_factory=lambda: ["lake", "serve"])
    timeout: float = DEFAULT_LSP_TIMEOUT


class LeanLspSession:
    """Manages a Lean 4 language server subprocess via JSON-RPC."""

    def __init__(self, config: LspConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the language server process."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        self.process = subprocess.Popen(
            self.config.lake_command,
            cwd=self.config.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Send initialize request
        self._send_request("initialize", {
            "processId": os.getpid(),
            "capabilities": {},
            "rootUri": f"file://{Path(self.config.cwd).resolve()}",
        })

        # Send initialized notification
        self._send_notification("initialized", {})

    def close(self) -> None:
        """Shut down the language server."""
        if self.process and self.process.poll() is None:
            try:
                self._send_request("shutdown", {})
                self._send_notification("exit", {})
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except Exception:
                    pass
        self.process = None

    def get_diagnostics(self, file_path: str) -> list[dict]:
        """Open a file and collect diagnostics from the language server."""
        uri = f"file://{Path(file_path).resolve()}"

        try:
            content = Path(file_path).read_text()
        except Exception as e:
            return [{"severity": "error", "message": f"Cannot read file: {e}"}]

        # Open the document
        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "lean4",
                "version": 1,
                "text": content,
            }
        })

        # Wait for diagnostics (they arrive as notifications)
        diagnostics = self._collect_diagnostics(uri, timeout=self.config.timeout)

        # Close the document
        self._send_notification("textDocument/didClose", {
            "textDocument": {"uri": uri}
        })

        return diagnostics

    def hover(self, file_path: str, line: int, character: int) -> str | None:
        """Get hover information at a position."""
        uri = f"file://{Path(file_path).resolve()}"
        result = self._send_request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        if result and "contents" in result:
            contents = result["contents"]
            if isinstance(contents, dict):
                return contents.get("value", "")
            return str(contents)
        return None

    def _send_request(self, method: str, params: dict) -> Any:
        """Send a JSON-RPC request and wait for response."""
        with self._lock:
            self._request_id += 1
            msg = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
            self._write_message(msg)
            return self._read_response(self._request_id)

    def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._write_message(msg)

    def _write_message(self, msg: dict) -> None:
        """Write a JSON-RPC message with Content-Length header."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("LSP process not running")
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + body)
        self.process.stdin.flush()

    def _read_response(self, request_id: int, timeout: float = 30) -> Any:
        """Read JSON-RPC messages until we get the response for request_id."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._read_message(timeout=deadline - time.monotonic())
            if msg and msg.get("id") == request_id:
                return msg.get("result")
        return None

    def _read_message(self, timeout: float = 5) -> dict | None:
        """Read one JSON-RPC message from stdout."""
        if not self.process or not self.process.stdout:
            return None

        import select as _select

        stdout_fd = self.process.stdout.fileno()
        ready, _, _ = _select.select([stdout_fd], [], [], timeout)
        if not ready:
            return None

        # Read Content-Length header
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = os.read(stdout_fd, 1)
            if not chunk:
                return None
            header += chunk

        length_line = header.decode("ascii").strip()
        length = int(length_line.split(":")[1].strip())

        # Read body
        body = b""
        while len(body) < length:
            chunk = os.read(stdout_fd, length - len(body))
            if not chunk:
                return None
            body += chunk

        return json.loads(body.decode("utf-8"))

    def _collect_diagnostics(self, uri: str, timeout: float) -> list[dict]:
        """Collect diagnostic notifications for a URI."""
        diagnostics: list[dict] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            msg = self._read_message(timeout=min(2, deadline - time.monotonic()))
            if msg is None:
                break
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params", {})
                if params.get("uri") == uri:
                    diagnostics = params.get("diagnostics", [])
                    # Wait a bit for final diagnostics
                    time.sleep(0.5)
                    # Check for updated diagnostics
                    while True:
                        update = self._read_message(timeout=1)
                        if update is None:
                            break
                        if (
                            update.get("method") == "textDocument/publishDiagnostics"
                            and update.get("params", {}).get("uri") == uri
                        ):
                            diagnostics = update["params"].get("diagnostics", [])
                    break

        return diagnostics


def create_lsp_server(session: LeanLspSession) -> FastMCP:
    """Create a FastMCP server wrapping a LeanLspSession."""
    server = FastMCP(name="autoform-lsp")

    @server.tool
    def lean_diagnostic_messages(file_path: str) -> str:
        """Get compilation diagnostics for a Lean file.

        Returns errors, warnings, and info messages from the Lean language server.

        Args:
            file_path: Absolute path to the .lean file.
        """
        diagnostics = session.get_diagnostics(file_path)
        if not diagnostics:
            return "No diagnostics — file compiles cleanly."

        lines = []
        for d in diagnostics:
            severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(d.get("severity", 0), "unknown")
            pos = d.get("range", {}).get("start", {})
            line = pos.get("line", 0) + 1
            col = pos.get("character", 0)
            msg = d.get("message", "")
            lines.append(f"{line}:{col}: {severity}: {msg}")

        errors = sum(1 for d in diagnostics if d.get("severity") == 1)
        warnings = sum(1 for d in diagnostics if d.get("severity") == 2)
        header = f"Diagnostics: {errors} error(s), {warnings} warning(s)"
        return header + "\n" + "\n".join(lines)

    @server.tool
    def lean_hover(file_path: str, line: int, character: int) -> str:
        """Get type information at a position in a Lean file.

        Args:
            file_path: Absolute path to the .lean file.
            line: 0-indexed line number.
            character: 0-indexed character position.
        """
        result = session.hover(file_path, line, character)
        return result or "No hover information at this position."

    return server


if __name__ == "__main__":
    cwd = os.environ.get("LEAN_PROJECT_DIR", ".")
    config = LspConfig(cwd=cwd)
    session = LeanLspSession(config)
    session.start()

    try:
        server = create_lsp_server(session)
        server.run(transport="stdio")
    finally:
        session.close()
