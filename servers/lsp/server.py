"""Lean execution backend for diagnostics and type information over LSP.

Wraps Lean 4 language server processes and provides file diagnostics and hover
information independently from the REPL pool.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

from fastmcp.server import FastMCP

from servers import resolve_lean_file, resolve_lean_project_dir

logger = getLogger(__name__)

DEFAULT_LSP_TIMEOUT = 60


class LspProtocolError(RuntimeError):
    """The Lean language server returned or emitted invalid JSON-RPC state."""


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
        # A session has one stdout stream. Serialize the complete document
        # lifecycle so concurrent MCP calls cannot race two readers against
        # that stream or consume one another's diagnostics/responses.
        self._operation_lock = threading.Lock()

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

        try:
            # An initialize response must be an InitializeResult object. A
            # timeout, JSON-RPC error, or malformed result means the backing
            # Lean server is unusable, so do not expose an apparently healthy
            # MCP server on top of it.
            result = self._send_request("initialize", {
                "processId": os.getpid(),
                "capabilities": {},
                "rootUri": Path(self.config.cwd).resolve().as_uri(),
            })
            if not isinstance(result, dict):
                raise LspProtocolError(
                    "LSP initialize returned a non-object result: "
                    f"{result!r}"
                )

            self._send_notification("initialized", {})
        except BaseException:
            self._abort_process()
            raise

    def _abort_process(self) -> None:
        """Force-close the backing process without attempting more JSON-RPC."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass

    def close(self) -> None:
        """Shut down the language server."""
        if self.process and self.process.poll() is None:
            try:
                self._send_request("shutdown", {})
                self._send_notification("exit", {})
                self.process.wait(timeout=5)
            except Exception:
                self._abort_process()
        self.process = None

    def get_diagnostics(self, file_path: str) -> list[dict]:
        """Open a file and collect diagnostics from the language server."""
        with self._operation_lock:
            return self._get_diagnostics(file_path)

    def _get_diagnostics(self, file_path: str) -> list[dict]:
        path = Path(file_path).resolve()
        uri = path.as_uri()

        try:
            content = path.read_text()
        except Exception as e:
            return [{"severity": "error", "message": f"Cannot read file: {e}"}]

        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "lean4",
                "version": 1,
                "text": content,
            }
        })

        try:
            # An empty published diagnostic list means the file is clean. No
            # publication at all is a timeout/error and must never be conflated
            # with that valid empty result.
            return self._collect_diagnostics(uri, timeout=self.config.timeout)
        finally:
            try:
                self._send_notification("textDocument/didClose", {
                    "textDocument": {"uri": uri}
                })
            except Exception:
                logger.warning("failed to close LSP document %s", uri, exc_info=True)

    def hover(self, file_path: str, line: int, character: int) -> str | None:
        """Get hover information at a position."""
        with self._operation_lock:
            return self._hover(file_path, line, character)

    def _hover(self, file_path: str, line: int, character: int) -> str | None:
        path = Path(file_path).resolve()
        content = path.read_text()
        uri = path.as_uri()
        self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "lean4",
                "version": 1,
                "text": content,
            }
        })
        try:
            result = self._send_request("textDocument/hover", {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
            })
        finally:
            try:
                self._send_notification("textDocument/didClose", {
                    "textDocument": {"uri": uri}
                })
            except Exception:
                logger.warning("failed to close LSP document %s", uri, exc_info=True)
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
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = self._read_message(timeout=remaining)
            if msg and msg.get("id") == request_id:
                if "error" in msg:
                    error = msg["error"]
                    if isinstance(error, dict):
                        code = error.get("code", "unknown")
                        message = error.get("message", "unspecified protocol error")
                        data = error.get("data")
                        detail = f" ({data!r})" if data is not None else ""
                        raise LspProtocolError(
                            f"LSP request {request_id} failed [{code}]: "
                            f"{message}{detail}"
                        )
                    raise LspProtocolError(
                        f"LSP request {request_id} returned malformed error: {error!r}"
                    )
                if "result" not in msg:
                    raise LspProtocolError(
                        f"LSP response {request_id} has neither result nor error"
                    )
                return msg["result"]
        raise TimeoutError(
            f"timed out after {timeout:g}s waiting for LSP response {request_id}"
        )

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
                raise LspProtocolError("LSP process closed stdout mid-header")
            header += chunk

        try:
            header_lines = header[:-4].decode("ascii").split("\r\n")
        except UnicodeDecodeError as error:
            raise LspProtocolError("LSP emitted a non-ASCII header") from error
        content_lengths = [
            value.strip()
            for line in header_lines
            if ":" in line
            for name, value in [line.split(":", 1)]
            if name.strip().lower() == "content-length"
        ]
        if len(content_lengths) != 1:
            raise LspProtocolError(
                f"LSP message has {len(content_lengths)} Content-Length headers"
            )
        try:
            length = int(content_lengths[0])
        except ValueError as error:
            raise LspProtocolError(
                f"invalid LSP Content-Length: {content_lengths[0]!r}"
            ) from error
        if length < 0:
            raise LspProtocolError(f"invalid negative LSP Content-Length: {length}")

        # Read body
        body = b""
        while len(body) < length:
            chunk = os.read(stdout_fd, length - len(body))
            if not chunk:
                raise LspProtocolError("LSP process closed stdout mid-message")
            body += chunk

        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LspProtocolError("LSP emitted an invalid JSON body") from error
        if not isinstance(message, dict):
            raise LspProtocolError("LSP JSON-RPC message is not an object")
        return message

    def _collect_diagnostics(self, uri: str, timeout: float) -> list[dict]:
        """Collect diagnostic notifications for a URI."""
        diagnostics: list[dict] = []
        deadline = time.monotonic() + timeout
        received = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if received:
                    return diagnostics
                raise TimeoutError(
                    f"timed out after {timeout:g}s waiting for diagnostics for {uri}"
                )

            # Before the first publication, keep waiting all the way to the
            # configured deadline. After one arrives, a one-second quiet period
            # is enough to treat the latest publication as final, while still
            # respecting the same total deadline.
            read_timeout = min(1 if received else 2, remaining)
            msg = self._read_message(timeout=read_timeout)
            if msg is None:
                if received:
                    return diagnostics
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params", {})
                if not isinstance(params, dict):
                    raise LspProtocolError(
                        "publishDiagnostics params must be an object"
                    )
                published = params.get("diagnostics")
                if not isinstance(published, list):
                    raise LspProtocolError(
                        "publishDiagnostics diagnostics must be a list"
                    )
                if params.get("uri") == uri:
                    received = True
                    diagnostics = published


