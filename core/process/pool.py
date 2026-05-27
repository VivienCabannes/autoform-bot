# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Thread-based RPC session pool.

Provides a generic pool of stateful RPC backends (sessions) with
queue-based load balancing, lazy initialization, and both sync
and async APIs.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future, InvalidStateError
from logging import getLogger
from types import TracebackType
from typing import Any, Callable, Generic, Literal, TypeVar

logger = getLogger(__name__)

MemoryUnit = Literal["B", "KB", "MB", "GB"]

DEFAULT_QUEUE_POLL_TIMEOUT = 0.5
DEFAULT_STARTUP_STAGGER_SECONDS = 0.0

# -----------------------------------------------------------------------------
# Single-Thread RPC Session
# -----------------------------------------------------------------------------

ConfigType = TypeVar("ConfigType")


class RpcSession(ABC, Generic[ConfigType]):
    @abstractmethod
    def __init__(self, config: ConfigType) -> None: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_memory_usage(self, unit: MemoryUnit = "GB") -> float:
        logger.warning("RpcSession.get_memory_usage not implemented, returning 0.0")
        return 0.0

    def is_alive(self) -> bool:
        """Check if the session's backend is still functional.

        Subclasses should override to check subprocess health, etc.
        Returns True by default (optimistic).
        """
        return True

    def __enter__(self):
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close()


# -----------------------------------------------------------------------------
# RPC Session Pool
# -----------------------------------------------------------------------------


class RpcSessionPool(Generic[ConfigType]):
    """Thread-based pool of RpcSession instances.

    Exposes both blocking and async interfaces so callers can choose how to wait.
    """

    def __init__(
        self,
        args: ConfigType,
        session_factory: Callable[[ConfigType], RpcSession[ConfigType]],
        capacity: int,
        loop: asyncio.AbstractEventLoop | None = None,
        startup_stagger: float = DEFAULT_STARTUP_STAGGER_SECONDS,
    ) -> None:
        self.capacity = capacity
        self.args = args
        self._session_factory = session_factory
        self._loop = loop
        self._startup_stagger = startup_stagger
        self.name = getattr(session_factory, "__name__", session_factory.__class__.__name__)

        # Each task is: (args, kwargs, future)
        self._tasks: queue.Queue[tuple[tuple[Any, ...], dict[str, Any], Future]] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._sessions: list[RpcSession[ConfigType]] = []
        self._sessions_lock = threading.Lock()
        self._shutdown = False

        logger.info(f"Initializing {self.name}-RpcSessionPool with capacity={capacity}")
        for i in range(self.capacity):
            t = threading.Thread(
                target=self._worker_loop,
                args=(i,),
                name=f"{self.name}-rpc-session-worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def _worker_loop(self, worker_index: int) -> None:
        # Each worker owns its own RpcSession instance
        session: RpcSession[ConfigType] | None = None
        name = threading.current_thread().name
        first_init = True
        try:
            while True:
                try:
                    args, kwargs, fut = self._tasks.get(timeout=DEFAULT_QUEUE_POLL_TIMEOUT)
                except queue.Empty:
                    if self._shutdown:
                        break
                    continue

                if fut.cancelled():
                    self._tasks.task_done()
                    continue

                try:
                    # Lazy initialization of the session
                    if session is None:
                        # Stagger startup to avoid thundering herd
                        if first_init and self._startup_stagger > 0:
                            delay = worker_index * self._startup_stagger
                            if delay > 0:
                                logger.info(f"{name} staggering startup by {delay:.1f}s")
                                time.sleep(delay)
                            first_init = False
                        logger.info(f"{name} is starting a new session")
                        session = self._session_factory(self.args)
                        try:
                            session.start()
                        except Exception:
                            session.close()
                            session = None
                            raise
                        with self._sessions_lock:
                            self._sessions.append(session)

                    result = session.run(*args, **kwargs)
                except Exception as e:
                    if not fut.cancelled():
                        try:
                            logger.exception(f"{name} encountered error: {e}")
                            fut.set_exception(e)
                        except InvalidStateError:
                            pass
                    # Destroy broken sessions so next task gets a fresh one
                    if session is not None and not session.is_alive():
                        logger.warning(f"{name} session is dead, destroying it")
                        try:
                            session.close()
                        except Exception:
                            pass
                        with self._sessions_lock:
                            if session in self._sessions:
                                self._sessions.remove(session)
                        session = None
                else:
                    if not fut.cancelled():
                        try:
                            fut.set_result(result)
                        except InvalidStateError:
                            pass
                finally:
                    self._tasks.task_done()
        finally:
            if session is not None:
                session.close()
                logger.info(f"{name} closed session")

    def submit(self, *args: Any, **kwargs: Any) -> Future:
        if self._shutdown:
            raise RuntimeError("Pool is shutting down")
        fut = Future()
        self._tasks.put((args, kwargs, fut))
        return fut

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """Public API: simple blocking call that hides the Future."""
        fut = self.submit(*args, **kwargs)
        return fut.result()

    def shutdown(self, wait: bool = True) -> None:
        self._shutdown = True
        logger.info(f"Shutting down {self.name}-RpcSessionPool")
        if wait:
            for t in self._threads:
                t.join()
            logger.info(f"{self.name}-RpcSessionPool shutdown complete")

    def get_memory_usage(self, unit: MemoryUnit = "GB") -> float:
        """Return total memory usage of all sessions."""
        total = 0.0
        with self._sessions_lock:
            for session in self._sessions:
                try:
                    total += session.get_memory_usage(unit=unit)
                except Exception:
                    pass
        return total

    def __enter__(self) -> RpcSessionPool[ConfigType]:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.shutdown(wait=True)

    # --------------------------------------------------------------------------
    # Async APIs
    # --------------------------------------------------------------------------

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        return self._loop

    def submit_async(self, *args: Any, **kwargs: Any) -> asyncio.Future:
        """Async version of submit(): returns an asyncio.Future."""
        base_fut = self.submit(*args, **kwargs)
        async_fut = asyncio.wrap_future(base_fut, loop=self._get_loop())

        def _cancel_base(_async_fut: asyncio.Future) -> None:
            if _async_fut.cancelled() and not base_fut.done():
                base_fut.cancel()

        async_fut.add_done_callback(_cancel_base)
        return async_fut

    async def run_async(self, *args: Any, **kwargs: Any) -> Any:
        fut = self.submit_async(*args, **kwargs)
        return await fut

    async def shutdown_async(self, wait: bool = True) -> None:
        loop = self._get_loop()
        await loop.run_in_executor(None, self.shutdown, wait)

    async def __aenter__(self) -> RpcSessionPool[ConfigType]:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.shutdown_async(wait=True)
