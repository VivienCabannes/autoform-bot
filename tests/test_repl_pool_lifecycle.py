"""Lifecycle regression tests for transactional REPL pool startup."""

from __future__ import annotations

import pytest

from servers.repl import pool as repl_pool


def test_partial_pool_startup_closes_all_constructed_workers(monkeypatch):
    workers = []

    class FakeRepl:
        def __init__(self, config):
            self.number = len(workers) + 1
            self.started = False
            self.closed = False
            workers.append(self)

        def start(self):
            self.started = True
            if self.number == 2:
                raise RuntimeError("second worker failed")

        def close(self):
            self.closed = True

    monkeypatch.setattr(repl_pool, "LeanRepl", FakeRepl)
    config = repl_pool.LeanReplPoolConfig(num_repls=3, startup_stagger=0)

    with pytest.raises(RuntimeError, match="second worker failed"):
        repl_pool.LeanReplPool(config)

    assert len(workers) == 2
    assert workers[0].started is True
    assert workers[0].closed is True
    assert workers[1].closed is True


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
