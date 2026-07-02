"""Tests for the shared cross-process lock (scripts/review_ui/fslock.py).

The dashboard and the dispatch engine both load-mutate-save ``task_queue.json``
and ``review_status.json``; every such cycle must run under ``fslock.locked``.
The lock is an exclusive ``fcntl.flock`` on a sidecar ``<name>.lock`` file, per
open file description — so it excludes concurrent threads (each ``locked()``
opens its own fd) and concurrent processes alike.
"""
import json
import multiprocessing
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import fslock  # noqa: E402


def _bump(path, rounds):
    """One writer: `rounds` load-mutate-save increments, each under the lock."""
    for _ in range(rounds):
        with fslock.locked(path):
            n = json.loads(path.read_text())["n"]
            path.write_text(json.dumps({"n": n + 1}))


def test_locked_creates_sidecar_lock_file(tmp_path):
    target = tmp_path / "task_queue.json"
    target.write_text(json.dumps({"n": 0}))
    with fslock.locked(target):
        pass
    assert (tmp_path / "task_queue.json.lock").exists()
    # the target itself is untouched by the lock
    assert json.loads(target.read_text()) == {"n": 0}


def test_locked_serializes_threads_no_lost_update(tmp_path):
    # Two "writers" racing load-mutate-save: without the lock, increments are
    # lost (both read n, both write n+1). Under the lock, every update lands.
    target = tmp_path / "review_status.json"
    target.write_text(json.dumps({"n": 0}))
    rounds = 50
    threads = [threading.Thread(target=_bump, args=(target, rounds))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(target.read_text())["n"] == 2 * rounds


def _proc_bump(path_str, rounds):
    _bump(Path(path_str), rounds)


def test_locked_serializes_processes_no_lost_update(tmp_path):
    # The real deployment shape: two PROCESSES (dashboard + engine) sharing the
    # file. flock is kernel-side, so the same guarantee must hold across fork.
    target = tmp_path / "task_queue.json"
    target.write_text(json.dumps({"n": 0}))
    rounds = 25
    procs = [multiprocessing.Process(target=_proc_bump,
                                     args=(str(target), rounds))
             for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0
    assert json.loads(target.read_text())["n"] == 2 * rounds


def test_locked_releases_on_exception(tmp_path):
    target = tmp_path / "task_queue.json"
    target.write_text("[]")
    try:
        with fslock.locked(target):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    # the lock must be free again — a second acquire returns immediately
    with fslock.locked(target):
        pass


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
