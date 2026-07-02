"""Lean REPL pool — thread pool of REPL instances with queue-based load balancing."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .core import LeanRepl, LeanReplConfig, ReplProcessRestarted

logger = getLogger(__name__)

DEFAULT_RAM_FRACTION = 0.5
DEFAULT_STARTUP_STAGGER_SECONDS = 2.0
DEFAULT_WARMUP_WAIT_SECONDS = 15.0


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of Lean REPL instances."""

    num_repls: int | None = None
    startup_stagger: float = DEFAULT_STARTUP_STAGGER_SECONDS
    warmup_wait: float = DEFAULT_WARMUP_WAIT_SECONDS

    def __post_init__(self) -> None:
        if self.num_repls is None:
            try:
                import psutil

                total_gb = psutil.virtual_memory().total / (1024**3)
                self.num_repls = max(1, int(total_gb * DEFAULT_RAM_FRACTION / self.instance_mem_limit_gb))
            except ImportError:
                self.num_repls = 1


class LeanReplPool:
    """Pool of Lean REPL instances with queue-based load balancing.

    Each worker owns its own LeanRepl subprocess. Tasks are distributed
    to idle workers via a FIFO queue.

    Constructing the pool does not start any REPL: each one preloads
    Mathlib, which can take minutes. Call ``start()`` to warm the pool
    synchronously, or ``start_background()`` to warm it in a daemon
    thread — workers become available as they finish starting.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        self.config = config
        self.capacity = config.num_repls or 1
        self._shutdown = False

        self._workers: list[LeanRepl] = []
        self._idle: queue.Queue[LeanRepl] = queue.Queue()
        self._lock = threading.Lock()

        self._ready = 0
        self._failed = 0
        self._pending = 0
        self._any_ready = threading.Event()
        self._warm_done = threading.Event()
        self._warm_thread: threading.Thread | None = None

    # -- startup ------------------------------------------------------

    def _make_repl(self) -> LeanRepl:
        """Create one worker. Overridable hook for tests."""
        return LeanRepl(self.config)

    def start(self) -> None:
        """Start all workers, blocking until each has finished starting.

        A worker that fails to start is logged and skipped; the pool
        continues with the others. If startup fails as a whole (an error
        outside the per-worker handling), any already-started workers are
        shut down before the exception propagates.
        """
        try:
            for i in range(self.capacity):
                if self._shutdown:
                    break
                if i > 0 and self.config.startup_stagger > 0:
                    time.sleep(self.config.startup_stagger)
                repl = self._make_repl()
                try:
                    repl.start()
                except Exception:
                    logger.exception("REPL worker %d/%d failed to start; continuing", i + 1, self.capacity)
                    with self._lock:
                        self._failed += 1
                    continue
                with self._lock:
                    if self._shutdown:
                        repl.shutdown()
                        break
                    self._workers.append(repl)
                    self._ready += 1
                self._idle.put(repl)
                self._any_ready.set()
        except BaseException:
            self.shutdown()
            raise
        finally:
            self._warm_done.set()

    def start_background(self) -> threading.Thread:
        """Warm the pool in a daemon thread and return the thread."""
        thread = threading.Thread(target=self._start_logged, name="lean-repl-pool-warmup", daemon=True)
        self._warm_thread = thread
        thread.start()
        return thread

    def _start_logged(self) -> None:
        try:
            self.start()
        except Exception:
            logger.exception("REPL pool warm-up failed")

    # -- execution ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Snapshot of pool state: ready/starting/failed counts and queue depth."""
        with self._lock:
            ready = self._ready
            failed = self._failed
            pending = self._pending
        warming = not self._warm_done.is_set()
        starting = max(0, self.capacity - ready - failed) if warming else 0
        return {
            "capacity": self.capacity,
            "ready": ready,
            "starting": starting,
            "failed": failed,
            "warming": warming,
            "idle_workers": self._idle.qsize(),
            "pending_requests": pending,
            "shutdown": self._shutdown,
        }

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL.

        While the pool is still warming up and no worker is ready yet,
        blocks up to ``config.warmup_wait`` seconds, then returns a
        friendly retry message instead of hanging.

        Retries once if the REPL restarted mid-command — but only when no
        ``env_id`` was passed: a restart renumbers environments, so
        retrying with a stale env_id would run against the wrong (or a
        missing) environment. In that case ReplProcessRestarted is
        surfaced to the caller instead. (run_lean_code never passes
        env_id, so the common path keeps its retry.)
        """
        if self._shutdown:
            return {"repl_error": "REPL pool is shut down."}
        if not self._any_ready.wait(timeout=self.config.warmup_wait):
            status = self.status()
            if status["warming"]:
                return {
                    "repl_error": (
                        f"REPL pool still warming up ({status['ready']}/{self.capacity} ready) — retry shortly."
                    )
                }
            return {"repl_error": f"No REPL workers available ({status['failed']}/{self.capacity} failed to start)."}

        with self._lock:
            self._pending += 1
        try:
            while True:
                if self._shutdown:
                    return {"repl_error": "REPL pool is shut down."}
                try:
                    repl = self._idle.get(timeout=0.5)
                    break
                except queue.Empty:
                    continue
        finally:
            with self._lock:
                self._pending -= 1

        try:
            try:
                return repl.run(code, **kwargs)
            except ReplProcessRestarted:
                if kwargs.get("env_id") is not None:
                    raise
                logger.info("REPL was restarted, retrying command...")
                return repl.run(code, **kwargs)
        finally:
            self._idle.put(repl)

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        with self._lock:
            workers = list(self._workers)
        return sum(w.get_memory_usage() for w in workers)

    def shutdown(self) -> None:
        """Shut down all REPL instances; in-flight runs won't restart them."""
        self._shutdown = True
        with self._lock:
            workers = list(self._workers)
            self._workers.clear()
        for worker in workers:
            worker.shutdown()
