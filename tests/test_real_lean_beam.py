"""Opt-in contract test for Autoform's pinned Lean Beam MCP boundary."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryFile

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOFORM_RUN_REAL_LEAN_BEAM_TESTS") != "1",
    reason="set AUTOFORM_RUN_REAL_LEAN_BEAM_TESTS=1 to exercise the installed Beam runtime",
)

MODERN_MCP_VERSION = "2026-07-28"
REQUEST_TIMEOUT_SECONDS = 180


class McpClient:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        self._next_id = 0
        self._stderr = TemporaryFile(mode="w+t", encoding="utf-8")
        self._stdout_buffer = b""
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            bufsize=0,
            start_new_session=True,
        )

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        )
        self.process.stdin.flush()

    def _read_message(self, deadline: float) -> dict:
        assert self.process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while True:
                line, separator, remainder = self._stdout_buffer.partition(b"\n")
                if separator:
                    self._stdout_buffer = remainder
                    return json.loads(line)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise AssertionError("timed out waiting for a complete MCP response")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise AssertionError(
                        f"Lean Beam exited before a complete MCP response: {self.stderr()}"
                    )
                self._stdout_buffer += chunk
        finally:
            selector.close()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while True:
            response = self._read_message(deadline)
            if response.get("method") is not None:
                continue
            if response.get("id") == request_id:
                return response

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def modern_request(self, method: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN_MCP_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "autoform-integration-test",
                "version": "0",
            },
        }
        return self.request(method, params)

    def discover(self) -> None:
        response = self.modern_request("server/discover")
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"] == [MODERN_MCP_VERSION]

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[dict, dict]:
        response = self.modern_request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )
        result = response["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "lean-beam-mcp"
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        return result, structured

    def stderr(self) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()

    def close(self) -> None:
        close_error: str | None = None
        try:
            if self.process.poll() is None:
                assert self.process.stdin is not None
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    close_error = "Lean Beam did not shut down after MCP EOF"
            returncode = self.process.poll()
            stderr = self.stderr()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                close_error = close_error or "Lean Beam left a process in its MCP process group"
        finally:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if self.process.poll() is None:
                self.process.wait(timeout=10)
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            self._stderr.close()
        if close_error is not None:
            raise AssertionError(close_error)
        if returncode != 0:
            raise AssertionError(f"Lean Beam exited with {returncode}: {stderr}")


def workspace(root: Path) -> dict[str, str]:
    return {"root": str(root.resolve())}


def assert_success(result: dict, structured: dict) -> None:
    assert result.get("isError") is not True, result
    assert structured.get("success") is True, structured


def test_pinned_beam_explicit_session_contract(repo_root: Path, tmp_path: Path) -> None:
    beam_command = os.environ["AUTOFORM_LEAN_BEAM_MCP"]
    toolchain = os.environ.get("AUTOFORM_REAL_LEAN_TOOLCHAIN", "leanprover/lean4:v4.33.0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text(f"{toolchain}\n", encoding="utf-8")
    (project / "lakefile.toml").write_text(
        'name = "BeamSmoke"\ndefaultTargets = ["BeamSmoke"]\n\n[[lean_lib]]\nname = "BeamSmoke"\n',
        encoding="utf-8",
    )
    source = project / "BeamSmoke.lean"
    source.write_text(
        "import BeamSmoke.A\n\n"
        "def answer : Nat := dependencyValue\n\n"
        "set_option linter.unusedVariables true in\n"
        "theorem warnOnly (n : Nat) : True := by\n"
        "  trivial\n\n"
        '#check ("😀", Nat)\n',
        encoding="utf-8",
    )
    (project / "BeamSmoke").mkdir()
    dependency_source = project / "BeamSmoke" / "A.lean"
    dependency_source.write_text("def dependencyValue : Nat := 42\n", encoding="utf-8")
    initial_build = subprocess.run(
        ["lake", "build"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    assert initial_build.returncode == 0, initial_build.stdout + initial_build.stderr

    assert Path(beam_command).is_absolute()
    assert os.access(beam_command, os.X_OK)
    client = McpClient(
        [beam_command],
        cwd=repo_root,
        env=dict(os.environ),
    )
    try:
        lock = json.loads((repo_root / "lean-beam.lock.json").read_text(encoding="utf-8"))
        client.discover()
        assert lock["mcp_protocol"] == MODERN_MCP_VERSION
        version_result, identity = client.call_tool("beam_version")
        assert version_result.get("isError") is not True
        assert identity["name"] == "lean-beam-mcp"
        assert identity["version"] == lock["version"]
        assert identity["mcp_protocol"] == lock["mcp_protocol"]
        assert identity["source_commit"] == lock["commit"]
        assert identity["runtime_current"] is True
        assert "runtime_error" not in identity
        assert identity.get("source_dirty") is not True

        descriptor = workspace(project)
        sync_result, synced = client.call_tool(
            "lean_sync",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "diagnostic_scope": "all",
                "diagnostics_in_result": True,
            },
        )
        assert sync_result.get("isError") is not True
        snapshot = synced["snapshot"]
        assert isinstance(snapshot, str) and snapshot
        assert synced["workspace"] == descriptor
        assert synced["readiness"]["save_ready"] is True
        counts = synced["diagnostics"]["counts"]
        assert counts["total"] == sum(
            counts[name]
            for name in ("error", "warning", "information", "hint", "unknown")
        )
        assert counts["warning"] >= 1
        diagnostics = synced["diagnostics"]["items"]
        assert any(item["severity"] == "warning" for item in diagnostics)
        assert all(item["path"] == "BeamSmoke.lean" for item in diagnostics)
        assert all(item["snapshot"] == snapshot for item in diagnostics)
        assert synced["document_progress"]["done"] is True

        runtime_result, runtime_probe = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": 1,
                "character": 0,
                "text": "#eval Lean.versionString",
            },
        )
        assert_success(runtime_result, runtime_probe)
        expected_lean_version = toolchain.rsplit(":v", 1)[1]
        assert any(
            expected_lean_version in message["text"]
            for message in runtime_probe["messages"]
        )

        hover_result, hover = client.call_tool(
            "lean_hover",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": 8,
                "character": 14,
            },
        )
        assert hover_result.get("isError") is not True
        assert "Nat" in hover["contents"]["value"]

        mint_result, minted = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": 3,
                "character": 0,
                "text": "def transient : Nat := answer + 1",
            },
        )
        assert_success(mint_result, minted)
        first_handle = minted["next_handle"]
        assert isinstance(first_handle, dict)

        other_project = tmp_path / "other-project"
        other_project.mkdir()
        (other_project / "lean-toolchain").write_text(f"{toolchain}\n", encoding="utf-8")
        (other_project / "lakefile.toml").write_text(
            'name = "OtherSmoke"\ndefaultTargets = ["OtherSmoke"]\n\n[[lean_lib]]\nname = "OtherSmoke"\n',
            encoding="utf-8",
        )
        (other_project / "OtherSmoke.lean").write_text("def other : Nat := 7\n", encoding="utf-8")
        cross_workspace_result, cross_workspace = client.call_tool(
            "lean_run_with",
            {
                "workspace": workspace(other_project),
                "path": "OtherSmoke.lean",
                "handle": first_handle,
                "text": "#check transient",
            },
        )
        assert cross_workspace_result["isError"] is True
        assert cross_workspace["code"] == "invalidParams"
        assert "does not match handle workspace" in cross_workspace["message"]

        failed_result, failed_continuation = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": first_handle,
                "text": 'def broken : Nat := "not a Nat"',
            },
        )
        assert failed_result.get("isError") is not True
        assert failed_continuation["success"] is False
        assert failed_continuation["next_handle"] is None

        isolated_result, isolated = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": 3,
                "character": 0,
                "text": "#check transient",
            },
        )
        assert isolated_result.get("isError") is not True
        assert isolated["success"] is False
        assert any(
            "unknown identifier" in message["text"].lower()
            for message in isolated["messages"]
        )

        continuation_result, continued = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": first_handle,
                "text": "def continued : Nat := transient + 1",
            },
        )
        assert_success(continuation_result, continued)
        next_handle = continued["next_handle"]
        assert isinstance(next_handle, dict)
        linear_result, linear = client.call_tool(
            "lean_run_with_linear",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": next_handle,
                "text": "def linear : Nat := continued + 1",
            },
        )
        assert_success(linear_result, linear)
        linear_handle = linear["next_handle"]
        assert isinstance(linear_handle, dict)
        consumed_result, consumed = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": next_handle,
                "text": "#check continued",
            },
        )
        assert consumed_result["isError"] is True
        assert consumed["code"] == "invalidParams"
        for handle in (linear_handle, first_handle):
            release_result, released = client.call_tool(
                "lean_release",
                {
                    "workspace": descriptor,
                    "path": "BeamSmoke.lean",
                    "handle": handle,
                },
            )
            assert release_result.get("isError") is not True
            assert released.get("result") is None

        edit_handle_result, edit_handle_payload = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": 3,
                "character": 0,
                "text": "def staleAfterEdit : Nat := answer",
            },
        )
        assert_success(edit_handle_result, edit_handle_payload)
        edit_handle = edit_handle_payload["next_handle"]

        source.write_text(
            source.read_text(encoding="utf-8") + "\n-- changed after snapshot\n",
            encoding="utf-8",
        )
        _, updated = client.call_tool(
            "lean_update",
            {"workspace": descriptor, "path": "BeamSmoke.lean"},
        )
        assert updated["snapshot"] != snapshot
        stale_result, stale = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": snapshot,
                "line": 1,
                "character": 0,
                "text": "#check answer",
            },
        )
        assert stale_result["isError"] is True
        assert stale["code"] == "contentModified"
        assert stale["data"]["reason"] == "snapshotMismatch"
        assert stale["data"]["expectedSnapshot"] == snapshot
        assert stale["data"]["currentSnapshot"] == updated["snapshot"]
        stale_edit_handle_result, stale_edit_handle = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": edit_handle,
                "text": "#check staleAfterEdit",
            },
        )
        assert stale_edit_handle_result["isError"] is True
        assert stale_edit_handle["code"] == "contentModified"

        _, invalidated = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": updated["snapshot"],
                "line": 3,
                "character": 0,
                "text": "def invalidatedAfterDrop : Nat := answer",
            },
        )
        invalidated_handle = invalidated["next_handle"]
        assert isinstance(invalidated_handle, dict)

        dependency_source.write_text(
            dependency_source.read_text(encoding="utf-8")
            + "\ndef availableAfterExternalBuild : Nat := dependencyValue\n",
            encoding="utf-8",
        )
        dependency_sync_result, dependency_sync = client.call_tool(
            "lean_sync",
            {"workspace": descriptor, "path": "BeamSmoke/A.lean"},
        )
        assert dependency_sync_result.get("isError") is not True
        assert dependency_sync["readiness"]["save_ready"] is True

        build = subprocess.run(
            ["lake", "build"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        assert build.returncode == 0, build.stdout + build.stderr

        drop_result, dropped = client.call_tool(
            "lean_drop_workspace",
            {"workspace": descriptor},
        )
        assert drop_result.get("isError") is not True
        assert dropped["dropped"] is True
        assert dropped["invalidated_handles"] is True
        stale_handle_result, stale_handle = client.call_tool(
            "lean_run_with",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "handle": invalidated_handle,
                "text": "#check invalidatedAfterDrop",
            },
        )
        assert stale_handle_result["isError"] is True
        assert stale_handle["code"] == "contentModified"
        _, resynced = client.call_tool(
            "lean_sync",
            {"workspace": descriptor, "path": "BeamSmoke.lean"},
        )
        assert resynced["readiness"]["save_ready"] is True
        assert resynced["snapshot"] != updated["snapshot"]
        stale_generation_result, stale_generation = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": updated["snapshot"],
                "line": 1,
                "character": 0,
                "text": "#check answer",
            },
        )
        assert stale_generation_result["isError"] is True
        assert stale_generation["code"] == "contentModified"
        assert stale_generation["data"]["reason"] == "snapshotMismatch"
        assert stale_generation["data"]["expectedSnapshot"] == updated["snapshot"]
        assert stale_generation["data"]["currentSnapshot"] == resynced["snapshot"]
        dependency_probe_result, dependency_probe = client.call_tool(
            "lean_run_at",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": resynced["snapshot"],
                "line": 1,
                "character": 0,
                "text": "#check availableAfterExternalBuild",
            },
        )
        assert_success(dependency_probe_result, dependency_probe)

        eof_handle_result, eof_handle = client.call_tool(
            "lean_run_at_handle",
            {
                "workspace": descriptor,
                "path": "BeamSmoke.lean",
                "snapshot": resynced["snapshot"],
                "line": 3,
                "character": 0,
                "text": "def liveAtEof : Nat := answer",
            },
        )
        assert_success(eof_handle_result, eof_handle)
        assert isinstance(eof_handle["next_handle"], dict)
    finally:
        client.close()
