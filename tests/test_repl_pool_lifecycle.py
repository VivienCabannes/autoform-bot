"""Lifecycle regression tests for transactional REPL pool startup."""

from __future__ import annotations

import os
import threading

import pytest

from servers.repl import core as repl_core
from servers.repl import pool as repl_pool


def test_pool_construction_keeps_every_worker_cold(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.started = False
            self.closed = False
            workers.append(self)

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    config = repl_pool.LeanReplPoolConfig(num_repls=3, startup_stagger=0)

    pool = repl_pool.LeanReplPool(config)

    assert len(workers) == 3
    assert all(worker.started is False for worker in workers)
    assert all(worker.closed is False for worker in workers)
    assert pool._idle.qsize() == 3
    pool.shutdown()


def test_shutdown_closes_every_worker_and_drains_idle_queue(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.closed = False
            workers.append(self)

        def start(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=2, startup_stagger=0)
    )

    pool.shutdown()

    assert all(worker.closed for worker in workers)
    assert pool._workers == []
    assert pool._idle.empty()


def test_shutdown_shares_one_cleanup_deadline_across_workers(monkeypatch):
    deadlines = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def close(self):
            pytest.fail("deadline-aware cleanup should be used")

        def close_with_deadline(self, deadline):
            deadlines.append(deadline)

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=3, startup_stagger=0)
    )

    pool.shutdown()

    assert len(deadlines) == 3
    assert len(set(deadlines)) == 1


