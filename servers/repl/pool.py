"""Pooled Lean REPL instances with load balancing."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from typing import Any

from .core import (
    DEFAULT_REPL_CLEANUP_SECONDS,
    LeanRepl,
    LeanReplConfig,
    ReplCleanupError,
)

logger = getLogger(__name__)

DEFAULT_PORT = 8990
DEFAULT_RAM_FRACTION = 0.5
DEFAULT_STARTUP_STAGGER_SECONDS = 2.0
DEFAULT_POOL_CLEANUP_SECONDS = DEFAULT_REPL_CLEANUP_SECONDS


class ReplPoolBusyError(TimeoutError):
    """A REPL request expired before any worker could receive it."""


class ReplPoolUnavailableError(RuntimeError):
    """A REPL pool stopped before any worker could receive the request."""


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
    """Pool of cold Lean REPL slots with queue-based load balancing.

    Each slot owns a ``LeanRepl`` wrapper, but no subprocess survives a public
    request. Tasks are distributed to idle slots via a FIFO queue.
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
            for _ in range(self.capacity):
                repl = LeanRepl(config)
                self._workers.append(repl)
                self._idle.put(repl)
        except BaseException:
            try:
                self.shutdown()
            except BaseException:
                logger.exception("failed to clean up a partially constructed REPL pool")
            raise

    @staticmethod
    def _close_worker(worker: LeanRepl, deadline: float | None = None) -> None:
        close_with_deadline = getattr(worker, "close_with_deadline", None)
        if deadline is not None and close_with_deadline is not None:
            close_with_deadline(deadline)
            return
        worker.close()

    def _close_workers(self, deadline: float) -> None:
        """Close every worker and retain any whose cleanup failed."""
        failed_workers = []
        first_error: BaseException | None = None
        for worker in self._workers:
            try:
                self._close_worker(worker, deadline)
            except BaseException as error:
                logger.exception("failed to close REPL worker")
                failed_workers.append(worker)
                if first_error is None:
                    first_error = error
        self._workers = failed_workers
        while True:
            try:
                self._idle.get_nowait()
            except queue.Empty:
                break
        if first_error is not None:
            raise first_error

    def run(self, code: str, **kwargs: Any) -> dict[str, Any]:
        """Run code on an idle REPL within one queue-and-execution timeout."""
        timeout = kwargs.pop("timeout", None)
        deadline = kwargs.pop("deadline", None)
        if kwargs:
            names = ", ".join(sorted(kwargs))
            raise TypeError(f"unsupported Lean REPL pool arguments: {names}")
        if timeout is not None and deadline is not None:
            raise TypeError("pass timeout or deadline, not both")
        if deadline is None and timeout is not None:
            deadline = time.monotonic() + timeout
        timeout_description = f" after {timeout:g}s" if timeout is not None else ""
        with self._condition:
            if self._shutdown:
                raise ReplPoolUnavailableError("Lean REPL pool is shut down")
            self._active_calls += 1
        repl: LeanRepl | None = None

        def run_once() -> dict[str, Any]:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReplPoolBusyError(
                        f"timed out{timeout_description} waiting for an idle Lean REPL"
                    )
                return repl.run_disposable(code, deadline=deadline)
            return repl.run_disposable(code)

        result: dict[str, Any] | None = None
        request_error: BaseException | None = None
        try:
            while repl is None:
                with self._condition:
                    if self._shutdown:
                        raise ReplPoolUnavailableError("Lean REPL pool is shut down")
                wait = 0.1
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ReplPoolBusyError(
                            f"timed out{timeout_description} waiting for an idle Lean REPL"
                        )
                    wait = min(wait, remaining)
                try:
                    repl = self._idle.get(timeout=wait)
                except queue.Empty:
                    continue
            with self._condition:
                if self._shutdown:
                    raise ReplPoolUnavailableError("Lean REPL pool is shut down")
            result = run_once()
        except BaseException as error:
            request_error = error
        finally:
            reusable = repl is None
            cleanup_error: BaseException | None = None
            if repl is not None:
                try:
                    self._close_worker(
                        repl,
                        time.monotonic() + DEFAULT_POOL_CLEANUP_SECONDS,
                    )
                    reusable = getattr(repl, "is_clean", lambda: True)()
                except BaseException as error:
                    cleanup_error = error
                finally:
                    if not reusable and cleanup_error is None:
                        cleanup_error = RuntimeError(
                            "Lean REPL process cleanup could not be confirmed"
                        )
                    with self._condition:
                        if not reusable:
                            self._shutdown = True
                        if reusable and not self._shutdown:
                            self._idle.put(repl)
                        self._active_calls -= 1
                        self._condition.notify_all()
            else:
                with self._condition:
                    self._active_calls -= 1
                    self._condition.notify_all()

        if isinstance(request_error, ReplCleanupError):
            if cleanup_error is None:
                return request_error.result
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            result = request_error.result
            if result.get("outcome_unknown") is True:
                result = dict(result)
                result["repl_error"] = (
                    f"{result['repl_error']}; process cleanup also failed: "
                    f"{cleanup_error}"
                )
                return result
            return {
                "repl_error": (
                    "Lean REPL command may have completed, but process cleanup "
                    f"could not be confirmed: {cleanup_error}. The request was not "
                    "retried and must not be replayed."
                ),
                "outcome_unknown": True,
            }
        if request_error is not None:
            if cleanup_error is not None:
                note = f"Lean REPL process cleanup also failed: {cleanup_error}"
                add_note = getattr(request_error, "add_note", None)
                if add_note is not None:
                    add_note(note)
                else:  # pragma: no cover - Python 3.10 compatibility
                    logger.error("%s", note)
            raise request_error.with_traceback(request_error.__traceback__)
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error.with_traceback(cleanup_error.__traceback__)
        if result is None:
            if cleanup_error is not None:
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            raise RuntimeError("Lean REPL pool call produced no result")
        if cleanup_error is not None:
            return {
                "repl_error": (
                    "Lean REPL command may have completed, but process cleanup "
                    f"could not be confirmed: {cleanup_error}. The request was not "
                    "retried and must not be replayed."
                ),
                "outcome_unknown": True,
            }
        return result

    def get_memory_usage(self) -> float:
        """Total memory usage across all REPL instances in GB."""
        return sum(w.get_memory_usage() for w in self._workers)

    def is_usable(self) -> bool:
        """Return whether the pool can admit another request."""
        with self._condition:
            return not self._shutdown and not self._closed

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
            deadline = time.monotonic() + DEFAULT_POOL_CLEANUP_SECONDS
            self._close_workers(deadline)
        finally:
            with self._condition:
                self._closing = False
                self._closed = not self._workers
                self._condition.notify_all()
