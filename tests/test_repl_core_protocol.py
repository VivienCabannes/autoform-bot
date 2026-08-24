"""Focused contracts salvaged from the retired standalone REPL implementation."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack

import pytest

from servers.repl import core as repl_core


def test_split_imports_preserves_body_offset_after_comments_and_blank_lines():
    code = """-- preface
import Mathlib.Data.Nat.Basic

-- body comment
#check Nat
"""

    imports, body, offset = repl_core._split_imports_and_body(code)

    assert imports == ["Mathlib.Data.Nat.Basic"]
    assert body == "#check Nat\n"
    assert offset == 4


def test_split_imports_stops_at_the_first_body_statement():
    imports, body, offset = repl_core._split_imports_and_body(
        "import Mathlib\n#check Nat\nimport Aesop\n"
    )

    assert imports == ["Mathlib"]
    assert body == "#check Nat\nimport Aesop\n"
    assert offset == 1


def test_run_rejects_disallowed_import_roots_before_touching_the_process():
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            allowed_imports=frozenset({"Mathlib"}),
            warmup_imports=frozenset(),
        )
    )

    response = repl.run("import Unsafe.Module\n#check Nat")

    assert response == {
        "repl_error": "Disallowed imports: Unsafe. Allowed roots: Mathlib."
    }
    assert repl.process is None


def test_run_offsets_diagnostics_after_stripping_import_header(monkeypatch):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            warmup_imports=frozenset(),
            validate_imports=False,
        )
    )
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(
        repl,
        "_run",
        lambda code, env_id, timeout: {
            "messages": [
                {
                    "severity": "error",
                    "data": "boom",
                    "pos": {"line": 1, "column": 2},
                    "endPos": {"line": 1, "column": 3},
                }
            ],
            "sorries": [
                {
                    "goal": "False",
                    "pos": {"line": 2, "column": 1},
                    "endPos": {"line": 2, "column": 2},
                }
            ],
        },
    )

    response = repl.run("import Mathlib\n\n#check Missing", timeout=1)

    assert response["messages"][0]["pos"]["line"] == 3
    assert response["messages"][0]["endPos"]["line"] == 3
    assert response["sorries"][0]["pos"]["line"] == 4
    assert response["sorries"][0]["endPos"]["line"] == 4


def test_format_repl_response_prioritizes_errors_and_keeps_sorries():
    formatted = repl_core.format_repl_response(
        {
            "messages": [
                {"severity": "warning", "data": "unused"},
                {
                    "severity": "error",
                    "data": "unknown identifier",
                    "pos": {"line": 3, "column": 5},
                },
                "malformed message",
            ],
            "sorries": [{"goal": "Nat = Nat", "pos": {"line": 7}}],
        }
    )

    assert formatted == (
        "Compilation Errors (1)\n"
        "  - 3:5: error: unknown identifier\n"
        "\nSorries (1)\n"
        "  - Line 7: Nat = Nat"
    )
    assert "unused" not in formatted


def test_format_repl_response_truncates_diagnostics():
    formatted = repl_core.format_repl_response(
        {
            "messages": [
                {"severity": "warning", "data": f"warning {index}"}
                for index in range(repl_core.DEFAULT_MAX_DIAGNOSTICS + 2)
            ]
        }
    )

    assert "Warnings (12)" in formatted
    assert "warning 9" in formatted
    assert "warning 10" not in formatted
    assert "... and 2 more" in formatted


def test_format_repl_response_reports_explicit_repl_error():
    assert (
        repl_core.format_repl_response({"repl_error": "worker unavailable"})
        == "REPL error: worker unavailable"
    )


class _PipeProcess:
    def __init__(self, stack: ExitStack, stdout_chunks: list[bytes], stderr: bytes = b""):
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        self.stdin = stack.enter_context(os.fdopen(stdin_write, "wb", buffering=0))
        self.stdout = stack.enter_context(os.fdopen(stdout_read, "rb", buffering=0))
        self.stderr = stack.enter_context(os.fdopen(stderr_read, "rb", buffering=0))
        self._stdin_read = stack.enter_context(os.fdopen(stdin_read, "rb", buffering=0))
        self._stdout_write = stack.enter_context(os.fdopen(stdout_write, "wb", buffering=0))
        self._stderr_write = stack.enter_context(os.fdopen(stderr_write, "wb", buffering=0))
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_bytes = stderr

    def poll(self):
        return None


def _repl_with_process(process: _PipeProcess, *, chunk_size: int = 4096, max_buffer_bytes: int = 1024):
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            chunk_size=chunk_size,
            max_buffer_bytes=max_buffer_bytes,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    repl.process = process
    return repl


def _patch_pipe_reads(monkeypatch, process: _PipeProcess):
    real_read = os.read

    def take_chunk(chunks: list[bytes], size: int) -> bytes:
        if not chunks:
            return b""
        result = chunks[0][:size]
        chunks[0] = chunks[0][size:]
        if not chunks[0]:
            chunks.pop(0)
        return result

    def fake_read(fd: int, size: int) -> bytes:
        if fd == process.stdout.fileno():
            return take_chunk(process.stdout_chunks, size)
        if fd == process.stderr.fileno():
            result = process.stderr_bytes[:size]
            process.stderr_bytes = process.stderr_bytes[size:]
            return result
        return real_read(fd, size)

    def fake_select(readable, writable, exceptional, timeout=None):
        if writable:
            return [], writable, []
        ready = []
        if process.stderr_bytes:
            ready.append(process.stderr.fileno())
        if process.stdout_chunks:
            ready.append(process.stdout.fileno())
        return ready, [], []

    monkeypatch.setattr(repl_core.os, "read", fake_read)
    monkeypatch.setattr(repl_core.select, "select", fake_select)


def test_wire_protocol_accepts_response_split_across_reads(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages":', b" []}\n", b"\n"])
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", env_id=3, timeout=1) == {"messages": []}

        request = process._stdin_read.read(4096)
        assert json.loads(request.decode().strip()) == {"cmd": "#check Nat", "env": 3}


def test_wire_protocol_preserves_utf8_split_across_reads(monkeypatch):
    response = json.dumps({"messages": [{"data": "Nat → Nat"}]}, ensure_ascii=False).encode()
    arrow = "→".encode()
    split = response.index(arrow) + 1
    with ExitStack() as stack:
        process = _PipeProcess(
            stack,
            [response[:split], response[split:] + b"\n\n"],
        )
        repl = _repl_with_process(process, chunk_size=7)
        _patch_pipe_reads(monkeypatch, process)

        result = repl._run("#check Nat", env_id=None, timeout=1)

    assert result["messages"][0]["data"] == "Nat → Nat"


def test_wire_protocol_reports_complete_stderr_on_premature_eof(monkeypatch):
    stderr = (b"x" * 5000) + b"lean crashed"
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b""], stderr=stderr)
        repl = _repl_with_process(process, max_buffer_bytes=len(stderr))
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplProcessExited) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert str(error.value).endswith(stderr.decode())


def test_wire_protocol_services_stdout_while_stderr_remains_readable(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        repl = _repl_with_process(process, chunk_size=8)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        monkeypatch.setattr(repl_core.os, "read", fake_read)

        # Endlessly readable stderr never starves stdout, so the response is
        # captured. It cannot be drained to a boundary though, so the process is
        # reported as unusable rather than silently reused.
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert error.value.response == {"messages": []}


def test_wire_protocol_times_out_while_stderr_remains_readable(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [], stderr=b"x")
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x"
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        with pytest.raises(TimeoutError, match="timed out"):
            repl._run("#check Nat", env_id=None, timeout=1)


def test_wire_protocol_rejects_oversized_stderr_even_when_stdout_is_ready(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"0123456789")
        repl = _repl_with_process(process, chunk_size=10, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(RuntimeError, match="stderr exceeded 8 bytes") as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    # The diagnostic that explains the failure survives the rejection.
    assert "Tail: " in str(error.value)
    assert "0123456789" in str(error.value)


def test_wire_protocol_drains_queued_stderr_after_the_response_frame_completes(monkeypatch):
    # One stdout read completes the frame while stderr still holds several chunks.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=1024)
        _patch_pipe_reads(monkeypatch, process)

        assert repl._run("#check Nat", env_id=None, timeout=5) == {"messages": []}

        # Nothing is left to be charged against, or misattributed to, the next command.
        assert process.stderr_bytes == b""


def test_wire_protocol_reports_a_backlog_when_the_cap_ends_the_post_response_drain(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", env_id=None, timeout=5)

    # The response survives, so the caller need not recompute it...
    assert error.value.response == {"messages": []}
    # ...but this command's stderr is still in the pipe, which is exactly why the
    # process must not serve another request.
    assert process.stderr_bytes == b"e" * 4


def test_wire_protocol_keeps_the_response_when_the_deadline_ends_the_stderr_drain(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        repl = _repl_with_process(process, max_buffer_bytes=1_000_000)
        _patch_pipe_reads(monkeypatch, process)

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            if fd == process.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        # Endlessly readable stderr must not hang the post-response drain, and the
        # deadline must not discard a response that was already captured.
        with pytest.raises(repl_core.ReplStderrBacklog) as error:
            repl._run("#check Nat", env_id=None, timeout=1)

    assert error.value.response == {"messages": []}


def _patch_reads_across_processes(monkeypatch, processes: list[_PipeProcess]):
    """Serve reads and readiness for several processes, keyed by descriptor."""
    real_read = os.read

    def take_chunk(chunks: list[bytes], size: int) -> bytes:
        if not chunks:
            return b""
        result = chunks[0][:size]
        chunks[0] = chunks[0][size:]
        if not chunks[0]:
            chunks.pop(0)
        return result

    def fake_read(fd: int, size: int) -> bytes:
        for process in processes:
            if fd == process.stdout.fileno():
                return take_chunk(process.stdout_chunks, size)
            if fd == process.stderr.fileno():
                result = process.stderr_bytes[:size]
                process.stderr_bytes = process.stderr_bytes[size:]
                return result
        return real_read(fd, size)

    def fake_select(readable, writable, exceptional, timeout=None):
        if writable:
            return [], writable, []
        ready = []
        for process in processes:
            if process.stderr_bytes:
                ready.append(process.stderr.fileno())
            if process.stdout_chunks:
                ready.append(process.stdout.fileno())
        return [fd for fd in ready if fd in readable], [], []

    monkeypatch.setattr(repl_core.os, "read", fake_read)
    monkeypatch.setattr(repl_core.select, "select", fake_select)


def test_backlog_recycles_the_process_so_two_commands_cannot_share_stderr(monkeypatch):
    with ExitStack() as stack:
        first = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        second = _PipeProcess(stack, [b'{"env": 1}\n\n'])
        repl = _repl_with_process(first, chunk_size=18, max_buffer_bytes=20)
        _patch_reads_across_processes(monkeypatch, [first, second])

        # Command one completes, but its stderr cannot be drained within budget.
        assert repl.run("#check Nat", timeout=5) == {"messages": []}

        # The process holding the remainder is gone, so nothing can inherit it.
        assert repl.process is None
        assert first.stderr_bytes == b"e" * 4

        monkeypatch.setattr(repl, "restart", lambda timeout=None: setattr(repl, "process", second))

        # Command two runs on a clean process and sees only its own streams.
        assert repl.run("#check Nat", timeout=5) == {"env": 1}

        assert second.stderr_bytes == b""
        # Command one's stderr was never consumed by, or charged against, command two.
        assert first.stderr_bytes == b"e" * 4


def test_env_scoped_request_refuses_to_outlive_the_recycled_process(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        # An explicit environment cannot transparently survive the recycle, so the
        # caller is told rather than handed a response tied to a dead process.
        with pytest.raises(repl_core.ReplProcessRestarted):
            repl.run("#check Nat", env_id=7, timeout=5)

        assert repl.process is None


def test_deadline_ended_drain_recycles_the_process_so_two_commands_cannot_share_stderr(monkeypatch):
    with ExitStack() as stack:
        first = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"x")
        second = _PipeProcess(stack, [b'{"env": 1}\n\n'])
        # A ceiling far out of reach, so the deadline is what ends the drain.
        repl = _repl_with_process(first, max_buffer_bytes=1_000_000)
        _patch_reads_across_processes(monkeypatch, [first, second])

        real_read = repl_core.os.read

        def fake_read(fd: int, size: int) -> bytes:
            # first's stderr never empties, so no clean boundary is ever reached.
            if fd == first.stderr.fileno():
                return b"x" * size
            return real_read(fd, size)

        now = 0.0

        def fake_monotonic() -> float:
            nonlocal now
            now += 0.25
            return now

        monkeypatch.setattr(repl_core.os, "read", fake_read)
        monkeypatch.setattr(repl_core.time, "monotonic", fake_monotonic)

        # Command one still gets its response: the deadline must not starve stdout.
        assert repl.run("#check Nat", timeout=5) == {"messages": []}

        # But the process that still holds unread stderr is out of service.
        assert repl.process is None
        assert first.stderr_bytes

        monkeypatch.setattr(repl, "restart", lambda timeout=None: setattr(repl, "process", second))
        monkeypatch.setattr(repl_core.os, "read", real_read)

        # Command two runs on a clean process, unaffected by command one's stderr.
        assert repl.run("#check Nat", timeout=5) == {"env": 1}
        assert second.stderr_bytes == b""


def test_run_never_leaves_a_reusable_process_when_a_backlog_stops_the_drain(monkeypatch):
    # The invariant holds at the source, so no caller of _run() can skip it.
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"messages": []}\n\n'], stderr=b"e" * 40)
        repl = _repl_with_process(process, chunk_size=18, max_buffer_bytes=20)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(repl_core.ReplStderrBacklog):
            repl._run("#check Nat", env_id=None, timeout=5)

        assert repl.process is None
        assert repl.is_alive() is False


def test_wire_protocol_rejects_invalid_json(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b"not-json\n\n"])
        repl = _repl_with_process(process)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(json.JSONDecodeError):
            repl._run("#check Nat", env_id=None, timeout=1)


def test_wire_protocol_rejects_oversized_response(monkeypatch):
    with ExitStack() as stack:
        process = _PipeProcess(stack, [b'{"data":"0123456789"}\n\n'])
        repl = _repl_with_process(process, max_buffer_bytes=8)
        _patch_pipe_reads(monkeypatch, process)

        with pytest.raises(RuntimeError, match="response exceeded 8 bytes"):
            repl._run("#check Nat", env_id=None, timeout=1)