def test_request_timeout_includes_waiting_for_an_idle_worker(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    borrowed = pool._idle.get_nowait()
    try:
        with pytest.raises(TimeoutError, match="waiting for an idle Lean REPL"):
            pool.run("#check Nat", timeout=0.01)
    finally:
        pool._idle.put(borrowed)
        pool.shutdown()


def test_pool_closes_a_worker_before_requeue_after_request_exception(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            raise OSError("stdout failed")

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    try:
        with pytest.raises(OSError, match="stdout failed"):
            pool.run("#check Nat")

        assert workers[0].close_calls == 1
        assert pool._idle.qsize() == 1
    finally:
        pool.shutdown()


def test_pool_closes_a_worker_before_requeue_after_success(monkeypatch):
    events = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            events.append(("run", code, kwargs))
            return {"messages": []}

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        assert pool.run("#check Nat") == {"messages": []}
        assert pool._idle.qsize() == 1
    finally:
        pool.shutdown()

    assert events[:2] == [("run", "#check Nat", {}), ("close",)]


def test_pool_reserves_cleanup_time_after_request_deadline(monkeypatch):
    now = [100.0]
    close_deadlines = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            now[0] = 102.999
            return {"messages": []}

        def close_with_deadline(self, deadline):
            close_deadlines.append(deadline)

    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        assert pool.run("#check Nat", timeout=3) == {"messages": []}
        assert close_deadlines == [
            102.999 + repl_pool.DEFAULT_POOL_CLEANUP_SECONDS
        ]
    finally:
        pool.shutdown()


def test_pool_forwards_absolute_deadline_without_resetting_it(monkeypatch):
    now = [100.0]
    observed = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            now[0] = 104.0
            observed.append(kwargs)
            return {"messages": []}

        def close(self):
            pass

    monkeypatch.setattr(repl_pool.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        assert pool.run("#check Nat", deadline=105.0) == {"messages": []}
    finally:
        pool.shutdown()

    assert observed == [{"deadline": 105.0}]


def test_pool_never_requeues_a_worker_that_failed_to_close(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            raise repl_core.ReplCleanupError(
                "first cleanup failed",
                {"messages": []},
            )

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("cleanup failed")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    response = pool.run("#check Nat")

    assert workers[0].close_calls == 1
    assert response["outcome_unknown"] is True
    assert "must not be replayed" in response["repl_error"]
    assert pool._idle.empty()
    assert pool._active_calls == 0
    assert pool._shutdown is True
    with pytest.raises(RuntimeError, match="pool is shut down"):
        pool.run("#check Bool")
    pool.shutdown()


def test_pool_returns_captured_response_after_cleanup_retry_succeeds(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.dirty = True
            self.close_calls = 0
            workers.append(self)

        def run_disposable(self, code, **kwargs):
            return {"messages": []}

        def close(self):
            self.close_calls += 1
            self.dirty = False

        def is_clean(self):
            return not self.dirty

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    try:
        assert pool.run("#check Nat") == {"messages": []}
        assert workers[0].close_calls == 1
        assert pool._idle.qsize() == 1
        assert pool.is_usable() is True
    finally:
        pool.shutdown()


def test_pool_preserves_cancellation_when_cleanup_also_fails(monkeypatch):
    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            raise KeyboardInterrupt

        def close(self):
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        pool.run("#check Nat")

    if hasattr(raised.value, "add_note"):
        assert raised.value.__notes__ == [
            "Lean REPL process cleanup also failed: cleanup failed"
        ]
    assert pool._shutdown is True
    assert pool._idle.empty()


def test_shutdown_retains_and_retries_a_worker_that_failed_to_close(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.close_calls = 0
            workers.append(self)

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("cleanup failed")

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        pool.shutdown()

    assert pool._workers == workers
    assert pool._closed is False
    assert pool._idle.empty()

    pool.shutdown()

    assert workers[0].close_calls == 2
    assert pool._workers == []
    assert pool._closed is True


def test_shutdown_never_requeues_a_borrowed_worker(monkeypatch):
    running = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()
    calls = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def run_disposable(self, code, **kwargs):
            calls.append(code)
            running.set()
            release.wait(timeout=2)
            return {"messages": []}

        def close(self):
            release.set()

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )
    first = threading.Thread(target=pool.run, args=("first",))
    first.start()
    assert running.wait(timeout=1)

    errors = []

    def wait_for_worker():
        try:
            pool.run("second", timeout=0.1)
        except (RuntimeError, TimeoutError) as error:
            errors.append(error)

    second = threading.Thread(target=wait_for_worker)
    second.start()

    def shut_down():
        pool.shutdown()
        shutdown_done.set()

    shutdown = threading.Thread(target=shut_down)
    shutdown.start()
    with pool._condition:
        assert pool._condition.wait_for(lambda: pool._shutdown, timeout=1)
    assert not shutdown_done.is_set()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    shutdown.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not shutdown.is_alive()
    assert shutdown_done.is_set()
    assert calls == ["first"]
    assert len(errors) == 1
    assert pool._idle.empty()


def test_concurrent_shutdown_closes_each_worker_once(monkeypatch):
    close_started = threading.Event()
    release_close = threading.Event()
    second_started = threading.Event()
    close_calls = []

    class FakeRepl:
        def __init__(self, config):
            pass

        def close(self):
            close_calls.append(self)
            close_started.set()
            release_close.wait(timeout=2)

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    pool = repl_pool.LeanReplPool(
        repl_pool.LeanReplPoolConfig(num_repls=1, startup_stagger=0)
    )

    first = threading.Thread(target=pool.shutdown)

    def shut_down_second():
        second_started.set()
        pool.shutdown()

    second = threading.Thread(target=shut_down_second)
    first.start()
    assert close_started.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    release_close.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(close_calls) == 1


def test_repl_retry_recovery_uses_the_original_deadline(monkeypatch):
    clock = {"now": 0.0}
    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            max_retries=1,
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    calls = []
    closed = []

    monkeypatch.setattr(repl_core.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(repl, "is_alive", lambda: True)
    monkeypatch.setattr(repl, "_check_memory_and_maybe_restart", lambda timeout: None)
    monkeypatch.setattr(repl, "close", lambda **kwargs: closed.append(True))

    def consume_deadline(code, env_id, timeout):
        calls.append(timeout)
        clock["now"] += timeout
        raise TimeoutError("ambiguous timeout")

    monkeypatch.setattr(repl, "_run", consume_deadline)
    response = repl.run("#check Nat", timeout=1)

    assert calls == [1]
    assert closed == [True]
    assert "timed out" in response["repl_error"]


def test_repl_request_write_uses_the_operation_deadline():
    read_fd, write_fd = os.pipe()
    stdout_read_fd, stdout_write_fd = os.pipe()
    stderr_read_fd, stderr_write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    while True:
        try:
            os.write(write_fd, b"x" * 65536)
        except BlockingIOError:
            break

    stdin = os.fdopen(write_fd, "wb", buffering=0)

    class StalledProcess:
        stdout = None
        stderr = None

        def poll(self):
            return None

    process = StalledProcess()
    process.stdin = stdin
    process.stdout = os.fdopen(stdout_read_fd, "rb", buffering=0)
    process.stderr = os.fdopen(stderr_read_fd, "rb", buffering=0)

    repl = repl_core.LeanRepl(
        repl_core.LeanReplConfig(
            validate_imports=False,
            warmup_imports=frozenset(),
        )
    )
    repl.process = process
    try:
        with pytest.raises(TimeoutError, match="while writing"):
            repl._run("#check Nat", env_id=None, timeout=0.02)
    finally:
        stdin.close()
        process.stdout.close()
        process.stderr.close()
        os.close(read_fd)
        os.close(stdout_write_fd)
        os.close(stderr_write_fd)
