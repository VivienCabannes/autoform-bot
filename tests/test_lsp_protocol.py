"""Protocol and lifecycle regression tests for the Lean server's LSP backend."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from servers.lsp import server as lsp


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 999_999_999
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGTERM

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def test_json_rpc_error_response_raises_protocol_error(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    monkeypatch.setattr(
        session,
        "_read_message",
        lambda timeout: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "initialization failed"},
        },
    )

    with pytest.raises(lsp.LspProtocolError, match="initialization failed"):
        session._read_response(1)


def test_start_aborts_process_when_initialize_fails(monkeypatch):
    process = _FakeProcess()
    popen_kwargs = {}
    for name in ("ELAN_TOOLCHAIN", "LEAN_PATH", "LAKE_CONFIG", "PYTHONPATH"):
        monkeypatch.setenv(name, "poisoned")

    def popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(lsp.subprocess, "Popen", popen)

    session = lsp.LeanLspSession(lsp.LspConfig())

    def fail_initialize(method, params):
        raise lsp.LspProtocolError("initialize rejected")

    monkeypatch.setattr(session, "_send_request", fail_initialize)

    with pytest.raises(lsp.LspProtocolError, match="initialize rejected"):
        session.start()

    assert process.killed is True
    assert process.waited is True
    assert session.process is None
    assert popen_kwargs["stderr"] is subprocess.DEVNULL
    assert popen_kwargs["start_new_session"] is True
    assert all(
        name not in popen_kwargs["env"]
        for name in ("ELAN_TOOLCHAIN", "LEAN_PATH", "LAKE_CONFIG", "PYTHONPATH")
    )


def test_fatal_operation_poisons_session_before_releasing_next_waiter(
    tmp_path: Path, monkeypatch
):
    first = tmp_path / "First.lean"
    second = tmp_path / "Second.lean"
    first.write_text("#check Nat\n")
    second.write_text("#check Int\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=1))
    first_entered = threading.Event()
    release_first = threading.Event()
    protocol_calls = []
    errors = []

    def get_diagnostics(file_path, *, timeout):
        protocol_calls.append(file_path)
        first_entered.set()
        assert release_first.wait(timeout=1)
        raise lsp.LspProtocolError("broken shared stream")

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_get_diagnostics", get_diagnostics)
    monkeypatch.setattr(session, "_abort_process", abort)

    def invoke(path):
        try:
            session.get_diagnostics(str(path))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=invoke, args=(first,)),
        threading.Thread(target=invoke, args=(second,)),
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()
    time.sleep(0.05)
    release_first.set()
    for thread in threads:
        thread.join(timeout=2)

    assert len(protocol_calls) == 1
    assert len(errors) == 2
    assert any("broken shared stream" in str(error) for error in errors)
    assert any("no longer usable" in str(error) for error in errors)


def test_lsp_abort_terminates_the_process_group_with_bounded_wait(tmp_path: Path):
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        "open(sys.argv[1], 'w').write(str(child.pid)); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(child_pid_file)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process

    started = time.monotonic()
    session.abort()

    assert time.monotonic() - started < 2
    assert session.process is None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("LSP descendant survived process-group abort")


def test_lsp_abort_escalates_to_kill_without_unbounded_wait(monkeypatch):
    signals = []

    class StubbornProcess:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("lake serve", timeout)

    monkeypatch.setattr(lsp.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = StubbornProcess()

    session.abort()

    assert signals == [
        (12345, signal.SIGTERM),
        (12345, signal.SIGKILL),
    ]
    assert session.process is None


def test_lsp_abort_kills_the_group_even_if_the_wrapper_exits_on_term(monkeypatch):
    signals = []

    class ExitedWrapper:
        pid = 12345

        def poll(self):
            return None

        def wait(self, timeout):
            return -signal.SIGTERM

    monkeypatch.setattr(lsp.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = ExitedWrapper()

    session.abort()

    assert signals == [
        (12345, signal.SIGTERM),
        (12345, signal.SIGKILL),
    ]


def test_lsp_abort_kills_descendant_after_group_leader_already_exited(tmp_path: Path):
    child_pid_file = tmp_path / "orphan.pid"
    script = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)']); "
        "open(sys.argv[1], 'w').write(str(child.pid))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(child_pid_file)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    assert process.wait(timeout=2) == 0

    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = process
    session._process_group_id = process.pid
    try:
        session.abort()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("LSP descendant survived abort after its wrapper exited")
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_diagnostics_wait_through_initial_quiet_period(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    uri = "file:///tmp/Test.lean"
    messages = iter(
        [
            None,
            None,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            },
            None,
        ]
    )
    monkeypatch.setattr(session, "_read_message", lambda timeout: next(messages))

    # The first two quiet reads are not evidence that the file is clean. The
    # explicit empty publication is, and should be returned as a valid result.
    assert session._collect_diagnostics(uri, timeout=60) == []


def test_diagnostics_timeout_without_publication(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    clock = {"now": 0.0}

    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])

    def quiet_read(timeout):
        clock["now"] += timeout
        return None

    monkeypatch.setattr(session, "_read_message", quiet_read)

    with pytest.raises(TimeoutError, match="waiting for diagnostics"):
        session._collect_diagnostics("file:///tmp/Test.lean", timeout=5)

    assert clock["now"] == 5


def test_malformed_diagnostics_payload_is_protocol_error(monkeypatch):
    session = lsp.LeanLspSession(lsp.LspConfig())
    uri = "file:///tmp/Test.lean"
    messages = iter(
        [
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": {"not": "a list"}},
            }
        ]
    )
    monkeypatch.setattr(session, "_read_message", lambda timeout: next(messages))

    with pytest.raises(lsp.LspProtocolError, match="diagnostics must be a list"):
        session._collect_diagnostics(uri, timeout=60)


@pytest.mark.parametrize(
    "diagnostics",
    [
        [1],
        [{"range": 1}],
        [{"range": {"start": 1}}],
        [{"range": {"start": {"line": "0"}}}],
        [{"severity": []}],
        [{"message": 1}],
    ],
)
def test_malformed_diagnostic_entry_poisons_session(
    tmp_path: Path, monkeypatch, diagnostics
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    uri = source.resolve().as_uri()
    session = lsp.LeanLspSession(lsp.LspConfig())
    messages = iter(
        [
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": diagnostics},
            }
        ]
    )

    monkeypatch.setattr(session, "_send_notification", lambda *args, **kwargs: None)
    monkeypatch.setattr(session, "_read_message", lambda timeout: next(messages))

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_abort_process", abort)

    with pytest.raises(lsp.LspProtocolError, match="publishDiagnostics"):
        session.get_diagnostics(str(source))
    assert session._poisoned is True


def test_get_diagnostics_closes_document_after_timeout(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("example : True := by trivial\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    notifications: list[str] = []

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )

    def timeout(uri, timeout):
        raise TimeoutError("no diagnostics publication")

    monkeypatch.setattr(session, "_collect_diagnostics", timeout)

    with pytest.raises(TimeoutError, match="no diagnostics publication"):
        session.get_diagnostics(str(source))

    assert notifications == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]


def test_failed_document_close_poisons_the_session(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())

    def notify(method, params, **kwargs):
        if method == "textDocument/didClose":
            raise lsp.LspProtocolError("close failed")

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_send_notification", notify)
    monkeypatch.setattr(session, "_collect_diagnostics", lambda uri, timeout: [])
    monkeypatch.setattr(session, "_abort_process", abort)

    assert session.get_diagnostics(str(source)) == []
    assert session._poisoned is True
    with pytest.raises(lsp.LspProtocolError, match="no longer usable"):
        session.get_diagnostics(str(source))


def test_exhausted_diagnostics_deadline_poisons_before_next_waiter(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=1))
    clock = {"now": 0.0}
    notifications = []

    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )

    def collect(uri, timeout):
        clock["now"] = 1.0
        return []

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_collect_diagnostics", collect)
    monkeypatch.setattr(session, "_abort_process", abort)

    assert session.get_diagnostics(str(source)) == []
    assert notifications == ["textDocument/didOpen"]
    assert session._poisoned is True
    with pytest.raises(lsp.LspProtocolError, match="no longer usable"):
        session.get_diagnostics(str(source))


def test_hover_opens_and_closes_document(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source_text = "#check Nat\n"
    source.write_text(source_text)
    session = lsp.LeanLspSession(lsp.LspConfig())
    notifications: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append((method, params)),
    )

    def send_request(method, params, timeout=30):
        assert method == "textDocument/hover"
        assert params["textDocument"]["uri"] == source.resolve().as_uri()
        return {"contents": {"kind": "plaintext", "value": "Nat : Type"}}

    monkeypatch.setattr(session, "_send_request", send_request)

    assert session.hover(str(source), 0, 7) == "Nat : Type"
    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
    ]
    assert notifications[0][1]["textDocument"]["text"] == source_text


def test_malformed_hover_result_poisons_the_session(tmp_path: Path, monkeypatch):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())

    monkeypatch.setattr(session, "_send_notification", lambda *args, **kwargs: None)
    monkeypatch.setattr(session, "_send_request", lambda *args, **kwargs: 1)

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_abort_process", abort)

    with pytest.raises(lsp.LspProtocolError, match="non-object"):
        session.hover(str(source), 0, 0)
    assert session._poisoned is True


def test_malformed_hover_contents_value_poisons_the_session(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig())

    monkeypatch.setattr(session, "_send_notification", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        session,
        "_send_request",
        lambda *args, **kwargs: {"contents": {"value": 7}},
    )

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_abort_process", abort)

    with pytest.raises(lsp.LspProtocolError, match="contents.value"):
        session.hover(str(source), 0, 0)
    assert session._poisoned is True


def test_exhausted_hover_deadline_poisons_before_next_waiter(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "Test.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=1))
    clock = {"now": 0.0}
    notifications = []

    monkeypatch.setattr(lsp.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: notifications.append(method),
    )

    def request(method, params, timeout):
        clock["now"] = 1.0
        return {"contents": "Nat : Type"}

    def abort():
        session._poisoned = True
        session.process = None

    monkeypatch.setattr(session, "_send_request", request)
    monkeypatch.setattr(session, "_abort_process", abort)

    assert session.hover(str(source), 0, 0) == "Nat : Type"
    assert notifications == ["textDocument/didOpen"]
    assert session._poisoned is True
    with pytest.raises(lsp.LspProtocolError, match="no longer usable"):
        session.hover(str(source), 0, 0)


def test_document_operations_are_serialized_per_session(tmp_path: Path, monkeypatch):
    first = tmp_path / "First.lean"
    second = tmp_path / "Second.lean"
    first.write_text("#check Nat\n")
    second.write_text("#check Int\n")
    session = lsp.LeanLspSession(lsp.LspConfig())
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    monkeypatch.setattr(
        session,
        "_send_notification",
        lambda method, params, **kwargs: None,
    )

    def collect(uri, timeout):
        nonlocal calls
        with calls_lock:
            calls += 1
            number = calls
        if number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        return []

    monkeypatch.setattr(session, "_collect_diagnostics", collect)
    threads = [
        threading.Thread(target=session.get_diagnostics, args=(str(first),)),
        threading.Thread(target=session.get_diagnostics, args=(str(second),)),
    ]
    threads[0].start()
    assert first_entered.wait(timeout=1)
    threads[1].start()

    # The second call cannot begin reading the shared stdout stream until the
    # first document's complete didOpen/diagnostics/didClose lifecycle finishes.
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    for thread in threads:
        thread.join(timeout=2)

    assert second_entered.is_set()
    assert all(not thread.is_alive() for thread in threads)


def test_lsp_queue_wait_is_bounded_by_session_timeout(tmp_path: Path):
    source = tmp_path / "Queued.lean"
    source.write_text("#check Nat\n")
    session = lsp.LeanLspSession(lsp.LspConfig(timeout=0.01))
    session._operation_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="waiting for the Lean LSP session"):
            session.hover(str(source), 0, 0)
    finally:
        session._operation_lock.release()


@pytest.mark.parametrize(
    "partial",
    [
        b"Content-Length: 10\r\n",
        b"Content-Length: 10\r\n\r\n{}",
    ],
)
def test_partial_lsp_frame_cannot_block_past_deadline(partial):
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = SimpleNamespace(stdout=reader)
    try:
        os.write(write_fd, partial)
        with pytest.raises(TimeoutError, match="reading an LSP"):
            session._read_message(timeout=0.01)
    finally:
        os.close(write_fd)
        reader.close()


def test_lsp_write_cannot_block_on_a_full_pipe():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    while True:
        try:
            os.write(write_fd, b"x" * 4096)
        except BlockingIOError:
            break

    writer = os.fdopen(write_fd, "wb", buffering=0)
    session = lsp.LeanLspSession(lsp.LspConfig())
    session.process = SimpleNamespace(stdin=writer)
    try:
        with pytest.raises(TimeoutError, match="writing an LSP message"):
            session._write_message({"jsonrpc": "2.0"}, timeout=0.01)
    finally:
        writer.close()
        os.close(read_fd)
