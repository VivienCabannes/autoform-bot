"""Pooled Lean REPL instances with load balancing."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .core import LeanRepl, LeanReplConfig

logger = getLogger(__name__)

DEFAULT_PORT = 8990
DEFAULT_RAM_FRACTION = 0.5
DEFAULT_STARTUP_STAGGER_SECONDS = 2.0


@dataclass
class LeanReplPoolConfig(LeanReplConfig):
    """Configuration for a pool of Lean REPL instances."""

    num_repls: int | None = None
    startup_stagger: float = DEFAULT_STARTUP_STAGGER_SECONDS

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

    Each worker thread owns its own LeanRepl subprocess. Tasks are
    distributed to idle workers via a FIFO queue.
    """

    def __init__(self, config: LeanReplPoolConfig) -> None:
        self.config = config
        self.capacity = config.num_repls or 1
        self._shutdown = False

        self._workers: list[LeanRepl] = []
        self._idle: queue.Queue[LeanRepl] = queue.Queue()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_calls = 0
        self._closing = False
        self._closed = False

        try:
            for i in range(self.capacity):
                if i > 0:
                    import time

                    time.sleep(config.startup_stagger)
                repl = LeanRepl(config)
                try:
                    repl.start()
                except BaseException:
                    # LeanRepl.start() currently cleans up its own process, but
                    # keep the pool transaction safe for alternate/test workers
                    # and future implementations too.
                    try:
                        repl.close()
                    except Exception:
                        logger.exception("failed to close REPL after startup error")
                    raise
                self._workers.append(repl)
                self._idle.put(repl)
        except BaseException:
            self._close_workers()
            raise

    def _close_workers(self) -> None:
        """Close every constructed worker, preserving cleanup after one failure."""
        for worker in reversed(self._workers):
            try:
                worker.close()
            except Exception:
                logger.exception("failed to close REPL worker")
        self._workers.clear()
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL within one queue-and-execution timeout."""
        timeout = kwargs.pop("timeout", None)
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            if self._shutdown:
                raise RuntimeError("Lean REPL pool is shut down")
            self._active_calls += 1
        repl: LeanRepl | None = None

        try:
            while repl is None:
                with self._condition:
                    if self._shutdown:
                        raise RuntimeError("Lean REPL pool is shut down")
                wait = 0.1
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                        )
                    wait = min(wait, remaining)
                try:
                    repl = self._idle.get(timeout=wait)
                except queue.Empty:
                    continue
            with self._condition:
                if self._shutdown:
                    raise RuntimeError("Lean REPL pool is shut down")

            call_kwargs = dict(kwargs)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout:g}s waiting for an idle Lean REPL"
                    )
                call_kwargs["timeout"] = remaining
            return repl.run(code, **call_kwargs)
        finally:
            if repl is not None:
                with self._condition:
                    if not self._shutdown:
                        self._idle.put(repl)
            with self._condition:
                self._active_calls -= 1
                self._condition.notify_all()

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        return sum(w.get_memory_usage() for w in self._workers)

    def shutdown(self) -> None:
        """Shut down all REPL instances."""
        with self._condition:
            self._shutdown = True
            self._condition.notify_all()
            while self._active_calls:
                self._condition.wait()
            while self._closing:
                self._condition.wait()
            if self._closed:
                return
            self._closing = True
        try:
            self._close_workers()
        finally:
            with self._condition:
                self._closing = False
                self._closed = True
                self._condition.notify_all()
