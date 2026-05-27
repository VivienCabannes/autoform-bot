# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Base server lifecycle management for local inference servers.

Provides process-safe coordination for starting/stopping HTTP servers
(e.g. vLLM, Ollama) with file-lock synchronization so that only one
server instance runs per rendezvous point (hostname, job, user, GPU).
"""

from __future__ import annotations

import contextlib
import enum
import fcntl
import logging
import os
import socket
import subprocess
import threading
import time
from abc import ABC
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from contextlib import AbstractContextManager
from typing import ClassVar, Generic, TextIO, TypeVar

from .monitor import inherit_clean_env, kill_subprocesses

logger = logging.getLogger(__name__)

DEFAULT_KILL_TIMEOUT = 10.0
DEFAULT_CONNECT_TIMEOUT = 1
DEFAULT_POLL_INTERVAL = 0.2
DEFAULT_LOCK_RETRY_INTERVAL = 0.1


# ---------------------------------------------------------------------------
# Rendezvous point — determines server instance granularity
# ---------------------------------------------------------------------------


@enum.unique
class RdvPoint(enum.StrEnum):
    NODE = enum.auto()
    JOB = enum.auto()
    USER = enum.auto()
    GPU = enum.auto()


# ---------------------------------------------------------------------------
# Server arguments
# ---------------------------------------------------------------------------


@dataclass
class RdvServerArgs:
    """Configuration shared by all rendezvous-based servers."""

    NAME: ClassVar[str] = "service"

    dump_dir: str
    server_connection_timeout: float = 300.0
    rdvpoint: RdvPoint = RdvPoint.NODE

    log_dir: str = field(init=False)

    def get_rdv_identifier(self) -> str:
        match self.rdvpoint:
            case RdvPoint.NODE:
                return socket.gethostname()
            case RdvPoint.JOB:
                return os.getenv("JOB_ID", "default_job")
            case RdvPoint.USER:
                return os.getenv("USER", "default_user")
            case RdvPoint.GPU:
                return os.getenv("GPU_ID", "default_gpu")

    def __post_init__(self) -> None:
        self.dump_dir = os.path.expandvars(self.dump_dir)
        self.log_dir = os.path.join(self.dump_dir, self.NAME, self.get_rdv_identifier())

    def get_server_env_vars(self) -> dict[str, str]:
        """Extra env vars passed to the server subprocess."""
        return {}


# ---------------------------------------------------------------------------
# PerRdvServer — abstract base for process-safe servers
# ---------------------------------------------------------------------------


class PerRdvServer(ABC):
    """Process-safe server ensuring a single instance per rendezvous point.

    Uses file locks for coordination across processes. Subclasses implement
    ``_start_server_process`` to launch the actual server binary.
    """

    def __init__(self, args: RdvServerArgs) -> None:
        self.args = args
        self.name = args.NAME
        self.process: subprocess.Popen | None = None
        self._host = self._get_advertised_host()

        log_dir = Path(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.stdout = log_dir / "stdout.log"
        self.stderr = log_dir / "stderr.log"

        self._process_lock_file = log_dir / ".lock"
        self._server_addr_file = log_dir / "server_address"
        self._client_counter_file = log_dir / "client_counter"

        self.timeout = args.server_connection_timeout
        self.kill_timeout = DEFAULT_KILL_TIMEOUT
        self.address: str | None = None

    # -- Public API --------------------------------------------------------

    def start(self) -> None:
        """Start the server if not already running (file-lock protected)."""
        with self._acquire_lock(self._process_lock_file, timeout=self.timeout):
            if not os.path.exists(self._server_addr_file):
                self._start_server(timeout=self.timeout)
            else:
                logger.info("%s server already started, skipping.", self.name)

    def close(self) -> None:
        """Stop the server after all clients disconnect."""
        if not self.process or self.process.poll() is not None:
            return

        logger.info("Waiting for %s clients to disconnect…", self.name)
        deadline = time.time() + self.timeout
        warned = False
        while True:
            if time.time() > deadline:
                logger.warning(
                    "%s close() timed out after %.0fs waiting for clients. Forcing shutdown.",
                    self.name,
                    self.timeout,
                )
                break
            with self._acquire_lock(self._process_lock_file, timeout=self.timeout):
                with open(self._client_counter_file) as f:
                    counter = int(f.read().strip() or "0")
                    if counter == 0:
                        break
                    if not warned:
                        logger.info("%s still has %d client(s) connected.", self.name, counter)
                        warned = True
                    time.sleep(1)

        logger.info("All %s clients disconnected. Stopping server…", self.name)
        with self._acquire_lock(self._process_lock_file, timeout=self.timeout):
            kill_subprocesses(self.process)
            os.remove(self._server_addr_file)
            os.remove(self._client_counter_file)
            self.process = None
            logger.info("%s server stopped.", self.name)

    def get_server_address(self) -> str:
        """Read the base server address written during startup."""
        if self.address is not None:
            return self.address
        with open(self._server_addr_file) as f:
            self.address = f.read().strip()
        if not self.address:
            raise RuntimeError(f"{self.name} server address file is empty.")
        return self.address

    def init_connection_counter(self) -> None:
        with open(self._client_counter_file, "w") as f:
            f.write("0")

    def update_connection_counter(self, num: int = 1) -> None:
        with self._acquire_lock(self._process_lock_file, timeout=self.timeout):
            with open(self._client_counter_file) as f:
                counter = int(f.read().strip() or "0")
            counter += num
            with open(self._client_counter_file, "w") as f:
                f.write(str(counter))

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> PerRdvServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close()

    # -- Subclass hooks -----------------------------------------------------

    def _start_server_process(
        self,
        port: int,
        env: dict[str, str],
        stdout: TextIO,
        stderr: TextIO,
        args: RdvServerArgs,
    ) -> str:
        """Launch the server subprocess and return its base address.

        Override this in subclasses that use the default ``_start_server``
        implementation.  Subclasses that override ``_start_server`` directly
        (e.g. ``LeanReplServer``) do not need to implement this method.
        """
        raise NotImplementedError

    # -- Internals ---------------------------------------------------------

    @staticmethod
    def _get_advertised_host() -> str:
        hostname = socket.gethostname()
        try:
            socket.gethostbyname(hostname)
            return hostname
        except socket.gaierror:
            logger.warning("Hostname %s unresolvable, falling back to localhost.", hostname)
            return "localhost"

    def _start_server(self, timeout: float) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self._host, 0))
        port = sock.getsockname()[1]
        sock.close()

        env = inherit_clean_env()
        env |= self.args.get_server_env_vars()

        with (
            open(self.stdout, "w") as stdout_file,
            open(self.stderr, "w") as stderr_file,
        ):
            server_addr = self._start_server_process(
                port=port,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                args=self.args,
            )
        # Note: closing the Python file objects above does NOT affect the
        # subprocess — Popen inherits the underlying OS file descriptors
        # (via fileno()), and the kernel keeps them alive as long as the
        # child process holds them open.

        self.init_connection_counter()

        logger.info("%s server started on %s.", self.name, server_addr)
        with open(self._server_addr_file, "w") as f:
            f.write(server_addr)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((self._host, port), timeout=DEFAULT_CONNECT_TIMEOUT):
                    break
            except OSError:
                time.sleep(DEFAULT_POLL_INTERVAL)
        else:
            raise RuntimeError(f"{self.name} server failed to start within {timeout}s on port {port}")

    @staticmethod
    @contextmanager
    def _acquire_lock(lock_file_path: Path, timeout: float) -> Iterator[None]:
        with open(lock_file_path, "a") as lock_file:
            try:
                deadline = time.time() + timeout
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (IOError, OSError):
                        if time.time() >= deadline:
                            raise TimeoutError(f"Could not acquire lock within {timeout}s")
                        time.sleep(DEFAULT_LOCK_RETRY_INTERVAL)
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# RdvServerClient — connection counting client base
# ---------------------------------------------------------------------------


class RdvServerClient(ABC):
    """Base client that tracks connections to a PerRdvServer."""

    def __init__(self, server: PerRdvServer) -> None:
        self.server = server

    def start(self) -> None:
        self.server.update_connection_counter(1)

    def close(self) -> None:
        self.server.update_connection_counter(-1)

    def __enter__(self) -> RdvServerClient:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Singleton — thread-safe context-managed singleton
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=AbstractContextManager)


class Singleton(Generic[T]):
    """Thread-safe double-checked locking singleton with ExitStack cleanup."""

    def __init__(self, lock: threading.Lock | None = None) -> None:
        self._instance: T | None = None
        self._lock = lock or threading.Lock()

    def get_instance(
        self,
        context_stack: contextlib.ExitStack,
        cls: type[T],
        *args,
        **kwargs,
    ) -> T:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    context_stack.enter_context(self._reset_on_exit())
                    self._instance = cls(*args, **kwargs)
                    context_stack.enter_context(self._instance)
        return self._instance

    def peek(self) -> T | None:
        with self._lock:
            return self._instance

    @contextlib.contextmanager
    def _reset_on_exit(self) -> Iterator[None]:
        try:
            yield
        finally:
            with self._lock:
                self._instance = None