class LeanLspProjects:
    """Lazily keep one language-server session per explicit Lean project."""

    def __init__(
        self,
        session_factory: Callable[[Path], LeanLspSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or self._start_session
        self._sessions: dict[Path, LeanLspSession] = {}
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _start_session(project_dir: Path) -> LeanLspSession:
        session = LeanLspSession(LspConfig(cwd=str(project_dir)))
        session.start()
        return session

    def get(self, project_dir: str) -> LeanLspSession:
        """Return the session for a validated absolute Lake project."""
        root = resolve_lean_project_dir(project_dir)
        with self._lock:
            if self._closed:
                raise RuntimeError("Lean LSP project router is closed")
            session = self._sessions.get(root)
            if session is None:
                session = self._session_factory(root)
                self._sessions[root] = session
            return session

    def close(self) -> None:
        """Close all sessions created by this router."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._closed = True
        for session in sessions:
            session.close()


def create_lsp_server(projects: LeanLspProjects) -> FastMCP:
    """Create the LSP MCP server for explicit Lean projects."""
    server = FastMCP(name="autoform-lsp")

    @server.tool
    def lean_diagnostic_messages(project_dir: str, file_path: str) -> str:
        """Return Lean diagnostics for an in-project file.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
        """
        root, path = resolve_lean_file(project_dir, file_path)
        diagnostics = projects.get(str(root)).get_diagnostics(str(path))
        if not diagnostics:
            return "No diagnostics — file compiles cleanly."

        lines: list[str] = []
        for diagnostic in diagnostics:
            severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
                diagnostic.get("severity", 0), "unknown"
            )
            position = diagnostic.get("range", {}).get("start", {})
            line = position.get("line", 0) + 1
            column = position.get("character", 0)
            message = diagnostic.get("message", "")
            lines.append(f"{line}:{column}: {severity}: {message}")

        errors = sum(item.get("severity") == 1 for item in diagnostics)
        warnings = sum(item.get("severity") == 2 for item in diagnostics)
        return f"Diagnostics: {errors} error(s), {warnings} warning(s)\n" + "\n".join(lines)

    @server.tool
    def lean_hover(project_dir: str, file_path: str, line: int, character: int) -> str:
        """Return Lean hover information at a zero-indexed position.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            line: Zero-indexed line number.
            character: Zero-indexed character position.
        """
        root, path = resolve_lean_file(project_dir, file_path)
        result = projects.get(str(root)).hover(str(path), line, character)
        return result or "No hover information at this position."

    return server


def main() -> None:
    projects = LeanLspProjects()
    try:
        create_lsp_server(projects).run(transport="stdio")
    finally:
        projects.close()


if __name__ == "__main__":
    main()
