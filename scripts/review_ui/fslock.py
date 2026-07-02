#!/usr/bin/env python3
"""Cross-process file locking for the shared review/dispatch JSON files.

``task_queue.json`` and ``review_status.json`` have TWO writers: the dashboard
(``serve_review.py``) and the engine (``dispatch_runner.py`` / ``dispatch_queue.py``),
each doing load → mutate → save. An atomic save alone only prevents *torn* files —
it cannot stop one process's save from silently erasing the other's just-landed
mutation (e.g. a human verdict overwritten by the runner's re-save of a stale
sidecar). So every load-mutate-save of a shared file, in every process, must run
under the SAME cross-process lock.

``locked(path)`` takes an exclusive ``fcntl.flock`` on a sidecar ``<name>.lock``
file next to ``path`` (never on ``path`` itself — the atomic ``os.replace`` swaps
the inode out from under any lock held on it). flock is advisory, per open file
description: it excludes other processes AND other threads of the same process
(each ``locked()`` opens its own fd), and it is released automatically by the OS
if the holder dies — a crashed engine can never leave the dashboard wedged.

Usage::

    with fslock.locked(queue_path):
        queue = load(queue_path)
        ...mutate...
        save(queue_path, queue)     # atomic write, still required

Do not nest ``locked()`` on the same path in one thread (flock does not reenter
across fds — the inner acquire would deadlock).
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path):
    """Hold an exclusive cross-process lock scoped to ``path`` for the block.

    The lock lives on ``<path>.lock`` beside the target (created if absent, never
    deleted — deleting a lock file while another process holds/opens it reopens
    the race the lock exists to close). Blocks until acquired.
    """
    p = Path(path)
    lock_path = p.with_name(p.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
