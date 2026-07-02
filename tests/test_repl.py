"""Behavioral tests for the pooled Lean REPL server.

No real ``lake exe repl`` is spawned: LeanRepl is exercised with fake
processes backed by OS pipes, and LeanReplPool with a FakeRepl class.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from servers.repl.core import (
    LeanRepl,
    LeanReplConfig,
    ReplProcessRestarted,
    _adjust_line_numbers,
    _append_capped,
    _split_imports_and_body,
    format_repl_response,
)
from servers.repl.pool import LeanReplPool, LeanReplPoolConfig


# ---------------------------------------------------------------------------
# _split_imports_and_body
# ---------------------------------------------------------------------------


class TestSplitImportsAndBody:
    def test_plain_import(self):
        imports, body, header = _split_imports_and_body("import Mathlib\n\ntheorem t : True := trivial")
        assert imports == ["Mathlib"]
        assert body == "theorem t : True := trivial"
        assert header == 2

    def test_multiple_imports(self):
        code = "import Mathlib\nimport Aesop\ndef x := 1"
        imports, body, header = _split_imports_and_body(code)
        assert imports == ["Mathlib", "Aesop"]
        assert body == "def x := 1"
        assert header == 2

    def test_block_comment_before_import(self):
        code = "/- copyright -/\nimport Mathlib\ndef x := 1"
        imports, body, header = _split_imports_and_body(code)
        assert imports == ["Mathlib"]
        assert body == "def x := 1"
        assert header == 2

    def test_multiline_block_comment_before_import(self):
        code = "/-\nCopyright (c) 2026.\nReleased under Apache 2.0.\n-/\nimport Mathlib\ndef x := 1"
        imports, body, header = _split_imports_and_body(code)
        assert imports == ["Mathlib"]
        assert body == "def x := 1"
        assert header == 5

    def test_line_comment_before_import(self):
        code = "-- header\nimport Mathlib\ndef x := 1"
        imports, body, header = _split_imports_and_body(code)
        assert imports == ["Mathlib"]
        assert body == "def x := 1"
        assert header == 2

    def test_no_imports(self):
        code = "theorem t : True := trivial"
        imports, body, header = _split_imports_and_body(code)
        assert imports == []
        assert body == code
        assert header == 0

    def test_import_inside_string_stays_in_body(self):
        code = 'def s := "\nimport Fake\n"'
        imports, body, header = _split_imports_and_body(code)
        assert imports == []
        assert body == code
        assert header == 0

    def test_doc_comment_stays_in_body(self):
        code = "import Mathlib\n/-- doc for foo -/\ndef foo := 1"
        imports, body, header = _split_imports_and_body(code)
        assert imports == ["Mathlib"]
        assert body == "/-- doc for foo -/\ndef foo := 1"
        assert header == 1

    def test_submodule_import(self):
        imports, _, _ = _split_imports_and_body("import Mathlib.Data.Nat.Basic\n#check Nat")
        assert imports == ["Mathlib.Data.Nat.Basic"]


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


class TestAdjustLineNumbers:
    def test_offsets_messages_and_sorries(self):
        resp = {
            "messages": [
                {"pos": {"line": 1, "column": 0}, "endPos": {"line": 2, "column": 5}, "severity": "error"},
            ],
            "sorries": [{"pos": {"line": 3}, "endPos": {"line": 3}, "goal": "True"}],
        }
        _adjust_line_numbers(resp, 2)
        assert resp["messages"][0]["pos"]["line"] == 3
        assert resp["messages"][0]["endPos"]["line"] == 4
        assert resp["sorries"][0]["pos"]["line"] == 5
        assert resp["sorries"][0]["endPos"]["line"] == 5

    def test_zero_offset_is_noop(self):
        resp = {"messages": [{"pos": {"line": 1}, "severity": "error"}]}
        _adjust_line_numbers(resp, 0)
        assert resp["messages"][0]["pos"]["line"] == 1


class TestFormatReplResponse:
    def test_repl_error(self):
        assert format_repl_response({"repl_error": "boom"}) == "REPL error: boom"

    def test_success_no_messages(self):
        assert format_repl_response({"env": 1}) == "Compiles successfully"

    def test_errors(self):
        resp = {
            "messages": [
                {"severity": "error", "data": "unknown identifier", "pos": {"line": 3, "column": 5}},
            ]
        }
        out = format_repl_response(resp)
        assert "Compilation Errors (1)" in out
        assert "3:5: error: unknown identifier" in out

    def test_sorries(self):
        resp = {"sorries": [{"pos": {"line": 2}, "goal": "⊢ True"}]}
        out = format_repl_response(resp)
        assert "Compiles successfully" in out
        assert "Sorries (1)" in out
        assert "Line 2: ⊢ True" in out

    def test_warnings_truncated(self):
        resp = {
            "messages": [{"severity": "warning", "data": f"w{i}", "pos": {"line": i, "column": 0}} for i in range(12)]
        }
        out = format_repl_response(resp)
        assert "Warnings (12)" in out
        assert "... and 2 more" in out


class TestAppendCapped:
    def test_below_cap(self):
        assert _append_capped("ab", "cd", 10) == "abcd"

    def test_keeps_tail_when_over_cap(self):
        assert _append_capped("abcdef", "ghij", 4) == "ghij"


# ---------------------------------------------------------------------------
# LeanRepl — fake processes, no subprocess
# ---------------------------------------------------------------------------


class _PipeProcess:
    """Fake Popen backed by real OS pipes, for driving LeanRepl._run."""

    def __init__(self, stdout_bytes: bytes = b"", broken_stdin: bool = False):
        stdin_r, stdin_w = os.pipe()
        if broken_stdin:
            os.close(stdin_r)  # writes to stdin now raise BrokenPipeError
            self._stdin_r = None
        else:
            self._stdin_r = stdin_r
        self.stdin = os.fdopen(stdin_w, "wb")

        stdout_r, stdout_w = os.pipe()
        if stdout_bytes:
            os.write(stdout_w, stdout_bytes)
        os.close(stdout_w)
        self.stdout = os.fdopen(stdout_r, "rb")

        stderr_r, self._stderr_w = os.pipe()
        self.stderr = os.fdopen(stderr_r, "rb")

        self.pid = -1

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def cleanup(self):
        for f in (self.stdin, self.stdout, self.stderr):
            try:
                f.close()
            except OSError:
                pass
        for fd in (self._stdin_r, self._stderr_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _repl_config(**kwargs) -> LeanReplConfig:
    defaults = dict(instance_mem_limit_gb=0, max_retries=0, request_timeout=5.0)
    defaults.update(kwargs)
    return LeanReplConfig(**defaults)


class TestLeanRepl:
    def test_run_happy_path_adjusts_lines(self):
        repl = LeanRepl(_repl_config())
        response = {"env": 1, "messages": [{"severity": "error", "data": "oops", "pos": {"line": 1, "column": 0}}]}
        proc = _PipeProcess(stdout_bytes=json.dumps(response).encode() + b"\n\n")
        repl.process = proc
        try:
            resp = repl.run("import Mathlib\n\ntheorem t : False := oops")
            assert resp["env"] == 1
            # error at body line 1 maps back to source line 3 (two header lines)
            assert resp["messages"][0]["pos"]["line"] == 3
        finally:
            proc.cleanup()

    def test_run_rejects_disallowed_import(self):
        repl = LeanRepl(_repl_config())
        resp = repl.run("import Paperproof\n#check Nat")
        assert "Disallowed imports: Paperproof" in resp["repl_error"]

    def test_run_rejects_allowed_but_unavailable_import(self):
        config = _repl_config(allowed_imports=frozenset({"Mathlib", "MyLib"}))
        repl = LeanRepl(config)
        resp = repl.run("import MyLib\n#check Nat")
        assert "Imports not available in the preloaded environment: MyLib" in resp["repl_error"]

    def test_run_allows_imports_provided_by_warmup_env(self, monkeypatch):
        repl = LeanRepl(_repl_config())
        seen = {}

        def fake_run(code, env_id, timeout):
            seen["code"] = code
            return {"env": 1}

        monkeypatch.setattr(repl, "_run", fake_run)
        repl.process = None  # _run is faked; no process needed
        resp = repl.run("import Mathlib\nimport Aesop\nimport Batteries\n#check Nat")
        assert resp == {"env": 1}
        assert seen["code"] == "#check Nat"

    def test_broken_pipe_on_write_is_handled_and_restarts(self, monkeypatch):
        """A process death between poll() and stdin.write() raises
        BrokenPipeError; it must hit the restart path, not escape run()."""
        repl = LeanRepl(_repl_config())
        proc = _PipeProcess(broken_stdin=True)
        repl.process = proc
        restarts = []
        monkeypatch.setattr(repl, "restart", lambda: restarts.append(1))
        try:
            resp = repl.run("#check Nat")
            assert "repl_error" in resp
            assert restarts == [1]
        finally:
            proc.cleanup()

    def test_oserror_from_run_is_handled(self, monkeypatch):
        repl = LeanRepl(_repl_config())
        restarts = []
        monkeypatch.setattr(repl, "restart", lambda: restarts.append(1))
        monkeypatch.setattr(repl, "_run", lambda code, env_id, timeout: (_ for _ in ()).throw(BrokenPipeError("gone")))
        resp = repl.run("#check Nat")
        assert "repl_error" in resp
        assert restarts == [1]

    def test_env_id_run_surfaces_restart(self, monkeypatch):
        repl = LeanRepl(_repl_config(max_retries=3))
        restarts = []
        monkeypatch.setattr(repl, "restart", lambda: restarts.append(1))
        monkeypatch.setattr(repl, "_run", lambda code, env_id, timeout: (_ for _ in ()).throw(BrokenPipeError("gone")))
        with pytest.raises(ReplProcessRestarted):
            repl.run("#check Nat", env_id=7)
        assert restarts == [1]  # restarted once, no retry with the stale env_id

    def test_run_after_shutdown_refuses(self, monkeypatch):
        repl = LeanRepl(_repl_config())
        monkeypatch.setattr(repl, "_run", lambda code, env_id, timeout: pytest.fail("_run should not be called"))
        repl.shutdown()
        resp = repl.run("#check Nat")
        assert resp == {"repl_error": "REPL is shut down."}

    def test_shutdown_during_run_prevents_restart(self, monkeypatch):
        repl = LeanRepl(_repl_config(max_retries=3))
        restarts = []
        monkeypatch.setattr(repl, "restart", lambda: restarts.append(1))

        def dying_run(code, env_id, timeout):
            repl._shutdown = True  # simulate pool.shutdown() landing mid-run
            raise BrokenPipeError("gone")

        monkeypatch.setattr(repl, "_run", dying_run)
        resp = repl.run("#check Nat")
        assert "repl_error" in resp
        assert restarts == []

    def test_restart_is_noop_after_shutdown(self, monkeypatch):
        repl = LeanRepl(_repl_config())
        monkeypatch.setattr(repl, "start", lambda: pytest.fail("start should not be called"))
        repl._shutdown = True
        repl.restart()


# ---------------------------------------------------------------------------
# LeanReplPool — FakeRepl workers
# ---------------------------------------------------------------------------


class FakeRepl:
    """In-memory stand-in for LeanRepl."""

    def __init__(self, name="fake", fail_start=False, run_results=None, start_gate=None):
        self.name = name
        self.fail_start = fail_start
        self.start_gate = start_gate
        self.started = False
        self.shut_down = False
        self.run_calls: list[tuple[str, dict]] = []
        self._results = list(run_results or [])

    def start(self):
        if self.start_gate is not None:
            self.start_gate.wait(timeout=5)
        if self.fail_start:
            raise RuntimeError("fake start failure")
        self.started = True

    def run(self, code, **kwargs):
        self.run_calls.append((code, kwargs))
        if self._results:
            result = self._results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {"env": 0, "worker": self.name}

    def shutdown(self):
        self.shut_down = True

    def close(self):
        self.shut_down = True

    def get_memory_usage(self):
        return 0.0


class FakeReplPool(LeanReplPool):
    """Pool whose _make_repl hands out pre-built fakes (or raises on None)."""

    def __init__(self, config, repls):
        self._fakes = list(repls)
        super().__init__(config)

    def _make_repl(self):
        fake = self._fakes.pop(0)
        if fake is None:
            raise RuntimeError("factory exploded")
        return fake


def _pool_config(num_repls=2, warmup_wait=0.1) -> LeanReplPoolConfig:
    return LeanReplPoolConfig(
        num_repls=num_repls,
        startup_stagger=0.0,
        warmup_wait=warmup_wait,
        instance_mem_limit_gb=0,
        max_retries=0,
    )


class TestLeanReplPool:
    def test_start_makes_workers_ready(self):
        pool = FakeReplPool(_pool_config(), [FakeRepl("a"), FakeRepl("b")])
        pool.start()
        status = pool.status()
        assert status["ready"] == 2
        assert status["starting"] == 0
        assert status["failed"] == 0
        assert status["warming"] is False
        assert status["idle_workers"] == 2
        assert status["pending_requests"] == 0

    def test_run_returns_worker_to_queue(self):
        pool = FakeReplPool(_pool_config(), [FakeRepl("a"), FakeRepl("b")])
        pool.start()
        for _ in range(3):
            resp = pool.run("#check Nat")
            assert resp["worker"] in {"a", "b"}
            assert pool._idle.qsize() == 2

    def test_retry_on_restart_without_env_id(self):
        worker = FakeRepl(run_results=[ReplProcessRestarted("died"), {"env": 0, "retried": True}])
        pool = FakeReplPool(_pool_config(num_repls=1), [worker])
        pool.start()
        resp = pool.run("#check Nat")
        assert resp["retried"] is True
        assert len(worker.run_calls) == 2

    def test_no_retry_with_env_id(self):
        worker = FakeRepl(run_results=[ReplProcessRestarted("died")])
        pool = FakeReplPool(_pool_config(num_repls=1), [worker])
        pool.start()
        with pytest.raises(ReplProcessRestarted):
            pool.run("#check Nat", env_id=3)
        assert len(worker.run_calls) == 1
        assert pool._idle.qsize() == 1  # worker still returned to the queue

    def test_run_before_warmup_returns_friendly_message(self):
        pool = FakeReplPool(_pool_config(), [FakeRepl("a"), FakeRepl("b")])
        resp = pool.run("#check Nat")
        assert resp["repl_error"] == "REPL pool still warming up (0/2 ready) — retry shortly."

    def test_status_while_warming(self):
        pool = FakeReplPool(_pool_config(), [FakeRepl("a"), FakeRepl("b")])
        status = pool.status()
        assert status["ready"] == 0
        assert status["starting"] == 2
        assert status["warming"] is True

    def test_background_start_makes_workers_available_incrementally(self):
        gate = threading.Event()
        pool = FakeReplPool(_pool_config(), [FakeRepl("a"), FakeRepl("b", start_gate=gate)])
        thread = pool.start_background()

        deadline = time.monotonic() + 5
        while pool.status()["ready"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        status = pool.status()
        assert status["ready"] == 1
        assert status["starting"] == 1
        assert status["warming"] is True
        assert pool.run("#check Nat")["worker"] == "a"

        gate.set()
        thread.join(timeout=5)
        status = pool.status()
        assert status["ready"] == 2
        assert status["warming"] is False

    def test_worker_start_failure_is_skipped(self):
        pool = FakeReplPool(_pool_config(), [FakeRepl("a", fail_start=True), FakeRepl("b")])
        pool.start()
        status = pool.status()
        assert status["ready"] == 1
        assert status["failed"] == 1
        assert pool.run("#check Nat")["worker"] == "b"

    def test_all_workers_failed_message(self):
        pool = FakeReplPool(_pool_config(), [FakeRepl(fail_start=True), FakeRepl(fail_start=True)])
        pool.start()
        resp = pool.run("#check Nat")
        assert resp["repl_error"] == "No REPL workers available (2/2 failed to start)."

    def test_factory_failure_shuts_down_started_workers(self):
        worker = FakeRepl("a")
        pool = FakeReplPool(_pool_config(), [worker, None])
        with pytest.raises(RuntimeError, match="factory exploded"):
            pool.start()
        assert worker.shut_down is True
        assert pool._shutdown is True

    def test_shutdown_stops_new_work(self):
        workers = [FakeRepl("a"), FakeRepl("b")]
        pool = FakeReplPool(_pool_config(), list(workers))
        pool.start()
        pool.shutdown()
        resp = pool.run("#check Nat")
        assert resp["repl_error"] == "REPL pool is shut down."
        assert all(w.shut_down for w in workers)
